# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The web gateway: browsers use the radio's own front panel page through the conductor (ADR 0004).

The problem. The firmware's web control admits one /ws at a time (core/net/http.c:442-444). It gives control to
anyone who connects from the link-host address (core/net/webctl.c:456), and it checks no Origin and no Host
(see core/net/webctl.c and docs/dev/adr/0004-control-ownership.md). So a hostile web page on the same PC could press PTT.

What this part does. It stands between the browsers and the radio:
  * Inside, it is the PC on the radio's USB network. It speaks Ethernet, DHCP, IP and TCP itself (tcppeer.py),
    takes the address the radio leases (ipGatewayHostIP(), so the firmware's own rule gives it control) and
    holds the one internal /ws. It asks for the screen rows ("s1") at once, whether or not a browser watches.
  * Outside, it is a small HTTP and WebSocket server on 127.0.0.1 (net_guard.py), one port per radio.
    Every request needs the exact Host "127.0.0.1:<port>". A /ws needs the exact Origin "http://127.0.0.1:<port>".
    A one-time token starts a session; protected requests need its cookie selector and exact-origin proof.
  * Browsers get copies: the page fetched once at world start, the latest status JSON, the screen rows. No
    browser request ever reaches the firmware, so 0 or 10 browsers give the same firmware run. The firmware's
    "Access-Control-Allow-Origin: *" (http.c:267) is never passed on: the gateway writes its own headers.
  * A browser's d, u and e frames (keys, encoder; webctl.c:309-355) go to the radio only for the session whose
    principal holds the radio's lease (lease.py). Every refusal is audited. A held key is tracked (holds.py) and
    released with a "u<bit>" frame when the lease goes. a and s are answered here: audio stays off, and the
    screen is always on inside.

Words. A principal is "browser:s<n>", one per token. The hello is the first /ws frame, which says whether this
client may control; each session gets its own copy with "control" set from the lease and "audio" false.

Example. A world client asks "webgw.token" for radio 1 and opens the URL it gets,
http://127.0.0.1:41234/#rf-token=... The static page posts the fragment token to /session, stores the returned
origin proof in sessionStorage, and opens /ws with both cookie and proof. Cookies alone cannot authorize any live
data; another loopback port may receive them. The proof binds the session, origin, radio and per-launch key.
The page loads, opens /ws
and shows "View only". The world client grants the lease to browser:s1 (lease.grant). The page gets a new hello
with "control":true. It sends "d3"; the gateway checks the lease and passes "d3" to the radio over its own /ws.

The world setting. The gateway is off unless a world turns it on before its first radio starts:
webgw.get(world, enabled=True). Once a radio starts the setting is fixed, and it is recorded as a conductor
event. It needs "--host-port on": the two start commands go on the virtual UART.

At each radio start (the vhost flash is blank at every boot), as recorded conductor inputs on the UART:
  1.5 s   debug link RADIO OP_USB_NET_SETTINGS flags 4 (fixed link), IP 0: the radio is 192.168.10.1
  1.6 s   debug link RADIO OP_USB_NETWORK 1: the firmware switches to the network personality
Then, when the trace shows "BOARD net.select 1", DHCP; then GET / (kept); then /ws and "s1".

The debug link in network mode. Once the radio is a network adapter, the firmware sends every serial
protocol reply as UDP from port 3334 to whoever last sent to that port (core/hostlink/hostlink.c hostlink_write,
core/dmr/ipGateway.c ipGatewaySerialOut), as a real radio does (docs/spec/usb-network.md; tools/aprs_gate.py
"udp:<ip>:3334"). A request on the UART is still read, but its reply is lost. So while the network is selected and
DHCP is bound, the debug link multiplexer (dlmux.py) asks this part for its request as one UDP datagram to
radio_ip:3334 from host port SERIAL_HOST_PORT (serial_frame), and gets the replies back (dlmux.serial_out).
Example: the cache refresh at 2000000 us goes in as an ETH_IN frame, and the radio's GET_VIEW reply comes back in
the next ETH_OUT frames as UDP 3334 -> 3334.

Open items (other lanes): receive audio is not relayed. The conductor command line cannot turn the gateway on yet.
"""

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import secrets
import selectors
import socket
import struct
import time

from ..channel import Kind
from ..net_guard import bind as guarded_bind
from ..world import WorldError
from . import audit, dlmux, holds, lease, tcppeer


def browser_proof(key, origin, sid, radio):
    message = f"rf-browser-v1\0{origin}\0{sid}\0{radio}".encode("ascii")
    return base64.urlsafe_b64encode(hmac.digest(key, message, "sha256")).rstrip(b"=").decode("ascii")


RADIO = 0x0A                        # debug link RADIO command (dlmux.COMMANDS)
OP_USB_NETWORK, OP_USB_NET_SETTINGS = 25, 27     # core/debuglink/debuglink.c:53,55
FIXED_LINK = 4                      # OP_USB_NET_SETTINGS flags bit 2 (debuglink.c:569)
SETTINGS_US, NETWORK_US = 1_500_000, 1_600_000   # after the splash and host-task startup
SOURCE = 0x200                      # world input source of the gateway (dlmux sessions use 0x100 + n)
HTTP_PORT = 80                      # the only radio port the gateway ever opens: never 4532 (rigctld) or 8001 (KISS)
SLICE_US = 1000                     # while the link is busy, grants end every 1 ms so replies go out at once
ACTIVE_US = 20_000                  # how long after a frame from the radio the link counts as busy
IDLE_US = 5000                      # otherwise grants end at least this often: the host link's poll (hostlink.c:57)
SERIAL_PORT = 3334                  # IPGW_SERIAL_PORT (core/dmr/ipGateway.h:84): the serial protocols over UDP
SERIAL_HOST_PORT = 3334             # the host's end: fixed, so the radio's reply port never depends on a seed
RETRY_US = 1_000_000                # a failed page fetch or a closed /ws is tried again after this
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_TEXT, WS_BINARY, WS_CLOSE, WS_PING, WS_PONG = 1, 2, 8, 9, 10
REQUEST_MAX = 8192                  # one browser request head (and the radio's /ws reply head)
PAGE_MAX = 64 * 1024                # the radio's page (6903 bytes today); more than this is refused
ASSET_PATHS = ("/keys.json", "/radio.svg", "/mic.svg")   # the page's own files (core/net/webctl_assets, gen_webctl_assets.py)
INTERNAL_MAX = 64 * 1024            # unparsed bytes from the radio's /ws
PENDING_MAX = 512                   # unsent command bytes per radio before a browser command is refused
FORWARDED_MAX = 256                 # forwarded commands kept for status and tests
BROWSER_OUT_MAX = 1 << 20           # a browser this far behind is dropped
BROWSERS_MAX = 32                   # open connections per radio port
TOKENS_MAX = 16                     # tokens minted and not yet used
REQUEST_WALL_S = 10.0               # a browser connection with no whole request after this is closed
COMMAND = re.compile(r"([dueas])(-?[0-9]{1,4})")
CONTROL = "due"                     # the frames that need the lease
HOLD_KEY, HOLD_PTT = "webctl.key", "webctl.ptt"


def server_frame(opcode, payload):
    """A WebSocket frame from a server: never masked (RFC 6455 5.1)."""
    size = len(payload)
    if size < 126:
        head = bytes((0x80 | opcode, size))
    elif size < 65536:
        head = bytes((0x80 | opcode, 126)) + struct.pack("!H", size)
    else:
        head = bytes((0x80 | opcode, 127)) + struct.pack("!Q", size)
    return head + payload


def client_frame(opcode, payload, mask):
    """A WebSocket frame from a client: always masked (core/net/ws.c:142 refuses it otherwise)."""
    size = len(payload)
    if size < 126:
        head = bytes((0x80 | opcode, 0x80 | size))
    else:
        head = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", size)
    return head + mask + bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))


def parse_frames(buffer, masked, limit):
    """Take whole frames off the front of buffer: [(opcode, payload)], or None for a protocol error."""
    frames = []
    while len(buffer) >= 2:
        opcode, size = buffer[0] & 0x0F, buffer[1] & 0x7F
        if buffer[0] & 0x70 or bool(buffer[1] & 0x80) != masked or not buffer[0] & 0x80:
            return None             # extensions, a wrong mask bit, or fragments: none of them are used here
        at = 2
        if size == 126:
            if len(buffer) < 4:
                break
            size, at = struct.unpack_from("!H", buffer, 2)[0], 4
        elif size == 127:
            if len(buffer) < 10:
                break
            size, at = struct.unpack_from("!Q", buffer, 2)[0], 10
        if size > limit or opcode >= 8 and (size > 125 or opcode > WS_PONG or opcode == WS_CLOSE and size == 1):
            return None             # control frames are at most 125 bytes (RFC 6455 5.5); a close has 0 or 2+
        if opcode not in (WS_TEXT, WS_BINARY, WS_CLOSE, WS_PING, WS_PONG):
            return None
        mask = b""
        if masked:
            if len(buffer) < at + 4:
                break
            mask, at = bytes(buffer[at:at + 4]), at + 4
        if len(buffer) < at + size:
            break
        payload = bytes(buffer[at:at + size])
        if masked:
            payload = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
        del buffer[:at + size]
        frames.append((opcode, payload))
    return frames


class Screen:
    """The panel as the last rows the radio sent, so a new session gets the whole screen in one frame.

    A screen frame (webctl.c:178-184) is 'S', the palette size n, n RGB565 colors, then rows. A row is its y
    (u16, bit 15: raw) and either raw bytes or runs of one color. Each row stands alone, so keeping the latest
    bytes of each row is enough.
    """

    def __init__(self, width, height, bpp):
        self.width, self.height, self.bpp = width, height, bpp
        self.palette = b"\0"
        self.rows = {}

    def apply(self, frame):
        if len(frame) < 2 or frame[0] != ord("S"):
            return False
        at = 2 + 2 * frame[1]
        if len(frame) < at:
            return False
        rows = {}
        while at + 2 <= len(frame):
            start = at
            y = frame[at] | frame[at + 1] << 8
            at += 2
            if y & 0x8000:
                at += (self.width * self.bpp + 7) // 8
            else:
                x = 0
                while x < self.width and at < len(frame):
                    count = frame[at] >> 4
                    if count < 15:
                        x, at = x + count + 1, at + 1
                    elif at + 1 < len(frame):
                        x, at = x + 16 + frame[at + 1], at + 2
                    else:
                        return False
                if x < self.width:
                    return False
            if at > len(frame) or (y & 0x7FFF) >= self.height:
                return False
            rows[y & 0x7FFF] = frame[start:at]
        self.palette = frame[1:2 + 2 * frame[1]]
        self.rows.update(rows)
        return True

    def snapshot(self, limit=16384):
        """Every known row, in frames of at most about limit bytes."""
        frames, body = [], b""
        for y in sorted(self.rows):
            if body and len(body) + len(self.rows[y]) > limit:
                frames.append(b"S" + self.palette + body)
                body = b""
            body += self.rows[y]
        if body:
            frames.append(b"S" + self.palette + body)
        return frames


@dataclass
class Mark:
    """A release ("u<bit>") sent on the internal /ws, waiting for its proof."""
    link: object
    conn: object
    end: int
    acked: bool = False
    scans: int = 0


class Link:
    """One radio boot, seen from inside: the IP peer, the page, the hello, the status and the screen."""

    def __init__(self, gateway, radio, boot):
        self.gateway, self.radio, self.boot = gateway, radio, boot
        self.peer = tcppeer.Peer(b"webgw/%d/%d" % (radio, boot))
        self.peer.on_bound = self._bound
        self.peer.on_lost = lambda peer, reason: self._set("dhcp")
        self.peer.udp[SERIAL_HOST_PORT] = self._serial
        self.network = False        # the trace said "BOARD net.select 1" (and no 0 since)
        self.state = "switching"
        self.page = None
        self.page_head = None
        self.assets = {}            # path -> (type, coding, body) as the radio served it, or None when it answered 404
        self.hello = None
        self.status = None
        self.screen = None
        self.http = None
        self.ws = None
        self.ws_upgraded = False
        self.inbox = bytearray()
        self.retry_us = None
        self.active_until = 0
        self.now = 0
        self.masks = 0
        self.attempts = 0

    def _set(self, state):
        self.state = state

    # --- time ---

    def limit(self, now):
        # Without this, one grant could run to the world's end and the radio's frames would wait for it.
        times = [self.peer.next_due(), self.retry_us, now + IDLE_US]
        if self.peer.busy() or now < self.active_until:
            times.append(now + SLICE_US)
        return min((value for value in times if value is not None and value > now), default=None)

    def tick(self, now):
        self.now = now
        if self.retry_us is not None and now >= self.retry_us:
            self.retry_us = None
            if self.page is None:
                self._fetch(now)
            elif self._missing():
                self._fetch_asset(now)
            elif self.ws is None:
                self._open_ws(now)
        return self.peer.output(now)

    # --- the serial protocols over UDP ---

    def _serial(self, src, sport, data, now):
        mux = getattr(self.gateway.world, "dlmux", None)
        if sport == SERIAL_PORT and mux is not None:
            mux.serial_out(self.radio, data, now)

    def serial_frame(self, data):
        """data as one UDP datagram to the radio's serial port, or None while the UART still answers."""
        if not self.network:
            return None
        return self.peer.udp_frame(SERIAL_HOST_PORT, SERIAL_PORT, data)

    # --- page ---

    def _bound(self, peer, now):
        self.gateway.record("webgw_bound", radio=self.radio, boot=self.boot, host=tcppeer.dotted(peer.ip),
                            radio_ip=tcppeer.dotted(peer.radio_ip))
        if self.page is None:
            self._fetch(now)
        elif self._missing():
            self._fetch_asset(now)
        elif self.ws is None:
            self._open_ws(now)

    def _fetch(self, now):
        self._get("/", now, self._page)

    def _missing(self):
        return next((path for path in ASSET_PATHS if path not in self.assets), None)

    def _fetch_asset(self, now):
        path = self._missing()
        self._get(path, now, lambda raw, reason: self._asset(path, raw, reason))

    def _get(self, path, now, done):
        """One GET of the radio's own web control over the virtual network; done(raw reply, close reason)."""
        self.state = "page" if path == "/" else "assets"
        self.attempts += 1
        conn = self.http = self.peer.connect(HTTP_PORT, now)
        body = bytearray()
        request = (b"GET %s HTTP/1.1\r\nHost: %s\r\nAccept-Encoding: gzip\r\nConnection: close\r\n\r\n"
                   % (path.encode(), tcppeer.dotted(self.peer.radio_ip).encode()))
        conn.on_open = lambda: conn.send(request)
        def take(data, at):
            body.extend(data)
            if len(body) > PAGE_MAX:
                conn.abort("WEBGW_PAGE_TOO_BIG")

        conn.on_data = take
        conn.on_close = lambda reason: done(bytes(body), reason)

    @staticmethod
    def _split(raw):
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = dict((name.strip().lower(), value.strip()) for name, _, value in
                       (line.partition(":") for line in lines[1:]))
        return head, lines[0].split(" ")[1:2], headers, body

    def _asset(self, path, raw, reason):
        self.http = None
        _, status, headers, body = self._split(raw)
        whole = reason == "TCP_CLOSED" and headers.get("content-length") == str(len(body))
        if whole and status == ["200"]:
            # Kept as the radio sent it (gzip stays gzip), under the same rule as the page: type and coding only.
            self.assets[path] = (headers.get("content-type", "application/octet-stream"),
                                 headers.get("content-encoding"), body)
            self.gateway.record("webgw_asset", radio=self.radio, boot=self.boot, path=path, bytes=len(body))
        elif whole and status == ["404"]:
            self.assets[path] = None    # this radio has no such file (a radio without a drawing): a 404 stays a 404
            self.gateway.record("webgw_asset", radio=self.radio, boot=self.boot, path=path, bytes=0)
        else:
            self.state = "page_retry"
            self.retry_us = self.now + RETRY_US
            return
        if self._missing():
            self._fetch_asset(self.now)
        else:
            self._open_ws(self.now)

    def _page(self, raw, reason):
        self.http = None
        head, status, headers, body = self._split(raw)
        self.page_head = head       # kept only as evidence of what is not passed on (tests)
        if (reason == "TCP_CLOSED" and status == ["200"]
                and headers.get("content-length") == str(len(body))):
            # Only the type and coding are kept: the firmware's other headers (its CORS) never reach a browser.
            self.page = (headers.get("content-type", "text/html"), headers.get("content-encoding"), body)
            self.gateway.record("webgw_page", radio=self.radio, boot=self.boot, bytes=len(body))
            if self._missing():
                self._fetch_asset(self.now)
            else:
                self._open_ws(self.now)
        else:
            self.state = "page_retry"
            self.retry_us = self.now + RETRY_US

    # --- the internal /ws ---

    def _mask(self):
        self.masks += 1
        return hashlib.sha256(self.peer.seed + b"/mask/%d" % self.masks).digest()[:4]

    def _open_ws(self, now):
        if self.peer.ip is None:
            return
        self.state = "ws"
        self.attempts += 1
        key = base64.b64encode(hashlib.sha256(self.peer.seed + b"/key/%d" % self.attempts).digest()[:16])
        want = base64.b64encode(hashlib.sha1(key + WS_GUID).digest())
        conn = self.ws = self.peer.connect(HTTP_PORT, now)
        self.ws_upgraded = False
        self.inbox = bytearray()
        request = (b"GET /ws HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   b"Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                   % (tcppeer.dotted(self.peer.radio_ip).encode(), key))
        conn.on_open = lambda: conn.send(request)
        conn.on_data = lambda data, at: self._ws_data(conn, data, want)
        conn.on_close = lambda reason: self._ws_closed(conn, reason)

    def _ws_data(self, conn, data, want):
        self.inbox += data
        if len(self.inbox) > INTERNAL_MAX:
            conn.abort("WEBGW_RADIO_TOO_MUCH")
            return
        if not self.ws_upgraded:
            if b"\r\n\r\n" not in self.inbox:
                if len(self.inbox) > REQUEST_MAX:
                    conn.abort("WEBGW_BAD_UPGRADE")
                return
            head, _, rest = bytes(self.inbox).partition(b"\r\n\r\n")
            if not head.startswith(b"HTTP/1.1 101 ") or b"Sec-WebSocket-Accept: " + want not in head:
                conn.abort("WEBGW_BAD_UPGRADE")
                return
            self.ws_upgraded = True
            self.inbox = bytearray(rest)
        frames = parse_frames(self.inbox, False, INTERNAL_MAX)
        if frames is None:
            conn.abort("WEBGW_BAD_FRAME")
            return
        for opcode, payload in frames:
            self._ws_frame(conn, opcode, payload)

    def _ws_frame(self, conn, opcode, payload):
        if opcode == WS_PING:
            conn.send(client_frame(WS_PONG, payload, self._mask()))     # keeps webctl's 15 s idle timer fed
        elif opcode == WS_CLOSE:
            conn.send(client_frame(WS_CLOSE, payload[:2], self._mask()))
            conn.close()
        elif opcode == WS_TEXT and self.hello is None:
            try:
                hello = json.loads(payload)
            except ValueError:
                hello = None
            if not isinstance(hello, dict) or hello.get("hello") != 1:
                conn.abort("WEBGW_BAD_HELLO")
                return
            self.hello = hello
            screen = hello.get("screen")
            # The radio is not trusted: only a panel the virtual board can have (240x320 at most, webctl.c:425).
            if (isinstance(screen, list) and len(screen) == 7 and all(type(value) is int for value in screen)
                    and 0 < screen[0] <= 320 and 0 < screen[1] <= 320 and screen[2] in (1, 4, 16)):
                self.screen = Screen(screen[0], screen[1], screen[2])
                conn.send(client_frame(WS_TEXT, b"s1", self._mask()))
            self.state = "up"
            self.gateway.record("webgw_up", radio=self.radio, boot=self.boot, control=hello.get("control"))
            self.gateway.link_up(self)
        elif opcode == WS_TEXT:
            self.status = payload
            self.gateway.fan_out(self.radio, server_frame(WS_TEXT, payload))
        elif opcode == WS_BINARY and payload[:1] == b"S" and self.screen is not None:
            if self.screen.apply(payload):
                self.gateway.fan_out(self.radio, server_frame(WS_BINARY, payload), screen=True)
        # Audio ('A') and anything else: not relayed.

    def _ws_closed(self, conn, reason):
        if conn is not self.ws:
            return
        self.ws = None
        self.hello = None
        self.state = "ws_retry"
        self.retry_us = self.now + RETRY_US
        self.gateway.record("webgw_ws_closed", radio=self.radio, boot=self.boot, reason=reason)

    def command(self, text):
        """One command text frame on the internal /ws. False when the link is not up."""
        if self.ws is None or not self.ws.established or not self.ws_upgraded or self.hello is None:
            return False
        return self.ws.send(client_frame(WS_TEXT, text.encode(), self._mask()))


@dataclass
class Session:
    radio: int
    principal: str
    cookie: str


@dataclass
class Browser:
    sock: object
    opened: float
    inbox: bytearray = field(default_factory=bytearray)
    out: bytearray = field(default_factory=bytearray)
    session: Session | None = None
    ws: bool = False
    screen: bool = False
    closing: bool = False


class Server:
    """One radio's browser port on 127.0.0.1."""

    def __init__(self, radio):
        self.radio = radio
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            guarded_bind(self.listener, ("127.0.0.1", 0))
            self.listener.listen(16)
            self.listener.setblocking(False)
        except BaseException:
            self.listener.close()
            raise
        self.port = self.listener.getsockname()[1]
        self.host = f"127.0.0.1:{self.port}"
        self.origin = f"http://{self.host}"
        # Cookies cross ports; this name prevents collisions, while the separate proof carries authority.
        self.cookie_name = f"rfvr{self.port}"
        self.browser_key = secrets.token_bytes(32)
        self.browsers = {}
        self.holder = None


class Gateway:
    def __init__(self, world):
        self.world = world
        self.audit = audit.get(world)
        self.leases = lease.get(world)
        self.holds = holds.get(world)
        self.enabled = False
        self.fixed = False
        self.links = {}
        self.servers = {}
        self.tokens = {}
        self.sessions = {}
        self.marks = {}
        self.forwarded = []         # (radio, principal, text, world input sequence or None): for tests and status
        self.selector = selectors.DefaultSelector()
        self._ids = 0
        self.holds.register(HOLD_KEY, self._release, self._confirms)
        self.holds.register(HOLD_PTT, self._release, self._confirms)
        self.leases.on_revoke(self._revoked)
        self.grants = {}            # radio -> (principal, generation, grant secret), from lease.on_grant
        self.leases.on_grant("browser", self._granted)
        world.on_outbox(Kind.ETH_OUT, self._eth_out)
        world.on_outbox(Kind.TRACE, self._trace)
        world.add_grant_limit(self._limit)
        world.on_grant_done(self._grant_done)
        world.on_end(self.close)

    # --- the world setting ---

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("WEBGW_BAD_SETTING")
        if self.fixed and enabled != self.enabled:
            raise ValueError("WEBGW_SETTING_FIXED")
        if enabled:
            args = tuple(self.world.config.radio_args)
            if not any(args[index:index + 2] == ("--host-port", "on") for index in range(len(args) - 1)):
                raise ValueError("WEBGW_NEEDS_HOST_PORT")
            if self.world.config.replay:
                raise ValueError("WEBGW_REPLAY_WORLD")
        self.enabled = enabled

    def record(self, name, **fields):
        recorder = getattr(self.world, "recorder", None)
        if recorder is not None:
            recorder.conductor_event(name, **fields)

    def radio_started(self, event):
        if not self.fixed:
            self.fixed = True
            self.record("webgw_setting", enabled=self.enabled)
        if not self.enabled:
            return
        radio, boot = event["radio"], event["boot"]
        if Kind.ETH_IN not in getattr(self.world.radios[radio].channel, "supported_inputs", (Kind.ETH_IN,)):
            self.record("webgw_unavailable", radio=radio, reason="WORLD_UNSUPPORTED_INPUT:ETH_IN")
            return
        origin = self.world.radios[radio].origin
        self.links[radio] = Link(self, radio, boot)
        if radio not in self.servers:
            server = self.servers[radio] = Server(radio)
            self.selector.register(server.listener, selectors.EVENT_READ, server)
            self.record("webgw_listen", radio=radio, port=server.port)
        settings = dlmux.frame(RADIO, 0xF1, bytes((OP_USB_NET_SETTINGS,)) + struct.pack("<BI", FIXED_LINK, 0))
        network = dlmux.frame(RADIO, 0xF2, bytes((OP_USB_NETWORK, 1)))
        self.world.submit(radio, Kind.BYTES_IN, settings, SOURCE, time_us=origin + SETTINGS_US, internal=True)
        self.world.submit(radio, Kind.BYTES_IN, network, SOURCE, time_us=origin + NETWORK_US, internal=True)

    def _link(self, radio):
        link = self.links.get(radio)
        if link is None or radio not in self.world.radios or link.boot != self.world.boots[radio]:
            return None
        return link

    # --- the radio side ---

    def _eth_out(self, radio, message):
        link = self._link(radio)
        if link is None:
            return
        link.now = message.time_us
        link.peer.receive(message.payload, message.time_us)
        link.active_until = message.time_us + ACTIVE_US
        for mark in self.marks.values():
            if mark.link is link and not mark.acked and (
                    mark.conn.state == "CLOSED" or tcppeer.seq_le(mark.end, mark.conn.snd_una)):
                # The radio's TCP passes bytes to web control before it ACKs them (core/net/tcp.c:576-578).
                # A closed /ws also lets go: webctl releases every key on close (webctl.c:92, :406).
                mark.acked = True

    def _trace(self, radio, message):
        link = self._link(radio)
        if link is None:
            return
        line = message.payload
        if b" BOARD net.select " in line:
            link.network = b" BOARD net.select 1" in line
        if link.state == "switching" and b" BOARD net.select 1" in line:
            link.state = "dhcp"
            link.peer.start_dhcp(message.time_us)
        elif dlmux.KEY_SCAN in line:
            for mark in self.marks.values():
                if mark.link is link and mark.acked:
                    mark.scans += 1

    def serial_frame(self, radio, data):
        """dlmux.py: the ETH_IN frame that carries data to the radio's serial port, or None (use the UART)."""
        link = self._link(radio)
        return None if link is None else link.serial_frame(data)

    def _limit(self, now):
        values = [link.limit(now) for radio, link in self.links.items() if self._link(radio) is link]
        return min((value for value in values if value is not None), default=None)

    def _grant_done(self, target, outboxes):
        start = self.world.next_start
        for radio in sorted(self.links):
            link = self._link(radio)
            if link is None:
                continue
            for frame in link.tick(target):
                self.world.submit(radio, Kind.ETH_IN, frame, SOURCE, time_us=max(target, start), internal=True)
        for radio in self.servers:
            self._sync_control(radio)
        return ()

    # --- holds ---

    def _release(self, radio, hold):
        """holds.py: the "u<bit>" frame that lets go, as one ETH_IN input (earlier frames are submitted first)."""
        link = self._link(radio)
        bit = int(hold.name.split(":")[1])
        if link is None or not link.command(f"u{bit}"):
            raise WorldError("WEBGW_LINK_DOWN")
        frames = link.peer.output(self.world.now)
        if not frames or link.ws.unsent:
            raise WorldError("WEBGW_RELEASE_BLOCKED")    # the radio's window is shut: only a restart clears it
        for frame in frames[:-1]:
            self.world.submit(radio, Kind.ETH_IN, frame, SOURCE, internal=True)
        self.marks[id(hold)] = Mark(link, link.ws, link.ws.snd_nxt)
        self._forwarded(radio, hold.principal, f"u{bit}")
        return Kind.ETH_IN, frames[-1]

    def _confirms(self, radio, hold, line):
        mark = self.marks.get(id(hold))
        if mark is None or not mark.acked or mark.scans < 2:
            return False
        del self.marks[id(hold)]
        return True

    def _granted(self, radio, principal, generation, secret):
        """A lease to a browser principal. The browser never sees the world reply, so this part keeps the
        grant secret and presents it to lease.check for that page's commands."""
        self.grants[radio] = (principal, generation, secret)

    def _revoked(self, radio, principal, generation, reason):
        """The controlling page gets a close and comes back as an observer."""
        self.grants.pop(radio, None)
        server = self.servers.get(radio)
        if server is None:
            return
        for browser in list(server.browsers.values()):
            if browser.ws and browser.session.principal == principal and not browser.closing:
                self.audit.emit("webgw_demote", principal, radio, "closed", reason, lease_generation=generation)
                self._send(server, browser, server_frame(WS_CLOSE, struct.pack("!H", 1000)))
                browser.closing = True
        server.holder = None

    # --- browser control ---

    def _sync_control(self, radio):
        """A lease granted to a session's principal: that page gets a new hello with control true."""
        server = self.servers[radio]
        held = self.leases.held.get(radio)
        holder = held.principal if held is not None else None
        if holder == server.holder:
            return
        server.holder = holder
        for browser in list(server.browsers.values()):
            if browser.ws and not browser.closing and browser.session.principal == holder:
                self._hello(server, browser)

    def _hello(self, server, browser):
        link = self._link(server.radio)
        if link is None or link.hello is None:
            return
        held = self.leases.held.get(server.radio)
        hello = dict(link.hello, control=held is not None and held.principal == browser.session.principal,
                     audio=False)
        self._send(server, browser, server_frame(WS_TEXT, json.dumps(hello, separators=(",", ":")).encode()))

    def link_up(self, link):
        server = self.servers.get(link.radio)
        if server is None:
            return
        for browser in list(server.browsers.values()):
            if browser.ws and not browser.closing:
                self._hello(server, browser)
                if browser.screen:
                    self._snapshot(server, browser)

    def _snapshot(self, server, browser):
        link = self._link(server.radio)
        if link is not None and link.screen is not None:
            for frame in link.screen.snapshot():
                self._send(server, browser, server_frame(WS_BINARY, frame))

    def fan_out(self, radio, frame, screen=False):
        server = self.servers.get(radio)
        if server is None:
            return
        for browser in list(server.browsers.values()):
            if browser.ws and not browser.closing and (browser.screen or not screen):
                self._send(server, browser, frame)

    def _forwarded(self, radio, principal, text):
        self.forwarded.append((radio, principal, text, None))
        del self.forwarded[:-FORWARDED_MAX]

    def _command(self, server, browser, text):
        found = COMMAND.fullmatch(text)
        radio, principal = server.radio, browser.session.principal
        if found is None:
            self.audit.emit("control", principal, radio, "refused", "WEBGW_BAD_COMMAND", layer="browser")
            return
        name, value = found.group(1), int(found.group(2))
        if name == "s":
            browser.screen = value != 0
            if browser.screen:
                self._snapshot(server, browser)
            return
        if name == "a":
            if value:           # receive audio is not relayed: the hello says "audio":false
                self.audit.emit("control", principal, radio, "refused", "WEBGW_NO_AUDIO", layer="browser",
                                command="web.a")
            return
        granted = self.grants.get(radio)
        generation, secret = granted[1:] if granted is not None and granted[0] == principal else (None, None)
        try:
            # Refused (and audited by lease.py) unless this session's principal holds the current lease
            # and this part got its grant secret from lease.on_grant.
            self.leases.check(radio, principal, generation=generation, secret=secret, command=f"web.{name}")
        except WorldError:
            return
        link = self._link(radio)
        if name in "du" and not 0 <= value < 32 or name == "e" and not (value and -127 <= value <= 127):
            self.audit.emit("control", principal, radio, "refused", "WEBGW_BAD_COMMAND", layer="browser",
                            command=f"web.{name}")
            return
        if link is not None and link.ws is not None and len(link.ws.unsent) > PENDING_MAX:
            # The radio's TCP is behind: refuse rather than queue without bound (releases do not pass here).
            self.audit.emit("control", principal, radio, "refused", "WEBGW_BUSY", layer="browser",
                            command=f"web.{name}")
            return
        if link is None or not link.command(text):
            self.audit.emit("control", principal, radio, "refused", "WEBGW_LINK_DOWN", layer="browser",
                            command=f"web.{name}")
            return
        self._forwarded(radio, principal, text)
        if name == "d":
            kind = HOLD_PTT if link.hello.get("ptt") == value else HOLD_KEY
            self.holds.down(radio, kind, f"key:{value}", principal)
        # A page's own "u" leaves the hold tracked: nothing proves yet that the radio let go. A revoke then sends
        # "u" again (the firmware ignores a second one, webctl.c:131-134) and waits for its confirmation.

    # --- tokens and sessions ---

    def token(self, request):
        """World API webgw.token {radio}: a one-time URL for a new browser session."""
        radio = request.get("radio")
        if not self.enabled:
            raise WorldError("WEBGW_OFF")
        if type(radio) is not int or radio not in self.servers:
            raise WorldError("WORLD_UNKNOWN_RADIO")
        if len(self.tokens) >= TOKENS_MAX:
            raise WorldError("WEBGW_TOO_MANY_TOKENS")
        self._ids += 1
        principal = f"browser:s{self._ids}"
        value = secrets.token_urlsafe(32)
        # Registered before any audit line, so the value can never reach audit.jsonl or events.jsonl.
        self.audit.register_secret(value)
        self.tokens[value] = (radio, principal)
        self.audit.emit("webgw_token", principal, radio, "accepted", "OK", layer="browser")
        return {"radio": radio, "principal": principal, "url": f"{self.servers[radio].origin}/#rf-token={value}"}

    def _take_token(self, server, value):
        for token, (radio, principal) in list(self.tokens.items()):
            if hmac.compare_digest(token.encode(), value.encode()) and radio == server.radio:
                del self.tokens[token]
                return principal
        return None

    def status(self):
        return {str(radio): {"url": f"{server.origin}/", "state": self._link(radio).state if self._link(radio)
                             else "off", "browsers": sum(browser.ws for browser in server.browsers.values()),
                             "sessions": sum(session.radio == radio for session in self.sessions.values())}
                for radio, server in sorted(self.servers.items())}

    # --- browser sockets (the world thread only, chained onto world.api.poll) ---

    def poll(self):
        if not self.servers:
            return
        for key, mask in self.selector.select(0):
            server = key.data
            if key.fileobj is server.listener:
                self._accept(server)
                continue
            browser = server.browsers.get(key.fileobj)
            if browser is None:
                continue
            try:
                if mask & selectors.EVENT_READ:
                    data = browser.sock.recv(65536)
                    if not data:
                        self._drop(server, browser)
                        continue
                    browser.inbox += data
                    self._read(server, browser)
                if browser.sock in server.browsers and browser.out:
                    sent = browser.sock.send(browser.out)
                    del browser.out[:sent]
            except BlockingIOError:
                pass
            except OSError:
                self._drop(server, browser)
                continue
            self._after(server, browser)
        now = time.monotonic()
        for server in self.servers.values():
            for browser in list(server.browsers.values()):
                if not browser.ws and not browser.closing and now - browser.opened > REQUEST_WALL_S:
                    self._drop(server, browser)

    def _accept(self, server):
        try:
            sock, _ = server.listener.accept()
        except (BlockingIOError, OSError):
            return
        if len(server.browsers) >= BROWSERS_MAX:
            sock.close()
            return
        sock.setblocking(False)
        server.browsers[sock] = Browser(sock, time.monotonic())
        self.selector.register(sock, selectors.EVENT_READ, server)

    def _after(self, server, browser):
        if browser.sock not in server.browsers:
            return
        if browser.closing and not browser.out:
            self._drop(server, browser)
            return
        self.selector.modify(browser.sock, selectors.EVENT_READ | (selectors.EVENT_WRITE if browser.out else 0),
                             server)

    def _send(self, server, browser, data):
        if browser.sock not in server.browsers:
            return
        browser.out += data
        if len(browser.out) > BROWSER_OUT_MAX:
            self._drop(server, browser)
            return
        self._after(server, browser)

    def _drop(self, server, browser):
        if server.browsers.pop(browser.sock, None) is None:
            return
        try:
            self.selector.unregister(browser.sock)
        except (KeyError, ValueError):
            pass
        browser.sock.close()
        if browser.ws:
            principal = browser.session.principal
            self.audit.emit("webgw_ws_close", principal, server.radio, "closed", "OK", layer="browser")
            held = self.leases.held.get(server.radio)
            if held is not None and held.principal == principal and self.world.state != "Ended":
                try:
                    self.leases.revoke(server.radio, "LEASE_DISCONNECT")
                except WorldError:
                    pass

    def _read(self, server, browser):
        if browser.closing:
            browser.inbox.clear()       # one request per connection: the reply is going out, then the close
            return
        if browser.ws:
            frames = parse_frames(browser.inbox, True, 125)
            if frames is None:
                self.audit.emit("webgw_ws_close", browser.session.principal, server.radio, "refused",
                                "WEBGW_BAD_FRAME", layer="browser")
                self._close_ws(server, browser, 1002)
                return
            for opcode, payload in frames:
                if browser.closing:
                    return
                if opcode == WS_TEXT:
                    try:
                        text = payload.decode("ascii")
                    except UnicodeDecodeError:
                        text = ""
                    self._command(server, browser, text)
                elif opcode == WS_PING:
                    self._send(server, browser, server_frame(WS_PONG, payload))
                elif opcode == WS_CLOSE:
                    self._close_ws(server, browser, 1000)
                elif opcode != WS_PONG:
                    self.audit.emit("webgw_ws_close", browser.session.principal, server.radio, "refused",
                                    "WEBGW_BAD_FRAME", layer="browser")
                    self._close_ws(server, browser, 1003)
            return
        if b"\r\n\r\n" not in browser.inbox:
            if len(browser.inbox) > REQUEST_MAX:
                self._refuse(server, browser, 431, "Request Header Fields Too Large", "WEBGW_TOO_LARGE", "other")
            return
        head, _, rest = bytes(browser.inbox).partition(b"\r\n\r\n")
        if len(head) > REQUEST_MAX:
            self._refuse(server, browser, 431, "Request Header Fields Too Large", "WEBGW_TOO_LARGE", "other")
            return
        if self._request(server, browser, head, rest) is not False:
            browser.inbox.clear()

    def _close_ws(self, server, browser, code):
        self._send(server, browser, server_frame(WS_CLOSE, struct.pack("!H", code)))
        browser.closing = True

    def _reply(self, server, browser, code, reason, headers=(), body=b""):
        lines = [f"HTTP/1.1 {code} {reason}", *headers, f"Content-Length: {len(body)}", "Cache-Control: no-store",
                 "X-Content-Type-Options: nosniff", "Referrer-Policy: no-referrer", "Connection: close"]
        if not body and code != 204:
            lines.insert(1, "Content-Type: text/plain")
        self._send(server, browser, ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
        browser.closing = True

    def _refuse(self, server, browser, code, reason, why, route, principal=None):
        self.audit.emit("webgw_request", principal, server.radio, "refused", why, layer="browser", route=route)
        self._reply(server, browser, code, reason)

    def _request(self, server, browser, head, body=b""):
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            self._refuse(server, browser, 400, "Bad Request", "WEBGW_BAD_REQUEST", "other")
            return
        method, target, _ = parts
        path, _, query = target.partition("?")
        route = {"/": "page", "/session": "login", "/ws": "ws", "/status": "status",
                                                 **dict.fromkeys(ASSET_PATHS, "asset")}.get(path, "other")
        headers = {}
        for line in lines[1:]:
            name, colon, value = line.partition(":")
            name = name.strip().lower()
            if name in headers and name in ("x-radiofoundry-session", "sec-websocket-protocol"):
                self._refuse(server, browser, 403, "Forbidden", "WEBGW_BAD_PROOF", route)
                return
            if not colon or name in headers and name in ("host", "origin", "cookie", "content-type", "content-length",
                                                       "transfer-encoding", "sec-fetch-site", "x-radiofoundry-session",
                                                       "sec-websocket-protocol", "sec-websocket-key",
                                                       "sec-websocket-version"):
                self._refuse(server, browser, 400, "Bad Request", "WEBGW_BAD_REQUEST", route)
                return
            headers[name] = value.strip()
        # Prevent DNS rebinding: only the exact numeric-loopback name of this port.
        if headers.get("host") != server.host:
            self._refuse(server, browser, 421, "Misdirected Request", "WEBGW_BAD_HOST", route)
            return
        origin = headers.get("origin")
        # Cross-site pages: a foreign Origin never passes; a /ws needs our own (no missing or "null" Origin).
        if origin is not None and origin != server.origin or route in ("ws", "login") and origin is None:
            self._refuse(server, browser, 403, "Forbidden", "WEBGW_BAD_ORIGIN", route)
            return
        if route == "login":
            if method != "POST":
                self._refuse(server, browser, 405, "Method Not Allowed", "WEBGW_BAD_METHOD", route)
                return
            length = headers.get("content-length", "")
            if (headers.get("content-type") != "application/json" or "transfer-encoding" in headers
                    or not length.isascii() or not length.isdigit() or not 0 < int(length) <= 1024
                    or headers.get("sec-fetch-site", "same-origin") != "same-origin"):
                self._refuse(server, browser, 400, "Bad Request", "WEBGW_BAD_REQUEST", route)
                return
            if len(body) < int(length):
                return False
            try:
                pairs = json.loads(body, object_pairs_hook=lambda items: items)
                valid = (len(body) == int(length) and isinstance(pairs, list) and len(pairs) == 1
                         and isinstance(pairs[0], tuple) and pairs[0][0] == "token"
                         and isinstance(pairs[0][1], str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", pairs[0][1]))
            except (ValueError, UnicodeError):
                valid = False
            if not valid:
                self._refuse(server, browser, 400, "Bad Request", "WEBGW_BAD_REQUEST", route)
                return
            self._login(server, browser, pairs[0][1])
            return
        if method != "GET":
            self._refuse(server, browser, 405, "Method Not Allowed", "WEBGW_BAD_METHOD", route)
            return
        if route != "page":
            values = [item.strip().partition("=")[2] for item in headers.get("cookie", "").split(";")
                      if item.strip().partition("=")[0] == server.cookie_name]
            session = self.sessions.get(values[0]) if len(values) == 1 else None
            if session is None or session.radio != server.radio:
                self._refuse(server, browser, 403, "Forbidden", "WEBGW_NO_SESSION", route)
                return
            proof = headers.get("x-radiofoundry-session", "")
            if route == "ws":
                protocols = [item.strip() for item in headers.get("sec-websocket-protocol", "").split(",")]
                proof = protocols[1][len("rf-vr-proof."):] if (len(protocols) == 2
                    and protocols[0] == "rf-vr-v1" and protocols[1].startswith("rf-vr-proof.")) else ""
            expected = browser_proof(server.browser_key, server.origin, values[0], server.radio)
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", proof) or not hmac.compare_digest(proof, expected):
                self._refuse(server, browser, 403, "Forbidden", "WEBGW_BAD_PROOF", route)
                return
        link = self._link(server.radio)
        if route == "page":
            if link is None or link.page is None:
                self._reply(server, browser, 503, "Service Unavailable", ("Retry-After: 1",))
                return
            kind, coding, body = link.page
            extra = (f"Content-Type: {kind}",) + ((f"Content-Encoding: {coding}",) if coding else ())
            self._reply(server, browser, 200, "OK", extra, body)
        elif route == "asset":
            if link is None or path not in link.assets:
                self._reply(server, browser, 503, "Service Unavailable", ("Retry-After: 1",))
            elif link.assets[path] is None:
                self._reply(server, browser, 404, "Not Found")
            else:
                kind, coding, body = link.assets[path]
                extra = (f"Content-Type: {kind}",) + ((f"Content-Encoding: {coding}",) if coding else ())
                self._reply(server, browser, 200, "OK", extra, body)
        elif route == "status":
            if link is None or link.status is None:
                self._reply(server, browser, 503, "Service Unavailable", ("Retry-After: 1",))
                return
            self._reply(server, browser, 200, "OK", ("Content-Type: application/json",), link.status)
        elif route == "ws":
            self._upgrade(server, browser, session, headers)
        else:
            self._reply(server, browser, 404, "Not Found")

    def _login(self, server, browser, value):
        principal = self._take_token(server, value)
        if principal is None:
            self._refuse(server, browser, 403, "Forbidden", "WEBGW_BAD_TOKEN", "page")
            return
        cookie = secrets.token_urlsafe(32)
        proof = browser_proof(server.browser_key, server.origin, cookie, server.radio)
        self.audit.register_secret(cookie)
        self.audit.register_secret(proof)
        self.sessions[cookie] = Session(server.radio, principal, cookie)
        self.audit.emit("webgw_session", principal, server.radio, "accepted", "OK", layer="browser")
        self._reply(server, browser, 200, "OK",
                    ("Content-Type: application/json",
                     f"Set-Cookie: {server.cookie_name}={cookie}; Path=/; HttpOnly; SameSite=Strict"),
                    json.dumps({"proof": proof}).encode())

    def _upgrade(self, server, browser, session, headers):
        key = headers.get("sec-websocket-key", "")
        try:
            good = len(base64.b64decode(key, validate=True)) == 16
        except ValueError:
            good = False
        connection = [item.strip().lower() for item in headers.get("connection", "").split(",")]
        if (not good or headers.get("upgrade", "").lower() != "websocket" or "upgrade" not in connection
                or headers.get("sec-websocket-version") != "13"):
            self._refuse(server, browser, 400, "Bad Request", "WEBGW_BAD_HANDSHAKE", "ws", session.principal)
            return
        accept = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        self._send(server, browser, ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade"
                                     f"\r\nSec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: rf-vr-v1\r\n\r\n").encode())
        browser.ws, browser.session = True, session
        self.audit.emit("webgw_ws_open", session.principal, server.radio, "accepted", "OK", layer="browser")
        self._hello(server, browser)
        link = self._link(server.radio)
        if link is not None and link.status is not None:
            self._send(server, browser, server_frame(WS_TEXT, link.status))

    def close(self):
        for server in self.servers.values():
            for browser in list(server.browsers.values()):
                browser.ws = False      # the world has ended: no lease work on the way out
                self._drop(server, browser)
            try:
                self.selector.unregister(server.listener)
            except (KeyError, ValueError):
                pass
            server.listener.close()
        self.servers.clear()
        self.tokens.clear()
        self.sessions.clear()
        self.selector.close()


def get(world, enabled=None):
    if getattr(world, "webgw", None) is None:
        gateway = world.webgw = Gateway(world)
        api = world.api
        api.register("webgw.token", gateway.token)
        api.register("webgw.status", lambda request: gateway.status())
        poll = api.poll

        def chained(timeout=0):
            gateway.poll()
            poll(timeout)

        api.poll = chained
        notify = world.notify

        def watching_notify(event):
            notify(event)
            if event.get("event") == "radio_started":
                gateway.radio_started(event)

        world.notify = watching_notify
    if enabled is not None:
        world.webgw.set_enabled(enabled)
    return world.webgw


def install(world, enabled=None):
    get(world, enabled)
