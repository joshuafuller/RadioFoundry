# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The HIL endpoint against the real vhost firmware: the lease hand-off, the socket, the peer check."""

import os
from pathlib import Path
import shutil
import socket
import stat
import tempfile

import pytest

from tools.conductor.parts import audit, dlmux, hil_endpoint, recorder
from tools.conductor.serial_handlers import protocol_unix
from tools.conductor.world import Config, World, WorldError


@pytest.fixture
def live(tmp_path, vhost_elf, monkeypatch):
    runtime = tempfile.mkdtemp(prefix="hil")        # short: a socket path holds 108 bytes
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    world = World(Config(executable=str(vhost_elf)))
    recorder.install(world)
    endpoints = hil_endpoint.get(world)
    world.lines = []
    audit.get(world).add_sink(world.lines.append)
    world.api.bind("hil", tmp_path)
    try:
        world.start()
        world.run("max")
        run_until(world, 300_000)
        yield world, endpoints
    finally:
        world.close()
        shutil.rmtree(runtime, ignore_errors=True)


def run_until(world, time_us):
    while world.state not in ("Ended", "Paused") and world.now < time_us:
        world.advance()
        world.api.poll(0)


def make(world, endpoints, grant=True):
    made = endpoints.create({"radio": 1})
    if grant:
        granted = endpoints.grant({"endpoint": made["endpoint"]})
        # The grant secret stays inside the endpoint: not in the reply, not in its repr.
        ep = endpoints.endpoints[made["endpoint"]]
        assert "secret" not in granted and ep.secret and ep.secret not in repr(ep)
    return made, Path(made["url"].removeprefix("unix://"))


def connect(path):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(path))
    client.setblocking(False)
    return client


def ask(world, client, cmd=0x01, seq=1, payload=b"hi"):
    try:
        client.send(dlmux.frame(cmd, seq, payload))
    except OSError:
        return None
    for _ in range(50):
        run_until(world, world.now + 10_000)
        try:
            data = client.recv(4096)
        except BlockingIOError:
            continue
        except OSError:
            return None
        if not data:
            return None
        return [item[1:] for item in dlmux.Parser(dlmux.REPLY_MAX).feed(data) if item[0] == "frame"]
    raise AssertionError("no reply")


def kinds(world):
    return [(line["kind"], line["result"], line["reason"]) for line in world.lines]


def test_socket_is_private_and_a_ping_is_answered_under_the_lease(live):
    world, endpoints = live
    made, path = make(world, endpoints)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_ISSOCK(path.stat().st_mode)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    client = connect(path)
    assert ask(world, client) == [(0x81, 1, b"\0hi")]
    names = kinds(world)
    # The grant is audited before the connection, and the connection before the first control line.
    assert names.index(("lease_grant", "accepted", "OK")) < names.index(("hil_connect", "accepted", "OK")) \
        < names.index(("control", "accepted", "OK"))
    connect_line = next(line for line in world.lines if line["kind"] == "hil_connect")
    assert connect_line["peer_pid"] == os.getpid() and connect_line["peer_uid"] == os.getuid()


def test_no_connection_before_the_grant(live):
    world, endpoints = live
    made, path = make(world, endpoints, grant=False)
    client = connect(path)
    assert ask(world, client) is None
    assert ("hil_connect", "refused", "HIL_NO_LEASE") in kinds(world)
    assert endpoints.leases.held == {}


def test_second_connection_is_refused_and_the_first_keeps_working(live):
    world, endpoints = live
    made, path = make(world, endpoints)
    first = connect(path)
    assert ask(world, first) is not None
    second = connect(path)
    assert ask(world, second) is None
    assert ("hil_connect", "refused", "HIL_SECOND_CONNECTION") in kinds(world)
    assert ask(world, first, seq=2) == [(0x81, 2, b"\0hi")]


def test_other_user_is_refused_before_a_byte_is_read(live, monkeypatch):
    world, endpoints = live
    made, path = make(world, endpoints)
    monkeypatch.setattr(hil_endpoint, "peer_credentials", lambda connection: (1, os.getuid() + 1, 1))
    assert ask(world, connect(path)) is None
    assert ("hil_connect", "refused", "HIL_OTHER_USER") in kinds(world)


def test_reconnect_keeps_the_lease_and_a_half_frame_is_dropped(live):
    world, endpoints = live
    made, path = make(world, endpoints)
    first = connect(path)
    first.send(dlmux.frame(0x01, 9, b"cut")[:5])       # half a frame, then close
    run_until(world, world.now + 10_000)
    first.close()
    run_until(world, world.now + 20_000)
    assert endpoints.leases.held[1].principal == made["principal"]
    second = connect(path)
    assert ask(world, second, seq=3) == [(0x81, 3, b"\0hi")]       # the old half frame did not join it
    connections = [line["connection"] for line in world.lines if line["kind"] == "hil_connect"
                   and line["result"] == "accepted"]
    assert connections == [1, 2]


def test_release_ends_the_lease_and_removes_the_socket(live):
    world, endpoints = live
    made, path = make(world, endpoints)
    client = connect(path)
    assert ask(world, client) is not None
    endpoints.release({"endpoint": made["endpoint"]})
    assert not path.exists() and endpoints.leases.held == {}
    assert ask(world, client) is None                    # the connection was closed
    assert ("lease_revoke", "accepted", "HIL_RUN_END") in kinds(world)
    with pytest.raises(WorldError, match="HIL_UNKNOWN_ENDPOINT"):
        endpoints.release({"endpoint": made["endpoint"]})


def test_a_held_lease_blocks_the_grant(live):
    world, endpoints = live
    endpoints.leases.grant(1, "browser:b1")
    made = endpoints.create({"radio": 1})
    with pytest.raises(WorldError, match="LEASE_HELD"):
        endpoints.grant({"endpoint": made["endpoint"]})


def test_bad_radio_and_long_path_are_refused(live, monkeypatch):
    world, endpoints = live
    with pytest.raises(WorldError, match="WORLD_UNKNOWN_RADIO"):
        endpoints.create({"radio": 7})
    monkeypatch.setattr(hil_endpoint, "PATH_MAX", 10)
    with pytest.raises(WorldError, match="HIL_PATH_TOO_LONG"):
        endpoints.create({"radio": 1})


def test_world_end_revokes_the_lease_and_removes_the_socket(live):
    world, endpoints = live
    made, path = make(world, endpoints)
    world.end()
    assert not path.exists()
    assert ("lease_revoke", "accepted", "WORLD_END") in kinds(world)


def test_url_handler_accepts_only_absolute_unix_paths():
    port = protocol_unix.Serial()
    assert port.from_url("unix:///run/user/1/rfvr/a.sock") == "/run/user/1/rfvr/a.sock"
    for bad in ("unix://rel.sock", "unix:///a.sock?x=1", "socket:///a.sock", "unix://host/a.sock"):
        with pytest.raises(Exception):
            port.from_url(bad)
