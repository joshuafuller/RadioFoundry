# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""World authority, filesystem permissions, peer identity and bounded framing."""

import json
import os
import socket
import struct
import subprocess

import pytest

from tools.conductor.net_guard import bind, loopback_address
from tools.conductor.parts import lease
from tools.conductor.worldapi import LIMIT, peer_uid


def request(world, command, **fields):
    return world.api.dispatch({"version": 1, "command": command, **fields})


def test_version_registry_and_no_radio_control(stub_world):
    world = stub_world()
    world.start()
    assert world.api.dispatch({"version": 2})["error"] == "WORLD_BAD_VERSION"
    assert world.api.dispatch({"version": True})["error"] == "WORLD_BAD_VERSION"
    assert world.api.dispatch([])["error"] == "WORLD_BAD_REQUEST"
    assert request(world, "submit")["error"] == "WORLD_UNKNOWN_COMMAND"
    world.api.register("part", lambda data: data["value"])
    assert request(world, "part", value=7, id=3) == {"version": 1, "id": 3, "ok": True, "result": 7}
    with pytest.raises(ValueError, match="WORLD_DUPLICATE_HANDLER"):
        world.api.register("part", lambda data: None)
    assert request(world, "run", ratio=-1)["error"] == "WORLD_BAD_RATIO"
    assert request(world, "run", ratio="max")["ok"]
    assert request(world, "set_ratio", ratio=2)["ok"]
    assert request(world, "power_off", radio=1)["ok"]
    assert request(world, "start", radio=1)["ok"]
    assert request(world, "restart", radio=1)["ok"]
    assert request(world, "reboot", radio=3)["error"] == "WORLD_UNKNOWN_RADIO"
    assert request(world, "pause")["ok"]
    assert request(world, "step", until_us=1000)["ok"]
    assert request(world, "run")["error"] == "WORLD_BUSY"
    assert request(world, "status")["result"]["state"] == "Stepping"
    world.advance()
    assert request(world, "end")["ok"] and world.state == "Ended"


@pytest.mark.parametrize("state", ["Pausing", "Stepping"])
@pytest.mark.parametrize("command", ["run", "lease.grant", "lease.revoke", "lease.grant_maintenance"])
def test_busy_refusal_is_audited(stub_world, tmp_path, state, command):
    world = stub_world()
    world.api.directory = tmp_path
    lease.install(world)
    world.start()
    if state == "Pausing":
        world.on_pausing(lambda: lambda: False)
        world.run("max")
        world.pause()
    else:
        world.step()
    assert world.state == state
    response = request(world, command, radio=1, principal="script:runner", generation=1)
    assert response["error"] == "WORLD_BUSY"
    lines = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(lines) == 1
    line = lines[0]
    assert (line["principal"], line["radio"], line["result"], line["reason"], line["command"], line["state"]) == \
        ("script:runner", 1, "refused", "WORLD_BUSY", command, state)
    assert world.state == state and not world.leases.held
    assert request(world, "status")["result"]["state"] == state


@pytest.mark.parametrize("command", ["restart", "power_off", "end"])
def test_recovery_still_refused_while_stepping(stub_world, command):
    world = stub_world()
    world.start()
    world.step()
    assert request(world, command, radio=1)["error"] == "WORLD_BUSY"
    assert world.state == "Stepping" and not world.radios[1].channel.closed


def test_private_runtime_refuses_symlink_and_public_folder(stub_world, tmp_path):
    world = stub_world()
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="WORLD_UNSAFE_RUNTIME_DIRECTORY"):
        world.api.bind("test", public)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="WORLD_UNSAFE_RUNTIME_DIRECTORY"):
        world.api.bind("test", link)
    with pytest.raises(ValueError, match="WORLD_BAD_NAME"):
        world.api.bind("../escape", tmp_path)


def test_peer_uid_decoding():
    class Peer:
        def getsockopt(self, level, option, size):
            assert (level, option, size) == (socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            return struct.pack("3i", 1234, 5678, 90)
    assert peer_uid(Peer()) == 5678


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.9", "::", "localhost"])
def test_net_guard_refuses_nonloopback(host):
    class Listener:
        def bind(self, address):
            pytest.fail("unsafe address reached bind")
    with pytest.raises(ValueError, match="WORLD_NON_LOOPBACK_BIND"):
        bind(Listener(), (host, 1234))


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_net_guard_allows_loopback(host, capsys):
    bound = []
    class Listener:
        bind = staticmethod(bound.append)
    bind(Listener(), (host, 1234))
    assert bound == [(host, 1234)] and loopback_address(bound[0]) == bound[0]
    with capsys.disabled():
        print(f"\nnet_guard allows {host}; wildcard and LAN cases refused separately")


def exchange(world, client, data):
    client.sendall(data)
    world.api.poll(0)
    world.api.poll(0)
    return [json.loads(line) for line in client.recv(LIMIT).splitlines()]


def test_socket_permissions_framing_and_recorded_config(stub_world, tmp_path, capsys):
    world = stub_world(replay=True)
    path = world.api.bind("permissions", tmp_path)
    world.start()
    modes = subprocess.check_output(["stat", "-c", "%a", str(path.parent), str(path)], text=True)
    assert modes == "700\n600\n"
    assert json.loads((path.parent / "config.json").read_text())["replay"] is True
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(str(path))
        world.api.poll(0)
        world.api.poll(0)
        assert json.loads(client.recv(LIMIT))["state"] == "Paused"
        client.sendall(b'{"version":1,')
        world.api.poll(0)
        assert not next(iter(world.api.clients.values())).outgoing
        replies = exchange(world, client, b'"command":"status","id":9}\n')
        assert replies[0]["id"] == 9 and replies[0]["ok"]
        replies = exchange(world, client, b'bad json\n{"version":8}\n')
        assert [reply["error"] for reply in replies] == ["WORLD_BAD_JSON", "WORLD_BAD_VERSION"]
        world.notify({"event": "oversize", "data": "x" * LIMIT})
        assert world.api.clients                            # a pushed event that cannot fit is skipped, not the client
        replies = exchange(world, client, b'{"version":1,"command":"status","id":10}\n')
        assert replies[0] == {"event": "events_skipped", "count": 1, "version": 1} and replies[1]["id"] == 10
        client.sendall(b'{"version":1,"command":"status"}\n' * (LIMIT // 100 + 1))
        for _ in range(4):
            world.api.poll(0)
        assert not world.api.clients                        # replies that never get read still drop the client
    with capsys.disabled():
        print(f"\nstat -c %a {path.parent} {path}\n{modes}", end="")


def test_socket_refuses_different_peer_uid(stub_world, tmp_path, capsys):
    world = stub_world()
    world.api.uid_reader = lambda connection: os.getuid() + 1
    path = world.api.bind("wrong-uid", tmp_path)
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(str(path))
        world.api.poll(0)
        assert client.recv(1) == b"" and not world.api.clients
    with capsys.disabled():
        print("\nSO_PEERCRED different UID: connection refused (test hook)")


def audit_lines(path):
    return [json.loads(line) for line in (path / "audit.jsonl").read_text().splitlines()] \
        if (path / "audit.jsonl").exists() else []


def test_world_control_with_an_actor_is_audited_once(stub_world, tmp_path):
    world = stub_world()
    world.api.directory = tmp_path
    world.start()
    assert request(world, "run", ratio=2, actor="console:s1")["ok"]
    assert request(world, "set_ratio", ratio=0, actor="console:s1")["error"] == "WORLD_BAD_RATIO"
    assert request(world, "pause", actor="console:s1")["ok"]
    assert request(world, "step", until_us=1000, actor="console:s1")["ok"]
    assert request(world, "pause", actor="console:s1")["error"] == "WORLD_BUSY"
    assert request(world, "run", ratio=1)["error"] == "WORLD_BUSY"         # no actor: the old world_command line
    lines = audit_lines(tmp_path)
    assert [(line["kind"], line["principal"], line.get("command"), line["result"], line["reason"])
            for line in lines] == [
        ("world_control", "console:s1", "run", "accepted", "OK"),
        ("world_control", "console:s1", "set_ratio", "refused", "WORLD_BAD_RATIO"),
        ("world_control", "console:s1", "pause", "accepted", "OK"),
        ("world_control", "console:s1", "step", "accepted", "OK"),
        ("world_control", "console:s1", "pause", "refused", "WORLD_BUSY"),
        ("world_command", None, "run", "refused", "WORLD_BUSY")]
    assert all(line["layer"] == "console" for line in lines[:5])


@pytest.mark.parametrize("actor", ["browser:s1", "console:", "console:s1x", 7, "console:" + "s" * 30])
def test_a_bad_actor_is_refused_and_not_written(stub_world, tmp_path, actor):
    world = stub_world()
    world.api.directory = tmp_path
    world.start()
    assert request(world, "run", actor=actor)["error"] == "WORLD_BAD_ACTOR"
    assert world.state != "Running" and audit_lines(tmp_path) == []
    # An actor on a command that is not world control is only data, as before (lease.grant uses "principal").
    assert request(world, "status", actor=actor)["ok"]


def test_console_audit_records_refusals_and_grants_only(stub_world, tmp_path):
    world = stub_world()
    world.api.directory = tmp_path
    world.start()
    before = (world.state, world.now, world.pacing.ratio)
    ok = {"actor": "console:s2", "action": "pause", "result": "refused", "reason": "CONSOLE_BAD_ORIGIN"}
    assert request(world, "world_control.audit", **ok)["ok"]
    assert request(world, "world_control.audit", **dict(ok, suppressed=4))["ok"]
    assert request(world, "world_control.audit", actor="console:s2", action="unlock", result="granted",
                   reason="OK")["ok"]
    for bad in ({"result": "accepted"}, {"result": "granted"}, {"actor": "uid:1000"}, {"reason": "lowercase"},
                {"action": "key"}, {"suppressed": -1}, {"reason": "A" * 64}):
        assert request(world, "world_control.audit", **dict(ok, **bad))["error"] == "WORLD_BAD_REQUEST"
    lines = audit_lines(tmp_path)
    assert [(line["result"], line["reason"], line["action"], line.get("suppressed")) for line in lines] == [
        ("refused", "CONSOLE_BAD_ORIGIN", "pause", None), ("refused", "CONSOLE_BAD_ORIGIN", "pause", 4),
        ("granted", "OK", "unlock", None)]
    assert (world.state, world.now, world.pacing.ratio) == before      # an audit note changes nothing else


def connect(world, path):
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(2)
    client.connect(str(path))
    world.api.poll(0)
    world.api.poll(0)
    assert json.loads(client.recv(LIMIT))["event"] == "state"
    return client


def test_high_rate_events_go_only_to_clients_that_subscribe(stub_world, tmp_path):
    world = stub_world()
    path = world.api.bind("subscribe", tmp_path)
    world.start()
    plain, quiet, udp, every = (connect(world, path) for _ in range(4))
    try:
        assert exchange(world, quiet, b'{"version":1,"id":1,"command":"subscribe","events":[]}\n') == [
            {"version": 1, "id": 1, "ok": True, "result": {"events": []}}]
        assert exchange(world, udp, b'{"version":1,"id":2,"command":"subscribe","events":["udp_delivery"]}\n')[0][
            "result"] == {"events": ["udp_delivery"]}
        assert exchange(world, every, b'{"version":1,"id":3,"command":"subscribe","events":["*"]}\n')[0]["ok"]
        for bad in (b'"events":"udp_delivery"', b'"events":["UDP"]', b'"events":[1]',
                    b'"events":[' + b",".join([b'"a"'] * 65) + b']'):
            reply = exchange(world, plain, b'{"version":1,"id":4,"command":"subscribe",' + bad + b'}\n')
            assert reply == [{"version": 1, "id": 4, "ok": False, "error": "WORLD_BAD_SUBSCRIBE"}]
        assert exchange(world, plain, b'{"version":2,"id":5,"command":"subscribe","events":[]}\n')[0][
            "error"] == "WORLD_BAD_VERSION"
        assert world.api.dispatch({"version": 1, "command": "subscribe", "events": []})["error"] == \
            "WORLD_UNKNOWN_COMMAND"                         # a per-connection setting, never a world command
        world.notify({"event": "udp_delivery", "radio": 1})
        world.notify({"event": "air_delivery", "radio": 2})
        world.api.poll(0)
        world.api.poll(0)

        def kinds(client):
            client.setblocking(False)
            try:
                return [json.loads(line)["event"] for line in client.recv(LIMIT).splitlines()]
            except BlockingIOError:
                return []
        assert kinds(plain) == ["air_delivery"]             # the default: every kind but HIGH_RATE
        assert kinds(quiet) == []
        assert kinds(udp) == ["udp_delivery"]
        assert kinds(every) == ["udp_delivery", "air_delivery"]
    finally:
        for client in (plain, quiet, udp, every):
            client.close()


def test_idle_client_survives_a_flood_and_answers_a_second_call(stub_world, tmp_path):
    world = stub_world()
    path = world.api.bind("idle", tmp_path)
    world.start()
    with connect(world, path) as client:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        assert exchange(world, client, b'{"version":1,"id":1,"command":"status"}\n')[0]["ok"]
        for _ in range(5000):                               # about 1 MB pushed while the client reads nothing
            world.notify({"event": "air_delivery", "data": "x" * 200})
            world.api.poll(0)
        assert world.api.clients
        client.sendall(b'{"version":1,"id":2,"command":"status"}\n')
        lines, reply, buffer = [], None, b""
        while reply is None:
            world.api.poll(0.01)
            chunk = client.recv(LIMIT)
            assert chunk, "the world dropped the idle client"
            buffer += chunk
            *complete, buffer = buffer.split(b"\n")
            for line in complete:
                message = json.loads(line)
                lines.append(message)
                reply = message if message.get("id") == 2 else reply
        world.api.poll(0)
        skipped = sum(message.get("count", 0) for message in lines if message.get("event") == "events_skipped")
        assert reply["ok"] and skipped > 0
