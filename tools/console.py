#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""A console for a running world: python3 tools/console.py --world NAME [--control].

It shows every radio's live screen, the air timeline (each burst, who heard it, collisions, fault windows), the
events, assertions, virtual time and speed, all on one page at http://127.0.0.1:<port>/. Open the printed URL once:
its fragment token leaves the address bar and is exchanged for a session cookie and an exact-origin proof.

How it reads the world. Only through the paths any observer has (no private path, no world folder files):
  * the world socket, as a world client that sends only status, observe.status and observe.events
    (parts/observe_api.py). The allowlist is ALLOWED below, and nothing else can be sent. On connect it also
    sends one subscribe line with no event kinds (worldapi.py): the world then pushes it no events at all, only
    replies, because the console pulls the same history from observe.events. A world that pushed every event
    (udp_delivery alone is about 845 KB/s at 1x with 4 radios) would fill its queue and cut it off;
  * the frame stream, frames.sock (parts/frames.py). The console never writes to it.
The console process holds copies. A browser talks only to this process, so 0 or 5 browsers change nothing in the
world, and a slow browser never slows a radio.

Who may open the page. The same rules as the web gateway (parts/webgw.py): the server binds 127.0.0.1 only
(net_guard.py), every request needs the exact Host "127.0.0.1:<port>" (DNS rebinding), a foreign Origin is refused,
a cross-site fetch (Sec-Fetch-Site) is refused. Only the static shell is public. POST /session exchanges the
fragment token for a cookie (HttpOnly, SameSite=Strict) and a proof kept in exact-origin sessionStorage.
Cookies cross ports; a cookie alone grants no live-data access. Every /api/ route also needs the proof in
X-RadioFoundry-Session, which a foreign page cannot send without a CORS preflight. No preflight is permitted.
The proof binds the session, origin, console identity and per-launch key; it is never recovered from a cookie.

World control. Watching, radio control and world control are three separate permissions (ADR 0004
rule 7). This console never gets radio control: no key, PTT or lease command is on its allowlist. It gets world
control only in two steps:
  1. The user who owns the world socket starts it with --control. Without the flag the console is an observer:
     its world client can send only ALLOWED, and a control request is refused in this process (CONSOLE_READ_ONLY).
  2. The terminal then prints a second one-time code. A logged-in page gets the control capability only by pasting
     that code (POST /api/unlock). One session can hold it; the code works once.
A control request (POST /api/control: pause, resume, step 30 ms, speed, or a loss window) must pass, in this order:
the exact Host, a session cookie, an Origin that is present and exact, Sec-Fetch-Site, the X-RadioFoundry-Session proof as the CSRF check, Content-Type
application/json, a body of at most 1 KiB with only the known fields, the capability, and a per-session rate limit.
It is then sent as an ordinary world API command (pause, run, step, set_ratio, fault) over this console's own
world client, with "actor": "console:s<n>". The world writes one audit line per attempt, kind world_control
(worldapi.py). A request refused here is reported with world_control.audit, so it is in the same audit stream and
shows on the page; refusals are rate-limited (REPORT_RATE, and a smaller ANONYMOUS_RATE for requests with no
session, which any web page can cause) and the rest are counted in the next line. A speed picked while paused is
kept by the page and sent with resume, so no change waits unaudited here. A loss window is one world fault per
link with until_us, so its end can never be lost. Session labels (s1, s2) are counters, never the cookie.
Example: the page's Pause button sends {"action": "pause"}; the world receives
{"command": "pause", "actor": "console:s1"} and audits it.
An observer console sends nothing to the world for a refusal: it keeps the refusal in its own list (shown on the
page and printed to stderr), so a read-only console stays read-only. The console exits when the world ends.
"""

import argparse
from collections import deque
from dataclasses import dataclass, field
import hmac
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.conductor.net_guard import bind as guarded_bind  # noqa: E402
from tools.conductor.parts.audit import Audit
from tools.conductor.parts.webgw import browser_proof
from tools.conductor.worldapi import private_directory  # noqa: E402

STATIC = Path(__file__).resolve().parent / "console"
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/console.js": ("console.js", "text/javascript"),
         "/console.css": ("console.css", "text/css")}
ALLOWED = ("status", "observe.status", "observe.events")     # read-only world API commands; nothing else is sent
# With --control only: world control, never radio control (no key, enc, lease, webgw, debuglink or hil command).
CONTROL_ALLOWED = ALLOWED + ("pause", "run", "step", "set_ratio", "fault", "world_control.audit")
CONTROL_ROUTES = ("/api/control", "/api/unlock")
ACTIONS = {"pause": set(), "resume": {"ratio"}, "step": set(), "speed": {"ratio"},
           "fault": {"receivers", "rate_pct", "length_ms"}}
RATIOS = (0.1, 0.25, 0.5, 1, 2, 5, 10, "max")
STEP_US = 30000
BODY_MAX = 1024
RATE = (5.0, 10)                # per session: 5 control requests a second, bursts of 10
REPORT_RATE = (5.0, 20)         # refusals reported to the world audit: 5 a second, bursts of 20; the rest are counted
ANONYMOUS_RATE = (0.2, 5)       # refusals with no session (any web page can cause them): one each 5 s, bursts of 5
HANDLERS_MAX = 32               # requests served at once; more are closed unanswered
READ_S = 5                      # a request must arrive whole within this many seconds
FAULT_LEAD_US = 300000          # a loss window starts this long after the current virtual time, times the ratio
VERSION = 1
HEADER = struct.Struct("<IIIQIIIHHBBBB16H")      # parts/frames.py, platform/host/vframe.h
LENGTH = struct.Struct("<I")
PANEL_MAX = 240 * 320 * 2
EVENTS_MAX = 20000          # history kept by the console process for new browsers
SESSIONS_MAX = 16
POLL_S = 0.1
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class ConsoleError(Exception):
    """An actionable, single-sentence error for the command line."""


class WorldClient:
    """A world client that sends only the commands in allowed: call() refuses any other before it reaches the
    socket. One lock serializes calls, because the reader thread and the control handlers share this socket."""

    def __init__(self, path, timeout=10, allowed=ALLOWED):
        self.allowed = allowed
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.buffer = b""
        self.next_id = 0
        # Ask for no pushed events. Its reply (id 0) is never waited for and is dropped like any pushed line, so an
        # older world that answers WORLD_UNKNOWN_COMMAND still works; it changes nothing in the world.
        self.sock.sendall(json.dumps({"version": VERSION, "id": 0, "command": "subscribe", "events": []},
                                     separators=(",", ":")).encode() + b"\n")

    def request(self, command, **fields):
        """The world's whole reply: {"ok": true, "result": ...} or {"ok": false, "error": "WORLD_..."}."""
        if command not in self.allowed:
            raise PermissionError(f"CONSOLE_READ_ONLY: {command}")
        with self.lock:
            self.next_id += 1
            self.sock.sendall(json.dumps({"version": VERSION, "id": self.next_id, "command": command, **fields},
                                         separators=(",", ":")).encode() + b"\n")
            while True:
                while b"\n" not in self.buffer:
                    data = self.sock.recv(65536)
                    if not data:
                        raise EOFError("the world closed its socket")
                    self.buffer += data
                line, _, self.buffer = self.buffer.partition(b"\n")
                reply = json.loads(line)
                # Pushed world events are dropped here: observe.events holds the same history, numbered.
                if reply.get("id") == self.next_id:
                    return reply

    def call(self, command, **fields):
        reply = self.request(command, **fields)
        if not reply.get("ok"):
            raise RuntimeError(f"{command}: {reply.get('error')}")
        return reply["result"]

    def close(self):
        self.sock.close()


class ReadOnlyWorld(WorldClient):
    """The observer's world client: only ALLOWED."""

    def __init__(self, path, timeout=10):
        super().__init__(path, timeout, ALLOWED)


@dataclass
class Model:
    """What the console process knows, shared by its reader threads and the HTTP handlers."""
    world: str
    status: dict = field(default_factory=dict)
    events: deque = field(default_factory=lambda: deque(maxlen=EVENTS_MAX))
    frames: dict = field(default_factory=dict)          # radio -> (info dict, record bytes without length prefix)
    refusals: deque = field(default_factory=lambda: deque(maxlen=50))   # control refusals made by this console
    connected: bool = False
    ended: bool = False
    error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add_events(self, events):
        with self.lock:
            self.events.extend(events)

    def add_refusal(self, entry):
        with self.lock:
            self.refusals.append(entry)

    def add_frame(self, record):
        (radio, boot, frame_id, time_us, crc, size, offset, width, height,
         bpp, backlight, has_palette, reserved, *palette) = HEADER.unpack_from(record)
        info = {"radio": radio, "boot": boot, "frame_id": frame_id, "time_us": time_us, "width": width,
                "height": height, "bpp": bpp, "backlight": backlight}
        with self.lock:
            self.frames[radio] = (info, bytes(record))

    def snapshot(self, after):
        with self.lock:
            events = [event for event in self.events if event["seq"] > after]
            return {"world": self.world, "connected": self.connected, "ended": self.ended, "error": self.error,
                    "status": self.status, "events": events[-4000:], "refusals": list(self.refusals)[-10:],
                    "frames": {str(radio): info for radio, (info, _) in sorted(self.frames.items())}}

    def frame(self, radio):
        with self.lock:
            item = self.frames.get(radio)
            return item[1] if item else None


def follow_world(client, model, stop, poll_s=POLL_S):
    """Pull status and new history until the world ends or its socket closes."""
    cursor = 0
    try:
        while not stop.is_set():
            status = client.call("observe.status")
            while True:
                page = client.call("observe.events", after=cursor)
                model.add_events(page["events"])
                cursor = page["cursor"]
                if cursor >= page["latest"] or not page["events"]:
                    break
            with model.lock:
                model.status, model.connected = status, True
            if status["state"] == "Ended":
                break
            stop.wait(poll_s)
    except (EOFError, ConnectionError):
        pass                    # the world ended and closed its socket
    except (OSError, RuntimeError, ValueError) as error:
        model.error = str(error)
    finally:
        with model.lock:
            model.connected, model.ended = False, True


def follow_frames(sock, model):
    """Read frames.sock: a u32 length, then the 72-byte header and the panel. The latest panel per radio is kept."""
    buffer = bytearray()
    try:
        while True:
            data = sock.recv(1 << 16)
            if not data:
                return
            buffer.extend(data)
            while len(buffer) >= LENGTH.size:
                size, = LENGTH.unpack_from(buffer)
                if not HEADER.size < size <= HEADER.size + PANEL_MAX:
                    model.error = "CONSOLE_BAD_FRAME"
                    return
                if len(buffer) < LENGTH.size + size:
                    break
                model.add_frame(buffer[LENGTH.size:LENGTH.size + size])
                del buffer[:LENGTH.size + size]
    except OSError:
        return


# --- the HTTP gate: who may open the page, and who may control the world ---

class Bucket:
    """A token bucket: rate requests a second, at most burst at once. take() is False when it is empty."""

    def __init__(self, rate, burst, clock=time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.level, self.at = float(burst), clock()

    def take(self):
        now = self.clock()
        self.level = min(float(self.burst), self.level + (now - self.at) * self.rate)
        self.at = now
        if self.level < 1:
            return False
        self.level -= 1
        return True


@dataclass
class Session:
    label: str                  # "s1": the audit name is console:s1. Never the cookie.
    bucket: Bucket
    control: bool = False       # true only after this session pasted the one-time control code


@dataclass
class Verdict:
    code: int
    reason: str
    route: str = "other"
    headers: tuple = ()
    session: object = None


class Gate:
    """The request checks, without sockets, so tests can drive them. headers is a list of (name, value)."""

    def __init__(self, port, control=False, clock=time.monotonic):
        self.host = f"127.0.0.1:{port}"
        self.origin = f"http://{self.host}"
        self.cookie_name = f"rfconsole{port}"
        self.browser_key = secrets.token_bytes(32)
        self.audit = Audit(None)
        self.control = control
        self.clock = clock
        self.tokens = set()
        self.control_tokens = set()
        self.sessions = {}          # cookie value -> Session
        self.count = 0
        self.lock = threading.Lock()

    def mint(self):
        token = secrets.token_urlsafe(32)
        self.audit.register_secret(token)
        self.tokens.add(token)
        return f"{self.origin}/#rf-token={token}"

    def mint_control(self):
        """The second one-time token, printed on the terminal only. It exists only with --control."""
        if not self.control:
            raise ConsoleError("CONSOLE_READ_ONLY")
        token = secrets.token_urlsafe(32)
        self.audit.register_secret(token)
        self.control_tokens.add(token)
        return token

    @staticmethod
    def _take(pool, value):
        found = False
        for token in list(pool):
            if hmac.compare_digest(token.encode(), value.encode()):
                pool.discard(token)
                found = True
        return found

    def _session(self, cookies):
        values = [item.strip().partition("=")[2] for item in cookies.split(";")
                  if item.strip().partition("=")[0] == self.cookie_name]
        sid = values[0] if len(values) == 1 else ""
        return sid, self.sessions.get(sid)

    def bootstrap(self, body):
        if (not isinstance(body, dict) or set(body) != {"token"} or not isinstance(body["token"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", body["token"])):
            return Verdict(400, "CONSOLE_BAD_BODY", "login"), None
        with self.lock:
            if not self._take(self.tokens, body["token"]) or len(self.sessions) >= SESSIONS_MAX:
                return Verdict(403, "CONSOLE_BAD_TOKEN", "login"), None
            cookie = secrets.token_urlsafe(32)
            proof = browser_proof(self.browser_key, self.origin, cookie, "console")
            self.audit.register_secret(cookie)
            self.audit.register_secret(proof)
            self.count += 1
            self.sessions[cookie] = Session(f"s{self.count}", Bucket(*RATE, clock=self.clock))
        return Verdict(200, "OK", "login", (f"Set-Cookie: {self.cookie_name}={cookie}; "
                                           "Path=/; HttpOnly; SameSite=Strict",)), {"proof": proof}

    def unlock(self, session, code):
        """Give session the control capability if code is the outstanding control token. Returns a reason."""
        with self.lock:
            if not self.control:
                return "CONSOLE_READ_ONLY"
            if not isinstance(code, str) or not code.isascii() or not self._take(self.control_tokens, code):
                return "CONSOLE_BAD_CONTROL_CODE"
            session.control = True
            return "OK"

    def check(self, method, target, headers):
        path, _, query = target.partition("?")
        route = ("login" if path == "/session" else "control" if path in CONTROL_ROUTES else "api" if path.startswith("/api/")
                 else "page" if path in FILES else "other")
        seen = {}
        for name, value in headers:
            name = name.strip().lower()
            if name in seen and name == "x-radiofoundry-session":
                return Verdict(403, "CONSOLE_BAD_PROOF", route)
            if name in seen and name in ("host", "origin", "cookie", "x-radiofoundry-session", "sec-fetch-site", "content-type",
                                        "content-length", "transfer-encoding"):
                return Verdict(400, "CONSOLE_BAD_REQUEST", route)
            seen[name] = value.strip()
        with self.lock:
            sid, session = self._session(seen.get("cookie", ""))

        def refuse(code, reason, extra=()):
            return Verdict(code, reason, route, extra, session)

        if seen.get("host") != self.host:
            return refuse(421, "CONSOLE_BAD_HOST")
        origin = seen.get("origin")
        if origin is not None and origin != self.origin:
            return refuse(403, "CONSOLE_BAD_ORIGIN")
        if seen.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return refuse(403, "CONSOLE_CROSS_SITE")
        if route in ("control", "login"):
            if method != "POST":
                return refuse(405, "CONSOLE_BAD_METHOD", ("Allow: POST",))
        elif method != "GET":
            return refuse(405, "CONSOLE_BAD_METHOD", ("Allow: GET",))
        if route in ("control", "login") and origin is None:
            return refuse(403, "CONSOLE_BAD_ORIGIN")
        if route == "page":
            return Verdict(200, "OK", route)
        if route == "login":
            length = seen.get("content-length", "")
            if (seen.get("content-type") != "application/json" or "transfer-encoding" in seen
                    or not length.isascii() or not length.isdigit() or not 0 < int(length) <= BODY_MAX
                    or seen.get("sec-fetch-site", "same-origin") != "same-origin"):
                return refuse(400, "CONSOLE_BAD_BODY")
            return Verdict(200, "OK", route)
        if session is None:
            return refuse(403, "CONSOLE_NO_SESSION")
        proof = seen.get("x-radiofoundry-session", "")
        expected = browser_proof(self.browser_key, self.origin, sid, "console")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", proof) or not hmac.compare_digest(proof, expected):
            return refuse(403, "CONSOLE_BAD_PROOF")
        if route == "other":
            return refuse(404, "CONSOLE_NOT_FOUND")
        if route == "control":
            if seen.get("sec-fetch-site", "same-origin") != "same-origin":
                return refuse(403, "CONSOLE_CROSS_SITE")
            # A browser always sends Origin on a POST; a request without one did not come from this page.
            if origin is None:
                return refuse(403, "CONSOLE_BAD_ORIGIN")
            if seen.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                return refuse(415, "CONSOLE_BAD_CONTENT_TYPE")
            if not self.control:
                return refuse(403, "CONSOLE_READ_ONLY")
            with self.lock:
                fresh = session.bucket.take()
            if not fresh:
                return refuse(429, "CONSOLE_RATE_LIMITED")
            if path == "/api/control" and not session.control:
                return refuse(403, "CONSOLE_NO_CAPABILITY")
        return Verdict(200, "OK", route, (), session)


# --- world control: one page request becomes ordinary world API commands ---

def _ratio(value):
    if value == "max":
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return next((choice for choice in RATIOS if choice != "max" and choice == value), None)


class Controller:
    """Checks one control request's fields, then sends world API commands over the console's own world client.

    Each command carries "actor": "console:s<n>", so the world audits it as world_control (worldapi.py). Refusals
    made here are kept in the model and, with --control, also reported to the world audit (world_control.audit).
    """

    def __init__(self, client, model, gate, *, clock=time.monotonic):
        self.client = client
        self.model = model
        self.gate = gate
        self.lock = threading.Lock()            # one control action at a time
        self.report_lock = threading.Lock()     # the refusal budget; never held while self.lock is taken
        self.reports = Bucket(*REPORT_RATE, clock=clock)
        self.anonymous = Bucket(*ANONYMOUS_RATE, clock=clock)
        self.suppressed = 0

    @staticmethod
    def actor(session):
        return f"console:{session.label}" if session is not None else "console:s0"

    def refused(self, session, action, reason):
        """Record one refusal made by this console. Never sends the cookie or a token anywhere."""
        actor = self.actor(session)
        entry = {"actor": actor, "action": action, "reason": reason, "at": round(time.time(), 3)}
        entry = self.gate.audit.clean(entry, [], "")
        actor, action, reason = (entry[key] for key in ("actor", "action", "reason"))
        self.model.add_refusal(entry)
        with self.report_lock:
            if not (self.reports if session is not None else self.anonymous).take():
                self.suppressed += 1        # a flood is counted, not written line by line
                return
            suppressed, self.suppressed = self.suppressed, 0
        if not self.gate.control or self.client is None:
            print(f"console: refused {action} from {actor}: {reason}"
                  f"{f' (+{suppressed} not shown)' if suppressed else ''}", file=sys.stderr, flush=True)
            return
        try:
            self.client.request("world_control.audit", actor=actor, action=action, result="refused",
                                reason=reason, suppressed=suppressed)
        except (OSError, EOFError, ValueError):
            pass

    def granted(self, session):
        try:
            self.client.request("world_control.audit", actor=self.actor(session), action="unlock",
                                result="granted", reason="OK")
        except (OSError, EOFError, ValueError):
            pass

    def _send(self, command, actor, **fields):
        reply = self.client.request(command, actor=actor, **fields)
        return {"command": command, "ok": bool(reply.get("ok")), "error": reply.get("error"),
                **{key: value for key, value in fields.items() if key in ("ratio", "until_us", "radio", "target",
                                                                          "rate_ppm", "time_us")}}

    def perform(self, session, body):
        """Answer {"ok": bool, "action": ..., "results": [...], "error": reason}."""
        action = body.get("action") if isinstance(body, dict) else None
        if not isinstance(action, str) or action not in ACTIONS or set(body) != {"action"} | ACTIONS[action]:
            return self._refuse(session, action if isinstance(action, str) and action in ACTIONS else "other",
                                "CONSOLE_BAD_REQUEST")
        if self.client is None:
            return self._refuse(session, action, "CONSOLE_WORLD_GONE")
        actor = self.actor(session)
        try:
            with self.lock:
                return {"action": action, **self._perform(session, actor, action, body)}
        except (OSError, EOFError, ValueError, KeyError, TypeError):
            return self._refuse(session, action, "CONSOLE_WORLD_GONE")

    def _refuse(self, session, action, reason):
        self.refused(session, action, reason)
        return {"ok": False, "action": action, "error": reason, "results": []}

    def _perform(self, session, actor, action, body):
        def done(results):
            failed = next((item["error"] for item in results if not item["ok"]), None)
            return {"ok": failed is None, "error": failed, "results": results}

        if action == "pause":
            return done([self._send("pause", actor)])
        if action in ("resume", "speed"):
            # The page keeps the speed chosen while paused and sends it with resume, so every change is audited.
            ratio = _ratio(body["ratio"])
            if ratio is None:
                return self._refuse(session, action, "CONSOLE_BAD_RATIO")
            return done([self._send("run" if action == "resume" else "set_ratio", actor, ratio=ratio)])
        if action == "step":
            now = self.client.call("status")["time_us"]
            return done([self._send("step", actor, until_us=now + STEP_US)])
        # A loss window: every other powered radio's bursts are lost at each chosen receiver. Each link is one world
        # fault with until_us (parts/faults.py): both edges are accepted or refused together, the old rate comes
        # back at the end, and a second window on a busy link is refused (WORLD_FAULT_OVERLAP).
        receivers, rate, length = body["receivers"], body["rate_pct"], body["length_ms"]
        if not (isinstance(receivers, list) and 1 <= len(receivers) <= 16
                and all(type(r) is int for r in receivers) and len(set(receivers)) == len(receivers)
                and type(rate) is int and 1 <= rate <= 100 and type(length) is int and 10 <= length <= 10000):
            return self._refuse(session, action, "CONSOLE_BAD_FAULT")
        status = self.client.call("observe.status")
        radios = {item["radio"]: item["on"] for item in status["radios"]}
        if status["state"] != "Running":
            return self._refuse(session, action, "CONSOLE_NOT_RUNNING")
        if status["ratio"] == "max":
            return self._refuse(session, action, "CONSOLE_FAULT_NEEDS_SPEED")
        if not set(receivers) <= set(radios):
            return self._refuse(session, action, "CONSOLE_UNKNOWN_RADIO")
        links = [(sender, receiver) for receiver in sorted(receivers) for sender in sorted(radios)
                 if radios[sender] and sender != receiver]
        if not links:
            return self._refuse(session, action, "CONSOLE_NO_SENDER")
        start = status["time_us"] + int(FAULT_LEAD_US * max(1, status["ratio"]))
        end = start + length * 1000
        results = []
        for sender, receiver in links:
            results.append(self._send("fault", actor, kind="loss", radio=sender, target=receiver,
                                      rate_ppm=rate * 10000, time_us=start, until_us=end))
            if not results[-1]["ok"]:
                break
        return {**done(results), "window": [start, end]}


def _no_constants(value):
    raise ValueError("CONSOLE_BAD_BODY")


def _unique(pairs):
    names = [name for name, _ in pairs]
    if len(names) != len(set(names)):
        raise ValueError("CONSOLE_BAD_BODY")
    return dict(pairs)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "rfconsole"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = READ_S            # a slow or partial request is dropped, so it cannot hold a thread

    def log_message(self, format, *args):       # the request line may hold the one-time token
        pass

    def _send(self, code, body=b"", kind="text/plain; charset=utf-8", headers=()):
        self.send_response(code)
        for line in headers:
            name, _, value = line.partition(": ")
            self.send_header(name, value)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _json(self, code, value):
        self._send(code, json.dumps(value, separators=(",", ":"), default=str).encode(), "application/json")

    def _body(self):
        """The request body as a JSON object of at most BODY_MAX bytes, or None."""
        if "transfer-encoding" in (name.lower() for name in self.headers.keys()):
            return None
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not lengths[0].strip().isdigit() or not 0 < int(lengths[0]) <= BODY_MAX:
            return None
        data = self.rfile.read(int(lengths[0]))
        try:
            value = json.loads(data, parse_constant=_no_constants, object_pairs_hook=_unique)
        except (ValueError, UnicodeError):
            return None
        return value if isinstance(value, dict) else None

    def _control(self, verdict, path):
        controller = self.server.controller
        action = "unlock" if path == "/api/unlock" else "other"
        if verdict.code != 200:
            controller.refused(verdict.session, action, verdict.reason)
            self._send(verdict.code, verdict.reason.encode() + b"\n", headers=verdict.headers)
            return
        body = self._body()
        if body is None:
            controller.refused(verdict.session, action, "CONSOLE_BAD_BODY")
            self._send(400, b"CONSOLE_BAD_BODY\n")
            return
        if path == "/api/unlock":
            reason = self.server.gate.unlock(verdict.session, body.get("code")) if set(body) == {"code"} \
                else "CONSOLE_BAD_BODY"
            if reason != "OK":
                controller.refused(verdict.session, "unlock", reason)
                self._send(403 if reason != "CONSOLE_BAD_BODY" else 400, reason.encode() + b"\n")
                return
            controller.granted(verdict.session)
            self._json(200, {"ok": True, "control": "unlocked"})
            return
        self._json(200, controller.perform(verdict.session, body))

    def _handle(self):
        verdict = self.server.gate.check(self.command, self.path, list(self.headers.items()))
        path, _, query = self.path.partition("?")
        if verdict.route == "control":
            self._control(verdict, path)
            return
        if verdict.code != 200:
            self._send(verdict.code, b"" if verdict.code == 303 else verdict.reason.encode() + b"\n",
                       headers=verdict.headers)
            return
        if verdict.route == "login":
            verdict, body = self.server.gate.bootstrap(self._body())
            self._send(verdict.code, json.dumps(body or {"error": verdict.reason}).encode(),
                       "application/json", headers=verdict.headers)
            return
        model = self.server.model
        if path in FILES:
            name, kind = FILES[path]
            self._send(200, (STATIC / name).read_bytes(), kind)
        elif path == "/api/state":
            try:
                after = int(parse_qs(query).get("after", ["0"])[0])
            except ValueError:
                after = 0
            state = model.snapshot(after)
            gate = self.server.gate
            state["control"] = {"mode": "unlocked" if verdict.session.control else "locked" if gate.control
                                else "off", "session": Controller.actor(verdict.session)}
            self._send(200, json.dumps(state, separators=(",", ":"), default=str).encode(), "application/json")
        elif match := re.fullmatch(r"/api/frame/([0-9]{1,9})", path):
            record = model.frame(int(match.group(1)))
            if record is None:
                self._send(404, b"CONSOLE_NO_FRAME\n")
            else:
                self._send(200, record, "application/octet-stream")
        else:
            self._send(404, b"CONSOLE_NOT_FOUND\n")

    do_GET = _handle
    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, model, port=0, *, control=False, client=None):
        self.model = model
        super().__init__(("127.0.0.1", port), Handler, bind_and_activate=False)
        try:
            guarded_bind(self.socket, ("127.0.0.1", port))
            self.server_address = self.socket.getsockname()
            self.server_activate()
        except BaseException:
            self.server_close()
            raise
        self.gate = Gate(self.server_address[1], control)
        self.controller = Controller(client, model, self.gate)
        self.slots = threading.BoundedSemaphore(HANDLERS_MAX)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)          # too many at once: close it unanswered
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def default_runtime():
    """The runtime folder tools/vradio.py uses: $XDG_RUNTIME_DIR, else <tmp>/rfoundry-runtime-<uid>."""
    return os.environ.get("XDG_RUNTIME_DIR") or f"{tempfile.gettempdir()}/rfoundry-runtime-{os.getuid()}"


def world_folder(name, runtime=None):
    runtime = runtime or default_runtime()
    parent = Path(runtime) / "rfoundry-vr"
    directory = parent / name
    if not (directory / "world.sock").exists():
        raise ConsoleError(f"world {name} is not running; use tools/vradio.py status to find a running world.")
    private_directory(Path(runtime))
    private_directory(parent)
    private_directory(directory)
    return directory


def main(argv=None):
    parser = argparse.ArgumentParser(prog="console.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--world", required=True, help="the world name (tools/vradio.py status lists them)")
    parser.add_argument("--runtime", help="the runtime folder that holds rfoundry-vr/ (default: as tools/vradio.py, $XDG_RUNTIME_DIR "
                        "or <tmp>/rfoundry-runtime-<uid>)")
    parser.add_argument("--port", type=int, default=0, help="the local port, 0 for any free port")
    parser.add_argument("--no-browser", action="store_true", help="print the URL without opening a browser")
    parser.add_argument("--control", action="store_true",
                        help="allow world control (pause, resume, step, speed, loss window) from a page that pastes "
                        "the one-time control code printed here; only the owner of the world socket may use it")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.world):
        parser.error("use 1 to 64 letters, digits, underscores or hyphens for the world name")
    try:
        directory = world_folder(args.world, args.runtime)
        model = Model(args.world)
        if args.control:
            info = (directory / "world.sock").lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ConsoleError("--control needs a world socket owned by this user; start the console as the "
                                   "user who started the world.")
            client = WorldClient(directory / "world.sock", allowed=CONTROL_ALLOWED)
        else:
            client = ReadOnlyWorld(directory / "world.sock")
        frames = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        frames.connect(str(directory / "frames.sock"))
        server = Server(model, args.port, control=args.control, client=client)
    except ConsoleError as error:
        print(f"console: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"console: {error}; check that the world is running and retry.", file=sys.stderr)
        return 1
    stop = threading.Event()
    readers = [threading.Thread(target=follow_world, args=(client, model, stop), name="world", daemon=True),
               threading.Thread(target=follow_frames, args=(frames, model), name="frames", daemon=True)]
    for reader in readers:
        reader.start()
    web = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, name="http", daemon=True)
    web.start()
    url = server.gate.mint()
    print(f"console: {url}", flush=True)
    if args.control:
        # Only here, on the terminal of the user who owns the world. It never reaches a log, the URL or the world.
        print(f"console: control code {server.gate.mint_control()} (paste it on the page to unlock world control; "
              "it works once)", flush=True)
    if not args.no_browser and not webbrowser.open(url):
        print("console: browser unavailable; open the printed URL.", file=sys.stderr)
    try:
        readers[0].join()
        time.sleep(0.5)     # let an open page fetch the final state
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        client.close()
        frames.close()
    print(f"console: world {args.world} {'ended' if model.ended else 'left'}"
          f"{': ' + model.error if model.error else ''}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
