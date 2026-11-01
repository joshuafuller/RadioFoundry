# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The web gateway (webgw.py) and its IP peer (tcppeer.py), mostly against the real vhost firmware.

The browser side of each live test is a plain socket client in a thread. It touches only its sockets; the
world (and so the gateway) runs on the test's own thread, as in the conductor. Run with -s to see the evidence.
"""

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time

import pytest

from tools.conductor.channel import Kind
from tools.conductor.conductor import install_parts
from tools.conductor.parts import audit, dlcache, dlmux, recorder, tcppeer, webgw
from tools.conductor.world import Config, World

CORS = b"access-control-allow-origin"


# --- a browser, in plain sockets ---

def http(port, target, host=None, headers=(), keep=None, method="GET", body=b""):
    """One request; returns (status, head bytes, body). keep, a list, collects every head."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    lines = [f"{method} {target} HTTP/1.1", f"Host: {host or f'127.0.0.1:{port}'}", *headers, "", ""]
    sock.sendall("\r\n".join(lines).encode() + body)
    data = b""
    while chunk := sock.recv(65536):
        data += chunk
    sock.close()
    head, _, body = data.partition(b"\r\n\r\n")
    if keep is not None:
        keep.append(head)
    return int(head.split(b" ")[1]), head, body


def mint(world):
    """On the world's thread: a one-time token URL from the world API."""
    reply = world.api.dispatch({"version": 1, "command": "webgw.token", "radio": 1})
    assert reply["ok"], reply
    return reply


class Cookie(str):
    def __new__(cls, value, proof):
        cookie = super().__new__(cls, value)
        cookie.proof = proof
        return cookie


def auth_headers(cookie):
    return [f"Cookie: {cookie}", f"X-RadioFoundry-Session: {cookie.proof}"]


def exchange(port, token, keep=None):
    body = json.dumps({"token": token}).encode()
    return http(port, "/session", method="POST", body=body, keep=keep,
                headers=[f"Origin: http://127.0.0.1:{port}", "Content-Type: application/json",
                         f"Content-Length: {len(body)}"])


def login(reply, port, keep=None):
    url = reply["result"]["url"]
    assert "/#rf-token=" in url and "?" not in url
    token = url.split("#rf-token=")[1]
    status, head, body = exchange(port, token, keep)
    assert status == 200
    cookie = next(line for line in head.split(b"\r\n") if line.startswith(b"Set-Cookie: "))
    assert b"HttpOnly" in cookie and b"SameSite=Strict" in cookie and b"Domain=" not in cookie
    value = cookie[len(b"Set-Cookie: "):].split(b";")[0].decode()
    return reply["result"], Cookie(value, json.loads(body)["proof"]), token


class Ws:
    def __init__(self, port, cookie, origin=None, keep=None, proof=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        proof = getattr(cookie, "proof", "") if proof is None else proof
        self.sock.sendall((f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                           f"Sec-WebSocket-Protocol: rf-vr-v1, rf-vr-proof.{proof}\r\n"
                           f"Origin: {origin or f'http://127.0.0.1:{port}'}\r\nCookie: {cookie}\r\n\r\n").encode())
        self.buffer = b""
        while b"\r\n\r\n" not in self.buffer:
            self.buffer += self.sock.recv(65536)
        self.head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        if keep is not None:
            keep.append(self.head)
        self.frames = []

    def send(self, text, opcode=1):
        mask = os.urandom(4)
        data = text.encode()
        self.sock.sendall(bytes((0x80 | opcode, 0x80 | len(data))) + mask +
                          bytes(byte ^ mask[index & 3] for index, byte in enumerate(data)))

    def next(self, timeout=10):
        end = time.monotonic() + timeout
        while True:
            if len(self.buffer) >= 2:
                size, at = self.buffer[1] & 0x7F, 2
                if size == 126:
                    size, at = struct.unpack_from("!H", self.buffer, 2)[0], 4
                elif size == 127:
                    size, at = struct.unpack_from("!Q", self.buffer, 2)[0], 10
                if len(self.buffer) >= at + size:
                    frame = (self.buffer[0] & 15, self.buffer[at:at + size])
                    self.buffer = self.buffer[at + size:]
                    self.frames.append(frame)
                    return frame
            self.sock.settimeout(max(0.01, end - time.monotonic()))
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buffer += chunk

    def until(self, test, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            frame = self.next(end - time.monotonic())
            if frame is None or test(frame):
                return frame
        return None

    def close(self):
        self.sock.close()


def hello_of(frame):
    if frame is not None and frame[0] == 1 and b'"hello"' in frame[1]:
        return json.loads(frame[1])
    return None


# --- the world ---

def make_world(tmp_path, elf, name="webgw", end_us=120_000_000, **options):
    world = World(Config(executable=str(elf), end_us=end_us, **options))
    recorder.install(world)
    gateway = webgw.get(world, enabled=True)
    world.api.bind(name, tmp_path)
    world.audit_lines = []
    audit.get(world).add_sink(world.audit_lines.append)
    world.eth_in = []
    world.on_input(lambda event: event.kind == Kind.ETH_IN and world.eth_in.append(event))
    return world, gateway


def step(world):
    world.api.poll(0)
    if world.state == "Running":
        world.advance()


def run_until(world, test, wall_s=60):
    end = time.monotonic() + wall_s
    while not test():
        assert world.state != "Ended" and time.monotonic() < end, world.reason
        step(world)


def pump(world, fn, *args, wall_s=60):
    """fn(*args) in a thread (sockets only) while this thread drives the world."""
    result, failure = [], []
    thread = threading.Thread(target=lambda: _call(fn, args, result, failure))
    thread.start()
    end = time.monotonic() + wall_s
    while thread.is_alive():
        assert time.monotonic() < end, "browser thread hung"
        step(world)
    thread.join()
    if failure:
        raise failure[0]
    return result[0]


def _call(fn, args, result, failure):
    try:
        result.append(fn(*args))
    except BaseException as error:      # handed to the test thread
        failure.append(error)


@pytest.fixture
def live(tmp_path, vhost_elf):
    world, gateway = make_world(tmp_path, vhost_elf)
    try:
        world.start()
        world.run("max")
        run_until(world, lambda: gateway._link(1) is not None and gateway._link(1).state == "up"
                  and gateway._link(1).status is not None and len(gateway._link(1).screen.rows) == 128)
        yield world, gateway, gateway.servers[1].port
    finally:
        world.close()


def audits(world, reason=None, kind=None):
    return [line for line in world.audit_lines if (reason is None or line["reason"] == reason)
            and (kind is None or line["kind"] == kind)]


# --- acceptance: sessions ---

def test_cookie_alone_cannot_read_or_upgrade(live):
    world, gateway, port = live
    token = mint(world)
    before = len(gateway.forwarded)

    def attacker():
        _, cookie, _ = login(token, port)
        status = http(port, "/status", headers=[f"Cookie: {cookie}"])[0]
        ws = Ws(port, str(cookie))
        code = int(ws.head.split(b" ")[1])
        ws.close()
        return status, code

    assert pump(world, attacker) == (403, 403)
    assert len(gateway.forwarded) == before


def test_bootstrap_and_proof_boundaries(live):
    world, gateway, port = live
    first, second = mint(world), mint(world)

    def browser():
        token = first["result"]["url"].split("#rf-token=")[1]
        body = json.dumps({"token": token}).encode()
        good = [f"Origin: http://127.0.0.1:{port}", "Content-Type: application/json",
                f"Content-Length: {len(body)}"]
        assert http(port, "/")[0] == 200
        for method, headers, data in [
                ("GET", good, body), ("POST", good[1:], body),
                ("POST", ["Origin: http://evil.example", *good[1:]], body),
                ("POST", good + ["Content-Length: 1"], body),
                ("POST", good + ["Transfer-Encoding: chunked"], body),
                ("POST", [good[0], "Content-Type: text/plain", good[2]], body),
                ("POST", [*good[:2], "Content-Length: 2048"], b"x" * 2048),
                ("POST", good + ["Sec-Fetch-Site: same-site"], body),
                ("POST", [*good[:2], "Content-Length: 2"], b"{}")]:
            assert http(port, "/session", method=method, headers=headers, body=data)[0] >= 400
        _, cookie, _ = login(first, port)
        _, other, _ = login(second, port)
        for headers in ([f"X-RadioFoundry-Session: {cookie.proof}"],
                        [f"Cookie: {cookie}", f"X-RadioFoundry-Session: {other.proof}"],
                        [*auth_headers(cookie), f"X-RadioFoundry-Session: {cookie.proof}"],
                        [f"Cookie: {cookie}; {cookie}", f"X-RadioFoundry-Session: {cookie.proof}"]):
            assert http(port, "/status", headers=headers)[0] >= 400
        for proof in ("", "x" * 43, other.proof):
            ws = Ws(port, cookie, proof=proof)
            assert ws.head.startswith(b"HTTP/1.1 403 ") and not ws.buffer
            ws.close()
        assert http(port, "/status", headers=auth_headers(cookie))[0] == 200
        ws = Ws(port, cookie)
        assert ws.head.startswith(b"HTTP/1.1 101 ")
        assert b"Sec-WebSocket-Protocol: rf-vr-v1\r\n" in ws.head + b"\r\n"
        assert cookie.proof.encode() not in ws.head
        ws.close()
        return cookie

    cookie = pump(world, browser)
    server = gateway.servers[1]
    for attribute, replacement in (("origin", "http://127.0.0.1:1"), ("radio", 2), ("browser_key", b"x" * 32)):
        original = getattr(server, attribute)
        setattr(server, attribute, replacement)
        try:
            assert pump(world, lambda: http(port, "/status", headers=auth_headers(cookie)))[0] == 403
        finally:
            setattr(server, attribute, original)


def test_bootstrap_token_is_bound_to_its_radio(tmp_path, vhost_elf):
    world, gateway = make_world(tmp_path, vhost_elf, radios=2)
    try:
        world.start()
        token = mint(world)["result"]["url"].split("#rf-token=")[1]
        assert pump(world, lambda: exchange(gateway.servers[2].port, token))[0] == 403
        assert pump(world, lambda: exchange(gateway.servers[1].port, token))[0] == 200
    finally:
        world.close()

def test_session_from_token_loads_page_and_upgrades_ws(live):
    world, gateway, port = live
    heads = []

    token = mint(world)

    def browser():
        session, cookie, target = login(token, port, heads)
        again = exchange(port, target, keep=heads)[0]              # the token works once
        page = http(port, "/", headers=[*auth_headers(cookie)], keep=heads)
        status = http(port, "/status", headers=[*auth_headers(cookie)], keep=heads)
        ws = Ws(port, cookie, keep=heads)
        hello = hello_of(ws.next())
        ws.send("s1")
        screen = ws.until(lambda frame: frame[0] == 2 and frame[1][:1] == b"S")
        ws.close()
        return session, again, page, status, ws.head, hello, screen

    session, again, page, status, ws_head, hello, screen = pump(world, browser)
    print(f"\nprincipal {session['principal']}; token again -> {again}")
    print(f"GET / -> {page[0]}, {len(page[2])} bytes, first two {page[2][:2].hex()}")
    print(page[1].decode())
    print(f"GET /status -> {status[0]}: {status[2][:60]!r}...")
    print(ws_head.decode().splitlines()[0], "| hello:", hello)
    print(f"screen frame: {len(screen[1])} bytes, {len(gateway._link(1).screen.rows)} rows known")
    assert again == 403
    assert page[0] == 200 and page[2][:2] == b"\x1f\x8b" and len(page[2]) == len(gateway._link(1).page[2])
    assert b"Content-Encoding: gzip" in page[1]
    assert status[0] == 200 and json.loads(status[2])["radio"] == "Virtual"
    assert ws_head.startswith(b"HTTP/1.1 101 ")
    assert hello["hello"] == 1 and hello["control"] is False and hello["audio"] is False
    assert screen is not None and len(gateway._link(1).screen.rows) == 128
    # No secret reaches the files: the token and the cookie are refused by value.
    files = (world.api.directory / "audit.jsonl").read_text() + (world.api.directory / "events.jsonl").read_text()
    assert session["url"].split("token=")[1] not in files
    assert not any(secret in files for secret in audit.get(world).secrets)
    assert audits(world, kind="webgw_session") and audits(world, "WEBGW_BAD_TOKEN")


def test_wrong_host_wrong_origin_and_no_session_are_refused_and_audited(live):
    world, gateway, port = live

    token = mint(world)

    def browser():
        _, cookie, _ = login(token, port)
        out = {"host": http(port, "/", host=f"rebind.example:{port}", headers=[*auth_headers(cookie)]),
               "localhost": http(port, "/", host=f"localhost:{port}", headers=[*auth_headers(cookie)]),
               "origin": http(port, "/", headers=[*auth_headers(cookie), "Origin: http://evil.example"])}
        for origin in ("http://evil.example", "null"):
            ws = Ws(port, cookie, origin=origin)
            out[f"ws origin {origin}"] = (int(ws.head.split(b" ")[1]), ws.head, b"")
            ws.close()
        out["ws no origin"] = http(port, "/ws", headers=[*auth_headers(cookie), "Upgrade: websocket",
                                                         "Connection: Upgrade", "Sec-WebSocket-Version: 13",
                                                         "Sec-WebSocket-Key: " + base64.b64encode(b"k" * 16).decode()])
        out["no session"] = http(port, "/status")
        out["no session ws"] = (int(Ws(port, "rfvr1=forged").head.split(b" ")[1]), b"", b"")
        return out

    before = len(world.audit_lines)
    out = pump(world, browser)
    lines = world.audit_lines[before:]
    for name, (status, _, _) in out.items():
        print(f"{name:28} -> {status}")
    for line in lines:
        if line["kind"] == "webgw_request":
            print(json.dumps({key: line[key] for key in ("kind", "principal", "radio", "result", "reason", "route")}))
    assert out["host"][0] == 421 and out["localhost"][0] == 421
    assert out["origin"][0] == 403 and out["ws origin http://evil.example"][0] == 403
    assert out["ws origin null"][0] == 403 and out["ws no origin"][0] == 403
    assert out["no session"][0] == 403 and out["no session ws"][0] == 403
    refused = [line["reason"] for line in lines if line["kind"] == "webgw_request" and line["result"] == "refused"]
    assert refused == ["WEBGW_BAD_HOST"] * 2 + ["WEBGW_BAD_ORIGIN"] * 4 + ["WEBGW_NO_SESSION"] * 2


def test_firmware_cors_header_is_in_no_reply(live):
    world, gateway, port = live
    heads = []

    token = mint(world)

    def browser():
        _, cookie, _ = login(token, port, heads)
        for target in ("/", "/status", "/api/screen.bmp", "/missing"):
            http(port, target, headers=[*auth_headers(cookie)], keep=heads)
        http(port, "/", host="evil.example", keep=heads)
        http(port, "/", keep=heads)
        Ws(port, cookie, keep=heads).close()
        Ws(port, cookie, origin="http://evil.example", keep=heads).close()

    pump(world, browser)
    firmware = gateway._link(1).page_head
    print("\nthe firmware's own GET / head:\n" + firmware.decode())
    print("the gateway's statuses: " + ", ".join(head.split(b"\r\n")[0].split(b" ", 1)[1].decode() for head in heads))
    assert b"Access-Control-Allow-Origin: *" in firmware
    assert len(heads) == 9 and not any(CORS in head.lower() for head in heads)


# --- acceptance: the page's own files ---

def test_asset_paths_are_what_the_radio_serves_or_its_404(live):
    """/keys.json, /radio.svg and /mic.svg come from the radio's own web control (fetched by the link, cached).
    Whatever the radio answered is what a browser gets: its bytes with a 200, or a 404 when the board has no such file
    (the vhost board has neither a key enum header nor a drawing: boards/vhost, tools/gen_webctl_assets.py)."""
    world, gateway, port = live
    link = gateway._link(1)
    token = mint(world)

    def browser():
        _, cookie, _ = login(token, port)
        return {path: http(port, path, headers=[*auth_headers(cookie)]) for path in webgw.ASSET_PATHS}

    replies = pump(world, browser)
    assert sorted(link.assets) == sorted(webgw.ASSET_PATHS)       # all asked of the radio before its /ws opened
    for path, (status, head, body) in replies.items():
        radio = link.assets[path]
        print(f"{path}: radio {'404' if radio is None else f'{len(radio[2])} bytes'}; gateway {status}, {len(body)} bytes")
        assert CORS not in head.lower()
        if radio is None:
            assert status == 404
        else:
            assert status == 200 and hashlib.sha256(body).digest() == hashlib.sha256(radio[2]).digest()


def test_asset_replies_are_the_cached_radio_bytes_with_their_type_and_coding(live):
    world, gateway, port = live
    link = gateway._link(1)
    link.assets.update({"/keys.json": ("application/json", None, b'{"select":0}'),
                        "/radio.svg": ("image/svg+xml", "gzip", b"\x1f\x8bdrawing"), "/mic.svg": None})
    token = mint(world)

    def browser():
        _, cookie, _ = login(token, port)
        got = {path: http(port, path, headers=[*auth_headers(cookie)]) for path in webgw.ASSET_PATHS}
        link.assets.pop("/keys.json")                             # not fetched yet: try again soon, not a 404
        return got, http(port, "/keys.json", headers=[*auth_headers(cookie)])

    replies, unfetched = pump(world, browser)
    assert unfetched[0] == 503 and b"Retry-After: 1" in unfetched[1]
    assert replies["/keys.json"][0] == 200 and replies["/keys.json"][2] == b'{"select":0}'
    assert b"Content-Type: application/json" in replies["/keys.json"][1]
    assert replies["/radio.svg"][2] == b"\x1f\x8bdrawing" and b"Content-Encoding: gzip" in replies["/radio.svg"][1]
    assert replies["/mic.svg"][0] == 404
    assert not any(CORS in head.lower() for _, head, _ in replies.values())


def test_asset_paths_keep_the_host_origin_and_session_rules(live):
    world, gateway, port = live
    token = mint(world)

    def browser():
        _, cookie, _ = login(token, port)
        out = []
        for path in webgw.ASSET_PATHS:
            out.append(http(port, path)[0])                                            # no session
            out.append(http(port, path, host="evil.example", headers=[*auth_headers(cookie)])[0])
            out.append(http(port, path, headers=[*auth_headers(cookie), "Origin: http://evil.example"])[0])
        return out

    assert pump(world, browser) == [403, 421, 403] * 3
    assert len(audits(world, "WEBGW_NO_SESSION")) >= 3 and len(audits(world, "WEBGW_BAD_HOST")) >= 3
    assert len(audits(world, "WEBGW_BAD_ORIGIN")) >= 3


# --- acceptance: lease and holds ---

def two_sessions(tokens, port):
    a, cookie_a, _ = login(tokens[0], port)
    b, cookie_b, _ = login(tokens[1], port)
    first, second = Ws(port, cookie_a), Ws(port, cookie_b)
    assert hello_of(first.next())["control"] is False and hello_of(second.next())["control"] is False
    return a["principal"], b["principal"], first, second


def grant(world, principal):
    return world.api.dispatch({"version": 1, "command": "lease.grant", "radio": 1, "principal": principal})


def sent_to_radio(gateway, text):
    """Has the radio's TCP taken every byte of the latest forwarded text? (snd_una past the queue)"""
    link = gateway._link(1)
    return link.ws is not None and not link.ws.unsent and not link.ws.flight


def test_second_session_d3_is_dropped_while_the_first_holds_the_lease(live):
    world, gateway, port = live
    a, b, first, second = pump(world, two_sessions, (mint(world), mint(world)), port)
    granted = grant(world, a)
    assert granted["ok"], granted
    hello = pump(world, lambda: hello_of(first.until(lambda frame: hello_of(frame) is not None)))
    assert hello["control"] is True
    before = len(gateway.forwarded)
    refused_before = len(audits(world, "LEASE_NOT_HOLDER"))
    pump(world, second.send, "d3")
    run_until(world, lambda: len(audits(world, "LEASE_NOT_HOLDER")) > refused_before)
    pump(world, first.send, "d3")
    run_until(world, lambda: len(gateway.forwarded) > before)
    run_until(world, lambda: sent_to_radio(gateway, "d3"))
    refused = audits(world, "LEASE_NOT_HOLDER")[-1]
    print(f"\n{b} d3: {json.dumps({k: refused[k] for k in ('kind', 'principal', 'result', 'reason', 'command')})}")
    print(f"forwarded to the radio: {gateway.forwarded[before:]}")
    assert refused["principal"] == b and refused["command"] == "web.d"
    assert [entry[1:3] for entry in gateway.forwarded[before:]] == [(a, "d3")]
    assert [hold.principal for hold in world.holds.tracked(1)] == [a]
    first.close()
    second.close()


def test_revoke_releases_a_held_key_with_a_u_frame_before_the_successor(live):
    world, gateway, port = live
    a, b, first, second = pump(world, two_sessions, (mint(world), mint(world)), port)
    assert grant(world, a)["ok"]
    pump(world, lambda: first.until(lambda frame: hello_of(frame) is not None))
    pump(world, first.send, "d3")
    run_until(world, lambda: world.holds.tracked(1) and sent_to_radio(gateway, "d3"))
    eth_before = len(world.eth_in)
    revoked = world.api.dispatch({"version": 1, "command": "lease.revoke", "radio": 1})
    release = next(line for line in reversed(world.audit_lines) if line["kind"] == "hold_release")
    early = grant(world, b)
    print(f"\nrevoke: {revoked['result']}")
    print(f"release: {json.dumps({k: release[k] for k in ('principal', 'result', 'hold', 'sequence')})}")
    print(f"successor at once: {early}")
    assert revoked["ok"] and revoked["result"]["pending"] is True
    assert release["result"] == "sent" and release["hold"] == "key:3"
    assert gateway.forwarded[-1][1:3] == (a, "u3")
    # The u3 frame went in as an ETH_IN input with the release's own sequence number.
    assert release["sequence"] in [event.sequence for event in world.eth_in[eth_before:]]
    assert early == {"version": 1, "id": None, "ok": False, "error": "LEASE_RELEASE_PENDING"}
    closed = pump(world, lambda: first.until(lambda frame: frame[0] == 8))
    assert closed is not None and closed[1] == struct.pack("!H", 1000)     # the holder's page is closed
    run_until(world, lambda: not world.holds.unconfirmed(1))
    confirmed = next(line for line in reversed(world.audit_lines) if line["kind"] == "hold_confirmed")
    late = grant(world, b)
    print(f"confirmed: {json.dumps({k: confirmed[k] for k in ('result', 'hold', 'seen_us')})}; successor: {late}")
    assert confirmed["result"] == "confirmed" and late["ok"]
    order = [line["kind"] for line in world.audit_lines if line["kind"] in
             ("hold_release", "hold_confirmed") or line["kind"] == "lease_grant" and line["principal"] == b
             and line["result"] == "accepted"]
    assert order[-3:] == ["hold_release", "hold_confirmed", "lease_grant"]
    first.close()
    second.close()


def test_a_page_s_own_key_up_is_still_confirmed_before_a_successor(live):
    world, gateway, port = live
    a, b, first, second = pump(world, two_sessions, (mint(world), mint(world)), port)
    assert grant(world, a)["ok"]
    pump(world, lambda: first.until(lambda frame: hello_of(frame) is not None))
    pump(world, first.send, "d3")
    run_until(world, lambda: world.holds.tracked(1) and sent_to_radio(gateway, "d3"))
    pump(world, first.send, "u3")
    run_until(world, lambda: gateway.forwarded[-1][2] == "u3" and sent_to_radio(gateway, "u3"))
    assert [hold.name for hold in world.holds.tracked(1)] == ["key:3"]     # not forgotten on the page's word
    revoked = world.api.dispatch({"version": 1, "command": "lease.revoke", "radio": 1})
    assert revoked["result"]["pending"] is True and grant(world, b)["error"] == "LEASE_RELEASE_PENDING"
    run_until(world, lambda: not world.holds.unconfirmed(1))
    assert grant(world, b)["ok"]
    first.close()
    second.close()


# --- acceptance: trace hash ---

def run_scenario(tmp_path, elf, name, browsers):
    world, gateway = make_world(tmp_path, elf, name, end_us=8_000_000)
    digest = hashlib.sha256()
    lines = []
    world.on_outbox(Kind.TRACE, lambda radio, message: (digest.update(message.payload + b"\n"),
                                                         lines.append(message.payload)))
    seen = []

    def visit(token):
        port = gateway.servers[1].port
        _, cookie, _ = login(token, port)
        page = http(port, "/", headers=[*auth_headers(cookie)])
        ws = Ws(port, cookie)
        hello = hello_of(ws.next())
        ws.send("s1")
        screen = ws.until(lambda frame: frame[0] == 2)
        ws.send("d3")                   # no lease: dropped at the gateway
        status = ws.until(lambda frame: frame[0] == 1 and hello_of(frame) is None)
        ws.close()
        seen.append((page[0], hello["control"], screen is not None, status is not None))

    threads = []
    try:
        world.start()
        world.run("max")
        while world.state != "Ended":
            if browsers and not threads and world.now >= 3_000_000:
                threads = [threading.Thread(target=visit, args=(mint(world),)) for _ in range(browsers)]
                for thread in threads:
                    thread.start()
            busy = any(thread.is_alive() for thread in threads)
            world.api.poll(0)
            if busy and world.now >= 6_000_000:
                continue        # hold virtual time (no grant) until the browsers have left
            world.advance()
        assert world.reason == "WORLD_END_TIME"
    finally:
        for thread in threads:
            thread.join(10)
        world.close()
    return digest.hexdigest(), len(lines), seen, len(world.eth_in)


def test_trace_hash_is_the_same_with_no_browser_and_two(tmp_path, vhost_elf):
    (tmp_path / "a").mkdir(mode=0o700)
    (tmp_path / "b").mkdir(mode=0o700)
    alone, count_alone, _, eth_alone = run_scenario(tmp_path / "a", vhost_elf, "alone", 0)
    watched, count_watched, seen, eth_watched = run_scenario(tmp_path / "b", vhost_elf, "watched", 2)
    print(f"\nno browser:   {alone}  ({count_alone} TRACE lines, {eth_alone} ETH_IN inputs)")
    print(f"two browsers: {watched}  ({count_watched} TRACE lines, {eth_watched} ETH_IN inputs)")
    print(f"browsers saw (page status, control, screen, status): {seen}")
    assert seen == [(200, False, True, True)] * 2
    assert alone == watched and eth_alone == eth_watched


# --- acceptance: the debug link with the gateway up ---

def view_state(mux, session, seq):
    """The "state=" line of GET_VIEW 0, as an observer reads it from the cache."""
    mux.feed(session, dlmux.frame(dlcache.GET_VIEW, seq, b"\0"))
    reply = dlmux.Parser(dlmux.REPLY_MAX).feed(session.take())
    assert reply and reply[-1][1] == dlcache.GET_VIEW | 0x80, reply
    text = reply[-1][3]
    return next((line[6:].decode() for line in text.split(b"\n") if line.startswith(b"state=")), None)


def test_debug_link_view_follows_a_ptt_while_a_browser_is_attached(tmp_path, vhost_elf):
    """As in a vradio.py world: every part, the gateway on, a browser session with the lease. PTT from the page;
    the cached debug link view shows the call. Before the fix the radio sent its debug link replies over UDP to
    nobody once the USB port was a network link, so the cache stayed at its last UART view (state=idle)."""
    world = World(Config(executable=str(vhost_elf), end_us=60_000_000))
    install_parts(world)
    gateway = webgw.get(world, enabled=True)
    world.api.bind("dlgw", tmp_path)
    selected = []
    world.on_outbox(Kind.TRACE, lambda radio, message: b" BOARD net.select 1" in message.payload
                    and selected.append(message.time_us))
    udp = []
    # IPv4 (no options) UDP to port 3334, read by hand so the check stands without the code under test
    world.on_input(lambda event: event.kind == Kind.ETH_IN and event.payload[12:14] == b"\x08\x00"
                   and event.payload[23] == 17 and event.payload[36:38] == struct.pack("!H", 3334)
                   and udp.append(event.time_us))
    try:
        world.start()
        world.run("max")
        run_until(world, lambda: gateway._link(1) is not None and gateway._link(1).state == "up")
        port = gateway.servers[1].port
        token = mint(world)
        principal = token["result"]["principal"]
        ws = pump(world, lambda: Ws(port, login(token, port)[1]))
        assert pump(world, lambda: hello_of(ws.next()))["control"] is False
        assert grant(world, principal)["ok"]
        hello = pump(world, lambda: hello_of(ws.until(lambda frame: hello_of(frame) is not None)))
        assert hello["control"] is True and type(hello.get("ptt")) is int, hello
        mux = world.dlmux
        session = mux.connect(1, "debug:dl-gateway-test", "observer")
        before = view_state(mux, session, 1)
        pressed = world.now
        pump(world, ws.send, f"d{hello['ptt']}")
        run_until(world, lambda: world.now >= pressed + 4 * world.dlcache.cadence_us)
        during = view_state(mux, session, 2)
        pump(world, ws.send, f"u{hello['ptt']}")
        print(f"\nnet.select 1 at {selected} us; debug link over UDP: {len(udp)} requests, first at "
              f"{udp[:1]} us; view state before PTT {before!r}, during PTT {during!r}")
        assert before == "idle" and during in ("tx", "tx_denied")
        assert selected and udp and udp[0] > selected[0]
        ws.close()
    finally:
        world.close()


# --- the parts on their own ---

def test_screen_cache_keeps_the_latest_row_of_each_y():
    screen = webgw.Screen(4, 2, 4)
    # palette of 2 colors; row 0 as runs (4 pixels of color 1), row 1 raw (2 bytes at bpp 4)
    assert screen.apply(b"S\x02" + b"\x00\x00\xff\xff" + b"\x00\x00\x31" + b"\x01\x80\x12\x34")
    assert screen.apply(b"S\x02" + b"\x00\x00\xff\xff" + b"\x00\x00\x30")
    assert screen.snapshot() == [b"S\x02\x00\x00\xff\xff" + b"\x00\x00\x30" + b"\x01\x80\x12\x34"]
    assert not screen.apply(b"S\x02\x00\x00\xff\xff\x00\x00\x10")      # a run row short of the width
    assert not screen.apply(b"S\x02\x00\x00\xff\xff\x02\x00\x30")      # y 2: outside a 2-row panel


def test_client_frames_must_be_masked():
    frame = webgw.client_frame(1, b"d3", b"\x01\x02\x03\x04")
    assert webgw.parse_frames(bytearray(frame), True, 125) == [(1, b"d3")]
    assert webgw.parse_frames(bytearray(webgw.server_frame(1, b"d3")), True, 125) is None
    # A radio's oversized ping is a protocol error, never a pong we cannot build.
    assert webgw.parse_frames(bytearray(webgw.server_frame(9, b"x" * 126)), False, 1 << 16) is None
    assert webgw.parse_frames(bytearray(webgw.server_frame(3, b"")), False, 125) is None


def test_peer_ignores_malformed_dhcp_and_tcp_lengths():
    peer = tcppeer.Peer(b"test")
    peer.start_dhcp(0)
    peer.output(0)
    bootp = struct.pack("!BBBBI", 2, 1, 6, 0, peer.xid) + b"\0" * 20 + peer.mac + b"\0" * 202 + \
        struct.pack("!I", 0x63825363) + b"\x35\x00\xff"                     # option 53 with length 0
    segment = struct.pack("!HHHH", 67, 68, 8 + len(bootp), 0) + bootp
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(segment), 1, 0, 64, 17, 0, b"\xc0\xa8\x0a\x01",
                         b"\xff" * 4)
    header = header[:10] + struct.pack("!H", tcppeer.checksum(header)) + header[12:]
    peer.receive(peer.mac + b"\x02\x56\x48\x00\x00\x01\x08\x00" + header + segment, 1000)
    assert peer.dhcp == "selecting"
    # A TCP segment with data offset 0 never reaches a connection.
    peer.ip, peer.radio_ip = b"\xc0\xa8\x0a\x02", b"\xc0\xa8\x0a\x01"
    conn = peer.connect(80, 0)
    tcp = struct.pack("!HHIIBBHHH", 80, conn.sport, 1, conn.snd_nxt, 0, 0x12, 1000, 0, 0)
    pseudo = peer.radio_ip + peer.ip + struct.pack("!BBH", 0, 6, len(tcp))
    tcp = tcp[:16] + struct.pack("!H", tcppeer.checksum(pseudo + tcp)) + tcp[18:]
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 2, 0, 64, 6, 0, peer.radio_ip, peer.ip)
    header = header[:10] + struct.pack("!H", tcppeer.checksum(header)) + header[12:]
    bad = peer.stats["bad"]
    peer.receive(peer.mac + b"\x02\x56\x48\x00\x00\x01\x08\x00" + header + tcp, 2000)
    assert peer.stats["bad"] == bad + 1 and conn.state == "SYN_SENT"


def test_dhcp_renews_at_half_the_lease_and_restarts_after_a_nak():
    peer = tcppeer.Peer(b"test")
    peer.start_dhcp(0)
    discover = peer.output(0)
    assert len(discover) == 1 and discover[0][:6] == tcppeer.BROADCAST_MAC

    def reply(kind, lease=600):
        options = bytes((53, 1, kind, 54, 4, 192, 168, 10, 1)) + (b"\x33\x04" + struct.pack("!I", lease)
                                                                   if kind != 6 else b"") + b"\xff"
        bootp = struct.pack("!BBBBI", 2, 1, 6, 0, peer.xid) + b"\0" * 8 + bytes((192, 168, 10, 2)) + \
            bytes((192, 168, 10, 1)) + b"\0" * 4 + peer.mac + b"\0" * 202 + struct.pack("!I", 0x63825363) + options
        segment = struct.pack("!HHHH", 67, 68, 8 + len(bootp), 0) + bootp
        header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(segment), 1, 0, 64, 17, 0,
                             bytes((192, 168, 10, 1)), b"\xff" * 4)
        header = header[:10] + struct.pack("!H", tcppeer.checksum(header)) + header[12:]
        return peer.mac + b"\x02\x56\x48\x00\x00\x01" + b"\x08\x00" + header + segment

    bound = []
    peer.on_bound = lambda peer, now: bound.append(now)
    peer.receive(reply(2), 1000)
    assert len(peer.output(1000)) == 1 and peer.dhcp == "requesting"
    peer.receive(reply(5), 2000)
    assert bound == [2000] and tcppeer.dotted(peer.ip) == "192.168.10.2" and peer.next_due() == 2000 + 300_000_000
    renew = peer.output(300_002_000)
    assert len(renew) == 1 and renew[0][:6] == b"\x02\x56\x48\x00\x00\x01" and peer.dhcp == "renewing"
    peer.receive(reply(6), 300_003_000)
    assert peer.ip is None and peer.dhcp == "selecting"
