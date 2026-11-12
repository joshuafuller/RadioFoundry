# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Focused no-radio CoT mapping and real gateway/DMR/FEC integration check."""

import argparse
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import socket
import struct
import subprocess
import xml.etree.ElementTree as ET

from bridge import to_app, to_radio

HOST_MAC = bytes.fromhex("025246000002")


def checksum(data: bytes) -> int:
    if len(data) & 1:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def multicast_mac(address: str) -> bytes:
    value = int(ipaddress.IPv4Address(address)) & 0x7FFFFF
    return b"\x01\x00\x5e" + value.to_bytes(3, "big")


def ethernet(radio_id: int, destination: tuple[str, int], payload: bytes, protocol: int = 17, fragment: int = 0x4000) -> bytes:
    source = ipaddress.IPv4Address(0x0C000000 | radio_id).packed
    target = ipaddress.IPv4Address(destination[0]).packed
    udp = struct.pack("!HHHH", destination[1], destination[1], 8 + len(payload), 0) + payload
    pseudo = source + target + struct.pack("!BBH", 0, protocol, len(udp))
    udp = udp[:6] + struct.pack("!H", checksum(pseudo + udp) or 0xFFFF) + udp[8:]
    if protocol == 6:
        udp = struct.pack("!HHIIHHHH", destination[1], destination[1], 1, 0, 0x5002, 1024, 0, 0)
        pseudo = source + target + struct.pack("!BBH", 0, protocol, len(udp))
        udp = udp[:16] + struct.pack("!H", checksum(pseudo + udp)) + udp[18:]
    elif protocol == 2:
        udp = b"\x16\0\0\0" + target
        udp = udp[:2] + struct.pack("!H", checksum(udp)) + udp[4:]
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, fragment, 1 if protocol == 2 else 64, protocol, 0, source, target)
    ip = ip[:10] + struct.pack("!H", checksum(ip)) + ip[12:]
    return multicast_mac(destination[0]) + HOST_MAC + b"\x08\x00" + ip + udp


def decode(frame: bytes) -> tuple[str, int, bytes]:
    assert len(frame) >= 42 and frame[12:15] == b"\x08\x00\x45", "not a plain IPv4 Ethernet frame"
    ip = frame[14:34]
    assert checksum(ip) == 0 and ip[9] == 17 and ip[8] == 1, "invalid checksum/protocol/multicast TTL"
    assert int.from_bytes(ip[2:4], "big") == len(frame) - 14
    udp = frame[34:]
    source_port, port, length, _ = struct.unpack("!HHHH", udp[:8])
    assert length == len(udp) and source_port == port
    assert checksum(ip[12:20] + struct.pack("!BBH", 0, 17, length) + udp) == 0
    destination = str(ipaddress.IPv4Address(ip[16:20]))
    assert frame[:6] == multicast_mac(destination), "wrong reconstructed multicast MAC"
    return destination, port, udp[8:]


class Node:
    def __init__(self, worker: str, radio_id: int):
        self.id = radio_id
        self.sock, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.sock.settimeout(5)
        try:
            self.process = subprocess.Popen(
                [worker, str(radio_id), str(child.fileno())], pass_fds=(child.fileno(),), stderr=subprocess.PIPE
            )
        except BaseException:
            self.sock.close()
            raise
        finally:
            child.close()

    def __enter__(self):
        return self

    def command(self, packet: bytes) -> list[bytes]:
        assert self.sock.send(packet) == len(packet)
        replies = []
        while True:
            reply = self.sock.recv(4096)
            if not reply:
                raise RuntimeError("radio-node closed its IPC socket")
            if reply == b"Z":
                return replies
            assert reply[:1] in (b"B", b"F", b"E", b"S"), "unknown node reply"
            replies.append(reply)

    def __exit__(self, *_):
        try:
            if self.process.poll() is None:
                self.sock.send(b"Q")
        except OSError:
            pass
        self.sock.close()
        try:
            code = self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise RuntimeError("owned radio-node failed to stop")
        finally:
            error = self.process.stderr.read().decode(errors="replace")
            self.process.stderr.close()
        if code:
            raise RuntimeError(f"radio-node failed ({code}): {error}")


def fcs16(data: bytes) -> int:
    fcs = 0xFFFF
    for byte in data:
        fcs ^= byte
        for _ in range(8):
            fcs = (fcs >> 1) ^ 0x8408 if fcs & 1 else fcs >> 1
    return fcs ^ 0xFFFF


class Ppp:
    """A minimal PPP client written from RFC 1661, 1662 and 1332: the phone's end of the serial pipe to a radio.

    The pipe is modeled as writes of at most `chunk` bytes into the radio's host port (20: a default BLE write)."""

    LCP, IPCP, IPV4 = 0xC021, 0x8021, 0x0021

    def __init__(self, node: Node, chunk: int = 20):
        self.node, self.chunk, self.rx, self.rf = node, chunk, b"", []
        self.address = self.radio = self.mru = None

    @staticmethod
    def frame(protocol: int, information: bytes, corrupt: bool = False) -> bytes:
        body = b"\xff\x03" + struct.pack("!H", protocol) + information
        body += struct.pack("<H", fcs16(body) ^ (1 if corrupt else 0))
        escaped = b"".join(bytes((0x7D, b ^ 0x20)) if b < 0x20 or b in (0x7D, 0x7E) else bytes((b,)) for b in body)
        return b"\x7e" + escaped + b"\x7e"

    def write(self, data: bytes) -> list[tuple[int, bytes]]:
        """Serial bytes to the radio. Returns the PPP frames it answered with; its RF bursts collect in self.rf."""
        for at in range(0, len(data), self.chunk):
            self.take(self.node.command(b"P" + data[at:at + self.chunk]))
        return self.frames()

    def take(self, replies: list[bytes]) -> None:
        for reply in replies:
            if reply[:1] == b"S":
                self.rx += reply[1:]
            elif reply[:1] == b"B":
                self.rf.append(reply)
            else:
                raise AssertionError("PPP host port answered with an Ethernet frame or an error")

    def frames(self) -> list[tuple[int, bytes]]:
        *complete, self.rx = self.rx.split(b"\x7e")
        found = []
        for raw in complete:
            if not raw:
                continue
            body, escape = bytearray(), False
            for b in raw:
                if escape:
                    body.append(b ^ 0x20)
                elif b != 0x7D:
                    body.append(b)
                escape = b == 0x7D and not escape
            assert len(body) >= 6 and body[:2] == b"\xff\x03", "radio PPP frame without FF 03"
            assert fcs16(bytes(body[:-2])) == int.from_bytes(body[-2:], "little"), "radio PPP frame with a bad FCS"
            found.append((int.from_bytes(body[2:4], "big"), bytes(body[4:-2])))
        return found

    def open(self) -> None:
        """LCP then IPCP, taking the address the radio gives (our request starts at 0.0.0.0, as RFC 1332 allows)."""
        address, ours_open, queue = b"\0\0\0\0", set(), [(self.LCP, b"\x01\x01\x00\x0a\x05\x06" + b"\x4c\x41\x42" + bytes((self.node.id & 255,)))]
        for _ in range(20):
            if not queue:
                break
            received = self.write(b"".join(self.frame(protocol, packet) for protocol, packet in queue))
            queue = []
            for protocol, packet in received:
                code, ident, options = packet[0], packet[1], packet[4:int.from_bytes(packet[2:4], "big")]
                assert protocol in (self.LCP, self.IPCP), "data before the link is open"
                if code == 1:  # the radio's Configure-Request: everything it asks for is acceptable here
                    if protocol == self.LCP:
                        assert options[:2] == b"\x01\x04", "radio LCP request does not start with its MRU"
                        self.mru = int.from_bytes(options[2:4], "big")
                    else:
                        assert options[:2] == b"\x03\x06"
                        self.radio = ipaddress.IPv4Address(options[2:6])
                    queue.append((protocol, b"\x02" + packet[1:4] + options))
                elif code == 2:  # our request was taken
                    ours_open.add(protocol)
                    if protocol == self.LCP:
                        queue.append((self.IPCP, b"\x01\x02\x00\x0a\x03\x06" + address))
                    else:
                        self.address = ipaddress.IPv4Address(address)
                elif code == 3 and protocol == self.IPCP:  # Configure-Nak: the address we must use
                    address = options[2:6]
                    queue.append((self.IPCP, bytes((1, ident + 1)) + b"\x00\x0a\x03\x06" + address))
                else:
                    raise AssertionError(f"unexpected PPP code {code} for protocol {protocol:#06x}")
        assert ours_open == {self.LCP, self.IPCP} and self.radio and self.mru, "PPP link did not open"

    def send(self, packet: bytes, corrupt: bool = False) -> list[bytes]:
        """One IPv4 packet to the radio. Returns the RF bursts it produced."""
        assert not self.write(self.frame(self.IPV4, packet, corrupt)), "radio answered a datagram for the air"
        bursts, self.rf = self.rf, []
        return bursts

    def receive(self, replies: list[bytes]) -> list[bytes]:
        """What the radio wrote to the pipe after an RF burst: the IPv4 packets in it."""
        self.take(replies)
        assert not self.rf, "received RF packet echoed onto RF"
        frames = self.frames()
        assert all(protocol == self.IPV4 for protocol, _ in frames)
        return [packet for _, packet in frames]


def check(worker: str, output: Path) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = lambda value: value.isoformat().replace("+00:00", "Z")
    event = (
        f'<event version="2.0" uid="RF-LAB-A" type="a-f-G-U-C" how="m-g" '
        f'time="{stamp(now)}" start="{stamp(now)}" stale="{stamp(now + timedelta(minutes=5))}">'
        '<point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/>'
        '<detail><contact callsign="RF-LAB-A"/></detail></event>'
    ).encode()
    chat = event.replace(b'uid="RF-LAB-A"', b'uid="RF-LAB-CHAT-1"').replace(b'type="a-f-G-U-C"', b'type="b-t-f"')
    chat = chat.replace(b"</detail>", b'<remarks>Radio transport test</remarks></detail>')
    cases = (("239.2.3.1", 6969, "225.0.0.201", event), ("224.10.10.1", 17012, "225.0.0.202", chat))
    # First red: a requested multicast destination must acquire an explicit radio-group mapping.
    assert to_radio((cases[0][0], cases[0][1]), event) == ("225.0.0.201", 6969, event)
    captures: list[tuple[int, bytes]] = []
    results = []
    ppp_results = []
    clock_ms = 0

    def transmit(sender: Node, receiver: Node, frame: bytes, drop: bool = False) -> tuple[list[bytes], int]:
        nonlocal clock_ms
        captures.append((clock_ms, frame))
        outgoing = sender.command(b"H" + frame)
        bursts = [message for message in outgoing if message[:1] == b"B"]
        assert len(bursts) > 3 and not any(message[:1] == b"E" for message in outgoing)
        delivered = []
        for index, burst in enumerate(bursts):
            assert len(burst) == 35, "RF IPC must contain type and a real 33-byte coded frame"
            clock_ms += 60
            if drop and index == len(bursts) - 2:
                continue
            incoming = receiver.command(burst)
            assert not any(message[:1] == b"B" for message in incoming), "received RF packet echoed onto RF"
            for message in incoming:
                if message[:1] == b"F":
                    captures.append((clock_ms, message[1:]))
                    delivered.append(message[1:])
        return delivered, len(bursts)

    with ExitStack() as stack:
        a = stack.enter_context(Node(worker, 1001))
        b = stack.enter_context(Node(worker, 1002))
        for app_ip, port, radio_ip, payload in cases:
            mapped = to_radio((app_ip, port), payload)
            assert mapped == (radio_ip, port, payload)
            for sender, receiver in ((a, b), (b, a)):
                received, bursts = transmit(sender, receiver, ethernet(sender.id, mapped[:2], mapped[2]))
                assert len(received) == 1
                destination, delivered_port, delivered_payload = decode(received[0])
                assert destination == radio_ip and delivered_port == port and delivered_payload == payload
                assert ipaddress.IPv4Address(received[0][26:30]) == ipaddress.IPv4Address(0x0C000000 | sender.id)
                assert to_app((destination, delivered_port), delivered_payload) == (app_ip, port, payload)
                assert ET.fromstring(delivered_payload).attrib["uid"] == ET.fromstring(payload).attrib["uid"]
                results.append({"sender": sender.id, "receiver": receiver.id, "app_group": app_ip,
                    "radio_group": radio_ip, "port": port, "payload_bytes": len(payload), "bursts": bursts,
                    "virtual_air_ms": bursts * 60, "separate_tx_guard_ms": 250})
                print(f"PASS: {sender.id}->{receiver.id} {app_ip}:{port} via {radio_ip}: {len(payload)} B, {bursts} bursts, {bursts*60} virtual air ms", flush=True)

        # Firmware rejects these original ATAK groups; translation, not changed firmware, enabled the path above.
        for app_ip, port, _, payload in cases:
            assert not a.command(b"H" + ethernet(a.id, (app_ip, port), payload))
        assert not a.command(b"H" + ethernet(a.id, ("225.0.0.201", 6969), event, protocol=6))
        assert not a.command(b"H" + ethernet(a.id, ("225.0.0.201", 6969), event, protocol=2))
        assert not a.command(b"H" + ethernet(a.id, ("225.0.0.201", 6969), event, fragment=0x2000))
        print("PASS: unmapped conventional groups, TCP, IGMP and fragmented IP do not cross the firmware gateway", flush=True)

        mapped = to_radio(("239.2.3.1", 6969), event)
        received, _ = transmit(a, b, ethernet(a.id, mapped[:2], mapped[2]), drop=True)
        assert not received, "incomplete unconfirmed group packet was delivered"
        received, _ = transmit(a, b, ethernet(a.id, mapped[:2], mapped[2]))
        assert len(received) == 1 and decode(received[0])[2] == event
        print("PASS: dropped group-data burst prevents delivery; next complete transmission recovers", flush=True)

        largest = b"x" * 558
        mapped = to_radio(("239.2.3.1", 6969), largest)
        received, _ = transmit(a, b, ethernet(a.id, mapped[:2], mapped[2]))
        assert len(received) == 1 and decode(received[0])[2] == largest
        for destination, payload, operation in (
            (("239.2.3.1", 6969), b"x" * 559, to_radio),
            (("239.255.1.1", 6969), event, to_radio),
            (("239.2.3.1", 17012), event, to_radio),
            (("225.0.0.203", 6969), event, to_app),
        ):
            try:
                operation(destination, payload)
            except ValueError:
                pass
            else:
                raise AssertionError("unsupported or oversized datagram accepted by mapping")
        print("PASS: exact USB UDP limit (558 B) delivered; oversize and unmapped destinations refused", flush=True)

        # The serial host link: each phone runs PPP over a byte pipe to its radio (the nodes stay in PPP from here on).
        phones = {node.id: Ppp(node) for node in (a, b)}
        for node in (a, b):
            phone = phones[node.id]
            phone.open()
            assert phone.address == ipaddress.IPv4Address(0x0C000000 | node.id), "host address is not 12.<DMR ID>"
            assert int(phone.radio) == int(phone.address) ^ 1 and phone.mru == 584
        ppp_udp_max = phones[a.id].mru - 28

        def over_ppp(sender: Node, receiver: Node, packet: bytes, corrupt: bool = False) -> tuple[list[bytes], int]:
            nonlocal clock_ms
            bursts = phones[sender.id].send(packet, corrupt)
            delivered = []
            for burst in bursts:
                clock_ms += 60
                delivered += phones[receiver.id].receive(receiver.command(burst))
            return delivered, len(bursts)

        for app_ip, port, radio_ip, payload in cases:
            mapped = to_radio((app_ip, port), payload, ppp_udp_max)
            for sender, receiver in ((a, b), (b, a)):
                received, bursts = over_ppp(sender, receiver, ethernet(sender.id, mapped[:2], mapped[2])[14:])
                assert len(received) == 1
                frame = multicast_mac(radio_ip) + HOST_MAC + b"\x08\x00" + received[0]
                assert decode(frame) == (radio_ip, port, payload), "CoT changed between the two serial pipes"
                assert ipaddress.IPv4Address(received[0][12:16]) == phones[sender.id].address
                ppp_results.append({"sender": sender.id, "receiver": receiver.id, "radio_group": radio_ip, "port": port,
                    "payload_bytes": len(payload), "bursts": bursts, "virtual_air_ms": bursts * 60})
                print(f"PASS: PPP {sender.id}->{receiver.id} {app_ip}:{port} via {radio_ip}: {len(payload)} B, {bursts} bursts", flush=True)

        largest = b"x" * ppp_udp_max
        received, _ = over_ppp(a, b, ethernet(a.id, ("225.0.0.201", 6969), largest)[14:])
        assert len(received) == 1 and received[0][28:] == largest
        for packet, corrupt in ((ethernet(a.id, ("225.0.0.201", 6969), b"x" * (ppp_udp_max + 1))[14:], False),
                                (ethernet(a.id, ("225.0.0.201", 6969), event)[14:], True)):
            received, bursts = over_ppp(a, b, packet, corrupt)
            assert not received and not bursts, "an oversize or corrupted serial frame reached the air"
        try:
            to_radio(("239.2.3.1", 6969), b"x" * (ppp_udp_max + 1), ppp_udp_max)
        except ValueError:
            pass
        else:
            raise AssertionError("mapping accepted a datagram over the PPP limit")
        print(f"PASS: PPP opens on 20-byte serial writes; exact limit ({ppp_udp_max} B) delivered; oversize and bad-FCS frames stay off the air", flush=True)

    with (output / "ethernet.pcap").open("wb") as capture:
        capture.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for milliseconds, frame in captures:
            capture.write(struct.pack("<IIII", milliseconds // 1000, (milliseconds % 1000) * 1000, len(frame), len(frame)))
            capture.write(frame)
    (output / "results.json").write_text(json.dumps({"passed": True, "transport": "two isolated host processes",
        "firmware_data_path": "IPv4 gateway + DMR data + 33-byte frame FEC", "cases": results,
        "ppp_serial_cases": ppp_results,
        "limits": ["no RF or Android/ATAK app", "no queue/RTOS/USB driver timing", "no production encryption"]}, indent=2) + "\n")
    print(f"PASS: multicast proof; artifacts: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    check(args.worker, args.out)
