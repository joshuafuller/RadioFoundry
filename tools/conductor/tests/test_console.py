# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""tools/console.py: Host, Origin, cross-site, method and session checks, the read-only world client, frames."""

import http.client
import json
import os
import socket
import threading
import zlib

import pytest

from tools import console
from tools.conductor.tests.test_webgw import Cookie


def headers(gate, **extra):
    base = {"Host": gate.host}
    base.update({name.replace("_", "-"): value for name, value in extra.items()})
    return list(base.items())


def login(gate):
    url = gate.mint()
    assert "/#rf-token=" in url and "?" not in url
    token = url.split("#rf-token=")[1]
    verdict, body = gate.bootstrap({"token": token})
    assert verdict.code == 200
    cookie = next(h for h in verdict.headers if h.startswith("Set-Cookie: ")).split(": ", 1)[1]
    return token, Cookie(cookie.split(";")[0], body["proof"])


def test_cookie_alone_cannot_read_or_unlock():
    gate = console.Gate(4242, control=True)
    _, cookie = login(gate)
    for method, path in (("GET", "/api/state"), ("POST", "/api/unlock"), ("POST", "/api/control")):
        verdict = gate.check(method, path, headers(gate, Cookie=cookie,
                                                   Origin=gate.origin, Content_Type="application/json"))
        assert (verdict.code, verdict.reason) == (403, "CONSOLE_BAD_PROOF")


def test_proof_is_bound_to_session_origin_and_launch_key():
    gate = console.Gate(4242)
    _, cookie = login(gate)
    _, other = login(gate)
    for sid, proof in ((cookie, ""), (cookie, "x" * 43), (cookie, other.proof), (other, cookie.proof),
                       (cookie + "; " + cookie, cookie.proof)):
        assert gate.check("GET", "/api/state", headers(gate, Cookie=sid, X_RadioFoundry_Session=proof)).code == 403
    for attribute, replacement in (("origin", "http://127.0.0.1:4243"), ("browser_key", b"x" * 32)):
        original = getattr(gate, attribute)
        setattr(gate, attribute, replacement)
        assert gate.check("GET", "/api/state", headers(gate, Cookie=cookie,
                                                       X_RadioFoundry_Session=cookie.proof)).code == 403
        setattr(gate, attribute, original)
    replacement = console.Gate(4242)
    assert replacement.check("GET", "/api/state", headers(replacement, Cookie=cookie,
                                                          X_RadioFoundry_Session=cookie.proof)).code == 403
    assert gate.check("GET", "/api/state", headers(gate, Cookie=cookie,
                                                   X_RadioFoundry_Session=cookie.proof)).code == 200


def test_bootstrap_requires_strict_same_origin_post():
    gate = console.Gate(4242)
    valid = headers(gate, Origin=gate.origin, Content_Type="application/json", Content_Length="55")
    assert gate.check("POST", "/session", valid).code == 200
    for name, value in (("Host", "localhost:4242"), ("Origin", None), ("Origin", "http://127.0.0.1:4243"),
                        ("Content-Type", "text/plain"), ("Content-Length", None), ("Content-Length", "2048"),
                        ("Transfer-Encoding", "chunked"), ("Sec-Fetch-Site", "same-site")):
        items = [(key, old) for key, old in valid if key != name]
        if value is not None:
            items.append((name, value))
        assert gate.check("POST", "/session", items).code >= 400
    for item in valid:
        assert gate.check("POST", "/session", [*valid, item]).code >= 400
    assert gate.check("GET", "/session", valid).code == 405
    for body in (None, {"token": "bad"}, {"token": gate.mint().split("#rf-token=")[1], "extra": True}):
        assert gate.bootstrap(body)[0].code == 400


def test_token_is_single_use_and_becomes_a_strict_cookie():
    gate = console.Gate(4242)
    token, cookie = login(gate)
    assert cookie.startswith("rfconsole4242=")
    verdict, _ = gate.bootstrap({"token": gate.mint().split("#rf-token=")[1]})
    set_cookie = next(h for h in verdict.headers if h.startswith("Set-Cookie"))
    assert "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie and "Path=/" in set_cookie
    assert gate.bootstrap({"token": token})[0].reason == "CONSOLE_BAD_TOKEN"
    assert gate.bootstrap({"token": "x" * 43})[0].reason == "CONSOLE_BAD_TOKEN"
    assert gate.check("GET", "/", headers(gate, Cookie=cookie)).code == 200


@pytest.mark.parametrize("host", [None, "localhost:4242", "127.0.0.1", "127.0.0.1:4243", "evil.example:4242",
                                  "127.0.0.1:4242.evil.example"])
def test_host_must_be_exact(host):
    gate = console.Gate(4242)
    _, cookie = login(gate)
    items = [("Cookie", cookie)] + ([("Host", host)] if host else [])
    assert gate.check("GET", "/", items).code == 421


def test_duplicate_security_headers_are_refused():
    gate = console.Gate(4242)
    _, cookie = login(gate)
    for name in ("Host", "Origin", "Cookie", "X-RadioFoundry-Session"):
        value = {"Host": gate.host, "Origin": gate.origin, "Cookie": cookie, "X-RadioFoundry-Session": cookie.proof}[name]
        items = headers(gate, Cookie=cookie, X_RadioFoundry_Session=cookie.proof, Origin=gate.origin) + [(name, value)]
        assert gate.check("GET", "/api/state", items[:-1]).code == 200
        assert gate.check("GET", "/api/state", items).code == (403 if name == "X-RadioFoundry-Session" else 400)


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://localhost:4242", "http://127.0.0.1:4243",
                                    "https://127.0.0.1:4242"])
def test_foreign_origin_is_refused_even_with_a_session(origin):
    gate = console.Gate(4242)
    _, cookie = login(gate)
    verdict = gate.check("GET", "/api/state", headers(gate, Cookie=cookie, X_RadioFoundry_Session=cookie.proof, Origin=origin))
    assert (verdict.code, verdict.reason) == (403, "CONSOLE_BAD_ORIGIN")
    assert gate.check("GET", "/api/state", headers(gate, Cookie=cookie, X_RadioFoundry_Session=cookie.proof,
                                                    Origin=gate.origin)).code == 200


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_site_fetch_is_refused(site):
    gate = console.Gate(4242)
    _, cookie = login(gate)
    verdict = gate.check("GET", "/api/state", headers(gate, Cookie=cookie, X_RadioFoundry_Session=cookie.proof, Sec_Fetch_Site=site))
    assert verdict.reason == "CONSOLE_CROSS_SITE"


def test_session_and_api_header_are_required():
    gate = console.Gate(4242)
    assert gate.check("GET", "/", headers(gate)).code == 200
    assert gate.check("GET", "/console.js", headers(gate, Cookie="rfconsole4242=forged")).code == 200
    _, cookie = login(gate)
    other = console.Gate(4243)
    assert other.check("GET", "/api/state", [("Host", other.host), ("Cookie", cookie)]).reason == "CONSOLE_NO_SESSION"
    assert gate.check("GET", "/api/state", headers(gate, Cookie=cookie)).reason == "CONSOLE_BAD_PROOF"
    assert gate.check("GET", "/api/frame/1", headers(gate, Cookie=cookie, X_RadioFoundry_Session=cookie.proof)).code == 200
    assert gate.check("GET", "/../etc/passwd", headers(gate, Cookie=cookie, X_RadioFoundry_Session=cookie.proof)).code == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"])
def test_only_get(method):
    gate = console.Gate(4242)
    _, cookie = login(gate)
    verdict = gate.check(method, "/api/state", headers(gate, Cookie=cookie, X_RadioFoundry_Session=cookie.proof))
    assert (verdict.code, verdict.headers) == (405, ("Allow: GET",))


def test_session_count_is_bounded():
    gate = console.Gate(4242)
    for _ in range(console.SESSIONS_MAX):
        login(gate)
    token = gate.mint().split("token=")[1]
    assert gate.bootstrap({"token": token})[0].reason == "CONSOLE_BAD_TOKEN"


def test_read_only_world_client_refuses_control_before_sending(tmp_path):
    path = tmp_path / "world.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()
    client = console.ReadOnlyWorld(path, timeout=2)
    peer, _ = listener.accept()
    try:
        peer.settimeout(2)
        assert json.loads(peer.recv(4096)) == {"version": 1, "id": 0, "command": "subscribe", "events": []}
        for command in ("run", "pause", "end", "key", "fault", "air_loss", "lease.grant", "webgw.token",
                        "hil.create", "restart", "observe"):
            with pytest.raises(PermissionError, match="CONSOLE_READ_ONLY"):
                client.call(command)
        peer.setblocking(False)
        with pytest.raises(BlockingIOError):
            peer.recv(1)                                    # nothing reached the world
        peer.setblocking(True)
        reply = threading.Thread(target=lambda: (peer.recv(4096), peer.sendall(
            b'{"event":"state","state":"Running"}\n{"version":1,"id":1,"ok":true,"result":{"state":"Paused"}}\n')))
        reply.start()
        assert client.call("observe.status") == {"state": "Paused"}
        reply.join()
    finally:
        client.close()
        peer.close()
        listener.close()
    assert console.ALLOWED == ("status", "observe.status", "observe.events")


def frame_record(radio=1, frame_id=3, panel=b"\x80" + b"\0" * 15):
    header = console.HEADER.pack(radio, 1, frame_id, 1_000_000, zlib.crc32(panel), len(panel), 0, 16, 8, 1, 75, 0, 0,
                                 *([0] * 16))
    return console.LENGTH.pack(len(header) + len(panel)) + header + panel


def test_frames_keep_the_latest_panel_per_radio():
    model = console.Model("w")
    left, right = socket.socketpair()
    try:
        data = frame_record(frame_id=1) + frame_record(frame_id=2) + frame_record(radio=2, frame_id=9)
        right.sendall(data[:50])
        right.sendall(data[50:])
        right.close()
        console.follow_frames(left, model)
    finally:
        left.close()
    state = model.snapshot(0)
    assert state["frames"]["1"]["frame_id"] == 2 and state["frames"]["2"]["frame_id"] == 9
    assert model.frame(1) == frame_record(frame_id=2)[4:]


def test_live_server_applies_the_gate():
    model = console.Model("w")
    model.add_events([{"seq": 1, "event": "state", "state": "Running", "time_us": 0}])
    model.status = {"state": "Running", "time_us": 5}
    server = console.Server(model)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    port = server.server_address[1]
    assert server.server_address[0] == "127.0.0.1"

    def get(path, method="GET", body=None, **extra):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.putrequest(method, path, skip_host=True)
            for name, value in {"Host": f"127.0.0.1:{port}", **extra}.items():
                if value is not None:
                    conn.putheader(name.replace("_", "-"), value)
            conn.endheaders(body)
            reply = conn.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read()
        finally:
            conn.close()

    try:
        token = server.gate.mint().split("token=")[1]
        assert get("/", Host=f"localhost:{port}")[0] == 421
        assert get("/")[0] == 200
        data = json.dumps({"token": token}).encode()
        status, head, body = get("/session", method="POST", body=data, Origin=server.gate.origin,
                                 Content_Type="application/json", Content_Length=str(len(data)))
        assert status == 200
        cookie = Cookie(head["Set-Cookie"].split(";")[0], json.loads(body)["proof"])
        status, head, body = get("/", Cookie=cookie)
        assert status == 200 and b"Air timeline" in body
        assert "frame-ancestors 'none'" in head["Content-Security-Policy"] and head["Cache-Control"] == "no-store"
        assert get("/api/state", Cookie=cookie)[0] == 403
        status, _, body = get("/api/state?after=0", Cookie=cookie, X_RadioFoundry_Session=cookie.proof)
        state = json.loads(body)
        assert status == 200 and state["status"]["time_us"] == 5 and state["events"][0]["seq"] == 1
        assert json.loads(get("/api/state?after=1", Cookie=cookie, X_RadioFoundry_Session=cookie.proof)[2])["events"] == []
        assert get("/api/frame/1", Cookie=cookie, X_RadioFoundry_Session=cookie.proof)[0] == 404
        assert get("/api/state", Cookie=cookie, X_RadioFoundry_Session=cookie.proof, Origin="http://evil.example")[0] == 403
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/state", headers={"Cookie": cookie, "X-RadioFoundry-Session": cookie.proof})
        assert conn.getresponse().status == 405
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def test_world_folder_needs_a_running_world(tmp_path):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    with pytest.raises(console.ConsoleError, match="not running"):
        console.world_folder("nope", str(runtime))


def test_world_folder_defaults_to_the_launchers_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(console.tempfile, "gettempdir", lambda: str(tmp_path))
    directory = tmp_path / f"rfoundry-runtime-{os.getuid()}" / "rfoundry-vr" / "w1"
    directory.mkdir(parents=True)
    for path in (directory, directory.parent, directory.parent.parent):
        path.chmod(0o700)
    (directory / "world.sock").touch()
    assert console.world_folder("w1") == directory


# --- world control ---

class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def control_headers(gate, cookie, **extra):
    base = {"Cookie": cookie, "X_RadioFoundry_Session": cookie.proof, "Origin": gate.origin, "Content_Type": "application/json"}
    base.update(extra)
    return headers(gate, **{name: value for name, value in base.items() if value is not None})


def test_read_only_console_refuses_control_and_get_is_still_get_only():
    gate = console.Gate(4242)
    _, cookie = login(gate)
    verdict = gate.check("POST", "/api/control", control_headers(gate, cookie))
    assert (verdict.code, verdict.reason, verdict.route, verdict.session.label) == (403, "CONSOLE_READ_ONLY",
                                                                                    "control", "s1")
    with pytest.raises(console.ConsoleError):
        gate.mint_control()
    assert gate.check("GET", "/api/control", control_headers(gate, cookie)).headers == ("Allow: POST",)
    assert gate.check("POST", "/api/state", control_headers(gate, cookie)).headers == ("Allow: GET",)


@pytest.mark.parametrize("change, reason", [
    ({"Origin": None}, "CONSOLE_BAD_ORIGIN"), ({"Origin": "http://evil.example"}, "CONSOLE_BAD_ORIGIN"),
    ({"Cookie": None}, "CONSOLE_NO_SESSION"), ({"X_RadioFoundry_Session": None}, "CONSOLE_BAD_PROOF"),
    ({"Content_Type": "text/plain"}, "CONSOLE_BAD_CONTENT_TYPE"), ({"Sec_Fetch_Site": "cross-site"},
                                                                   "CONSOLE_CROSS_SITE"),
    ({"Host": "localhost:4242"}, "CONSOLE_BAD_HOST")])
def test_a_control_request_needs_every_check(change, reason):
    gate = console.Gate(4242, control=True)
    _, cookie = login(gate)
    assert gate.unlock(next(iter(gate.sessions.values())), gate.mint_control()) == "OK"
    assert gate.check("POST", "/api/control", control_headers(gate, cookie)).code == 200
    items = [item for item in control_headers(gate, cookie, **change) if item[0] != "Host" or "Host" not in change]
    if "Host" in change:
        items.append(("Host", change["Host"]))
    assert gate.check("POST", "/api/control", items).reason == reason


def test_capability_comes_only_from_the_one_time_code():
    gate = console.Gate(4242, control=True)
    _, first = login(gate)
    _, second = login(gate)
    one, two = gate.sessions.values()
    assert gate.check("POST", "/api/control", control_headers(gate, first)).reason == "CONSOLE_NO_CAPABILITY"
    code = gate.mint_control()
    assert gate.unlock(one, "guess") == "CONSOLE_BAD_CONTROL_CODE" and not one.control
    assert gate.unlock(one, code) == "OK" and one.control
    assert gate.unlock(two, code) == "CONSOLE_BAD_CONTROL_CODE" and not two.control       # it works once
    assert gate.check("POST", "/api/control", control_headers(gate, first)).code == 200
    assert gate.check("POST", "/api/control", control_headers(gate, second)).reason == "CONSOLE_NO_CAPABILITY"
    # The unlock route itself needs the session and the same request checks, but not the capability.
    assert gate.check("POST", "/api/unlock", control_headers(gate, second)).code == 200
    assert gate.check("POST", "/api/unlock", control_headers(gate, second, Origin=None)).reason == \
        "CONSOLE_BAD_ORIGIN"


def test_control_requests_are_rate_limited_per_session():
    clock = Clock()
    gate = console.Gate(4242, control=True, clock=clock)
    _, cookie = login(gate)
    _, other = login(gate)
    for session in gate.sessions.values():
        session.control = True
    codes = [gate.check("POST", "/api/control", control_headers(gate, cookie)).code for _ in range(12)]
    assert codes == [200] * console.RATE[1] + [429] * (12 - console.RATE[1])
    assert gate.check("POST", "/api/control", control_headers(gate, other)).code == 200
    clock.now += 1.0
    assert gate.check("POST", "/api/control", control_headers(gate, cookie)).code == 200


class FakeWorld:
    """Records what the controller sends; answers like the world API."""

    def __init__(self, state="Running", time_us=1_000_000, ratio=1, radios=(1, 2, 3), policy=None, fail=None):
        self.sent = []
        self.state, self.time_us, self.ratio, self.radios = state, time_us, ratio, radios
        self.policy, self.fail = policy or {"loss_ppm": []}, fail

    def request(self, command, **fields):
        assert command in console.CONTROL_ALLOWED
        self.sent.append({"command": command, **fields})
        if command == self.fail:
            return {"ok": False, "error": "WORLD_PAST_INPUT"}
        return {"ok": True, "result": self.call(command) if command in console.ALLOWED else {}}

    def call(self, command, **fields):
        if command == "status":
            return {"state": self.state, "time_us": self.time_us}
        return {"state": self.state, "time_us": self.time_us, "ratio": self.ratio, "policy": self.policy,
                "radios": [{"radio": radio, "on": True, "boot": 1} for radio in self.radios]}


def controller(world, control=True):
    gate = console.Gate(4242, control=control)
    login(gate)
    session = next(iter(gate.sessions.values()))
    return console.Controller(world, console.Model("w"), gate), session


def test_controls_become_ordinary_world_commands_with_the_session_actor():
    world = FakeWorld()
    ctl, session = controller(world)
    assert ctl.perform(session, {"action": "pause"})["ok"]
    world.state = "Paused"
    assert ctl.perform(session, {"action": "step"})["ok"]
    assert ctl.perform(session, {"action": "resume", "ratio": 0.5})["ok"]
    world.state = "Running"
    assert ctl.perform(session, {"action": "speed", "ratio": "max"})["ok"]
    sent = [item for item in world.sent if item["command"] not in console.ALLOWED]
    assert sent == [{"command": "pause", "actor": "console:s1"},
                    {"command": "step", "actor": "console:s1", "until_us": 1_030_000},
                    {"command": "run", "actor": "console:s1", "ratio": 0.5},
                    {"command": "set_ratio", "actor": "console:s1", "ratio": "max"}]


def test_a_loss_window_is_one_windowed_fault_per_link():
    world = FakeWorld(ratio=2, policy={"loss_ppm": [[1, 3, 250000]]})
    ctl, session = controller(world)
    reply = ctl.perform(session, {"action": "fault", "receivers": [3], "rate_pct": 100, "length_ms": 500})
    start = 1_000_000 + 2 * console.FAULT_LEAD_US
    assert reply["ok"] and reply["window"] == [start, start + 500_000]
    faults = [(f["radio"], f["target"], f["rate_ppm"], f["time_us"], f["until_us"]) for f in world.sent
              if f["command"] == "fault"]
    # The world restores each link's own old rate at until_us (parts/faults.py), so the console sends none.
    assert faults == [(1, 3, 1_000_000, start, start + 500_000), (2, 3, 1_000_000, start, start + 500_000)]
    assert all(f["actor"] == "console:s1" and f["kind"] == "loss" for f in world.sent if f["command"] == "fault")


def test_a_refused_window_stops_the_rest():
    world = FakeWorld(fail="fault")
    ctl, session = controller(world)
    reply = ctl.perform(session, {"action": "fault", "receivers": [2], "rate_pct": 50, "length_ms": 100})
    assert reply["ok"] is False and reply["error"] == "WORLD_PAST_INPUT"
    assert len([f for f in world.sent if f["command"] == "fault"]) == 1


@pytest.mark.parametrize("body, reason", [
    ({"action": "reboot"}, "CONSOLE_BAD_REQUEST"), ({"action": "pause", "extra": 1}, "CONSOLE_BAD_REQUEST"),
    ({"action": "speed", "ratio": 3}, "CONSOLE_BAD_RATIO"), ({"action": "speed", "ratio": True}, "CONSOLE_BAD_RATIO"),
    ({"action": "resume"}, "CONSOLE_BAD_REQUEST"), ({"action": "resume", "ratio": -1}, "CONSOLE_BAD_RATIO"),
    ({"action": []}, "CONSOLE_BAD_REQUEST"), ({"action": {}}, "CONSOLE_BAD_REQUEST"),
    ({"action": "fault", "receivers": [9], "rate_pct": 50, "length_ms": 100}, "CONSOLE_UNKNOWN_RADIO"),
    ({"action": "fault", "receivers": [2, 2], "rate_pct": 50, "length_ms": 100}, "CONSOLE_BAD_FAULT"),
    ({"action": "fault", "receivers": [2], "rate_pct": 0, "length_ms": 100}, "CONSOLE_BAD_FAULT"),
    ({"action": "fault", "receivers": [2], "rate_pct": 50, "length_ms": 5}, "CONSOLE_BAD_FAULT"),
    ({"action": "key", "radio": 1}, "CONSOLE_BAD_REQUEST")])
def test_bad_requests_are_refused_here_and_reported_to_the_world_audit(body, reason):
    world = FakeWorld()
    ctl, session = controller(world)
    reply = ctl.perform(session, body)
    assert (reply["ok"], reply["error"]) == (False, reason)
    assert [item for item in world.sent if item["command"] not in console.ALLOWED] == [
        {"command": "world_control.audit", "actor": "console:s1", "action": reply["action"], "result": "refused",
         "reason": reason, "suppressed": 0}]
    assert ctl.model.snapshot(0)["refusals"][-1]["reason"] == reason


def test_faults_need_a_running_paced_world():
    for world, reason in ((FakeWorld(state="Paused"), "CONSOLE_NOT_RUNNING"),
                          (FakeWorld(ratio="max"), "CONSOLE_FAULT_NEEDS_SPEED")):
        ctl, session = controller(world)
        reply = ctl.perform(session, {"action": "fault", "receivers": [2], "rate_pct": 50, "length_ms": 100})
        assert reply["error"] == reason and not [f for f in world.sent if f["command"] == "fault"]


def test_refusal_reports_are_rate_limited_and_counted():
    world = FakeWorld()
    ctl, session = controller(world)
    for _ in range(console.REPORT_RATE[1] + 7):
        ctl.refused(session, "pause", "CONSOLE_BAD_ORIGIN")
    assert len(world.sent) == console.REPORT_RATE[1] and ctl.suppressed == 7
    ctl.reports.level = 1
    ctl.refused(session, "pause", "CONSOLE_BAD_ORIGIN")
    assert world.sent[-1]["suppressed"] == 7 and ctl.suppressed == 0


def test_refusals_without_a_session_have_a_smaller_budget():
    world = FakeWorld()
    ctl, _ = controller(world)
    for _ in range(console.ANONYMOUS_RATE[1] + 30):
        ctl.refused(None, "other", "CONSOLE_BAD_ORIGIN")
    assert len(world.sent) == console.ANONYMOUS_RATE[1] and ctl.suppressed == 30
    assert world.sent[0]["actor"] == "console:s0"


def test_server_bounds_requests_at_once():
    server = console.Server(console.Model("w"))
    try:
        assert console.Handler.timeout == console.READ_S
        for _ in range(console.HANDLERS_MAX):
            assert server.slots.acquire(blocking=False)
        left, right = socket.socketpair()
        server.process_request(left, ("127.0.0.1", 1))       # no slot: closed unanswered, no thread
        assert right.recv(1) == b""
        right.close()
    finally:
        server.server_close()


def test_an_observer_console_sends_nothing_for_a_refusal(capsys):
    world = FakeWorld()
    ctl, session = controller(world, control=False)
    ctl.refused(session, "pause", "CONSOLE_READ_ONLY")
    assert world.sent == [] and "CONSOLE_READ_ONLY" in capsys.readouterr().err
    assert ctl.model.snapshot(0)["refusals"][0]["actor"] == "console:s1"


def test_live_control_route_reads_a_strict_json_body():
    model = console.Model("w")
    model.status = {"state": "Running", "time_us": 5}
    world = FakeWorld()
    server = console.Server(model, control=True, client=world)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    port = server.server_address[1]

    def call(method, path, body, **extra):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, body=body, headers={"Origin": f"http://127.0.0.1:{port}", "X-RadioFoundry-Session": getattr(extra.get("Cookie"), "proof", ""),
                                                           "Content-Type": "application/json", **extra})
            reply = conn.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read()
        finally:
            conn.close()

    try:
        token = server.gate.mint().split("token=")[1]
        status, head, body = call("POST", "/session", json.dumps({"token": token}).encode())
        assert status == 200
        cookie = Cookie(head["Set-Cookie"].split(";")[0], json.loads(body)["proof"])
        state = json.loads(call("GET", "/api/state", None, Cookie=cookie)[2])
        assert state["control"] == {"mode": "locked", "session": "console:s1"}
        assert call("POST", "/api/control", b'{"action":"pause"}', Cookie=cookie)[2] == b"CONSOLE_NO_CAPABILITY\n"
        code = server.gate.mint_control()
        assert call("POST", "/api/unlock", json.dumps({"code": code}).encode(), Cookie=cookie)[0] == 200
        assert call("POST", "/api/unlock", json.dumps({"code": code}).encode(), Cookie=cookie)[0] == 403
        for body in (b'{"action":"pause","action":"pause"}', b'[1]', b'{"action": NaN}', b"x" * 2000):
            assert call("POST", "/api/control", body, Cookie=cookie)[0] == 400
        status, _, body = call("POST", "/api/control", b'{"action":"pause"}', Cookie=cookie)
        assert status == 200 and json.loads(body)["ok"]
        assert json.loads(call("GET", "/api/state", None, Cookie=cookie)[2])["control"]["mode"] == "unlocked"
        commands = [item["command"] for item in world.sent]
        assert "pause" in commands and code not in json.dumps(world.sent) and cookie.split("=")[1] not in \
            json.dumps(world.sent)
        grants = [item for item in world.sent if item.get("result") == "granted"]
        assert grants == [{"command": "world_control.audit", "actor": "console:s1", "action": "unlock",
                           "result": "granted", "reason": "OK"}]
    finally:
        server.shutdown()
        server.server_close()


def test_control_allowlist_has_no_radio_control():
    radio = {"key", "enc", "lease.grant", "lease.revoke", "lease.grant_maintenance", "webgw.token",
             "debuglink.open", "hil.create", "hil.grant", "restart", "reboot", "power_off", "start", "end", "udp"}
    assert not radio & set(console.CONTROL_ALLOWED)
