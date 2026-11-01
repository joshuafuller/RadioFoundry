#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""control ownership: exercise ADR 0004 refusals in one live world with its gateway enabled.

Build: make -j8 BOARD=vhost
Run: python3 tools/conductor/checks/control_rules_check.py
Socket-free checker test: append --self-test.
Negative control: append --allow-observer-command (also works with --self-test).
Exit 0: all checks passed; 1: a rule failed; 2: prerequisites prevented the run.
Each refusal needs its audit line, copied into events.jsonl unless the recording had already stopped with a
named reason (a gateway world's recording stops at RECORD_NO_V2_KIND:ETH_IN). The non-loopback bind is a
startup guard with no client, so it has no audit line. Rule 11 is tested on the endpoints: a lease holder's
debug link cannot carry world commands, a world client cannot open a controller session without the lease,
and a second world client that copies the holder's principal and generation from lease.status is refused
too, because it lacks the one-time grant secret.
The private world folder is retained. Tokens and the grant secret stay in memory, including the grep
pattern passed on stdin; grep's per-file counts are printed without the token.
World control from the console (tools/console.py) is tested last, with two console processes on the
world socket: an observer console (no --control) refuses a control in its own process and sends nothing to the
world, so the proof there is the refusal, its entry on the page state and an unchanged world; a --control console
refuses a session without the pasted capability and a request with a foreign Origin, each audited as world_control
with the session's actor name; then the same session's correct pause is accepted and audited.
"""

import argparse
import ipaddress
from contextlib import ExitStack
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.conductor.channel import Kind
from tools.conductor.conductor import install_parts
from tools.conductor.net_guard import bind
from tools.conductor.parts import audit, dlmux, recorder, webgw
from tools.conductor.world import Config, World, WorldError

KEY = bytes((0, 5, 0, 0))       # debuglink.md: KEY, menu action, press


class CheckFailure(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise CheckFailure(reason)


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def refusal(refused, rows, *, forwarded=False, **fields):
    """Only a fresh, attributable refusal for this attempt proves the rule."""
    require(not forwarded, "command reached the radio")
    require(refused, "command was not refused")
    matches = [row for row in rows if row.get("type") == "audit" and row.get("result") == "refused"
               and all(row.get(key) == value for key, value in fields.items())]
    require(bool(matches), "missing matching refusal audit line")
    row = matches[-1]
    require(all(key in row for key in ("principal", "radio", "host_ns", "time_us"))
            and bool(row.get("kind")) and bool(row.get("reason")), "incomplete refusal audit line")
    return row


def recorded(row, events):
    """The audit line must be in events.jsonl, unless the recording had already stopped (with its reason)."""
    if any(all(item.get(key) == value for key, value in row.items()) for item in events):
        return ""
    stops = [item for item in events if item.get("type") == "recording_stopped"]
    require(bool(stops) and stops[-1]["time_us"] <= row["time_us"], "refusal missing from events.jsonl")
    return f" (recording stopped at {stops[-1]['time_us']} us: {stops[-1]['reason']}; in audit.jsonl only)"


class Check:
    def __init__(self, world):
        self.world = world
        self.directory = world.api.directory
        self.failures = []
        self.inputs = []
        world.on_input(self.inputs.append)

    def attempt(self, label, action, *, forwarded=lambda events: False, **fields):
        before = len(lines(self.directory / "audit.jsonl"))
        inputs = len(self.inputs)
        try:
            refused = action()
            rows = lines(self.directory / "audit.jsonl")[before:]
            row = refusal(refused, rows, forwarded=forwarded(self.inputs[inputs:]), **fields)
            note = recorded(row, lines(self.directory / "events.jsonl"))
            print(f"{label}: refused kind={row['kind']} reason={row['reason']}{note}", flush=True)
        except (CheckFailure, TimeoutError, WorldError) as error:
            self.failures.append(label)
            print(f"{label}: FAIL {error}", flush=True)


def allow_one_observer_command(mux):
    """Test hook: bypass the observer gate once, through the normal upstream queue."""
    original = mux._request

    def request(session, cmd, seq, payload):
        if session.role == "observer" and cmd == 0x06:
            mux._request = original
            mux._link(session.radio).queue.append(dlmux.Request(session, cmd, seq, payload))
            mux._pump(session.radio, internal=False)
        else:
            original(session, cmd, seq, payload)

    mux._request = request


def wait(world, ready, timeout=30):
    end = time.monotonic() + timeout
    while not ready():
        if world.state == "Ended":
            raise WorldError(world.reason)
        if time.monotonic() >= end:
            raise TimeoutError("control check timed out")
        world.api.poll(0)
        world.advance()


class Wire:
    """A real client socket, serviced while the calling thread advances the world."""
    def __init__(self, world, address):
        self.world = world
        self.sock = socket.socket(socket.AF_UNIX if isinstance(address, str) else socket.AF_INET,
                                  socket.SOCK_STREAM)
        self.sock.settimeout(5)
        try:
            self.sock.connect(address)
        except BaseException:
            self.sock.close()
            raise
        self.buffer = bytearray()

    def close(self):
        self.sock.close()

    def send(self, data):
        self.sock.settimeout(5)
        self.sock.sendall(data)

    def read(self, parse):
        found = []
        self.sock.setblocking(False)

        def ready():
            value = parse(self.buffer)
            if value is not None:
                found.append(value)
                return True
            try:
                data = self.sock.recv(65536)
            except BlockingIOError:
                return False
            if not data:
                raise CheckFailure("connection closed without the expected reply")
            self.buffer.extend(data)
            return False

        wait(self.world, ready)
        return found[0]

    def until(self, delimiter):
        def parse(buffer):
            if delimiter in buffer:
                head, _, rest = buffer.partition(delimiter)
                buffer[:] = rest
                return bytes(head)
        return self.read(parse)

    def debug(self, command, payload, status=None):
        status = dlmux.ST_REFUSED if status is None else status
        self.send(dlmux.frame(command, 42, payload))
        parser = dlmux.Parser(dlmux.REPLY_MAX)

        def parse(buffer):
            items = parser.feed(buffer)
            buffer.clear()
            for item in items:
                require(item[0] == "frame", "invalid debug reply")
                require(item[1:3] == (command | 0x80, 42), "wrong debug reply command or sequence")
                return item[3] == bytes((status,))
        return self.read(parse)

    def world_call(self, command, **fields):
        self.send(json.dumps({"version": 1, "id": 1, "command": command, **fields}).encode() + b"\n")
        while True:
            reply = json.loads(self.until(b"\n"))
            if reply.get("id") == 1:
                return reply


def http(wire, port, target="/", *, host=None, origin=None, cookie=None, proof=None, upgrade=False):
    headers = [f"GET {target} HTTP/1.1", f"Host: {host or f'127.0.0.1:{port}'}"]
    if origin is not None:
        headers.append(f"Origin: {origin}")
    if cookie is not None:
        headers.append(f"Cookie: {cookie}")
    if upgrade:
        if proof is not None:
            headers.append(f"Sec-WebSocket-Protocol: rf-vr-v1, rf-vr-proof.{proof}")
        headers += ["Upgrade: websocket", "Connection: Upgrade", "Sec-WebSocket-Version: 13",
                    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="]
    wire.send(("\r\n".join(headers) + "\r\n\r\n").encode())
    head = wire.until(b"\r\n\r\n")
    return int(head.split(b" ")[1]), head


def http_post(wire, port, path, body, *, origin, cookie, proof=None, kind="application/json"):
    """One console request: returns (status, body). origin None leaves the header out."""
    data = json.dumps(body).encode()
    headers = [f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", f"X-RadioFoundry-Session: {proof or ''}", f"Content-Type: {kind}",
               f"Content-Length: {len(data)}", f"Cookie: {cookie}"] + ([f"Origin: {origin}"] if origin else [])
    wire.send(("\r\n".join(headers) + "\r\n\r\n").encode() + data)
    return reply_body(wire)


def reply_body(wire):
    head = wire.until(b"\r\n\r\n")
    size = next(int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length:"))
    body = wire.read(lambda buffer: (bytes(buffer[:size]), buffer.clear())[0] if len(buffer) >= size else None)
    return int(head.split(b" ")[1]), head, body


def console_process(world, runtime, *extra):
    """Start tools/console.py on this world; return (process, port, url path, control code or None)."""
    process = subprocess.Popen([sys.executable, str(ROOT / "tools/console.py"), "--world", "rules", "--runtime",
                                str(runtime), "--no-browser", *extra], cwd=ROOT, stdout=subprocess.PIPE, text=True)
    lines = []
    reader = threading.Thread(target=lambda: lines.extend(process.stdout.readline() for _ in
                                                         range(2 if extra else 1)), daemon=True)
    reader.start()
    wait(world, lambda: not reader.is_alive())
    require(lines and lines[0].startswith("console: http://127.0.0.1:"), f"console did not start: {lines!r}")
    url = lines[0].strip().split(" ", 1)[1]
    port = int(url.split(":")[2].split("/")[0])
    code = lines[1].split()[3] if extra else None
    return process, port, url[len(f"http://127.0.0.1:{port}"):], code


def console_login(connect, port, target):
    status, head, body = http_post(connect(("127.0.0.1", port)), port, "/session",
                                    {"token": target.split("#rf-token=")[1]},
                                    origin=f"http://127.0.0.1:{port}", cookie="")
    require(status == 200, "console login failed")
    cookie = next(line.split(b": ", 1)[1].split(b";", 1)[0].decode()
                  for line in head.split(b"\r\n") if line.lower().startswith(b"set-cookie:"))
    return cookie, json.loads(body)["proof"]


def console_state(connect, port, cookie, proof):
    wire = connect(("127.0.0.1", port))
    wire.send((f"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-RadioFoundry-Session: {proof}\r\nCookie: {cookie}"
               "\r\n\r\n").encode())
    status, _, body = reply_body(wire)
    require(status == 200, "console state refused")
    return json.loads(body)


def token_counts(directory, token):
    paths = sorted(directory.glob("*.jsonl"))
    require({"audit.jsonl", "events.jsonl"} <= {path.name for path in paths}, "missing log files")
    # -f - keeps the secret out of argv, shell history, and the report.
    result = subprocess.run(["grep", "-F", "-c", "-f", "-", *map(str, paths)], input=token + "\n",
                            text=True, capture_output=True)
    print(result.stdout, end="", flush=True)
    require(result.returncode == 1 and len(result.stdout.splitlines()) == len(paths)
            and all(line.endswith(":0") for line in result.stdout.splitlines()), "token leaked or grep failed")


def run(executable, negative):
    runtime = Path(tempfile.mkdtemp(prefix="rf-control-"))
    world = World(Config(executable=executable))
    try:
        install_parts(world)
        gateway = webgw.get(world, enabled=True)
        world.api.bind("rules", runtime)
        print(f"world folder: {world.api.directory}", flush=True)
        require(Path(executable).is_file(), f"missing {executable}; run make -j8 BOARD=vhost")
        world.start()
        world.run("max")
        check = Check(world)

        def api(command, **fields):
            reply = world.api.dispatch({"version": 1, "command": command, **fields})
            require(reply["ok"], f"setup {command}: {reply.get('error')}")
            return reply["result"]

        with ExitStack() as stack:
            def connect(address):
                wire = Wire(world, address)
                stack.callback(wire.close)
                return wire

            def debug(principal, role="observer", generation=None, secret=None):
                opened = api("debuglink.open", radio=1, principal=principal, role=role, generation=generation,
                             secret=secret)
                return connect(opened["path"]), world.dlmux.sessions[opened["session"]]

            def nonloopback():
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                    try:
                        bind(listener, ("0.0.0.0", 0))
                    except ValueError as error:
                        return str(error) == "WORLD_NON_LOOPBACK_BIND"
                return False

            # Rule 18 is a startup guard, not a client control attempt: the gateway's bind address is fixed
            # (webgw.py), no client can choose it, so the refusal has no principal and no audit line.
            try:
                refused = nonloopback()
                require(refused, "non-loopback bind was not refused")
                bound = [server.listener.getsockname()[0] for server in gateway.servers.values()]
                require(bound and all(ipaddress.ip_address(host).is_loopback for host in bound),
                        f"gateway listener not on loopback: {bound}")
                print(f"rule18 non-loopback bind: refused reason=WORLD_NON_LOOPBACK_BIND; gateway bound {bound} "
                      "(startup guard, no audit line by design)", flush=True)
            except CheckFailure as error:
                check.failures.append("rule18 non-loopback bind")
                print(f"rule18 non-loopback bind: FAIL {error}", flush=True)
            observer, session = debug("script:observer")
            if negative:
                allow_one_observer_command(world.dlmux)
            check.attempt("rule8 debug observer KEY", lambda: observer.debug(0x06, KEY),
                          forwarded=lambda events: any(e.source == session.source for e in events),
                          principal=session.principal, radio=1, command="KEY", kind="control",
                          reason="DLMUX_OBSERVER_READ_ONLY")

            granted = api("lease.grant", radio=1, principal="script:holder")
            secret = granted.get("secret")      # .get: a conductor without grant secrets fails rule 11 below
            controller, holder = debug("script:holder", "controller", granted["generation"], secret)
            check.attempt("rule13 REBOOT without maintenance",
                          lambda: controller.debug(0x0D, struct.pack("<BI", 0, world._chip_token(1))),
                          forwarded=lambda events: any(e.source == holder.source for e in events),
                          principal=holder.principal, radio=1, command="REBOOT", kind="control",
                          reason="LEASE_NO_MAINTENANCE")
            api("lease.grant_maintenance", radio=1, principal=holder.principal, generation=holder.generation,
                secret=secret)
            # Exercise the aperture before the gateway's scheduled network switch (webgw.NETWORK_US).
            wait(world, lambda: world.now >= 1_100_000)
            check.attempt("rule12 READ_MEM outside fake array",
                          lambda: controller.debug(0x0C, struct.pack("<IH", 0x20004000, 1)),
                          principal=holder.principal, radio=1, command="READ_MEM")

            port = gateway.servers[1].port
            origin = f"http://127.0.0.1:{port}"
            minted = api("webgw.token", radio=1)
            target = minted["url"][len(origin):]
            token = target.split("token=", 1)[1]
            login = connect(("127.0.0.1", port))
            status, head, body = http_post(login, port, "/session", {"token": token}, origin=origin, cookie="")
            require(status == 200, "browser login failed")
            proof = json.loads(body)["proof"]
            cookie = next(line.split(b": ", 1)[1].split(b";", 1)[0].decode()
                          for line in head.split(b"\r\n") if line.lower().startswith(b"set-cookie:"))
            for label, code, reason, options in (
                ("wrong Host", 421, "WEBGW_BAD_HOST", {"host": "example.invalid", "cookie": cookie}),
                ("wrong Origin", 403, "WEBGW_BAD_ORIGIN", {"origin": "http://example.invalid", "cookie": cookie}),
                ("no session", 403, "WEBGW_NO_SESSION", {}),
            ):
                wire = connect(("127.0.0.1", port))
                check.attempt(f"rule6 browser {label}",
                              lambda: http(wire, port, "/ws", upgrade=True,
                                           **dict({"origin": origin}, **options))[0] == code,
                              kind="webgw_request", reason=reason, radio=1, route="ws")

            wait(world, lambda: gateway.links[1].state == "up")
            browser = connect(("127.0.0.1", port))
            require(http(browser, port, "/ws", origin=origin, cookie=cookie, proof=proof, upgrade=True)[0] == 101,
                    "observer WebSocket upgrade failed")

            def browser_command():
                browser.send(webgw.client_frame(webgw.WS_TEXT, b"e1", b"test")
                             + webgw.client_frame(webgw.WS_PING, b"barrier", b"test"))

                def pong(buffer):
                    frames = webgw.parse_frames(buffer, False, webgw.BROWSER_OUT_MAX)
                    require(frames is not None, "invalid WebSocket reply")
                    return True if (webgw.WS_PONG, b"barrier") in frames else None

                browser.read(pong)      # the command was processed before the pong, even if it was dropped
                return not any(principal == minted["principal"] for _, principal, _, _ in gateway.forwarded)

            check.attempt("rule8 WebSocket observer encoder", browser_command, principal=minted["principal"],
                          radio=1, command="web.e", kind="control", reason="LEASE_NOT_HOLDER")

            # Rule 11, radio control does not reach world commands. The world API is its own 0600 socket; reaching
            # it is the world capability (ADR 0004 rule 8, same UID trusted), so a "principal" field there is only
            # data. What a lease holder has is its debug link: a command outside the debug link set fails closed.
            state = (world.state, world.pacing.ratio)
            check.attempt("rule11 radio holder world command on its debug link",
                          lambda: controller.debug(0x42, b"set_ratio 2", dlmux.ST_UNKNOWN)
                          and (world.state, world.pacing.ratio) == state,
                          forwarded=lambda events: any(e.source == holder.source for e in events),
                          principal=holder.principal, radio=1, command="0x42", kind="control",
                          reason="DLMUX_UNKNOWN_COMMAND")
            # Rule 11, the world capability does not give radio control: a world client cannot open a
            # controller session on the debug link without the lease, even naming the current generation.
            world_client = connect(str(world.api.path))
            check.attempt("rule11 world client debug link control without lease",
                          lambda: world_client.world_call("debuglink.open", radio=1, principal="script:world",
                                                          role="controller", generation=holder.generation)
                          .get("error") == "LEASE_NOT_HOLDER",
                          forwarded=lambda events: any(e.kind == Kind.KEY for e in events),
                          principal="script:world", radio=1, kind="debuglink_open", reason="LEASE_NOT_HOLDER")
            # Rule 11, posing as the holder: a second world client on its own socket reads lease.status and
            # copies the holder's principal and generation. Those are public; only the caller of lease.grant got
            # the grant secret, so the controller open is refused and audited under the holder's name.
            copier = connect(str(world.api.path))

            def copy_holder():
                public = copier.world_call("lease.status")["result"]["1"]
                require((public["principal"], public["generation"]) == (holder.principal, holder.generation),
                        "lease.status does not show the holder")
                require("secret" not in json.dumps(public), "lease.status shows a secret")
                reply = copier.world_call("debuglink.open", radio=1, role="controller",
                                          principal=public["principal"], generation=public["generation"])
                return reply.get("error") == "LEASE_BAD_SECRET"

            sessions = len(world.dlmux.sessions)
            check.attempt("rule11 world client posing as the holder from lease.status", copy_holder,
                          forwarded=lambda events: any(e.kind == Kind.KEY for e in events)
                          or len(world.dlmux.sessions) != sessions,
                          principal=holder.principal, radio=1, kind="debuglink_open", role="controller",
                          reason="LEASE_BAD_SECRET")
            # Positive control: the caller of lease.grant, which has the secret, still opens a controller.
            # (A PING would not prove more here: after the network switch replies go as UDP, not on this link.)
            try:
                opened = api("debuglink.open", radio=1, role="controller", principal=holder.principal,
                             generation=holder.generation, secret=secret)
                require(world.dlmux.sessions[opened["session"]].role == "controller", "no controller session")
                print("rule11 holder with the grant secret: controller session opened", flush=True)
            except CheckFailure as error:
                check.failures.append("rule11 holder with the grant secret")
                print(f"rule11 holder with the grant secret: FAIL {error}", flush=True)

            # World control from a browser goes only through a --control console (ADR 0004 rule 7: watching,
            # radio control and world control are separate). Each control request below is a real HTTP request.
            if world.frames.listener is None:
                world.frames.bind()
            consoles = []

            def stop_consoles():
                for process in consoles:
                    try:
                        process.wait(timeout=10)        # each console exits by itself when the world ends
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                        check.failures.append("console exit")
                        print("console exit: FAIL a console did not exit after the world ended", flush=True)
            stack.callback(stop_consoles)
            stack.callback(world.close)
            process, port, target, _ = console_process(world, runtime)
            consoles.append(process)
            origin = f"http://127.0.0.1:{port}"
            cookie, proof = console_login(connect, port, target)
            state = (world.state, world.pacing.ratio)
            before = len(lines(check.directory / "audit.jsonl"))
            try:
                status, _, body = http_post(connect(("127.0.0.1", port)), port, "/api/control",
                                            {"action": "pause"}, origin=origin, cookie=cookie, proof=proof)
                require((status, body) == (403, b"CONSOLE_READ_ONLY\n"), f"observer console answered {status} {body}")
                require((world.state, world.pacing.ratio) == state, "observer console changed the world")
                written = [row for row in lines(check.directory / "audit.jsonl")[before:]
                           if str(row.get("principal", "")).startswith("console:")]
                require(not written, "observer console wrote to the world audit")
                shown = console_state(connect, port, cookie, proof)["refusals"]
                require(any(item["reason"] == "CONSOLE_READ_ONLY" and item["actor"] == "console:s1"
                            for item in shown), "observer refusal not on the page state")
                print("console observer session world control: refused reason=CONSOLE_READ_ONLY in the console "
                      "process; world unchanged and sent nothing (an observer console stays read-only)", flush=True)
            except CheckFailure as error:
                check.failures.append("console observer session world control")
                print(f"console observer session world control: FAIL {error}", flush=True)

            process, port, target, code = console_process(world, runtime, "--control")
            consoles.append(process)
            origin = f"http://127.0.0.1:{port}"
            cookie, proof = console_login(connect, port, target)

            def control(**options):
                status, _, body = http_post(connect(("127.0.0.1", port)), port, "/api/control", {"action": "pause"},
                                            **dict({"origin": origin, "cookie": cookie, "proof": proof}, **options))
                return status, body

            check.attempt("console no capability",
                          lambda: control() == (403, b"CONSOLE_NO_CAPABILITY\n") and world.state == "Running",
                          kind="world_control", principal="console:s1", reason="CONSOLE_NO_CAPABILITY")
            status, _, body = http_post(connect(("127.0.0.1", port)), port, "/api/unlock", {"code": code},
                                        origin=origin, cookie=cookie, proof=proof)
            require(status == 200, f"console unlock failed: {status} {body}")
            check.attempt("console wrong Origin",
                          lambda: control(origin="http://example.invalid") == (403, b"CONSOLE_BAD_ORIGIN\n")
                          and world.state == "Running",
                          kind="world_control", principal="console:s1", reason="CONSOLE_BAD_ORIGIN")
            check.attempt("console missing Origin",
                          lambda: control(origin=None) == (403, b"CONSOLE_BAD_ORIGIN\n") and world.state == "Running",
                          kind="world_control", principal="console:s1", reason="CONSOLE_BAD_ORIGIN")
            # Positive control: the same session, with the capability and the right Origin, pauses the world.
            try:
                before = len(lines(check.directory / "audit.jsonl"))
                status, body = control()
                reply = json.loads(body)
                require(status == 200 and reply["ok"], f"console pause refused: {status} {body}")
                wait(world, lambda: world.state == "Paused")
                rows = [row for row in lines(check.directory / "audit.jsonl")[before:]
                        if row.get("kind") == "world_control" and row.get("principal") == "console:s1"
                        and row.get("command") == "pause" and row.get("result") == "accepted"]
                require(rows, "accepted console pause has no world_control audit line")
                print("console capability with the right Origin: pause accepted, world Paused, audited "
                      "kind=world_control principal=console:s1", flush=True)
            except (CheckFailure, KeyError, ValueError, TimeoutError) as error:
                check.failures.append("console capability pause")
                print(f"console capability pause: FAIL {error}", flush=True)
            console_cookie = cookie.split("=", 1)[1]

        directory = world.api.directory
        world.close()
        token_counts(directory, token)
        token_counts(directory, cookie.split("=", 1)[1])
        require(bool(secret), "lease.grant returned no grant secret")
        token_counts(directory, secret)
        token_counts(directory, code)
        token_counts(directory, console_cookie)
        return int(bool(check.failures))
    finally:
        world.close()


def self_test(negative):
    """Exercise the real observer gate and the evidence checker without sockets or firmware."""
    with tempfile.TemporaryDirectory(prefix="rf-control-self-") as folder:
        world = World()
        world.api.directory = Path(folder)
        recorder.install(world)
        mux = dlmux.get(world)
        world.state = "Running"
        world.boots[1] = 1
        world.radios[1] = SimpleNamespace(channel=SimpleNamespace(close=lambda: None))
        check = Check(world)
        session = mux.connect(1, "script:observer", "observer")
        try:
            if negative:
                allow_one_observer_command(mux)

            def command():
                mux.feed(session, dlmux.frame(0x06, 42, KEY))
                return session.take() == dlmux.frame(0x86, 42, bytes((dlmux.ST_REFUSED,)))

            check.attempt("rule8 debug observer KEY", command,
                          forwarded=lambda events: any(e.source == session.source for e in events),
                          kind="control", principal=session.principal, radio=1, command="KEY",
                          reason="DLMUX_OBSERVER_READ_ONLY")
            if check.failures:
                return 1
            row = lines(Path(folder) / "audit.jsonl")[-1]
            for refused, rows, forwarded, fields in (
                (False, [row], False, {}), (True, [], False, {}),
                (True, [row], True, {}), (True, [row], False, {"principal": "script:other"}),
                (True, [row], False, {"command": "REBOOT"}),
                (True, [dict(row, result="accepted")], False, {}),
            ):
                try:
                    refusal(refused, rows, forwarded=forwarded, **fields)
                except CheckFailure:
                    continue
                raise CheckFailure("checker accepted bad refusal evidence")
            # A refusal missing from events.jsonl passes only after a recording_stopped line at or before it.
            stop = {"type": "recording_stopped", "reason": "TEST", "time_us": row["time_us"]}
            require(recorded(row, [stop]) != "", "checker refused a refusal after the recording stopped")
            for events in ([], [dict(stop, time_us=row["time_us"] + 1)]):
                try:
                    recorded(row, events)
                except CheckFailure:
                    continue
                raise CheckFailure("checker accepted a refusal missing from events.jsonl")
            token = "self-test-secret-a1b2c3d4e5f6g7h8"
            audit.get(world).register_secret(token)
            audit.get(world).emit("probe", "script:observer", 1, "refused", "TEST", detail=token)
            token_counts(Path(folder), token)
            print("CONTROL_RULES_SELF_TEST_OK (checker only; live rules not proven)", flush=True)
            return 0
        finally:
            world.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", default=Config().executable)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--allow-observer-command", action="store_true", help="test hook: must exit 1")
    args = parser.parse_args(argv)
    try:
        return self_test(args.allow_observer_command) if args.self_test else run(
            args.executable, args.allow_observer_command)
    except CheckFailure as error:
        print(f"CONTROL_RULES_FAIL {error}", flush=True)
        return 1
    except (OSError, WorldError, ValueError) as error:
        print(f"CONTROL_RULES_BLOCKED {error}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
