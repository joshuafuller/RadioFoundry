# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Scheduled IPv4 UDP over the virtual network part, not host sockets.

World command udp: radio, source_ip, destination, source_port, port, payload_hex,
and optional time_us/source (the input stamp's numeric source, not the IP address).
The caller selects network mode through the firmware's USB-network command first
(docs/spec/usb-network.md section 1). ETH_IN lands at time_us; the host task polls it.
For example, source_ip 12.0.3.233 to destination 12.0.3.232, port 3334 carries
debug link bytes for DMR ID 1001. The reply emits udp_delivery with world time,
radio, addresses, ports, payload_hex, payload_sha256 and the Ethernet bytes (data).
Non-UDP output is ignored; malformed IPv4 UDP output fails the run.
"""

import hashlib
import ipaddress
import re
import struct

from ..channel import ChannelError, Kind
from ..world import WorldError

FRAME_MAX = 600  # boards/vhost/netif_vhost.c, core/dmr/ipGateway.h
PAYLOAD_MAX = FRAME_MAX - 14 - 20 - 8
HOST_MAC = bytes.fromhex("025648000002")
RADIO_MAC = bytes.fromhex("025648000001")


def checksum(data):
    data += b"\0" * (len(data) % 2)
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xffff) + (total >> 16)
    return ~total & 0xffff


def ethernet(request):
    addresses = []
    for key in ("source_ip", "destination"):
        value = request[key]
        if not isinstance(value, str):
            raise WorldError("WORLD_BAD_UDP_ADDRESS")
        try:
            addresses.append(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError as error:
            raise WorldError("WORLD_BAD_UDP_ADDRESS") from error
    source, destination = addresses
    for key in ("source_port", "port"):
        if type(request[key]) is not int or not 1 <= request[key] <= 65535:
            raise WorldError("WORLD_BAD_UDP_PORT")
    text = request["payload_hex"]
    if not isinstance(text, str) or not re.fullmatch(rf"(?:[0-9a-fA-F]{{2}}){{0,{PAYLOAD_MAX}}}", text):
        raise WorldError("WORLD_BAD_UDP_PAYLOAD")
    payload = bytes.fromhex(text)
    udp = struct.pack("!HHHH", request["source_port"], request["port"], 8 + len(payload), 0) + payload
    pseudo = source.packed + destination.packed + struct.pack("!BBH", 0, 17, len(udp))
    udp = udp[:6] + struct.pack("!H", checksum(pseudo + udp) or 0xffff) + udp[8:]
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0x4000, 64, 17, 0,
                     source.packed, destination.packed)
    ip = ip[:10] + struct.pack("!H", checksum(ip)) + ip[12:]
    mac = RADIO_MAC
    if destination.is_multicast:
        mac = b"\x01\x00\x5e" + (int(destination) & 0x7fffff).to_bytes(3, "big")
    elif int(destination) == 0xffffffff:
        mac = b"\xff" * 6
    return mac + HOST_MAC + b"\x08\x00" + ip + udp


def decode(frame):
    if not 14 <= len(frame) <= FRAME_MAX:
        raise ChannelError("UDP_BAD_FRAME")
    if frame[12:14] != b"\x08\x00":
        return None
    ip = frame[14:]
    if len(ip) < 20 or ip[0] >> 4 != 4:
        raise ChannelError("UDP_BAD_IPV4")
    if ip[9] != 17:
        return None
    ihl, total = (ip[0] & 15) * 4, int.from_bytes(ip[2:4], "big")
    if (ihl < 20 or not ihl + 8 <= total <= len(ip) or
            int.from_bytes(ip[6:8], "big") & 0xbfff or checksum(ip[:ihl])):
        raise ChannelError("UDP_BAD_IPV4")
    udp = ip[ihl:total]  # Ethernet padding is not part of the datagram.
    source_port, port, length, crc = struct.unpack("!HHHH", udp[:8])
    if length != len(udp):
        raise ChannelError("UDP_BAD_LENGTH")
    if crc and checksum(ip[12:20] + struct.pack("!BBH", 0, 17, length) + udp):
        raise ChannelError("UDP_BAD_CHECKSUM")
    return {"source": str(ipaddress.IPv4Address(ip[12:16])),
            "destination": str(ipaddress.IPv4Address(ip[16:20])),
            "source_port": source_port, "port": port, "payload_hex": udp[8:].hex(),
            "payload_sha256": hashlib.sha256(udp[8:]).hexdigest(), "data": frame.hex()}


def install(world):
    def submit(request):
        event = world.submit(request["radio"], Kind.ETH_IN, ethernet(request), request.get("source", 0),
                             time_us=request.get("time_us"))
        return {"time_us": event.time_us, "sequence": event.sequence}

    def output(radio, message):
        packet = decode(message.payload)
        if packet is not None:
            world.notify({"event": "udp_delivery", "radio": radio, "time_us": message.time_us, **packet})

    world.api.register("udp", submit)
    world.on_outbox(Kind.ETH_OUT, output)
