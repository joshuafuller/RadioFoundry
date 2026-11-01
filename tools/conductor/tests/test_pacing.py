# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Wall deadlines and pacing against the actual virtual radio."""

import binascii
import hashlib
from pathlib import Path
import time

import pytest

from tools.conductor.channel import Kind
from tools.conductor.conductor import scenario_config
from tools.conductor.pacing import Pacing
from tools.conductor.world import World

SCENARIO = Path(__file__).resolve().parents[1] / "scenarios/pacing.json"


def test_deadlines_ratios_live_cap_and_resume():
    wall = [10.0]
    notes = []
    pacing = Pacing(notes, clock=lambda: wall[0])
    pacing.reset(1000, 2)
    assert pacing.cap() == 1000
    assert pacing.delay(1001000, 1000) == pytest.approx(0.5)
    wall[0] += 0.25
    assert pacing.stamp(1000) == 501000
    assert pacing.delay(1001000, 501000) == pytest.approx(0.25)
    wall[0] = 100
    pacing.reset(501000, 1)
    assert pacing.stamp(501001) == 501001
    assert notes[-1]["event"] == "late_input"
    assert pacing.delay(502000, 501000) == pytest.approx(0.001)
    pacing.reset(501000, "max")
    assert pacing.cap() is None and pacing.cap(live=True) == 1000
    assert pacing.delay(9000000, 501000) == 0 and pacing.stamp(501001) == 501001


def test_behind_span_without_skipping():
    wall, notes = [0.0], []
    pacing = Pacing(notes, clock=lambda: wall[0], lag_us=1000)
    pacing.reset(0, 1)
    wall[0] = 0.010
    assert pacing.delay(1000, 0) == 0
    assert notes[-1]["event"] == "Behind" and "end_us" not in notes[-1]
    assert pacing.delay(11000, 10000) == pytest.approx(0.001)
    assert notes[-1]["end_us"] == 10000
    assert pacing.origin_us == 0


@pytest.mark.parametrize("ratio", [0, -1, True, "fast", float("nan"), float("inf")])
def test_bad_ratio(ratio):
    with pytest.raises(ValueError, match="WORLD_BAD_RATIO"):
        Pacing([]).reset(0, ratio)


def test_run_until_and_resume_restarts_clock(stub_world):
    clock = [0.0]
    world = stub_world(clock=lambda: clock[0])
    world.start()
    world.run("max", until=2000)
    world.advance()
    assert world.state == "Paused" and world.now == 2000
    clock[0] = 90
    world.run(1)
    assert world.pacing.origin_wall == 90 and world.pacing.origin_us == 2000
    assert world.pacing.delay(3000, 2000) == pytest.approx(0.001)
    assert world.submit(1, Kind.BYTES_IN, b"live", 1).time_us == 2001
    world.set_ratio(0.5)
    assert world.pacing.delay(3000, 2000) == pytest.approx(0.002)


def test_scenario_has_valid_ping_and_boundary():
    config = scenario_config(SCENARIO)
    assert config.end_us == 5000000
    assert [event.time_us for event in config.inputs] == [1500000, 2800000]
    for event in config.inputs:
        assert event.kind == Kind.BYTES_IN
        assert event.payload[:3] == b"\xa5\x5a\x01"
        assert int.from_bytes(event.payload[-2:], "little") == binascii.crc_hqx(event.payload[2:-2], 0xffff)


def test_firmware_same_trace_ratio_one_and_max(vhost_elf, capsys):
    traces, elapsed = [], []
    for ratio in (1, "max"):
        wall = [0.0]
        world = World(scenario_config(SCENARIO, executable=str(vhost_elf)), clock=lambda: wall[0])
        world.api.poll = lambda seconds: wall.__setitem__(0, wall[0] + seconds + 1e-9)
        trace, replies, accepted = [], [], []
        world.on_outbox(Kind.TRACE, lambda radio, message: trace.append(message.payload + b"\n"))
        world.on_outbox(Kind.BYTES_OUT, lambda radio, message: replies.append(message.payload))
        world.on_input(accepted.append)
        try:
            world.start()
            started = time.monotonic()
            world.run(ratio)
            while world.state != "Ended":
                assert time.monotonic() - started < 60, "pacing scenario did not finish"
                world.advance()
            elapsed.append(wall[0])
            traces.append(b"".join(trace))
            assert world.reason == "WORLD_END_TIME" and world.now == 5000000
            assert [event.time_us for event in accepted] == [1500000, 2800000]
            assert b"\xa5\x5a\x81\x07" in b"".join(replies), "boundary PING was not answered"
            assert b"CREATE host" in traces[-1] and b"000005000000 " in traces[-1]
        finally:
            world.close()
    assert traces[0] == traces[1]
    assert elapsed[0] == pytest.approx(5) and elapsed[1] == 0
    with capsys.disabled():
        print(f"\npacing sha256sum {hashlib.sha256(traces[0]).hexdigest()}  trace (r=1 == max)")
        print(f"r=1 wall={elapsed[0]:.6f}s; max wall={elapsed[1]:.6f}s; end=5000000us")
