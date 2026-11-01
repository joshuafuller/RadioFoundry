# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""A small user-space TCP/IP peer for the virtual radio's network personality.

What it is. On a USB radio the PC and the radio swap whole Ethernet frames. Normally the PC's kernel speaks
IP and TCP for them. Giving the kernel the virtual radio's frames would need a TAP device, and that needs root.
So this peer speaks the protocols itself, in Python: Ethernet, ARP replies, a DHCP client (with renewal),
IPv4, UDP and a TCP client with retransmit, the peer's window and FIN. Every header is built with struct and
every checksum is computed here. Standard library only.

No clock and no randomness. The caller passes the virtual time (us) to every call, and every number a real
stack would pick at random (DHCP xid, TCP initial sequence, source port) comes from the seed. So the same
frames from the radio always give the same frames back. That keeps a world repeatable (ADR 0005).

How it is driven. receive(frame, now) takes one frame from the radio. output(now) returns the frames to send
now, built at that moment (so their sequence and ACK numbers are current). next_due() is the next virtual time
at which a timer (a DHCP retry, a TCP retransmit) wants output() called again.

Example, from webgw.py: the radio sends a DHCP OFFER in a grant. webgw calls receive(offer, t). At the end of
the grant it calls output(t2) and gets one frame, the DHCP REQUEST, and submits it as an ETH_IN input.

This file has no install(): the conductor's part loader skips it. webgw.py uses it.
"""

import hashlib
import struct

BROADCAST_MAC = b"\xff" * 6
HOST_MAC = bytes.fromhex("025648000002")    # boards/vhost/netif_vhost.c n_macs: the host's MAC
ETH_IPV4, ETH_ARP = 0x0800, 0x0806
PROTO_TCP, PROTO_UDP = 6, 17
DHCP_SERVER, DHCP_CLIENT = 67, 68
FRAME_MAX = 600                     # IPGW_MAX_FRAME (core/dmr/ipGateway.h:94)
MSS = 536                           # TCP_MSS (core/net/tcp.h:28): 14 + 20 + 20 + 536 = 590 bytes
WINDOW = 4096                       # what we advertise; we take every byte at once
DHCP_RETRY_US = 500_000
RTO_US, RTO_MAX_US = 200_000, 3_200_000
TCP_RETRIES = 6
FIN, SYN, RST, PSH, ACK = 0x01, 0x02, 0x04, 0x08, 0x10


def checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def seq_lt(a, b):
    return ((a - b) & 0xFFFFFFFF) > 0x7FFFFFFF


def seq_le(a, b):
    return a == b or seq_lt(a, b)


def udp_datagram(frame):
    """(source port, destination port, data) of an unfragmented IPv4 UDP datagram in an Ethernet frame, else None.
    Only lengths are checked: dlmux.py reads its own requests back from the world's inputs with this."""
    if len(frame) < 42 or frame[12:14] != b"\x08\x00" or frame[14] >> 4 != 4 or frame[23] != PROTO_UDP:
        return None
    length = (frame[14] & 15) * 4
    total = struct.unpack_from("!H", frame, 16)[0]
    if length < 20 or total < length + 8 or 14 + total > len(frame) or struct.unpack_from("!H", frame, 20)[0] & 0x3FFF:
        return None
    sport, dport, size = struct.unpack_from("!HHH", frame, 14 + length)
    if not 8 <= size <= total - length:
        return None
    at = 14 + length
    return sport, dport, bytes(frame[at + 8:at + size])


def dotted(address):
    return ".".join(str(byte) for byte in address)


class Tcp:
    """One TCP client connection (active open). States: SYN_SENT, ESTABLISHED, FIN_WAIT, CLOSE_WAIT, CLOSED.

    Callbacks: on_open(), on_data(bytes, now), on_close(reason). Data the radio sends in order is passed on at
    once and ACKed in the next output(). Data out of order is dropped and ACKed again (the radio retransmits).
    """

    def __init__(self, peer, port, sport, iss, now):
        self.peer, self.port, self.sport = peer, port, sport
        self.iss = iss
        self.snd_una = iss
        self.snd_nxt = (iss + 1) & 0xFFFFFFFF     # the SYN takes one number
        self.snd_wnd = 0
        self.rcv_nxt = 0
        self.state = "SYN_SENT"
        self.unsent = bytearray()
        self.flight = bytearray()                  # sent, not yet ACKed (data only)
        self.fin_wanted = False
        self.fin_seq = None
        self.peer_fin = False
        self.ack_pending = False
        self.syn_pending = True
        self.rto = RTO_US
        self.retries = 0
        self.timer = now
        self.on_open = self.on_data = self.on_close = None
        self.close_reason = None

    @property
    def established(self):
        return self.state == "ESTABLISHED"

    def send(self, data):
        """Queue bytes. They go out in the next output(), as the radio's window allows."""
        if self.state not in ("SYN_SENT", "ESTABLISHED", "CLOSE_WAIT") or self.fin_wanted:
            return False
        self.unsent += data
        return True

    def close(self):
        """Send FIN after the queued bytes."""
        if self.state in ("SYN_SENT", "CLOSED"):
            self._closed("TCP_CLOSED")
        else:
            self.fin_wanted = True

    def abort(self, reason="TCP_ABORTED"):
        if self.state not in ("SYN_SENT", "CLOSED"):
            self.peer._send_tcp(self, RST | ACK, self.snd_nxt)
        self._closed(reason)

    def _closed(self, reason):
        if self.state == "CLOSED":
            return
        self.state = "CLOSED"
        self.close_reason = reason
        self.timer = None
        self.peer.tcp.pop(self.sport, None)
        if self.on_close is not None:
            self.on_close(reason)

    # --- the radio's segments ---

    def segment(self, seq, ack, flags, window, data, now):
        if flags & RST:
            if self.state != "SYN_SENT" or flags & ACK and ack == self.snd_nxt:
                self._closed("TCP_RESET")
            return
        if self.state == "SYN_SENT":
            if flags & SYN and flags & ACK and ack == self.snd_nxt:
                self.rcv_nxt = (seq + 1) & 0xFFFFFFFF
                self.snd_una = ack
                self.snd_wnd = window
                self.syn_pending = False
                self.state = "ESTABLISHED"
                self.ack_pending = True
                self.retries, self.rto = 0, RTO_US
                self.timer = None
                if self.on_open is not None:
                    self.on_open()
            return
        if not flags & ACK:
            return
        if seq_lt(self.snd_una, ack) and seq_le(ack, self.snd_nxt):
            taken = (ack - self.snd_una) & 0xFFFFFFFF
            if self.fin_seq is not None and seq_lt(self.fin_seq, ack):
                taken -= 1
            del self.flight[:taken]
            self.snd_una = ack
            self.retries, self.rto = 0, RTO_US
            self.timer = None if not self.flight and not self._fin_unacked() else now + self.rto
        self.snd_wnd = window
        if data:
            if seq == self.rcv_nxt:
                self.rcv_nxt = (self.rcv_nxt + len(data)) & 0xFFFFFFFF
                if self.on_data is not None:
                    self.on_data(bytes(data), now)
            self.ack_pending = True
        if flags & FIN and (seq + len(data)) & 0xFFFFFFFF == self.rcv_nxt and not self.peer_fin:
            self.rcv_nxt = (self.rcv_nxt + 1) & 0xFFFFFFFF
            self.peer_fin = True
            self.ack_pending = True
            self.fin_wanted = True             # we have nothing more to say: close our side too
        if self.state == "CLOSED":
            return
        if self.peer_fin and self.fin_seq is not None and not self._fin_unacked():
            if self.ack_pending:
                self.state = "CLOSING"         # the last ACK goes out in output(), then CLOSED
            else:
                self._closed("TCP_CLOSED")

    def _fin_unacked(self):
        return self.fin_seq is not None and seq_le(self.snd_una, self.fin_seq)

    # --- our segments ---

    def output(self, now):
        if self.state == "CLOSED":
            return
        if self.state == "SYN_SENT":
            if self.syn_pending:
                self.syn_pending = False
                self.timer = now + self.rto
                self.peer._send_tcp(self, SYN, self.iss, options=struct.pack("!BBH", 2, 4, MSS))
            return
        sent = False
        while self.unsent and self.fin_seq is None:
            room = self.snd_wnd - len(self.flight)
            if room <= 0:
                break
            chunk = bytes(self.unsent[:min(MSS, room)])
            del self.unsent[:len(chunk)]
            seq = self.snd_nxt
            self.flight += chunk
            self.snd_nxt = (self.snd_nxt + len(chunk)) & 0xFFFFFFFF
            self.peer._send_tcp(self, PSH | ACK, seq, chunk)
            sent = True
        if self.fin_wanted and not self.unsent and self.fin_seq is None:
            self.fin_seq = self.snd_nxt
            self.snd_nxt = (self.snd_nxt + 1) & 0xFFFFFFFF
            self.peer._send_tcp(self, FIN | ACK, self.fin_seq)
            sent = True
        if (sent or self.unsent) and self.timer is None:
            self.timer = now + self.rto            # also the persist timer when the radio's window is shut
        if self.ack_pending and not sent:
            self.peer._send_tcp(self, ACK, self.snd_nxt)
        self.ack_pending = False
        if self.state == "CLOSING":
            self._closed("TCP_CLOSED")

    def expire(self, now):
        """The retransmit timer: send the oldest unACKed segment again, or give up."""
        self.retries += 1
        if self.retries > TCP_RETRIES:
            self.abort("TCP_TIMEOUT")
            return
        self.rto = min(self.rto * 2, RTO_MAX_US)
        self.timer = now + self.rto
        if self.state == "SYN_SENT":
            self.syn_pending = True
            self.timer = None
            self.output(now)
            return
        if self.flight:
            self.peer._send_tcp(self, PSH | ACK, self.snd_una, bytes(self.flight[:MSS]))
        elif self._fin_unacked():
            self.peer._send_tcp(self, FIN | ACK, self.fin_seq)
        elif self.unsent and self.snd_wnd == 0:
            # Zero window probe (RFC 9293 3.8.6.1): one byte beyond the window.
            byte = bytes(self.unsent[:1])
            del self.unsent[:1]
            seq = self.snd_nxt
            self.flight += byte
            self.snd_nxt = (self.snd_nxt + 1) & 0xFFFFFFFF
            self.peer._send_tcp(self, PSH | ACK, seq, byte)
        else:
            self.timer = None


class Peer:
    """The host's end of one radio boot's Ethernet link.

    seed: bytes that make every "random" choice; webgw uses the radio number and boot.
    on_bound(peer, now): DHCP gave us an address (also after a renewal that changed nothing: not called).
    on_lost(peer, reason): the lease is gone (a NAK); every TCP connection was closed.
    udp: {local port: fn(src_ip, src_port, data, now)} for datagrams to us.
    """

    def __init__(self, seed, mac=HOST_MAC):
        self.seed = bytes(seed)
        self.mac = mac
        self.ip = None
        self.radio_ip = None
        self.radio_mac = None
        self.lease_s = None
        self.bound_us = None
        self.dhcp = "idle"
        self.dhcp_due = None
        self.xid = self._number(b"xid")
        self.ip_id = 1
        self.tcp = {}
        self.udp = {}
        self.on_bound = None
        self.on_lost = None
        self.frames = []
        self.stats = {"rx": 0, "tx": 0, "ignored": 0, "bad": 0}
        self._ports = 0

    def _number(self, name, bits=32):
        return int.from_bytes(hashlib.sha256(self.seed + b"/" + name).digest()[:bits // 8], "big")

    # --- time ---

    def next_due(self):
        times = [self.dhcp_due] + [conn.timer for conn in self.tcp.values()]
        return min((time for time in times if time is not None), default=None)

    def busy(self):
        """True while a reply from us is owed soon: a pending ACK, unsent bytes or a handshake."""
        return any(conn.ack_pending or conn.unsent or conn.state != "ESTABLISHED" or conn.flight
                   for conn in self.tcp.values()) or self.dhcp in ("selecting", "requesting", "renewing")

    def output(self, now):
        """Fire due timers, then build every frame owed now. Returns a list of Ethernet frames."""
        if self.dhcp_due is not None and now >= self.dhcp_due:
            self._dhcp_timer(now)
        for conn in list(self.tcp.values()):
            if conn.timer is not None and now >= conn.timer and not conn.syn_pending:
                conn.expire(now)
        for conn in list(self.tcp.values()):
            conn.output(now)
        frames, self.frames = self.frames, []
        self.stats["tx"] += len(frames)
        return frames

    # --- frames out ---

    def _eth(self, dst, ethertype, payload):
        frame = dst + self.mac + struct.pack("!H", ethertype) + payload
        if len(frame) > FRAME_MAX:
            raise ValueError("TCPPEER_FRAME_TOO_LONG")
        self.frames.append(frame.ljust(60, b"\0"))

    def _ip(self, src, dst, proto, payload, dst_mac=None):
        header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), self.ip_id, 0x4000, 64, proto, 0,
                             src, dst)
        self.ip_id = (self.ip_id + 1) & 0xFFFF
        header = header[:10] + struct.pack("!H", checksum(header)) + header[12:]
        self._eth(dst_mac or self.radio_mac or BROADCAST_MAC, ETH_IPV4, header + payload)

    def _udp(self, src, dst, sport, dport, data, dst_mac=None):
        segment = struct.pack("!HHHH", sport, dport, 8 + len(data), 0) + data
        pseudo = src + dst + struct.pack("!BBH", 0, PROTO_UDP, len(segment))
        value = checksum(pseudo + segment) or 0xFFFF
        self._ip(src, dst, PROTO_UDP, segment[:6] + struct.pack("!H", value) + segment[8:], dst_mac)

    def udp_send(self, sport, dport, data):
        """One datagram to the radio (for example the debug link on port 3334). False before DHCP."""
        if self.ip is None:
            return False
        self._udp(self.ip, self.radio_ip, sport, dport, data)
        return True

    def udp_frame(self, sport, dport, data):
        """One datagram to the radio as an Ethernet frame, returned and not queued: the caller submits it at its
        own time (dlmux.py does, on its 1 ms grid). None before DHCP."""
        if self.ip is None:
            return None
        queued, self.frames = self.frames, []
        try:
            self._udp(self.ip, self.radio_ip, sport, dport, data)
            return self.frames[0]
        finally:
            self.frames = queued

    def _send_tcp(self, conn, flags, seq, data=b"", options=b""):
        offset = (20 + len(options)) // 4
        ack = conn.rcv_nxt if flags & ACK else 0
        header = struct.pack("!HHIIBBHHH", conn.sport, conn.port, seq, ack, offset << 4, flags, WINDOW, 0, 0)
        header += options
        pseudo = self.ip + self.radio_ip + struct.pack("!BBH", 0, PROTO_TCP, len(header) + len(data))
        header = header[:16] + struct.pack("!H", checksum(pseudo + header + data)) + header[18:]
        self._ip(self.ip, self.radio_ip, PROTO_TCP, header + data)

    # --- DHCP client ---

    def start_dhcp(self, now):
        """Begin DISCOVER now; retried every DHCP_RETRY_US until an OFFER comes."""
        self.dhcp = "selecting"
        self.dhcp_due = now

    def _bootp(self, kind, options=b"", ciaddr=b"\0" * 4):
        body = struct.pack("!BBBBIHH4s4s4s4s", 1, 1, 6, 0, self.xid, 0, 0x8000 if ciaddr == b"\0" * 4 else 0,
                           ciaddr, b"\0" * 4, b"\0" * 4, b"\0" * 4)
        body += self.mac + b"\0" * 10 + b"\0" * 192 + struct.pack("!I", 0x63825363)
        return body + bytes((53, 1, kind)) + options + b"\xff"

    def _dhcp_timer(self, now):
        self.dhcp_due = now + DHCP_RETRY_US
        if self.dhcp == "selecting":
            self._udp(b"\0" * 4, b"\xff" * 4, DHCP_CLIENT, DHCP_SERVER, self._bootp(1), BROADCAST_MAC)
        elif self.dhcp == "requesting":
            self._udp(b"\0" * 4, b"\xff" * 4, DHCP_CLIENT, DHCP_SERVER,
                      self._bootp(3, bytes((50, 4)) + self.offer + bytes((54, 4)) + self.server), BROADCAST_MAC)
        elif self.dhcp in ("bound", "renewing"):
            # Renewal (RFC 2131 4.4.5): ask the radio directly for the same address, from that address.
            self.dhcp = "renewing"
            self._udp(self.ip, self.radio_ip, DHCP_CLIENT, DHCP_SERVER, self._bootp(3, ciaddr=self.ip))
        else:
            self.dhcp_due = None

    def _dhcp_reply(self, data, src_mac, now):
        if len(data) < 240 or data[0] != 2 or struct.unpack_from("!I", data, 4)[0] != self.xid:
            return
        if data[28:34] != self.mac or struct.unpack_from("!I", data, 236)[0] != 0x63825363:
            return
        options = {}
        index = 240
        while index + 1 < len(data) and data[index] != 255:
            if data[index] == 0:
                index += 1
                continue
            end = index + 2 + data[index + 1]
            if end > len(data):
                return                          # an option runs past the message: not a reply we take
            options[data[index]] = data[index + 2:end]
            index = end
        if len(options.get(53, b"")) != 1:
            return
        kind = options[53][0]
        server = options.get(54)
        if kind == 2 and self.dhcp == "selecting" and server is not None and len(server) == 4:
            self.offer, self.server = data[16:20], server
            self.dhcp, self.dhcp_due = "requesting", now
        elif kind == 5 and self.dhcp in ("requesting", "renewing"):
            renewed = self.dhcp == "renewing"
            lease = options.get(51)
            self.lease_s = struct.unpack("!I", lease)[0] if lease is not None and len(lease) == 4 else 600
            self.ip, self.radio_ip, self.radio_mac = data[16:20], server or data[20:24], src_mac
            self.bound_us = now
            self.dhcp = "bound"
            self.dhcp_due = now + self.lease_s * 1_000_000 // 2      # T1: renew at half the lease
            if not renewed and self.on_bound is not None:
                self.on_bound(self, now)
        elif kind == 6 and self.dhcp in ("requesting", "renewing"):
            self._lose("DHCP_NAK", now)

    def _lose(self, reason, now):
        for conn in list(self.tcp.values()):
            conn._closed(reason)
        self.ip = self.radio_ip = None
        self.start_dhcp(now)
        if self.on_lost is not None:
            self.on_lost(self, reason)

    # --- TCP ---

    def connect(self, port, now):
        """Open a connection to the radio's TCP port. The SYN goes out in the next output()."""
        if self.ip is None:
            raise ValueError("TCPPEER_NO_ADDRESS")
        self._ports += 1
        sport = 49152 + (self._number(b"port", 16) + self._ports) % 16000
        while sport in self.tcp:
            sport = 49152 + (sport - 49152 + 1) % 16000
        conn = Tcp(self, port, sport, self._number(b"iss/%d" % self._ports), now)
        self.tcp[sport] = conn
        return conn

    # --- frames in ---

    def receive(self, frame, now):
        """One Ethernet frame from the radio."""
        self.stats["rx"] += 1
        if len(frame) < 14:
            self.stats["bad"] += 1
            return
        ethertype = struct.unpack_from("!H", frame, 12)[0]
        if frame[:6] not in (self.mac, BROADCAST_MAC):
            self.stats["ignored"] += 1
        elif ethertype == ETH_ARP:
            self._arp(frame)
        elif ethertype == ETH_IPV4:
            self._ipv4(frame, now)
        else:
            self.stats["ignored"] += 1

    def _arp(self, frame):
        arp = frame[14:42]
        if len(arp) < 28 or struct.unpack_from("!HHBBH", arp) != (1, ETH_IPV4, 6, 4, 1):
            return
        if self.ip is None or arp[24:28] != self.ip:
            return
        reply = struct.pack("!HHBBH", 1, ETH_IPV4, 6, 4, 2) + self.mac + self.ip + arp[8:14] + arp[14:18]
        self._eth(frame[6:12], ETH_ARP, reply)

    def _ipv4(self, frame, now):
        ip = frame[14:]
        if len(ip) < 20 or ip[0] >> 4 != 4:
            self.stats["bad"] += 1
            return
        length = (ip[0] & 15) * 4
        total = struct.unpack_from("!H", ip, 2)[0]
        if length < 20 or total < length or total > len(ip) or checksum(ip[:length]):
            self.stats["bad"] += 1
            return
        if struct.unpack_from("!H", ip, 6)[0] & 0x3FFF:
            self.stats["ignored"] += 1          # fragments: the radio never sends them on this link
            return
        src, dst, proto, payload = ip[12:16], ip[16:20], ip[9], ip[length:total]
        if proto == PROTO_UDP and len(payload) >= 8:
            sport, dport, size = struct.unpack_from("!HHH", payload)
            if not 8 <= size <= len(payload):
                self.stats["bad"] += 1
                return
            data = payload[8:size]
            if dport == DHCP_CLIENT and sport == DHCP_SERVER:
                self._dhcp_reply(data, frame[6:12], now)
            elif self.ip is not None and dst == self.ip and src == self.radio_ip and dport in self.udp:
                self.udp[dport](src, sport, data, now)
            else:
                self.stats["ignored"] += 1
        elif proto == PROTO_TCP and len(payload) >= 20 and self.ip is not None and dst == self.ip:
            pseudo = src + dst + struct.pack("!BBH", 0, PROTO_TCP, len(payload))
            if src != self.radio_ip or checksum(pseudo + payload):
                self.stats["bad"] += 1
                return
            sport, dport, seq, ack, offset, flags, window = struct.unpack_from("!HHIIBBH", payload)
            conn = self.tcp.get(dport)
            if conn is None or conn.port != sport:
                self.stats["ignored"] += 1      # nothing of ours: no RST, the radio times it out
                return
            if not 20 <= (offset >> 4) * 4 <= len(payload):
                self.stats["bad"] += 1
                return
            conn.segment(seq, ack, flags, window, payload[(offset >> 4) * 4:], now)
        else:
            self.stats["ignored"] += 1
