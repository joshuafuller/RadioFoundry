# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The Air group of the world API: each message sets the air policy, and only a world client may send one."""

import json
import os
import socket

from tools.conductor.airmodel import Burst, Policy, decide
from tools.conductor.conductor import install_parts
from tools.conductor.parts import airctl


def make(stub_world):
    world = stub_world(radios=3)
    control = airctl.install(world)
    world.start()
    return world, control


def send(world, command, **fields):
    return world.api.dispatch({"version": 1, "command": command, **fields})


def test_install_parts_loads_airctl(stub_world):
    world = stub_world(radios=2)
    install_parts(world)
    assert airctl.policy(world) == Policy()
    assert {"air_policy", "air_loss", "air_delay", "air_corrupt", "air_duplicate", "air_link"} <= set(world.api.handlers)


def test_loss_sets_rate_and_drop(stub_world):
    world, _ = make(stub_world)
    assert send(world, "air_loss", sender=1, receiver=2, rate_ppm=250_000)["ok"]
    assert airctl.policy(world).loss_ppm == (((1, 2), 250_000),)
    assert send(world, "air_loss", sender=1, ordinal=5)["ok"]
    assert airctl.policy(world).drops == {(1, 5)}
    assert send(world, "air_loss", sender=1, ordinal=5, dropped=False)["ok"]
    assert send(world, "air_loss", sender=1, receiver=2, rate_ppm=0)["ok"]
    assert airctl.policy(world) == Policy()
    assert send(world, "air_loss", sender=1, receiver=2, rate_ppm=1_000_001)["error"] == "WORLD_BAD_LOSS_RATE"
    assert send(world, "air_loss", sender=1, receiver=9, rate_ppm=1)["error"] == "WORLD_UNKNOWN_RADIO"
    assert send(world, "air_loss", sender=1, receiver=1, rate_ppm=1)["error"] == "WORLD_BAD_LINK"
    assert send(world, "air_loss", sender=1)["error"] == "WORLD_BAD_REQUEST"


def test_delay_corrupt_duplicate_set_one_burst(stub_world):
    world, _ = make(stub_world)
    assert send(world, "air_delay", sender=1, receiver=2, ordinal=3, delay_us=60_000)["ok"]
    mask = "ff" + "00" * 26
    assert send(world, "air_corrupt", sender=1, receiver=2, ordinal=3, xor_hex=mask)["ok"]
    assert send(world, "air_duplicate", sender=1, receiver=2, ordinal=3, enabled=True)["ok"]
    policy = airctl.policy(world)
    assert policy.delays_us == (((1, 2, 3), 60_000),)
    assert policy.corruptions == (((1, 2, 3), bytes.fromhex(mask)),)
    assert policy.duplicates == {(1, 2, 3)}
    # The air model uses exactly this snapshot: radio 2 hears burst 3 twice, damaged, 60 ms late.
    burst = Burst(1, 3, 1000, bytes(27))
    result = decide((1000, 2000), {1: [burst], 2: []}, policy, 0)
    assert [(d.air_start_us, d.copy, d.corrupted) for d in result.deliveries] == [(61_000, 0, True), (61_000, 1, True)]
    assert send(world, "air_corrupt", sender=1, receiver=2, ordinal=3, xor_hex="")["ok"]
    assert send(world, "air_delay", sender=1, receiver=2, ordinal=3, delay_us=0)["ok"]
    assert send(world, "air_duplicate", sender=1, receiver=2, ordinal=3, enabled=False)["ok"]
    assert airctl.policy(world) == Policy()
    assert send(world, "air_corrupt", sender=1, receiver=2, ordinal=3, xor_hex="0")["error"] == "WORLD_BAD_MASK"
    assert send(world, "air_corrupt", sender=1, receiver=2, ordinal=3, xor_hex="0000")["error"] == "WORLD_BAD_MASK"
    assert send(world, "air_corrupt", sender=1, receiver=2, ordinal=3, xor_hex="ff" * 28)["error"] == "WORLD_BAD_MASK"
    assert send(world, "air_corrupt", sender=1, receiver=2, ordinal=3, xor_hex="ff")["ok"]
    assert send(world, "air_delay", sender=1, receiver=2, ordinal=0, delay_us=1)["error"] == "WORLD_BAD_ORDINAL"
    assert send(world, "air_delay", sender=1, receiver=2, ordinal=1, delay_us=-1)["error"] == "WORLD_BAD_DELAY"
    assert send(world, "air_duplicate", sender=1, receiver=2, ordinal=1, enabled=1)["error"] == "WORLD_BAD_FLAG"


def test_link_up_and_down_whole_air_and_one_direction(stub_world):
    world, _ = make(stub_world)
    assert send(world, "air_link", up=False)["ok"] and not airctl.policy(world).link_up
    assert send(world, "air_link", up=True)["ok"] and airctl.policy(world).link_up
    assert send(world, "air_link", up=False, sender=2, receiver=1)["ok"]
    assert airctl.policy(world).down_links == {(2, 1)}
    assert send(world, "air_link", up=True, sender=2, receiver=1)["ok"]
    assert airctl.policy(world) == Policy()
    assert send(world, "air_link", up=False, sender=2)["error"] == "WORLD_BAD_REQUEST"


def test_policy_read_and_change_event(stub_world):
    world, _ = make(stub_world)
    events = []
    world.api.notify = events.append
    reply = send(world, "air_loss", sender=2, receiver=3, rate_ppm=5)
    assert reply["result"]["loss_ppm"] == [[2, 3, 5]]
    assert send(world, "air_policy")["result"] == reply["result"]
    assert events == [{"event": "air_policy", "command": "air_loss", "time_us": world.now, "policy": reply["result"]}]
    world.end()
    assert send(world, "air_link", up=False)["error"] == "WORLD_ENDED"


def lines(client):
    data = b""
    while not data.endswith(b"\n"):
        chunk = client.recv(65536)
        if not chunk:
            break
        data += chunk
    return [json.loads(line) for line in data.splitlines()]


def test_world_client_on_the_socket_sets_policy(stub_world, tmp_path):
    """Same UID on the world socket holds the world capability (ADR 0004 rules 7 and 8)."""
    world, _ = make(stub_world)
    path = world.api.bind("air-ok", tmp_path)
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(str(path))
        for _ in range(3):
            world.api.poll(0.05)
        assert lines(client)[0]["event"] == "state"
        client.sendall(b'{"version":1,"id":1,"command":"air_link","up":false}\n')
        for _ in range(4):
            world.api.poll(0.05)
        reply = lines(client)
        assert reply[-1]["ok"] and reply[-1]["id"] == 1
    assert not airctl.policy(world).link_up


def test_client_without_world_capability_is_refused(stub_world, tmp_path, capsys):
    """A peer of another UID has no world capability: it is closed before any air message reaches a handler."""
    world, _ = make(stub_world)
    world.api.uid_reader = lambda connection: os.getuid() + 1
    path = world.api.bind("air-refused", tmp_path)
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(str(path))
        try:
            client.sendall(b'{"version":1,"command":"air_link","up":false}\n')
        except OSError:
            pass
        for _ in range(4):
            world.api.poll(0.05)
        try:
            reply = client.recv(65536)
        except ConnectionResetError:
            reply = b""
        assert reply == b"" and not world.api.clients
    assert airctl.policy(world) == Policy()
    with capsys.disabled():
        print("\nair_link from a different UID: connection closed, policy unchanged")
