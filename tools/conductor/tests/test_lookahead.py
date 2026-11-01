# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Socket-free cap checks and real air-bridge counterexamples. No firmware substitutes in the proof."""

import json

import pytest

from tools.conductor.channel import Kind
from tools.conductor.checks import lookahead_check as check
from tools.conductor.conductor import install_parts, scenario_config
from tools.conductor.parts import air, airctl, lookahead
from tools.conductor.parts.dlcache import DEFAULT_CADENCE_US
from tools.conductor.world import WorldError


def configure(world, **fields):
    return world.api.dispatch({"version": 1, "command": "air_config", "groups": [[1, 2]], **fields})


@pytest.mark.parametrize("fields, expected", [({}, 1000), ({"lookahead_proven": False}, 1000),
                                             ({"lookahead_proven": True}, lookahead.VDMR_LOOKAHEAD_US)])
def test_air_config_controls_installed_cap(stub_world, fields, expected):
    world = stub_world(radios=2, end_us=1_000_000)
    install_parts(world)
    reply = configure(world, **fields)
    assert reply["ok"] and reply["result"]["grant_us"] == expected
    world.start()
    world.run("max")
    assert world._target() == expected
    world.advance()
    assert world.now == expected


def test_other_limits_and_shared_air_still_apply(stub_world):
    world = stub_world(radios=2, end_us=1_000_000)
    install_parts(world)
    assert configure(world, lookahead_proven=True)["ok"]
    world.start()
    world.run(1)
    assert world._target() == 1000  # ADR 0005's pacing cap is independent of the air cap.
    world.set_ratio("max")
    world.api.clients[object()] = None
    assert world._target() == 1000
    world.api.clients.clear()
    world.submit(1, Kind.KEY, b"\x04\x01", 100, time_us=500)
    assert world._target() == 500
    world.power_off(1)
    assert world.air_lookahead.grant_limit(0) is None
    # With no air, only the debug link cache cadence (dlcache) still limits the grant.
    assert world._target() == DEFAULT_CADENCE_US


@pytest.mark.parametrize("value", [1, 0, "true", None, [], {}])
def test_proven_flag_requires_boolean(stub_world, value):
    world = stub_world(radios=2)
    install_parts(world)
    reply = configure(world, lookahead_proven=value)
    assert reply["error"] == "WORLD_BAD_LOOKAHEAD_PROVEN"
    assert world.air_lookahead.grant_us == 1000


def test_reconfigure_defaults_and_failed_config_are_atomic(stub_world):
    world = stub_world(radios=2)
    install_parts(world)
    assert configure(world, lookahead_proven=True)["ok"]
    assert not configure(world, groups=[[3]])["ok"]
    assert world.air_lookahead.grant_us == lookahead.VDMR_LOOKAHEAD_US
    assert configure(world)["result"]["grant_us"] == 1000
    world.start()
    assert configure(world, lookahead_proven=True)["error"] == "WORLD_AIR_ALREADY_STARTED"
    assert world.air_lookahead.grant_us == 1000


# The air part hands each AIR_IN over at its slot's last microsecond (start + 29 999) and, within the stated
# lookahead, ends a grant no later than that (air.py, delivery_limit). So a 30 ms cap with a burst at the grant
# start is safe now: the grant from 1000 ends at 30 999, so its last microsecond is 30 998. Only a cap past the
# lookahead, which only the check may probe, is still refused.
@pytest.mark.parametrize("grant, start, violation", [(1000, 1000, False), (30_000, 1000, False),
                                                    (30_000, 30_998, False), (120_000, 1000, True)])
def test_air_deadline_and_last_microsecond(stub_world, grant, start, violation):
    world = stub_world(radios=2, end_us=500_000)
    air.install(world)
    airctl.install(world)
    lookahead.install(world)
    assert configure(world)["ok"]
    world.start()
    world.run("max")
    world.advance()  # [0, 1000); the following grant begins exactly at 1000.
    world.air_lookahead.grant_us = grant
    record = air.Record(1, 1, start, 1, 0, 1, 0, bytes(9), b"header")
    world.radios[1].channel.outputs.append((Kind.AIR_OUT, record.encode()))
    if violation:
        with pytest.raises(WorldError, match="^WORLD_BAD_AIR_TIME$"):
            world.advance()
        print(f"grant_us={grant} burst_start_us={start} barrier_us={world.now} WORLD_BAD_AIR_TIME")
    else:
        received = []
        while not received:
            world.advance()
            received = [m for m in world.radios[2].channel.sent if m.kind == Kind.AIR_IN]
        assert len(received) == 1 and air.Record.decode(received[0].payload) == record
        assert received[0].time_us == air.handover_us(start)


def test_scenario_has_real_inputs_and_keeps_cap_unproven():
    data = json.loads(check.SCENARIO.read_text())
    config = scenario_config(check.SCENARIO, radios=2)
    assert data["air"]["lookahead_proven"] is False
    assert data["phase_us"] == {"1": 29999, "2": 10000}
    assert data["reboot"]["time_us"] % 30_000 != 0
    assert any(e.time_us == data["boundary_input_us"] and e.kind == Kind.KEY for e in config.inputs)
    assert [e.radio for e in config.inputs if e.kind == Kind.BYTES_IN] == [1, 2]
    assert config.inputs[-1].time_us < config.end_us


def test_compare_reports_first_difference_and_refuses_empty_trace():
    check.compare({1: b"one\ntwo\n"}, {1: b"one\ntwo\n"}, "equal")
    with pytest.raises(WorldError, match="radio=1 byte=5 line=2"):
        check.compare({1: b"one\ntwo\n"}, {1: b"one\ntao\n"}, "changed")
    with pytest.raises(WorldError, match="radio=1 byte=4 line=2"):
        check.compare({1: b"one\ntwo\n"}, {1: b"one\n"}, "truncated")
    with pytest.raises(WorldError, match="LOOKAHEAD_EMPTY_TRACE"):
        check.compare({1: b""}, {1: b""}, "empty")


@pytest.mark.parametrize("reason, expected", [("WORLD_BAD_AIR_TIME", 1),
                                            ("WORLD_RADIO_FAILURE:VCHAN_BAD_AIR_TIME", 1),
                                            ("WORLD_FLASH_RESTORE_UNAVAILABLE", 2),
                                            ("WORLD_RADIO_FAILURE:Operation not permitted", 2),
                                            ("LOOKAHEAD_MISSING_RX", 2)])
def test_negative_control_never_counts_missing_prerequisites(monkeypatch, reason, expected):
    def failed(*args, **kwargs):
        raise WorldError(reason)

    monkeypatch.setattr(check, "run", failed)
    assert check.main(["--negative-control", "--executable", __file__]) == expected


def test_success_prints_four_hashes_per_radio(monkeypatch, capsys):
    calls = []

    def completed(grant, ratio, **kwargs):
        calls.append((grant, ratio))
        return {1: b"radio one\n", 2: b"radio two\n"}

    monkeypatch.setattr(check, "run", completed)
    assert check.main(["--executable", __file__]) == 0
    assert calls == [(1000, 1), (lookahead.VDMR_LOOKAHEAD_US, 1),
                     (1000, "max"), (lookahead.VDMR_LOOKAHEAD_US, "max")]
    for line in capsys.readouterr().out.splitlines():
        hashes = line.split()[2:]
        assert len(hashes) == 4 and len(set(hashes)) == 1 and len(hashes[0]) == 64


def test_failed_run_never_prints_equal_hashes(monkeypatch, capsys):
    def failed(*args, **kwargs):
        raise WorldError("WORLD_FLASH_RESTORE_UNAVAILABLE")

    monkeypatch.setattr(check, "run", failed)
    assert check.main(["--executable", __file__]) == 1
    output = capsys.readouterr().out
    assert "LOOKAHEAD_UNPROVEN" in output and "sha256" not in output
