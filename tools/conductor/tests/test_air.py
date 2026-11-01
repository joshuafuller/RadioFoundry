# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Grant barriers and air transport, plus an unskipped two-run firmware proof."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading

import pytest

from tools.conductor import airmodel
from tools.conductor.channel import Kind
from tools.conductor.parts import air, airctl
from tools.conductor.tests.conftest import RadioStub
from tools.conductor.world import Config, World, WorldError

SCENARIO = Path(__file__).resolve().parents[1] / "scenarios/air_pair.json"


def record(start=500, *, kind=1, payload=b"header"):
    return air.Record(1, kind, start, 1, 0, 1, 0, bytes(9), payload)


def setup(world, groups=((1, 2),), **options):
    part = air.install(world)
    airctl.install(world)
    events = []
    world.api.notify = events.append
    part.configure({"groups": [list(g) for g in groups], **options})
    world.start()
    world.run("max")
    return part, events


def air_inputs(world, radio):
    return [m for m in world.radios[radio].channel.sent if m.kind == Kind.AIR_IN]


def advance_to(world, time_us):
    """Advance until world time reaches time_us: a burst is decided at its hand-over, start + 29999 us."""
    while world.now < time_us and world.state != "Ended":
        world.advance()
    assert world.now == time_us


def test_reversed_arrivals_are_read_in_radio_order():
    answered = threading.Event()
    arrival, read = [], []

    class Reversed(RadioStub):
        def receive(self):
            message = super().receive()
            if message.kind == Kind.AIR_OUT:
                radio = int(self.args[self.args.index("--radio-index") + 1])
                if radio == 1:
                    assert answered.wait(5), "radio 2 never answered"
                arrival.append(radio)
                if radio == 2:
                    answered.set()
            return message

    world = World(Config(radios=3, end_us=100_000), channel_factory=Reversed)
    try:
        _, events = setup(world, ((1, 2, 3),), radios=[{"radio": 1}, {"radio": 2, "radio_index": 2}])
        world.on_outbox(Kind.AIR_OUT, lambda r, m: read.append(r))
        for radio in (1, 2):
            world.radios[radio].channel.outputs.append((Kind.AIR_OUT, record().encode()))
        world.advance()
        assert arrival == [2, 1] and read == [1, 2]
        assert not any(e["event"] == "air_decision" for e in events)  # decided at the hand-over, not now
        advance_to(world, air.handover_us(500))
        collisions = [e for e in events if e["event"] == "air_decision" and e["rule"] == "collision"]
        assert [e["sender"] for e in collisions] == [1, 2]
        assert all(e["receivers"] == (3,) for e in collisions)
        assert not any(e["event"] == "air_delivery" for e in events)
    finally:
        world.close()


def advance_until_air(world, radio):
    while not air_inputs(world, radio):
        world.advance()
    return air_inputs(world, radio)


def test_delivery_waits_for_slot_end_and_excludes_other_air(stub_world):
    # A burst starting at 500 us is handed over at its slot's last microsecond, 500 + 30000 - 1, not at a grant end.
    world = stub_world(radios=3, end_us=100_000)
    _, events = setup(world)
    grant_ends = []
    world.on_grant_done(lambda target, outboxes: grant_ends.append(target))
    world.radios[1].channel.outputs.append((Kind.AIR_OUT, record().encode()))
    world.advance()
    assert world.now == 1000
    assert not air_inputs(world, 2)
    received, = advance_until_air(world, 2)
    assert 30_499 in grant_ends  # the queued AIR_IN ended a grant there (World._target)
    assert (received.time_us, received.cls, received.source) == (30_499, 20, 1)
    assert air.Record.decode(received.payload) == record()
    assert not air_inputs(world, 1) and not air_inputs(world, 3)
    delivered, = [e for e in events if e["event"] == "air_delivery"]
    assert (delivered["air_start_us"], delivered["input_us"], delivered["time_us"]) == (500, 30_499, 30_499)
    decisions = [e for e in events if e["event"] == "air_decision"]
    assert decisions and all((e["t_us"], e["time_us"]) == (500, 30_499) for e in decisions)


def use_grant(world, part, grant_us):
    """Grant grant_us at a time while the air is shared, as the lookahead part does for its stated value."""
    index = world._grant_limits.index(part.grant_limit)
    world._grant_limits[index] = lambda now: now + grant_us if part.grant_limit(now) is not None else None


@pytest.mark.parametrize("grant_us", [500, 1000, 30_000])
def test_stamp_and_records_do_not_depend_on_grant(stub_world, grant_us):
    # The start drain covers 0, so the first grant runs from 1. A burst sent at 1 is due at 1 + 29 999.
    world = stub_world(radios=2, end_us=200_000)
    part, events = setup(world)
    use_grant(world, part, grant_us)
    world.radios[1].channel.outputs.append((Kind.AIR_OUT, record(1).encode()))
    world.advance()
    assert world.now == min(grant_us, 30_000)  # the stub's next event is due at its grant end
    received, = advance_until_air(world, 2)
    assert received.time_us == 30_000
    records = [e for e in events if e["event"] in ("air_delivery", "air_decision")]
    assert [(e["event"], e["time_us"]) for e in records] == [("air_decision", 30_000), ("air_delivery", 30_000)]


def out_of_step_records(world, part, events, grant_us, bursts):
    """Send each (radio, start) burst in the grant that covers its start, run to 150 ms, return the air records."""
    use_grant(world, part, grant_us)
    waiting = sorted(bursts, key=lambda b: b[1])
    while world.now < 150_000:
        target = world._target()
        while waiting and waiting[0][1] < target:
            radio, start = waiting.pop(0)
            assert start >= world.next_start
            world.radios[radio].channel.outputs.append((Kind.AIR_OUT, record(start).encode()))
        world.advance()
    assert not waiting
    return [(e["event"], e["time_us"], e.get("rule"), e.get("receivers"), e.get("receiver"))
            for e in events if e["event"] in ("air_decision", "air_delivery")]


def test_out_of_step_bursts_collide_by_air_overlap_at_any_grant(stub_world):
    """Radio 1 at 1 us and radio 2 at 10 001 us share 20 ms of air. Radio 3 hears a collision for both, and the two
    senders block each other (half duplex), at 500 us, 1 ms and 30 ms grants alike. With grant spans as the group,
    the 1 ms run delivered both bursts to radio 3 and the 30 ms run did not. Radio 1's burst at 70 001 us shares
    no air with either, so radios 2 and 3 both get it."""
    runs = {}
    for grant_us in (500, 1000, 30_000):
        world = stub_world(radios=3, end_us=200_000)
        part, events = setup(world, ((1, 2, 3),))
        runs[grant_us] = out_of_step_records(world, part, events, grant_us, [(1, 1), (2, 10_001), (1, 70_001)])
        assert [m.time_us for m in air_inputs(world, 3)] == [70_001 + 29_999]
    assert runs[500] == runs[1000] == runs[30_000]
    assert ("air_decision", 30_000, "collision", (3,), None) in runs[1000]
    assert ("air_decision", 40_000, "collision", (3,), None) in runs[1000]
    assert ("air_decision", 40_000, "half_duplex", (1,), None) in runs[1000]
    assert [r for r in runs[1000] if r[0] == "air_delivery"] == [
        ("air_delivery", 100_000, None, None, 2), ("air_delivery", 100_000, None, None, 3)]


def test_decision_waits_for_overlap_and_skips_a_receiver_turned_off(stub_world):
    """A burst is decided at its hand-over, so a burst that starts later but still overlaps it counts. A receiver
    switched off before the hand-over gets nothing, and the world keeps running."""
    world = stub_world(radios=3, end_us=200_000)
    part, events = setup(world, ((1, 2, 3),))
    world.radios[1].channel.outputs.append((Kind.AIR_OUT, record(1).encode()))
    world.advance()
    assert part.delivery_limit(world.now) == 1 + 29_999
    assert not any(e["event"] == "air_decision" for e in events)
    world.power_off(3)
    advance_to(world, 30_000)
    assert world.state == "Running"
    assert [(e["receiver"], e["input_us"]) for e in events if e["event"] == "air_delivery"] == [(2, 30_000)]
    assert not part.pending and not part.recent  # a later burst starts at 30 000 or after: no shared air left


def test_delivery_limit_follows_the_next_event_due(stub_world):
    world = stub_world(radios=3, end_us=1_000_000)
    part, _ = setup(world, ((1, 2),))
    for radio, due in ((1, 70_000), (2, 40_000), (3, 5000)):  # radio 3 is not on the shared air
        world.radios[radio].done = replace(world.radios[radio].done, next_due_us=due)
    assert part.delivery_limit(world.now) == 40_000 + 29_999
    world.radios[2].done = replace(world.radios[2].done, next_due_us=world.now)
    assert part.delivery_limit(world.now) == world.now + 29_999
    world.power_off(2)
    assert part.delivery_limit(world.now) is None


def test_grant_past_the_handover_is_refused(stub_world):
    world = stub_world(radios=2, end_us=200_000)
    part, _ = setup(world)
    # Without the delivery limit, a grant from 0 to 30 001 carries a burst sent at 1, due at 30 000: too late.
    use_grant(world, part, 30_001)
    world._grant_limits.remove(part.delivery_limit)
    world.radios[1].channel.outputs.append((Kind.AIR_OUT, record(1).encode()))
    with pytest.raises(WorldError, match="WORLD_BAD_AIR_TIME"):
        world.advance()


@pytest.mark.parametrize("groups, expected", [(((1, 2),), 1000), (((1,), (2,)), 100_000), ((), 100_000)])
def test_grant_limit_only_on_shared_air(stub_world, groups, expected):
    world = stub_world(radios=2, end_us=100_000)
    part, _ = setup(world, groups)
    assert world._target() == expected
    if expected == 1000:
        world.power_off(2)
        assert part.grant_limit(world.now) is None
        assert world._target() == 100_000


def test_drain_burst_and_radio_origins(stub_world):
    world = stub_world(radios=2, end_us=100_000)
    _, _ = setup(world)
    world.advance()
    world.pause()
    world.restart(2)
    assert world.radios[2].origin == 1000
    world.run("max")
    world.radios[1].channel.outputs.append((Kind.AIR_OUT, record(1500).encode()))
    world.advance()
    world.advance()
    received, = advance_until_air(world, 2)
    assert received.time_us == 1500 + 29_999 - 1000  # receiver origin 1000
    assert air.Record.decode(received.payload).t_us == 500
    sent = world.now
    world.radios[1].channel.outputs.append((Kind.AIR_OUT, record(sent).encode()))
    world.pause()  # the burst comes out in the drain at sent, which includes sent itself
    world.run("max")
    while len(air_inputs(world, 2)) == 1:
        world.advance()
    received = air_inputs(world, 2)[-1]
    assert received.time_us == sent + 29_999 - 1000
    assert air.Record.decode(received.payload).t_us == sent - 1000


def test_sequences_follow_radio_order_across_air_groups(stub_world):
    world = stub_world(radios=4, end_us=100_000)
    setup(world, ((1, 4), (2, 3)))
    for radio in (4, 2):
        world.radios[radio].channel.outputs.append((Kind.AIR_OUT, record().encode()))
    world.advance()
    advance_until_air(world, 3)
    first, = air_inputs(world, 3)
    second, = air_inputs(world, 1)
    assert first.source == 2 and second.source == 4 and first.sequence < second.sequence


def test_policy_delay_duplicate_tuning_and_seed(stub_world, monkeypatch):
    world = stub_world(radios=4, end_us=100_000)
    _, events = setup(world, ((1, 2, 3, 4),), seed=19, radios=[
        {"radio": 1, "identity": 3100001}, {"radio": 2, "identity": 3100002},
        {"radio": 3, "channel": 2}, {"radio": 4, "cc": 2}])
    seen = []
    decide = airmodel.decide

    def capture(span, out, policy, seed, context=()):
        seen.append((seed, policy.identities))
        return decide(span, out, policy, seed, context)

    monkeypatch.setattr(airmodel, "decide", capture)
    for command, fields in (("air_delay", {"delay_us": 5000}), ("air_duplicate", {"enabled": True})):
        assert world.api.dispatch({"version": 1, "command": command, "sender": 1,
                                   "receiver": 2, "ordinal": 1, **fields})["ok"]
    world.radios[1].channel.outputs.append((Kind.AIR_OUT, record().encode()))
    advance_to(world, air.handover_us(500))
    deliveries = [e for e in events if e["event"] == "air_delivery"]
    assert [(e["receiver"], e["air_start_us"], e["input_us"], e["copy"]) for e in deliveries] == [
        (2, 5500, 35_499, 0), (2, 5500, 35_499, 1)]
    assert seen[0][0] == 19 and dict(seen[0][1])[1] == 3100001
    assert {e["rule"] for e in events if e["event"] == "air_decision"} >= {"channel", "color_code"}


def test_voice_corruption_refused_without_losing_data_or_stopping(stub_world):
    world = stub_world(radios=2, end_us=100_000)
    _, events = setup(world)
    for ordinal in (1, 2):
        assert world.api.dispatch({"version": 1, "command": "air_corrupt", "sender": 1,
                                   "receiver": 2, "ordinal": ordinal, "xor_hex": "ff"})["ok"]
    for kind in (16, 1):
        world.radios[1].channel.outputs.append((Kind.AIR_OUT, record(kind=kind, payload=b"a").encode()))
    advance_to(world, air.handover_us(500))
    assert world.state == "Running"
    error, = [e for e in events if e["event"] == "air_error"]
    assert error["reason"] == "WORLD_CORRUPT_NEEDS_DATA"
    while len(air_inputs(world, 2)) < 2:
        world.advance()
    received = [air.Record.decode(m.payload) for m in air_inputs(world, 2)]
    assert [(r.payload, r.flags) for r in received] == [(b"a", 0), (b"\x9e", 4)]


def test_input_classes_remain_kind_specific(stub_world):
    world = stub_world(radios=1)
    world.start()
    world.run("max")
    for kind, good, bad in ((Kind.KEY, 30, 20), (Kind.AIR_IN, 20, 30)):
        world.submit(1, kind, b"\0\1", 1, cls=good)
        with pytest.raises(WorldError, match="WORLD_BAD_INPUT_CLASS"):
            world.submit(1, kind, b"\0\1", 1, cls=bad)


@pytest.mark.parametrize("change", [{"ts": 3}, {"kind": 31}, {"cc": -2}, {"voice_index": 6},
                                    {"sync": 5}, {"flags": 8}, {"t_us": (1 << 64) - 1}, {"payload": bytes(28)}])
def test_bad_air_records(change):
    with pytest.raises(WorldError, match="WORLD_BAD_AIR"):
        air.Record.decode(replace(record(), **change).encode())


def test_lengths_and_config_validation(stub_world):
    wire = record().encode()
    for broken in (b"", wire[:23], wire[:-1], wire + b"x"):
        with pytest.raises(WorldError, match="WORLD_BAD_AIR"):
            air.Record.decode(broken)
    world = stub_world(radios=2)
    part = air.install(world)
    for request in ({"groups": [[1, 2], [2]]}, {"groups": [[3]]}, {"seed": -1},
                    {"radios": [{"radio": 1, "cc": 16}]}, {"radios": [{"radio": 1, "radio_index": 1000}]},
                    {"radios": [{"radio": 1, "storage": "dmr-trio"}]}):
        reply = world.api.dispatch({"version": 1, "command": "air_config", **request})
        assert not reply["ok"]
    assert not part.groups


def test_scenario_start_options_and_ptt():
    world = air.scenario_world(SCENARIO, channel_factory=RadioStub)
    try:
        world.start()
        for radio, slot in world.radios.items():
            assert slot.channel.args[:6] == ("--host-port", "on", "--storage", "dmr-pair", "--radio-index", str(radio))
        assert [(e.radio, e.time_us, e.payload) for e in world.config.inputs] == [
            (1, 3_000_000, b"\x04\x01"), (1, 4_000_000, b"\x04\x00")]
        assert world.config.end_us == 5_000_000
        assert not world.api.dispatch({"version": 1, "command": "air_config"})["ok"]
    finally:
        world.close()


def test_firmware_pair_repeats(vhost_elf, tmp_path):
    """Needs Unix sockets. Never skip on EPERM: the lead runs this outside the sandbox."""
    hashes = []
    for run in (1, 2):
        world = air.scenario_world(SCENARIO)
        traces = {1: bytearray(), 2: bytearray()}
        deliveries = []
        world.on_outbox(Kind.TRACE, lambda r, m: traces[r].extend(m.payload + b"\n"))
        world.api.notify = lambda e: deliveries.append(e) if e["event"] == "air_delivery" else None
        try:
            world.start()
            world.run("max")
            while world.state != "Ended":
                world.advance()
            assert (world.now, world.reason) == (5_000_000, "WORLD_END_TIME")
            rx = [line for line in traces[2].splitlines() if b"dmr.rx " in line]
            assert any(b"kind=voice_lc_header" in line for line in rx)
            assert any(b"kind=voice_" in line and b"kind=voice_lc_header" not in line for line in rx)
            content = [bytes(traces[1]), bytes(traces[2]),
                       b"".join((json.dumps(e, sort_keys=True) + "\n").encode() for e in deliveries)]
            hashes.append([hashlib.sha256(data).hexdigest() for data in content])
            for name, data in zip(("radio-1.trace", "radio-2.trace", "air-deliveries.jsonl"), content):
                (tmp_path / f"run-{run}-{name}").write_bytes(data)
                print(f"run-{run} {name} {hashlib.sha256(data).hexdigest()}")
            print(next(line.decode() for line in rx if b"kind=voice_lc_header" in line))
            print(next(line.decode() for line in rx if b"kind=voice_" in line and b"kind=voice_lc_header" not in line))
        finally:
            world.close()
    assert hashes[0] == hashes[1]


GRANTS = (500, 1000, 30_000)

# Three radios out of step: slot edges at 0, 10 ms and 20 ms past each 30 ms. Radios 1 and 2 talk at once from
# 3.0 s, so their voice bursts overlap on air by 20 ms; radio 3 only listens. With grant spans as the group, a 1 ms
# grant never holds both bursts (no collision) while a 30 ms grant often does (collision): the run then depended on
# the grant. Grouped by air overlap, radio 3 hears a collision at every grant size.
THREE = {
    "SPDX-License-Identifier": "MIT", "copyright": "Copyright (c) 2026 The RadioFoundry Authors",
    "version": 1, "radios": 3, "end_us": 4_200_000,
    "air": {"groups": [[1, 2, 3]], "seed": 0, "radios": [
        {"radio": 1, "storage": "dmr-pair", "radio_index": 1, "identity": 3100001, "channel": 1, "cc": 1, "ts": 1},
        {"radio": 2, "storage": "dmr-pair", "radio_index": 1, "identity": 3100003, "channel": 1, "cc": 1, "ts": 1},
        {"radio": 3, "storage": "dmr-pair", "radio_index": 2, "identity": 3100002, "channel": 1, "cc": 1, "ts": 1}]},
    "inputs": [
        {"radio": 1, "kind": "KEY", "payload_hex": "0401", "time_us": 3_000_000, "source": 100},
        {"radio": 2, "kind": "KEY", "payload_hex": "0401", "time_us": 3_000_000, "source": 100},
        {"radio": 1, "kind": "KEY", "payload_hex": "0400", "time_us": 3_900_000, "source": 100},
        {"radio": 2, "kind": "KEY", "payload_hex": "0400", "time_us": 3_900_000, "source": 100}],
}
PHASES = {1: 0, 2: 10_000, 3: 20_000}


def firmware_run(scenario, grant_us, tmp_path, *, radio_args, phases=None):
    """Run one scenario file on the real firmware with grants of grant_us. Returns (contents, records, requests):
    the radio traces and the air records (air_decision and air_delivery events) as bytes each, the air records as
    dicts, and every BYTES_IN input (debug link frames) the radios got, as (radio, time_us, payload)."""
    from tools.conductor.conductor import install_parts, scenario_config
    data = json.loads(Path(scenario).read_text())
    world = World(scenario_config(scenario, radios=data["radios"], radio_args=radio_args))
    traces = {r: bytearray() for r in world.boots}
    records, requests = [], []
    grants = [0, 0]  # largest grant, last grant end
    try:
        install_parts(world)
        world.on_input(lambda e: requests.append(e) if e.kind == Kind.BYTES_IN else None)
        reply = world.api.dispatch({**data["air"], "version": 1, "command": "air_config",
                                    "lookahead_proven": grant_us > 1000})
        assert reply["ok"], reply
        if phases:
            world.add_radio_args(lambda r: ("--dmr-phase-us", str(phases[r])))
        if grant_us < 1000:
            world.add_grant_limit(lambda now: now + grant_us)
        world.on_outbox(Kind.TRACE, lambda r, m: traces[r].extend(m.payload + b"\n"))

        def barrier(target, outboxes):
            grants[:] = [max(grants[0], target - grants[1]), target]

        world.on_grant_done(barrier)
        world.api.notify = lambda e: records.append(e) if e["event"] in ("air_delivery", "air_decision") else None
        world.start()
        world.run("max")
        while world.state != "Ended":
            world.advance()
        assert (world.now, world.reason) == (data["end_us"], "WORLD_END_TIME")
        assert grants[0] == grant_us
        content = [bytes(traces[r]) for r in sorted(traces)]
        content.append(b"".join((json.dumps(e, sort_keys=True, default=list) + "\n").encode() for e in records))
        content.append("".join(f"{e.radio} {e.time_us} {e.payload.hex()}\n" for e in requests).encode())
        names = [f"radio-{r}.trace" for r in sorted(traces)] + ["air-records.jsonl", "bytes-in.txt"]
        for name, item in zip(names, content):
            (tmp_path / f"grant-{grant_us}-{name}").write_bytes(item)
        print(f"grant_us={grant_us} largest={grants[0]} sha256 "
              + " ".join(hashlib.sha256(item).hexdigest() for item in content))
        return content, records, requests
    finally:
        world.close()


@pytest.mark.parametrize("host_port", ["off", "on"])
def test_firmware_pair_same_run_for_any_grant(vhost_elf, tmp_path, host_port):
    """ADR 0005 and 0008: air_pair at 500 us, 1 ms and 30 ms grants gives the same radio traces and air records.
    With the host port on, the debug link cache refresh (dlmux.py) runs too: every 250 ms it reads VERSION,
    INFO_CHIP, GET_VIEW, GET_DIAG and GET_LOG, one request at a time, each on the 1 ms grid after the last reply.
    Needs Unix sockets. Never skip on EPERM: the lead runs this outside the sandbox."""
    radio_args = ("--host-port", "on") if host_port == "on" else ()
    runs = {}
    for grant_us in GRANTS:
        runs[grant_us], records, requests = firmware_run(SCENARIO, grant_us, tmp_path, radio_args=radio_args)
        rx = [line for line in runs[grant_us][1].splitlines() if b"dmr.rx " in line]
        assert any(b"kind=voice_lc_header" in line for line in rx)
        assert any(e["event"] == "air_delivery" for e in records)
        # With the host port on, the cache refresh really reached the firmware, 20 refreshes in 5 s.
        assert (len({e.time_us // 250_000 for e in requests}) >= 20) == (host_port == "on"), len(requests)
    assert runs[500] == runs[1000] == runs[30_000]


def test_firmware_three_radios_out_of_step_same_air_for_any_grant(vhost_elf, tmp_path):
    """Three radios with different slot phases (0, 10 ms, 20 ms), two talking at once: the same air decisions,
    deliveries and traces at 500 us, 1 ms and 30 ms grants. Radio 3 hears both talkers' bursts overlap on air, so
    it gets collisions, never one talker's voice."""
    scenario = tmp_path / "three.json"
    scenario.write_text(json.dumps(THREE))
    runs = {}
    for grant_us in GRANTS:
        runs[grant_us], records, _ = firmware_run(scenario, grant_us, tmp_path, radio_args=(), phases=PHASES)
        decisions = [e for e in records if e["event"] == "air_decision"]
        starts = {e["sender"]: e["t_us"] % airmodel.SLOT_US for e in decisions}
        assert starts == {1: 0, 2: 10_000}, starts
        assert any(e["rule"] == "collision" and 3 in e["receivers"] for e in decisions)
    assert runs[500] == runs[1000] == runs[30_000]
