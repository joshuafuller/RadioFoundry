#!/bin/sh
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# Build: make -j8 BOARD=vhost. Check: tools/conductor/checks/observe_check.sh
# --self-test needs no sockets or firmware. --broken-observer must exit 1.
# Exit 1 is a failed check; exit 2 is an unavailable prerequisite or incomplete run.
exec python3 - "$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)" "$@" <<'PY'
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import hashlib
import http.client
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import zlib

ROOT = Path(sys.argv.pop(1))
sys.path.insert(0, str(ROOT))

from tools.conductor.channel import ChannelError, Kind
from tools.conductor.conductor import scenario_config
from tools.conductor.parts import audit, dlcache, dlmux, frames, input, observe_api, recorder, webgw
from tools.conductor.world import Config, World, WorldError

SCENARIO = ROOT / "tools/conductor/scenarios/observe.json"
KEY_US, REQUEST_US = 2_000_000, 1_990_000
SHOT = struct.Struct("<BHHBBBBBBIIIIB")  # status and packed header, debuglink.md 3.3


class CheckFailure(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise CheckFailure(reason)


def checkpoint_hash(checkpoints):
    data = json.dumps([asdict(c) for c in checkpoints], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def compare(reference, candidate, label):
    require(reference["trace"] and reference["checkpoints"], "observer isolation FAIL empty reference")
    require(reference["trace"] == candidate["trace"], f"observer isolation FAIL {label}: trace differs")
    require(reference["checkpoints"] == candidate["checkpoints"],
            f"observer isolation FAIL {label}: FRAME checkpoint list differs")


def subsequence(reference, received):
    require(reference and received, "observer isolation FAIL slow observer: empty stream")
    remaining = iter(reference)
    for checkpoint in received:
        require(any(item == checkpoint for item in remaining),
                "observer isolation FAIL slow observer: not an ordered subsequence")
    require(received[-1] == reference[-1], "observer isolation FAIL slow observer: missing final checkpoint")


def decode_record(record):
    require(len(record) > frames.HEADER.size, "observer isolation FAIL truncated frame")
    (radio, boot, frame_id, stamp, crc, size, offset, width, height,
     bpp, backlight, has_palette, reserved, *palette) = frames.HEADER.unpack_from(record)
    panel = record[frames.HEADER.size:]
    require(offset == 0 and size == len(panel) and 0 < size <= frames.PANEL_MAX and
            bpp in (1, 4, 16) and width > 0 and height > 0 and
            size == ((width * bpp + 7) // 8) * height and backlight <= 100 and
            has_palette == int(bpp == 4) and reserved == 0 and
            (has_palette or not any(palette)), "observer isolation FAIL invalid frame header")
    require(zlib.crc32(panel) == crc, "observer isolation FAIL observer panel CRC differs")
    return frames.Checkpoint(radio, boot, frame_id, stamp, crc,
                             tuple(palette) if has_palette else (), backlight)


def read_frames(client, ready):
    ready.wait()  # The slow observer starts reading only after virtual time reaches the end.

    def exact(size, eof=False):
        data = bytearray()
        while len(data) < size:
            part = client.recv(size - len(data))
            if not part:
                require(eof and not data, "observer isolation FAIL truncated frame stream")
                return b""
            data.extend(part)
        return bytes(data)

    received = []
    while prefix := exact(frames.LENGTH.size, eof=True):
        size, = frames.LENGTH.unpack(prefix)
        require(frames.HEADER.size < size <= frames.HEADER.size + frames.PANEL_MAX,
                "observer isolation FAIL invalid frame record length")
        received.append(decode_record(exact(size)))
    return received


def bootstrap(url):
    target = urlsplit(url)
    conn = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
    try:
        conn.request("POST", "/session", body=json.dumps({"token": target.fragment.removeprefix("rf-token=")}),
                     headers={"Origin": f"http://{target.netloc}", "Content-Type": "application/json"})
        reply = conn.getresponse()
        cookie = reply.getheader("Set-Cookie")
        require(reply.status == 200 and cookie, "observer isolation FAIL browser login")
        return target, cookie.split(";")[0], json.loads(reply.read())["proof"]
    finally:
        conn.close()


def browser_visit(url):
    """A browser observer: exchange its token, upgrade, subscribe, receive a row, and leave."""
    target, cookie, proof = bootstrap(url)
    with socket.create_connection((target.hostname, target.port), timeout=10) as client:
        key = base64.b64encode(b"k" * 16).decode()
        client.sendall((f"GET /ws HTTP/1.1\r\nHost: {target.netloc}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n"
                        f"Sec-WebSocket-Protocol: rf-vr-v1, rf-vr-proof.{proof}\r\n"
                        f"Origin: http://{target.netloc}\r\nCookie: {cookie.split(';')[0]}\r\n\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data:
            part = client.recv(65536)
            require(part and len(data) < 65536, "observer isolation FAIL browser upgrade missing")
            data += part
        head, _, data = data.partition(b"\r\n\r\n")
        require(head.startswith(b"HTTP/1.1 101 "), "observer isolation FAIL browser upgrade refused")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        require(b"sec-websocket-accept: " + accept.lower() in head.lower(),
                "observer isolation FAIL browser upgrade accept")
        client.sendall(webgw.client_frame(1, b"s1", b"mask"))
        buffer = bytearray(data)
        hello = screen = False
        deadline = time.monotonic() + 10
        while not (hello and screen):
            require(time.monotonic() < deadline, "observer isolation FAIL browser observation timed out")
            messages = webgw.parse_frames(buffer, False, 1 << 20)
            require(messages is not None, "observer isolation FAIL malformed browser frame")
            for opcode, payload in messages:
                if opcode == 1 and (value := json.loads(payload)).get("hello") == 1:
                    require(value["control"] is False, "observer isolation FAIL browser has control")
                    hello = True
                elif opcode == 2 and payload.startswith(b"S"):
                    screen = True
            if not (hello and screen):
                part = client.recv(65536)
                require(part, "observer isolation FAIL browser closed before observing")
                buffer.extend(part)
        client.sendall(webgw.client_frame(8, struct.pack("!H", 1000), b"mask"))
    return hello, screen


def console_visit(url):
    """The read-only console (tools/console.py) as a browser sees it: log in, read its state and radio 1's frame."""
    target, cookie, proof = bootstrap(url)
    host = target.netloc

    def get(path, cookie=None):
        conn = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
        try:
            extra = {"Cookie": cookie, "X-RadioFoundry-Session": proof} if cookie else {}
            conn.request("GET", path, headers={"Host": host, **extra})
            reply = conn.getresponse()
            return reply.status, reply.getheader("Set-Cookie"), reply.read()
        finally:
            conn.close()

    deadline = time.monotonic() + 10
    while True:
        status, _, body = get("/api/state?after=0", cookie)
        require(status == 200, "observer isolation FAIL console state")
        state = json.loads(body)
        kinds = {event["event"] for event in state["events"]}
        if {"input", "state"} <= kinds and "1" in state["frames"]:
            break
        require(time.monotonic() < deadline, "observer isolation FAIL console saw no inputs or frames")
        time.sleep(0.05)
    status, _, body = get("/api/frame/1", cookie)
    require(status == 200 and len(body) > frames.HEADER.size, "observer isolation FAIL console frame")
    return len(state["events"]), state["frames"]["1"]["frame_id"]


def broken_observer_hook(mux, session):
    """Deliberately break cache isolation for exactly one GET_VIEW; never installed outside this check."""
    original = mux._request

    def forward(owner, cmd, seq, payload):
        require(owner is session and owner.role == "observer" and cmd == dlcache.GET_VIEW,
                "observer isolation FAIL invalid broken-observer hook")
        mux._link(owner.radio).queue.append(dlmux.Request(owner, cmd, seq, payload))
        mux._pump(owner.radio, internal=False)

    mux._request = forward
    try:
        mux.feed(session, dlmux.frame(dlcache.GET_VIEW, 17, b"\0"))
    finally:
        mux._request = original


def run(name, executable, runtime, *, observers=0, gateway=False, browsers=0, screenshot=False, broken=False,
        console=False):
    world = World(scenario_config(SCENARIO, executable=executable))
    clients, readers, visits = [], [], []
    slow_ready, fast_ready = threading.Event(), threading.Event()
    fast_ready.set()
    pool = ThreadPoolExecutor(max_workers=10)
    trace, accepted = bytearray(), []
    session = None
    viewer = None
    try:
        # These are the P2 parts under test. All runs install the same parts before boot.
        audit.install(world)
        dlcache.install(world, cadence_us=dlcache.scenario_cadence(SCENARIO))
        dlmux.install(world)
        frames.install(world)
        input.install(world)
        recorder.install(world)
        observe_api.install(world)      # the console's read-only world API, installed in every run alike
        webgw.install(world, enabled=gateway)
        world.on_outbox(Kind.TRACE, lambda radio, message: trace.extend(message.payload + b"\n"))
        world.on_input(accepted.append)
        world.add_grant_limit(lambda now: now + 1000)  # Same grant boundaries in every run.
        world.api.bind(name, runtime)
        if not Path(executable).is_file():
            raise FileNotFoundError(f"{executable}; run make -j8 BOARD=vhost")
        world.frames.bind()
        for index in range(observers):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            clients.append(client)
            client.settimeout(10)
            client.connect(str(world.frames.path))
            readers.append(pool.submit(read_frames, client, slow_ready if observers == 8 and index == 7
                                       else fast_ready))
        url = None
        if console:
            viewer = subprocess.Popen([sys.executable, str(ROOT / "tools/console.py"), "--world", name, "--runtime",
                                       runtime, "--no-browser"], cwd=ROOT, stdout=subprocess.PIPE, text=True)
            line = viewer.stdout.readline().strip()
            require(line.startswith("console: http://127.0.0.1:"), f"observer isolation FAIL console did not start: {line!r}")
            url = line.split(" ", 1)[1]
        deadline = time.monotonic() + 10
        while len(world.frames.clients) != observers + int(console) or console and not world.api.live:
            if time.monotonic() >= deadline:
                raise TimeoutError("frame observers did not attach before boot")
            world.api.poll(0.001)       # the console's world socket is accepted here
        world.start()
        world.run("max")
        lease = world.dlmux.leases.grant(1, "debug:observe-check")
        for key in json.loads(SCENARIO.read_text())["keys"]:
            reply = world.api.dispatch({"version": 1, "command": "key", **lease, **key})
            require(reply["ok"], f"observer isolation FAIL keypad request: {reply.get('error')}")
        if screenshot:
            session = world.dlmux.connect(1, "debug:observe-check", "controller", lease["generation"], lease["secret"])
        elif broken:
            session = world.dlmux.connect(1, "debug:broken-observer", "observer")
        deadline = time.monotonic() + 60
        while world.state != "Ended":
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{name} did not finish at virtual_us={world.now}")
            if world.now == REQUEST_US and session is not None:
                if screenshot:
                    world.dlmux.feed(session, dlmux.frame(0x05, 17, struct.pack("<IB", 0, 3)))
                else:
                    broken_observer_hook(world.dlmux, session)
            if console and not visits and world.now >= 3_000_000:
                visits.append(pool.submit(console_visit, url))
            if browsers and not visits and world.now >= 3_000_000:
                for _ in range(browsers):
                    token = world.api.dispatch({"version": 1, "command": "webgw.token", "radio": 1})
                    require(token["ok"], "observer isolation FAIL browser token")
                    visits.append(pool.submit(browser_visit, token["result"]["url"]))
            world.api.poll(0)
            # Browser I/O may take wall time, but never changes the fixed virtual grant schedule.
            if visits and world.now >= 4_500_000 and any(not visit.done() for visit in visits):
                time.sleep(0.001)
                continue
            world.advance()
        if world.reason != "WORLD_END_TIME":
            raise WorldError(world.reason)
        slow_ready.set()
        checkpoints = list(world.frames.checkpoints)
        require(trace and checkpoints, f"observer isolation FAIL {name}: empty run")
        for index, reader in enumerate(readers):
            received = reader.result(timeout=15)
            if observers == 8 and index == 7:
                subsequence(checkpoints, received)
                print(f"observer isolation slow observer: PASS subsequence={len(received)}/{len(checkpoints)} "
                      f"final_us={received[-1].time_us}", flush=True)
            else:
                require(received == checkpoints, f"observer isolation FAIL {name} observer {index}: frames differ")
                print(f"observer isolation {name} fast_observer={index} checkpoint_sha256={checkpoint_hash(received)}", flush=True)
        for visit in visits:
            if console:
                events, frame_id = visit.result(timeout=15)
                print(f"observer isolation console: PASS attached, events={events} radio1_frame={frame_id}", flush=True)
            else:
                require(visit.result(timeout=15) == (True, True), "observer isolation FAIL browser did not observe")
        if browsers:
            print(f"observer isolation browsers: PASS attached/subscribed/left={len(visits)}", flush=True)
        if session is not None:
            requests = [e for e in accepted if e.kind == Kind.BYTES_IN and e.source == session.source]
            require(len(requests) == 1 and requests[0].time_us == REQUEST_US,
                    "observer isolation/screenshot control FAIL controller/test-hook request not delivered once at 1990000 us")
            print(f"{'screenshot control SCREENSHOT' if screenshot else 'observer isolation test_hook GET_VIEW'} "
                  f"input_us={requests[0].time_us}", flush=True)
        return {"trace": bytes(trace), "checkpoints": checkpoints,
                "reply": session.take() if session is not None else b""}
    finally:
        slow_ready.set()
        if hasattr(world, "frames"):
            world.frames.close()
        for client in clients:
            client.close()
        pool.shutdown(wait=True, cancel_futures=True)
        world.close()
        if viewer is not None:
            try:
                viewer.wait(timeout=10)     # the console exits by itself when the world ends
            except subprocess.TimeoutExpired:
                viewer.kill()
                viewer.wait()
                raise CheckFailure("observer isolation FAIL console did not exit after the world ended")


def picture(checkpoint):
    return checkpoint.crc, checkpoint.palette


def key_frame(checkpoints):
    before = [c for c in checkpoints if c.time_us < KEY_US]
    require(before, "screenshot control FAIL missing pre-key frame")
    after = [c for c in checkpoints if c.time_us >= KEY_US and picture(c) != picture(before[-1])]
    require(after, "screenshot control FAIL key did not change the panel")
    return after[0]


def screenshot_bound(reply):
    replies = dlmux.Parser(dlmux.REPLY_MAX).feed(reply)
    require(len(replies) == 1 and replies[0][:3] == ("frame", 0x85, 17),
            "screenshot control FAIL missing SCREENSHOT reply")
    payload = replies[0][3]
    require(len(payload) >= SHOT.size and payload[0] == 0, "screenshot control FAIL SCREENSHOT refused/truncated")
    status, width, height, bpp, fmt, top, bottom, left, right, total, offset, size, count, flags = SHOT.unpack_from(payload)
    header = SHOT.size + (32 if bpp == 4 else 0)
    chunk = len(payload) - header
    require(width > 0 and height > 0 and bpp in (1, 4, 16) and fmt == 3 and offset == 0 and
            size == ((width * bpp + 7) // 8) * height and 0 < chunk <= total and flags == 0,
            "screenshot control FAIL invalid/in-progress packed SCREENSHOT")
    last = chunk == total
    return REQUEST_US + (250_000 if last else 1_000_000), last


def negative_control(reference, candidate):
    base, held = key_frame(reference["checkpoints"]), key_frame(candidate["checkpoints"])
    bound, last = screenshot_bound(candidate["reply"])
    print(f"screenshot control key_frame_us base={base.time_us} screenshot={held.time_us} "
          f"bound={bound} reply_ends_stream={str(last).lower()}", flush=True)
    require(KEY_US <= base.time_us <= KEY_US + 100_000, "screenshot control FAIL base key frame not near 2020 ms")
    require(picture(base) == picture(held), "screenshot control FAIL delayed frame is not the key's panel")
    require(held.time_us >= bound, "screenshot control FAIL screenshot did not delay the key frame to the bound")


def self_test():
    def rejected(label, fn):
        try:
            fn()
        except CheckFailure as error:
            print(f"self-test {label}: rejected: {error}")
        else:
            raise CheckFailure(f"self-test FAIL accepted {label}")

    first = frames.Checkpoint(1, 1, 1, 1_500_000, zlib.crc32(b"a"), (), 75)
    key = replace(first, frame_id=2, time_us=2_030_000, crc=zlib.crc32(b"b"))
    end = replace(key, frame_id=3, time_us=4_000_000)
    reference = {"trace": b"trace\n", "checkpoints": [first, key, end]}
    compare(reference, reference, "same")
    subsequence(reference["checkpoints"], [first, end])
    for label, received in (("empty slow stream", []), ("missing final", [first]),
                            ("reordered slow stream", [key, first, end]), ("duplicate frame", [first, first, end])):
        rejected(label, lambda received=received: subsequence(reference["checkpoints"], received))
    rejected("empty baseline", lambda: compare({"trace": b"", "checkpoints": []}, reference, "empty"))
    rejected("changed checkpoint", lambda: compare(reference, {**reference, "checkpoints": [first, end]}, "changed"))
    record = frames.HEADER.pack(1, 1, 1, first.time_us, first.crc, 1, 0, 8, 1, 1, 75, 0, 0, *([0] * 16)) + b"a"
    require(decode_record(record) == first, "self-test FAIL frame decoding")
    rejected("corrupt panel", lambda: decode_record(record[:-1] + b"b"))
    rejected("truncated frame", lambda: decode_record(record[:10]))

    # Exercise the real multiplexer hook without a socket or a firmware stand-in claiming live proof.
    world = World()
    world.radios[1] = SimpleNamespace(boot=1, channel=SimpleNamespace(close=lambda: None))
    world.boots[1], world.state, world.now = 1, "Running", REQUEST_US
    accepted = []
    world.on_input(accepted.append)
    mux = dlmux.get(world)
    session = mux.connect(1, "debug:self-test", "observer")
    try:
        mux.feed(session, dlmux.frame(dlcache.GET_VIEW, 16, b"\0"))
        require(not accepted, "self-test FAIL normal observer reached firmware")
        broken_observer_hook(mux, session)
        require(len(accepted) == 1 and accepted[0].kind == Kind.BYTES_IN and
                accepted[0].source == session.source and accepted[0].time_us == REQUEST_US and
                dlmux.Parser(64).feed(accepted[0].payload)[0][1] == dlcache.GET_VIEW,
                "self-test FAIL broken observer did not send GET_VIEW")
        rejected("broken compare (synthetic trace)", lambda: compare(
            reference, {**reference, "trace": reference["trace"] + accepted[0].payload}, "broken-observer"))
    finally:
        world.close()

    for last in (False, True):
        payload = SHOT.pack(0, 8, 1, 1, 3, 0, 0, 0, 0, 1 if last else 2, 0, 1, 1, 0) + b"x"
        reply = dlmux.frame(0x85, 17, payload)
        bound = REQUEST_US + (250_000 if last else 1_000_000)
        held = {"checkpoints": [first, replace(key, time_us=bound), end], "reply": reply}
        negative_control(reference, held)
        rejected("undelayed key", lambda: negative_control(reference, {**held, "checkpoints": reference["checkpoints"]}))
        rejected("one us before bound", lambda: negative_control(reference, {
            **held, "checkpoints": [first, replace(key, time_us=bound - 1), end]}))
    rejected("missing screenshot reply", lambda: screenshot_bound(b""))
    print("observe_check --self-test: PASS (socket-free checks; live observer isolation/screenshot control still required)")


def main():
    parser = argparse.ArgumentParser(description="observer isolation observer isolation and screenshot control screenshot negative control")
    parser.add_argument("--executable", default=Config().executable)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument("--broken-observer", action="store_true", help="inject one observer GET_VIEW; must exit 1")
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
            return 0
        with tempfile.TemporaryDirectory(prefix="rf-observe-") as runtime:
            base = run("none", args.executable, runtime)
            one = run("one", args.executable, runtime, observers=1, broken=args.broken_observer)
            compare(base, one, "broken-observer" if args.broken_observer else "one")
            if args.broken_observer:
                print("observer isolation BROKEN_OBSERVER_NOT_DETECTED (expected exit 1)")
                return 0
            many = run("eight", args.executable, runtime, observers=8)
            compare(base, many, "eight")
            watched = run("console", args.executable, runtime, console=True)
            compare(base, watched, "console")
            print(f"observer isolation gateway=off runs=none,one,eight,console "
                  f"trace_sha256={hashlib.sha256(base['trace']).hexdigest()} "
                  f"checkpoint_sha256={checkpoint_hash(base['checkpoints'])}", flush=True)
            web = run("web-none", args.executable, runtime, gateway=True)
            watched = run("web-two", args.executable, runtime, gateway=True, browsers=2)
            compare(web, watched, "two browsers")
            print(f"observer isolation gateway=on runs=none,two-browsers trace_sha256={hashlib.sha256(web['trace']).hexdigest()} "
                  f"checkpoint_sha256={checkpoint_hash(web['checkpoints'])}", flush=True)
            negative_control(base, run("screenshot", args.executable, runtime, screenshot=True))
            print("observe_check: PASS observer isolation and screenshot control")
            return 0
    except CheckFailure as error:
        print(str(error), flush=True)
        return 1
    except (OSError, ValueError, ChannelError, WorldError) as error:
        print(f"observe_check: INCOMPLETE: {type(error).__name__}: {error}", flush=True)
        return 2


raise SystemExit(main())
PY
