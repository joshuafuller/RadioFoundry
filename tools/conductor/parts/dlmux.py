# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The debug link multiplexer: many clients, one radio, one request upstream at a time (ADR 0004).

The firmware has one frame parser per stream (core/debuglink/debuglink.c:63) and no idea who is asking.
This part stands in front of it. Each client has its own frame parser (A5 5A, command, sequence, length,
payload, CRC; docs/spec/debuglink.md 2). The multiplexer sends one request upstream at a time, under its
own sequence number, and returns the reply to the client that asked, under the client's sequence number.

Roles. A session is bound to a role when the world client opens it (world API "debuglink.open"):
  observer    may only read, and is answered from host caches (dlcache.py). PING is echoed here.
              It never reaches the firmware. Anything else is refused and audited.
  controller  must hold the radio's current lease generation and present its grant secret (lease.py), the
              one-time string that only the caller of lease.grant got. Principal and generation alone are
              public (lease.status), so they never open a controller session. Each command passes
              lease.check first, which also refuses the maintenance commands (REBOOT, READ_MEM, ...)
              without the capability.
Unknown commands are refused for everyone before the firmware (status 1). GET_LOG is always answered from
the cache, so nobody drains the firmware log behind the cache's back.

Holds. A controller's KEY or PTT that leaves a key held is tracked with holds.py, so a revoke, disconnect
or pause releases it before anyone else gets control. Three hold kinds:
  debuglink.ptt    PTT down, KEY action 19 (ptt) DOWN or timed. Released by a "PTT off" frame.
  debuglink.raw    KEY kind 0 DOWN of a raw-edge key (monitor). Released by the same action's UP.
  debuglink.timed  every other KEY that holds a key for a time (PRESS, LONG, REPEAT, kind 1, kind 3). The
                   firmware cannot cancel these early (core/input/input_task.c:343-378: DOWN/UP act only on
                   raw-edge keys). The multiplexer never needs to: the input task ends each hold at its
                   time-out, at most INPUT_INJECT_MAX_MS (10 s). Its "release" is a PING, which changes
                   nothing; the confirm below waits for the input task to let go.

Confirming a release. holds.py counts a release only when a TRACE line confirms it. The proof uses the
firmware's own words:
  1. a GET_VIEW "link" section reply (debuglink_view.c:326, input.injected) shows input.injected=0x00000000,
     to a request that entered the radio after the release (a higher world input sequence). The host link
     answers in order, so the release had already run. input.injected covers both the KEY slot and the PTT
     slot (input_task.c:417-423);
  2. then two "BOARD key.scan" TRACE lines (boards/vhost/keypad_vhost.c:92). Each is one pass of the input
     task (input_task.c:120-144), so the second pass certainly combined its keys with nothing injected.
The link section is read by the cache refresh at its fixed cadence, so a confirm needs no extra firmware
request and the firmware traffic does not depend on whether a hold exists.

Program clients connect over a per-user Unix socket in the world folder (0600, peer UID checked), one
socket per opened session; it accepts one connection and is then removed.

Time of each request (ADR 0005 and 0008: grant length never changes what the firmware sees). A request goes up at
a time that depends only on the firmware's own output, never on where a grant happened to end:
  - a cache refresh starts at its cadence time (250000 us, 500000 us, ...);
  - the request after a reply goes at the first 1 ms grid point after the reply's own time;
  - the request after a time-out goes at the first grid point at or after the time-out.
While anything is queued or in flight, every grant ends on each grid point and on that due time. Example: the
radio answers GET_VIEW 0 at 250312 us. The multiplexer sees that reply at 250500 us with 500 us grants, at 251000 us
with 1 ms grants and at 251000 us with 30 ms grants. In all three it sends GET_VIEW 1 at 251000 us, with the same
world input sequence, because it waits for the due time before it submits the frame.

Which wire. A request goes on the UART (BYTES_IN) until the web gateway (webgw.py) has turned the
radio's USB port into a network link and holds a DHCP lease on it. From then on the firmware answers the serial
protocols only over UDP (core/hostlink/hostlink.c hostlink_write), so the request goes as one UDP datagram to the
radio's port 3334 (an ETH_IN frame the gateway builds, webgw.serial_frame), at the same time it would have gone on
the UART, and the reply comes back through webgw.Link._serial to serial_out(). Replies on the two wires have their
own parsers. Example: in a vradio world the refreshes at 250000 us .. 1500000 us go on the UART; the one at
1750000 us goes over UDP once DHCP is bound, so the cache keeps showing the radio while a browser is attached.

The world has no poll hook yet, so install() chains this part's socket service onto world.api.poll.
"""

from collections import deque
from dataclasses import dataclass, field
import binascii
import itertools
import os
import re
import selectors
import socket
import struct

from ..channel import Kind
from ..world import WorldError
from . import audit, dlcache, holds, lease, tcppeer

SOF = b"\xa5\x5a"
SERIAL_PORT = 3334       # IPGW_SERIAL_PORT (core/dmr/ipGateway.h:84): the debug link over UDP in network mode
REQUEST_MAX = 64
REPLY_MAX = 1024
ERROR = 0xFF
ST_OK, ST_UNKNOWN, ST_BAD_ARG, ST_BUSY, ST_UNAVAILABLE, ST_BAD_CRC, ST_TOO_LONG, ST_REFUSED = range(8)
REPLY_TIMEOUT_US = 200_000
SLICE_US = 1000          # while a request is in flight, grants end on every 1 ms point (the grid)
SESSION_SOURCE = 0x100   # world input source of session n is SESSION_SOURCE + n
LINK_SECTION = 6         # DL_VIEW_LINK (core/debuglink/debuglink.h:28)
ACT_PTT, ACT_ENC = 19, 23
GESTURE_DOWN, GESTURE_UP = 3, 4
OUTGOING_MAX = 1 << 20

COMMANDS = {0x01: "PING", 0x02: "VERSION", 0x03: "GET_DIAG", 0x04: "RESET_COUNTERS", 0x05: "SCREENSHOT",
            0x06: "KEY", 0x07: "PTT", 0x08: "GET_LOG", 0x09: "GET_VIEW", 0x0A: "RADIO", 0x0B: "INFO_CHIP",
            0x0C: "READ_MEM", 0x0D: "REBOOT", 0x0E: "DMR_DATA", 0x0F: "STORAGE", 0x10: "AUDIO_PROBE",
            0x11: "APRS", 0x15: "SPLIT", 0x19: "SNMP", 0x1A: "STUB", 0x1B: "CRASH", 0x1C: "CODEPLUG",
            0x1D: "WATCHDOG", 0x1E: "RX2", 0x1F: "IDDB"}
CACHED = frozenset((dlcache.VERSION, dlcache.GET_DIAG, dlcache.GET_VIEW, dlcache.INFO_CHIP, dlcache.GET_LOG))
INJECTED = re.compile(rb"\ninput\.injected=0x([0-9a-f]{8})\n")
KEY_SCAN = b" BOARD key.scan "


def crc16(data, crc=0xFFFF):
    """CRC-16/CCITT-FALSE (docs/spec/debuglink.md 2)."""
    return binascii.crc_hqx(data, crc)


def frame(cmd, seq, payload=b""):
    body = struct.pack("<BBH", cmd, seq, len(payload)) + payload
    return SOF + body + struct.pack("<H", crc16(body))


class Parser:
    """A byte stream in, whole frames out: ("frame", cmd, seq, payload) or ("error", cmd, seq, status)."""

    def __init__(self, limit):
        self.limit = limit
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer += data
        out = []
        buffer = self.buffer
        while True:
            start = buffer.find(SOF)
            if start < 0:
                # Junk is dropped, never forwarded; keep a trailing A5 that may start a frame.
                del buffer[:max(0, len(buffer) - 1) if buffer.endswith(b"\xa5") else len(buffer)]
                return out
            del buffer[:start]
            if len(buffer) < 6:
                return out
            cmd, seq, length = struct.unpack_from("<BBH", buffer, 2)
            if length > self.limit:
                out.append(("error", cmd, seq, ST_TOO_LONG))
                del buffer[:6]
                continue
            if len(buffer) < 8 + length:
                return out
            body = bytes(buffer[2:6 + length])
            (crc,) = struct.unpack_from("<H", buffer, 6 + length)
            del buffer[:8 + length]
            if crc != crc16(body):
                out.append(("error", cmd, seq, ST_BAD_CRC))
            else:
                out.append(("frame", cmd, seq, body[4:]))


@dataclass
class Session:
    id: int
    radio: int
    principal: str
    role: str
    generation: int | None
    outgoing: bytearray = field(default_factory=bytearray)
    cursors: dict = field(default_factory=dict)
    closed: bool = False
    connection: object = None
    listener: object = None
    path: object = None
    secret: str | None = field(default=None, repr=False)

    def __post_init__(self):
        self.parser = Parser(REQUEST_MAX)

    @property
    def source(self):
        return SESSION_SOURCE + self.id

    def take(self):
        """In-process clients and tests: the reply bytes so far."""
        data = bytes(self.outgoing)
        self.outgoing.clear()
        return data


@dataclass
class Request:
    owner: object            # a Session, "cache", or None (a conductor frame: release, scenario)
    cmd: int
    seq: int                 # the owner's sequence number (the client's), or the upstream one
    payload: bytes


@dataclass
class InFlight:
    request: Request
    upstream_seq: int
    input_sequence: int
    time_us: int


class Upstream:
    """One radio boot's link: the queue, what is in flight, the reply parser and the confirm evidence."""

    def __init__(self, boot):
        self.boot = boot
        self.queue = deque()
        self.flight = []          # sorted by (time_us, input_sequence)
        self.owners = {}          # upstream seq -> Request, set just before submit
        self.parser = Parser(REPLY_MAX)
        self.udp_parser = Parser(REPLY_MAX)    # replies over UDP (network mode): a stream of their own
        self.next_seq = itertools.cycle(range(256))
        self.clear = []           # [input_sequence, key scans since] of link views showing nothing injected
        self.due = 0              # world time before which the next request must not go up (module text)

    def seq(self):
        busy = {entry.upstream_seq for entry in self.flight} | set(self.owners)
        for _ in range(256):
            value = next(self.next_seq)
            if value not in busy:
                return value
        raise WorldError("DLMUX_NO_SEQUENCE")


class Multiplexer:
    def __init__(self, world):
        self.world = world
        self.audit = audit.get(world)
        self.holds = holds.get(world)
        self.leases = lease.get(world)
        self.caches = dlcache.get(world)
        self.sessions = {}
        self.links = {}
        self._ids = itertools.count(1)
        self._next_tick = None
        self.selector = None
        self.holds.register("debuglink.ptt", self._release_ptt, self._confirms)
        self.holds.register("debuglink.raw", self._release_raw, self._confirms)
        self.holds.register("debuglink.timed", self._release_timed, self._confirms)
        world.on_input(self._input)
        world.on_outbox(Kind.BYTES_OUT, self._bytes_out)
        world.on_outbox(Kind.TRACE, self._trace)
        world.add_grant_limit(self._limit)
        world.on_grant_done(self._grant_done)
        world.on_end(self.close)
        self.leases.on_revoke(self._revoked)

    # --- sessions ---

    def connect(self, radio, principal, role, generation=None, secret=None):
        """Bind a role to a new session (the world client's choice). Raises WorldError when refused.

        A controller needs the lease's principal, generation and grant secret (lease.holder audits a refusal).
        """
        if not isinstance(principal, str) or not lease.PRINCIPAL.fullmatch(principal):
            self._refuse(None, radio, "LEASE_BAD_PRINCIPAL", role=role)
        if type(radio) is not int or radio not in self.world.boots:
            self._refuse(principal, None, "WORLD_UNKNOWN_RADIO", role=role)
        slot = self.world.radios.get(radio)
        if slot is not None and Kind.BYTES_IN not in getattr(slot.channel, "supported_inputs", (Kind.BYTES_IN,)):
            self._refuse(principal, radio, "WORLD_UNSUPPORTED_INPUT:BYTES_IN", role=role)
        if role not in ("controller", "observer"):
            self._refuse(principal, radio, "DLMUX_BAD_ROLE")
        if role == "controller":
            self.leases.holder("debuglink_open", principal, radio, generation, secret, role=role)
        session = Session(next(self._ids), radio, principal, role, generation if role == "controller" else None,
                          secret=secret if role == "controller" else None)
        self.audit.emit("debuglink_open", principal, radio, "accepted", "OK", role=role,
                        lease_generation=session.generation, session=session.id)
        self.sessions[session.id] = session
        return session

    def _refuse(self, principal, radio, reason, **fields):
        self.audit.emit("debuglink_open", principal, radio, "refused", reason, **fields)
        raise WorldError(reason)

    def disconnect(self, session, reason="DLMUX_CLIENT_CLOSED"):
        if session.closed:
            return
        session.closed = True
        self.sessions.pop(session.id, None)
        link = self.links.get(session.radio)
        if link is not None:
            link.queue = deque(request for request in link.queue if request.owner is not session)
        self._close_socket(session)
        self.audit.emit("debuglink_close", session.principal, session.radio, "closed", reason,
                        role=session.role, session=session.id)
        held = self.leases.held.get(session.radio)
        if (session.role == "controller" and held is not None and held.principal == session.principal
                and held.generation == session.generation):
            self.leases.revoke(session.radio, "LEASE_DISCONNECT")

    def _revoked(self, radio, principal, generation, reason):
        """A raw debug client loses its connection with its lease."""
        for session in list(self.sessions.values()):
            if (session.role, session.radio, session.principal, session.generation) == (
                    "controller", radio, principal, generation):
                self.disconnect(session, reason)

    # --- client frames ---

    def feed(self, session, data):
        """Bytes from a client. Replies from the cache or refusals are queued on the session at once."""
        if session.closed:
            return
        for item in session.parser.feed(data):
            if item[0] == "error":
                _, cmd, seq, status = item
                self._answer(session, ERROR, seq, bytes((status, cmd)))
            else:
                self._request(session, *item[1:])

    def _answer(self, session, cmd, seq, payload):
        if session.closed:
            return
        session.outgoing += frame(cmd, seq, payload)
        if len(session.outgoing) > OUTGOING_MAX:
            self.disconnect(session, "DLMUX_SLOW_CLIENT")
        elif session.connection is not None and self.selector is not None:
            self.selector.modify(session.connection, selectors.EVENT_READ | selectors.EVENT_WRITE, session)

    def _deny(self, session, cmd, seq, status, reason, name):
        self.audit.emit("control", session.principal, session.radio, "refused", reason, layer="debug",
                        role=session.role, command=name, cmd=cmd, session=session.id)
        self._answer(session, cmd | 0x80, seq, bytes((status,)))

    def _request(self, session, cmd, seq, payload):
        name = COMMANDS.get(cmd)
        if name is None:
            self._deny(session, cmd, seq, ST_UNKNOWN, "DLMUX_UNKNOWN_COMMAND", f"0x{cmd:02x}")
            return
        if session.role == "observer" or cmd == dlcache.GET_LOG:
            if cmd == dlcache.PING:
                self._answer(session, cmd | 0x80, seq, bytes((ST_OK,)) + payload)
            elif cmd in CACHED:
                self._answer(session, cmd | 0x80, seq,
                             self.caches.answer(session.radio, cmd, payload, session.cursors))
            else:
                self._deny(session, cmd, seq, ST_REFUSED, "DLMUX_OBSERVER_READ_ONLY", name)
            return
        try:
            self.leases.check(session.radio, session.principal, generation=session.generation,
                              secret=session.secret, command=name)
        except WorldError:
            self._answer(session, cmd | 0x80, seq, bytes((ST_REFUSED,)))   # lease.check wrote the audit line
            return
        request = Request(session, cmd, seq, payload)
        link = self._link(session.radio)
        if link is None:
            self._deny(session, cmd, seq, ST_REFUSED, "WORLD_RADIO_OFF", name)
            return
        link.queue.append(request)
        self._pump(session.radio, internal=False)

    @staticmethod
    def _hold_of(cmd, payload):
        """(hold kind, name, "down" | "up") for a controller's KEY or PTT, or None."""
        if cmd == 0x07 and len(payload) in (1, 3):
            return "debuglink.ptt", "PTT", "down" if payload[0] else "up"
        if cmd != 0x06 or not payload:
            return None
        kind = payload[0]
        if kind == 0 and len(payload) >= 4:
            action, gesture = payload[1], payload[2]
            if action == ACT_ENC or gesture > GESTURE_UP:
                return None
            if action == ACT_PTT:
                if gesture in (GESTURE_DOWN, GESTURE_UP):
                    return "debuglink.ptt", "PTT", "down" if gesture == GESTURE_DOWN else "up"
                return None
            if gesture in (GESTURE_DOWN, GESTURE_UP):
                return "debuglink.raw", f"action:{action}", "down" if gesture == GESTURE_DOWN else "up"
            return "debuglink.timed", "timed", "down"
        if kind == 1 and len(payload) >= 7:
            return "debuglink.timed", "timed", "down"
        if kind == 3 and len(payload) >= 4:
            return ("debuglink.ptt", "PTT", "down") if payload[1] == ACT_PTT else ("debuglink.timed", "timed", "down")
        return None

    # --- upstream ---

    def _link(self, radio):
        if radio not in self.world.radios:
            self.links.pop(radio, None)
            return None
        if Kind.BYTES_IN not in getattr(self.world.radios[radio].channel, "supported_inputs", (Kind.BYTES_IN,)):
            return None
        boot = self.world.boots[radio]
        link = self.links.get(radio)
        if link is None or link.boot != boot:
            old = link
            link = self.links[radio] = Upstream(boot)
            if old is not None:
                for request in [entry.request for entry in old.flight] + list(old.queue):
                    self._lost(request, "DLMUX_RADIO_RESTARTED")
        return link

    def _lost(self, request, reason):
        if isinstance(request.owner, Session):
            self.audit.emit("debuglink_lost", request.owner.principal, request.owner.radio, "lost", reason,
                            cmd=request.cmd, session=request.owner.id)
            self._answer(request.owner, request.cmd | 0x80, request.seq, bytes((ST_BUSY,)))

    def _in_flight(self, link):
        return any(entry.time_us <= self.world.next_start for entry in link.flight)

    @staticmethod
    def _grid_after(time_us):
        """The first 1 ms grid point after time_us: 251000 for a reply at 250312, 251000 for one at 250000."""
        return (time_us // SLICE_US + 1) * SLICE_US

    def _pump(self, radio, *, time_us=None, internal=True):
        """Send the next queued request when nothing is in flight: one request upstream at a time."""
        link = self._link(radio)
        while (link is not None and link.queue and not self._in_flight(link)
               and self.world.next_start >= link.due):
            request = link.queue.popleft()
            owner = request.owner
            if isinstance(owner, Session) and owner.closed:
                continue
            seq = link.seq()
            link.owners[seq] = request
            source = owner.source if isinstance(owner, Session) else holds.CONDUCTOR_SOURCE
            try:
                self.world.submit(radio, *self._wire(radio, frame(request.cmd, seq, request.payload)), source,
                                  time_us=time_us, internal=internal)
            except WorldError as error:
                link.owners.pop(seq, None)
                if isinstance(owner, Session):
                    self._deny(owner, request.cmd, request.seq, ST_REFUSED, str(error), COMMANDS[request.cmd])
                continue
            if isinstance(owner, Session):
                hold = self._hold_of(request.cmd, request.payload)
                if hold is not None:
                    kind, name, edge = hold
                    if edge == "down":
                        self.holds.down(radio, kind, name, owner.principal)
                    else:
                        self.holds.up(radio, kind, name)

    def _wire(self, radio, data):
        """(kind, payload) that carries data to the radio's debug link: UDP once the gateway's network is up."""
        gateway = getattr(self.world, "webgw", None)
        datagram = gateway.serial_frame(radio, data) if gateway is not None else None
        return (Kind.ETH_IN, datagram) if datagram is not None else (Kind.BYTES_IN, data)

    def _input(self, event):
        """world.on_input: every debug link frame the radio will get, from any source, joins the flight list:
        BYTES_IN, and ETH_IN that is UDP to the radio's serial port."""
        payload = event.payload
        if event.kind == Kind.ETH_IN:
            datagram = tcppeer.udp_datagram(payload)
            if datagram is None or datagram[1] != SERIAL_PORT:
                return
            payload = datagram[2]
        elif event.kind != Kind.BYTES_IN:
            return
        link = self._link(event.radio)
        if link is None:
            return
        for item in Parser(REQUEST_MAX).feed(payload):
            if item[0] != "frame":
                continue
            _, cmd, seq, payload = item
            request = link.owners.pop(seq, None) or Request(None, cmd, seq, payload)
            link.flight.append(InFlight(request, seq, event.sequence, event.time_us))
        link.flight.sort(key=lambda entry: (entry.time_us, entry.input_sequence))

    def _bytes_out(self, radio, message):
        link = self._link(radio)
        if link is not None:
            self._replies(radio, link, link.parser, message.payload, message.time_us)

    def serial_out(self, radio, data, time_us):
        """webgw.py: the data of one UDP datagram from the radio's serial port (network mode), at its time."""
        link = self._link(radio)
        if link is not None:
            self._replies(radio, link, link.udp_parser, data, time_us)

    def _replies(self, radio, link, parser, data, time_us):
        for item in parser.feed(data):
            if item[0] != "frame":
                continue
            _, rcmd, seq, payload = item
            match = next((entry for entry in link.flight if entry.upstream_seq == seq and entry.time_us
                          <= time_us and rcmd in (entry.request.cmd | 0x80, ERROR)), None)
            if match is None:
                continue        # not an answer to a request we know of: never routed to a client
            link.due = max(link.due, self._grid_after(time_us))
            for entry in link.flight[:link.flight.index(match)]:
                if entry.time_us <= match.time_us:
                    link.flight.remove(entry)
                    self._lost(entry.request, "DLMUX_NO_REPLY")
            link.flight.remove(match)
            self._reply(radio, link, match, rcmd, payload)

    def _reply(self, radio, link, entry, rcmd, payload):
        request = entry.request
        if request.cmd == dlcache.GET_VIEW and request.payload[:1] == bytes((LINK_SECTION,)) and payload[:1] == b"\0":
            found = INJECTED.search(payload)
            if found and int(found.group(1), 16) == 0:
                link.clear = (link.clear + [[entry.input_sequence, 0]])[-8:]
            else:
                link.clear = []
        if isinstance(request.owner, Session):
            # Audit the completion status of every forwarded request. lease.check logged
            # "accepted"; this line says how the radio answered (READ_MEM outside the fake array: refused).
            # Generation and the client's sequence join it to that authorization line.
            owner = request.owner
            status = payload[0] if payload else None
            result, reason = (("completed", "OK") if status == ST_OK else
                              ("refused", "DLMUX_RADIO_REFUSED") if status == ST_REFUSED else
                              ("failed", "DLMUX_RADIO_STATUS"))
            self.audit.emit("control_reply", owner.principal, owner.radio, result, reason, layer="debug",
                            role=owner.role, command=COMMANDS.get(request.cmd, f"0x{request.cmd:02x}"),
                            cmd=request.cmd, status=status, session=owner.id, seq=request.seq,
                            lease_generation=owner.generation)
            self._answer(request.owner, rcmd, request.seq, payload)
        elif request.owner == "cache":
            for cmd, follow in self.caches.reply(radio, request.cmd, request.payload, payload):
                link.queue.append(Request("cache", cmd, 0, follow))

    def _trace(self, radio, message):
        link = self.links.get(radio)
        if link is not None and link.clear and KEY_SCAN in message.payload:
            for item in link.clear:
                item[1] += 1

    # --- holds ---

    def _conductor_frame(self, radio, cmd, payload):
        link = self._link(radio)
        if link is None:
            raise WorldError("WORLD_RADIO_OFF")
        seq = link.seq()
        link.owners[seq] = Request(None, cmd, seq, payload)
        return self._wire(radio, frame(cmd, seq, payload))

    def _release_ptt(self, radio, hold):
        return self._conductor_frame(radio, 0x07, b"\x00")

    def _release_raw(self, radio, hold):
        action = int(hold.name.split(":")[1])
        return self._conductor_frame(radio, 0x06, bytes((0, action, GESTURE_UP, 0)))

    def _release_timed(self, radio, hold):
        # Nothing can end a timed hold early; a PING marks the point after which the view must show it ended.
        return self._conductor_frame(radio, dlcache.PING, b"release")

    def _confirms(self, radio, hold, line):
        link = self.links.get(radio)
        return (link is not None and link.boot == hold.boot and hold.release_sequence is not None
                and any(sequence > hold.release_sequence and scans >= 2 for sequence, scans in link.clear))

    # --- time ---

    def _host_port(self):
        # A replay world feeds the recorded refreshes back as inputs, so it must not send its own.
        if self.world.config.replay:
            return False
        args = tuple(self.world.config.radio_args)
        return any(args[index:index + 2] == ("--host-port", "on") for index in range(len(args) - 1))

    def _limit(self, now):
        limits = []
        if self.world.radios and self._host_port():
            limits.append(self._next_tick if self._next_tick and self._next_tick > now
                          else self.caches.next_tick(now))
        if any(link.queue or self._in_flight(link) for link in self.links.values()):
            limits.append(self._grid_after(now))
        limits.extend(link.due for link in self.links.values() if link.queue and link.due > now)
        return min(limits, default=None)

    def _grant_done(self, target, outboxes):
        if self._next_tick is None:
            self._next_tick = self.caches.next_tick(0)
        tick = target >= self._next_tick and self._host_port()
        if tick:
            tick_us, self._next_tick = self._next_tick, self.caches.next_tick(target)
        start = self.world.next_start
        for radio in sorted(self.world.radios):
            link = self._link(radio)
            if link is None:
                continue
            for entry in [entry for entry in link.flight if entry.time_us + REPLY_TIMEOUT_US <= target]:
                link.flight.remove(entry)
                self._lost(entry.request, "DLMUX_NO_REPLY")
                deadline = entry.time_us + REPLY_TIMEOUT_US
                link.due = max(link.due, -(-deadline // SLICE_US) * SLICE_US)
            if tick and not any(request.owner == "cache" for request in link.queue):
                link.queue.extend(Request("cache", cmd, 0, payload)
                                  for cmd, payload in self.caches.tick_requests(radio))
                link.due = max(link.due, tick_us)
            self._pump(radio, time_us=max(target, start))
        return ()

    # --- program client sockets ---

    def open(self, request):
        """World API debuglink.open {radio, principal, role, generation, secret}: a private one-client socket.

        generation and secret are for a controller: the values the lease.grant reply gave its caller.
        """
        directory = self.world.api.directory
        if directory is None:
            raise WorldError("DLMUX_NO_WORLD_FOLDER")
        session = self.connect(request.get("radio"), request.get("principal"), request.get("role"),
                               request.get("generation"), request.get("secret"))
        path = directory / f"debuglink-{session.id}.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)
        try:
            listener.bind(str(path))
        finally:
            os.umask(previous)
        os.chmod(path, 0o600)
        listener.listen(1)
        listener.setblocking(False)
        session.listener, session.path = listener, path
        if self.selector is None:
            self.selector = selectors.DefaultSelector()
        self.selector.register(listener, selectors.EVENT_READ, session)
        return {"path": str(path), "session": session.id, "role": session.role}

    def _close_socket(self, session):
        for sock in (session.listener, session.connection):
            if sock is not None:
                if self.selector is not None:
                    try:
                        self.selector.unregister(sock)
                    except (KeyError, ValueError):
                        pass
                sock.close()
        session.listener = session.connection = None
        if session.path is not None:
            session.path.unlink(missing_ok=True)
            session.path = None

    def poll(self):
        if self.selector is None:
            return
        for key, mask in self.selector.select(0):
            session = key.data
            if session.closed:
                continue
            if key.fileobj is session.listener:
                connection, _ = session.listener.accept()
                try:
                    same = self.world.api.uid_reader(connection) == os.getuid()
                except OSError:
                    same = False
                if not same:
                    connection.close()
                    self.audit.emit("debuglink_connect", session.principal, session.radio, "refused",
                                    "DLMUX_OTHER_USER", session=session.id)
                    continue
                # One connection per session: the socket file goes away once it is taken.
                self.selector.unregister(session.listener)
                session.listener.close()
                session.listener = None
                session.path.unlink(missing_ok=True)
                session.path = None
                connection.setblocking(False)
                session.connection = connection
                self.selector.register(connection, selectors.EVENT_READ, session)
                continue
            try:
                if mask & selectors.EVENT_READ:
                    data = session.connection.recv(4096)
                    if not data:
                        self.disconnect(session)
                        continue
                    self.feed(session, data)
                if mask & selectors.EVENT_WRITE and not session.closed and session.outgoing:
                    sent = session.connection.send(session.outgoing)
                    del session.outgoing[:sent]
                if not session.closed and not session.outgoing:
                    self.selector.modify(session.connection, selectors.EVENT_READ, session)
            except BlockingIOError:
                continue
            except OSError:
                self.disconnect(session, "DLMUX_SOCKET_ERROR")

    def status(self):
        return {"cadence_us": self.caches.cadence_us,
                "sessions": [{"session": s.id, "radio": s.radio, "principal": s.principal, "role": s.role}
                             for s in self.sessions.values()]}

    def close(self):
        for session in list(self.sessions.values()):
            session.closed = True
            self._close_socket(session)
        self.sessions.clear()
        if self.selector is not None:
            self.selector.close()
            self.selector = None


def get(world):
    if getattr(world, "dlmux", None) is None:
        mux = world.dlmux = Multiplexer(world)
        api = world.api
        api.register("debuglink.open", mux.open)
        api.register("debuglink.status", lambda request: mux.status())
        poll = api.poll

        def chained(timeout=0):
            mux.poll()
            poll(timeout)

        api.poll = chained
    return world.dlmux


def install(world):
    get(world)
