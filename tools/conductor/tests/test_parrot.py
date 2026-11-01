# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The parrot and repeater echo air elements (parts/parrot.py) on stub radios."""

from dataclasses import replace

import pytest

from tools.conductor.channel import Kind
from tools.conductor.conductor import install_parts
from tools.conductor.parts import air, parrot
from tools.conductor.world import WorldError

LC = bytes.fromhex("000000092f4d612f4d")    # stands in for the link control; the parrot must never change it


def burst(start, kind):
    return air.Record(1, kind, start, 1, 0, 1, 2 if kind == parrot.VOICE else 1, LC,
                      bytes(27 if kind == parrot.VOICE else 12))


def configure(world, **options):
    install_parts(world)
    events = []
    world.api.notify = events.append
    reply = world.api.dispatch({"version": 1, "command": "air_config", "groups": [[1, 2, 3]],
                                "radios": [{"radio": r, "radio_index": r, "ts": 1, "cc": 1} for r in (1, 2, 3)],
                                **options})
    assert reply["ok"], reply
    world.start()
    world.run("max")
    return reply, events


def send(world, radio, records):
    """Each record in the grant that covers its start (a radio sends a burst only at its slot edge)."""
    waiting = sorted(records, key=lambda r: r.t_us)
    while waiting:
        target = world._target()
        while waiting and waiting[0].t_us < target:
            world.radios[radio].channel.outputs.append((Kind.AIR_OUT, waiting.pop(0).encode()))
        world.advance()


def run_to(world, time_us):
    while world.now < time_us and world.state != "Ended":
        world.advance()


def air_in(world, radio):
    return [m for m in world.radios[radio].channel.sent if m.kind == Kind.AIR_IN]


def call(first=60_000, voice=6):
    return ([burst(first, parrot.HEADER)] + [burst(first + 60_000 * (i + 1), parrot.VOICE) for i in range(voice)]
            + [burst(first + 60_000 * (voice + 1), parrot.TERMINATOR)])


def test_without_config_nothing_changes(stub_world):
    world = stub_world(radios=3, end_us=3_000_000)
    _, events = configure(world)
    send(world, 1, call())
    run_to(world, 2_900_000)
    assert world.parrot.grant_limit(world.now) is None
    assert not [e for e in events if e.get("element")]
    assert not air_in(world, 1)    # the air model never sends a radio its own burst


def test_replay_after_the_terminator_with_the_record_unchanged(stub_world):
    world = stub_world(radios=3, end_us=3_000_000)
    reply, events = configure(world, parrot={"replay": [1], "gap_us": 1_000_000})
    assert reply["result"]["parrot"] == {"replay": [1], "gap_us": 1_000_000, "echo": [], "echo_us": 90_000}
    sent = call()
    send(world, 1, sent)
    run_to(world, 2_900_000)
    # Terminator at 480 000; 1 s later, on the frame grid of the call's first burst: 1 500 000
    shift = 1_500_000 - 60_000
    copies = [e for e in events if e.get("element") == "parrot"]
    assert sorted({e["receiver"] for e in copies}) == [1, 2, 3]   # the sender too
    for radio in (1, 3):
        got = [air.Record.decode(m.payload) for m in air_in(world, radio) if m.time_us >= 1_000_000]
        assert got == [replace(r, t_us=r.t_us + shift) for r in sent]
        assert all(g.lc == LC for g in got)
        assert [m.time_us for m in air_in(world, radio) if m.time_us >= 1_000_000] == [
            air.handover_us(r.t_us + shift) for r in sent]
        assert all(m.cls == 20 and m.source == 1 for m in air_in(world, radio))


def test_echo_goes_to_the_sender_only_a_whole_frame_late(stub_world):
    world = stub_world(radios=3, end_us=1_000_000)
    _, events = configure(world, parrot={"echo": [1], "echo_us": 90_000})
    send(world, 1, call(voice=2))
    run_to(world, 900_000)
    copies = [e for e in events if e.get("element") == "echo"]
    assert {e["receiver"] for e in copies} == {1}
    assert [e["air_start_us"] for e in copies] == [60_000 * k + 120_000 for k in range(1, 5)]   # 90 ms -> 120 ms


def test_copies_do_not_depend_on_the_grant(stub_world):
    runs = {}
    for grant_us in (500, 1000, 30_000):
        world = stub_world(radios=3, end_us=2_000_000)
        configure(world, parrot={"replay": [1], "gap_us": 300_000, "echo": [1]})
        world.add_grant_limit(lambda now, grant_us=grant_us: now + grant_us)
        send(world, 1, call(voice=3))
        run_to(world, 1_900_000)
        runs[grant_us] = [(m.time_us, m.payload) for m in air_in(world, 1)]
        assert runs[grant_us] and runs[grant_us] == sorted(runs[grant_us], key=lambda s: s[0])
    assert runs[500] == runs[1000] == runs[30_000]


def test_parrot_config_command_and_refusals(stub_world):
    world = stub_world(radios=3, end_us=1_000_000)
    install_parts(world)
    world.api.notify = lambda event: None
    refused = world.api.dispatch({"version": 1, "command": "parrot_config", "replay": [1]})
    assert refused["error"] == "WORLD_PARROT_NO_AIR"
    assert world.api.dispatch({"version": 1, "command": "air_config", "groups": [[1, 2]]})["ok"]
    for request, reason in (({"replay": [4]}, "WORLD_UNKNOWN_RADIO"), ({"replay": [3]}, "WORLD_PARROT_NO_AIR"),
                            ({"replay": [1, 1]}, "WORLD_BAD_PARROT"), ({"gap_us": 0}, "WORLD_BAD_PARROT_GAP"),
                            ({"tail": 1}, "WORLD_BAD_PARROT")):
        assert world.api.dispatch({"version": 1, "command": "parrot_config", **request})["error"] == reason
    assert world.api.dispatch({"version": 1, "command": "parrot_config", "replay": [1, 2]})["ok"]
    world.start()
    with pytest.raises(WorldError, match="WORLD_PARROT_ALREADY_STARTED"):
        world.parrot.configure({"replay": [1]})
