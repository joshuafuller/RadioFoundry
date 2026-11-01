# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""World API input payloads, recording hook, and the real firmware scan boundary."""

import json
import struct

import pytest

from tools.conductor.channel import Kind
from tools.conductor.conductor import install_parts
from tools.conductor.parts.input import install
from tools.conductor.parts import lease
from tools.conductor.world import Config, World


def request(world, command="key", **fields):
    return world.api.dispatch({"version": 1, "command": command, "radio": 1, **getattr(world, "test_grant", {}), **fields})


@pytest.mark.parametrize("command,fields", [("key", {"key": "ptt", "state": "down"}),
                                            ("enc", {"detents": 1})])
def test_world_input_requires_current_lease(stub_world, command, fields):
    world = stub_world(radio_args=("--encoder", "on"))
    install(world)
    world.start()
    world.run("max")
    granted = lease.get(world).grant(1, "script:holder")
    accepted = []
    world.on_input(accepted.append)
    for credentials, error in [({}, "LEASE_BAD_PRINCIPAL"),
                               ({**granted, "principal": "script:other"}, "LEASE_NOT_HOLDER"),
                               ({**granted, "generation": 0}, "LEASE_STALE_GENERATION"),
                               ({**granted, "secret": "wrong"}, "LEASE_BAD_SECRET")]:
        reply = request(world, command, **fields, **credentials)
        assert not reply["ok"] and reply["error"] == error, reply
    assert accepted == []
    assert request(world, command, **fields, **granted)["ok"]


def test_world_key_pause_releases_delivered_hold_and_cancels_future_input(vhost_elf, tmp_path):
    world = World(Config(executable=str(vhost_elf), radio_args=("--encoder", "on")))
    install_parts(world)
    world.api.bind("input-lease", tmp_path)
    traces = []
    world.on_outbox(Kind.TRACE, lambda radio, message: traces.append(message.payload.decode()))
    try:
        world.start()
        world.run("max")
        granted = world.leases.grant(1, "script:holder")
        for state, time_us in (("down", 100_000), ("up", 400_000), ("down", 500_000)):
            assert request(world, key="ok", state=state, time_us=time_us, **granted)["ok"]
        assert request(world, "enc", detents=1, time_us=500_000, **granted)["ok"]
        while world.now < 200_000:
            world.advance()
        assert len(world.holds.tracked(1)) == 1
        world.pause()
        advance_until_paused(world)
        assert world.holds.tracked(1) == [] and not world.holds.unconfirmed(1)
        assert any("key.scan pins=00000000" in line for line in traces)
        pressed_scans = sum("key.scan pins=00000001" in line for line in traces)
        assert pressed_scans > 0
        world.run("max", until=600_000)
        successor = world.leases.grant(1, "script:next")
        assert successor["generation"] > granted["generation"]
        advance_until_paused(world)
        assert sum("key.scan pins=00000001" in line for line in traces) == pressed_scans
        assert not any("encoder.read" in line for line in traces)
    finally:
        world.close()
    saved = json.loads((world.api.directory / "recording/scenario.json").read_text())
    assert [e["key"]["down"] for e in saved["events"] if "key" in e] == [True, False]
    assert not any("enc" in e for e in saved["events"])


def record_inputs(world, path):
    # A recording subscriber at the public seam; this lane does not own the recorder.
    def record(event):
        with path.open("a") as output:
            output.write(json.dumps({"radio": event.radio, "kind": event.kind.name,
                                     "payload_hex": event.payload.hex(), "time_us": event.time_us,
                                     "source": event.source, "sequence": event.sequence}) + "\n")
    world.on_input(record)


def test_named_key_records_bit(stub_world, tmp_path, capsys):
    world = stub_world()
    install_parts(world)
    path = tmp_path / "events.jsonl"
    record_inputs(world, path)
    world.start()
    world.run("max")
    world.test_grant = lease.get(world).grant(1, "script:input")
    result = request(world, key="ok", state="down", time_us=2000000)
    assert result["ok"], result
    line = path.read_text().strip()
    assert json.loads(line) == {"radio": 1, "kind": "KEY", "payload_hex": "0001",
                                "time_us": 2000000, "source": 0, "sequence": 1}
    with capsys.disabled():
        print(f"\nevents.jsonl (recording subscriber): {line}")


@pytest.mark.parametrize("key,bit", [("select", 0), ("back", 1), ("up", 2), ("down", 3),
                                     ("ptt", 4), ("monitor", 5), ("0", 6), ("9", 15), (31, 31)])
def test_key_map_and_release(stub_world, key, bit):
    world = stub_world()
    install(world)
    accepted = []
    world.on_input(accepted.append)
    world.start()
    world.run("max")
    world.test_grant = lease.get(world).grant(1, "script:input")
    assert request(world, key=key, state="down", time_us=11)["ok"]
    assert request(world, key=key, state="up", time_us=16)["ok"]
    assert [(event.payload, event.time_us) for event in accepted] == [(bytes((bit, 1)), 11),
                                                                   (bytes((bit, 0)), 16)]


@pytest.mark.parametrize("fields,error", [
    ({"key": "missing"}, "WORLD_UNKNOWN_KEY"),
    ({"key": -1}, "WORLD_BAD_KEY"), ({"key": 32}, "WORLD_BAD_KEY"),
    ({"key": True}, "WORLD_BAD_KEY"), ({"key": []}, "WORLD_BAD_KEY"),
    ({"state": "press"}, "WORLD_BAD_KEY_STATE"),
    ({"time_us": -1}, "WORLD_BAD_TIME"), ({"time_us": 0}, "WORLD_PAST_INPUT"),
    ({"radio": 2}, "WORLD_UNKNOWN_RADIO"),
])
def test_key_refusals(stub_world, fields, error, capsys):
    world = stub_world()
    install(world)
    accepted = []
    world.on_input(accepted.append)
    world.start()
    world.run("max")
    world.test_grant = lease.get(world).grant(1, "script:input")
    response = request(world, **{"key": "ok", "state": "down", "time_us": 1000, **fields})
    assert not response["ok"] and response["error"] == error
    assert accepted == []
    if error == "WORLD_UNKNOWN_KEY":
        with capsys.disabled():
            print("\nkey missing: WORLD_UNKNOWN_KEY")


def test_paused_and_encoder_profile(stub_world):
    world = stub_world()
    install(world)
    world.start()
    assert request(world, key="ok", state="down")["error"] == "LEASE_BAD_PRINCIPAL"
    world.run("max")
    world.test_grant = lease.get(world).grant(1, "script:input")
    assert request(world, "enc", detents=1)["error"] == "WORLD_NO_ENCODER"


@pytest.mark.parametrize("count", [300, -300, 0, -(1 << 31), (1 << 31) - 1])
def test_encoder_payload(stub_world, count):
    world = stub_world(radio_args=("--encoder", "on"))
    install(world)
    accepted = []
    world.on_input(accepted.append)
    world.start()
    world.run("max")
    world.test_grant = lease.get(world).grant(1, "script:input")
    assert request(world, "enc", detents=count, time_us=1234)["ok"]
    assert accepted[0].kind == Kind.ENC and accepted[0].payload == struct.pack("<i", count)
    assert accepted[0].time_us == 1234


@pytest.mark.parametrize("count", [True, 1.5, "3", -(1 << 31) - 1, 1 << 31])
def test_encoder_bad_count(stub_world, count):
    world = stub_world(radio_args=("--encoder", "on"))
    install(world)
    world.start()
    world.run("max")
    world.test_grant = lease.get(world).grant(1, "script:input")
    assert request(world, "enc", detents=count)["error"] == "WORLD_BAD_DETENTS"


def advance_until_paused(world):
    for _ in range(100):
        if world.state not in ("Running", "Pausing"):
            break
        world.advance()
    assert world.state == "Paused", world.reason


def test_firmware_key_at_scan_boundary(vhost_elf, tmp_path, capsys):
    world = World(Config(executable=str(vhost_elf)))
    traces = []
    path = tmp_path / "events.jsonl"
    install(world)
    record_inputs(world, path)
    world.on_outbox(Kind.TRACE, lambda radio, message: traces.append(message.payload.decode()))
    try:
        world.start()
        world.run("max", until=2020000)
        world.test_grant = lease.get(world).grant(1, "script:input")
        assert request(world, key="ok", state="down", time_us=2000000)["ok"]
        advance_until_paused(world)
        at_scan = [line for line in traces if line.startswith("000002000000 ") and "key.scan" in line]
        with capsys.disabled():
            print("\n2000 ms firmware scan: " + "\n".join(at_scan))
        assert any("key.scan pins=00000001" in line for line in at_scan), at_scan
        assert json.loads(path.read_text().splitlines()[0])["payload_hex"] == "0001"
    finally:
        world.close()


def test_firmware_encoder_scans(vhost_elf):
    world = World(Config(executable=str(vhost_elf), radio_args=("--encoder", "on")))
    traces = []
    install(world)
    world.on_outbox(Kind.TRACE, lambda radio, message: traces.append(message.payload.decode()))
    try:
        world.start()
        world.run("max", until=2040000)
        world.test_grant = lease.get(world).grant(1, "script:input")
        assert request(world, "enc", detents=300, time_us=2000000)["ok"]
        advance_until_paused(world)
        reads = [line.split("BOARD ")[1] for line in traces if "BOARD encoder.read" in line]
        assert reads == ["encoder.read detents=127 remaining=173", "encoder.read detents=127 remaining=46",
                         "encoder.read detents=46 remaining=0"]
    finally:
        world.close()
