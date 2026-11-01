# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Shared air policy checks: docs/dev/virtual-radio-architecture.md, "Shared air policy".

Each case checks a named rule or its ordering from tools/conductor/airmodel.py.
"""

import hashlib
import random
import struct

import pytest

from tools.conductor.airmodel import (AirError, Burst, Decision, Delivery, Policy, Tuning, decide, loss_sample,
                                      lost, overlap)

SPAN = (3_000_000, 3_001_000)
PAYLOAD = bytes(range(33))


def burst(radio, ordinal=1, t_us=3_000_000, **fields):
    return Burst(radio, ordinal, t_us, fields.pop("payload", PAYLOAD), **fields)


def rules(result):
    return [(d.rule, d.sender, d.burst, d.receivers) for d in result.decisions]


def test_deliver_one_sender_to_every_other_radio():
    """One sender reaches every other radio once, at its start on air."""
    result = decide(SPAN, {1: [burst(1)], 2: [], 3: []}, Policy(), 0)
    assert result.deliveries == (Delivery(3_000_000, 2, 1, 1, 0, PAYLOAD), Delivery(3_000_000, 3, 1, 1, 0, PAYLOAD))
    assert rules(result) == [("deliver", 1, 1, (2, 3))]


def test_link_down_loses_every_burst_at_the_sender():
    """With the air off nobody hears anything."""
    result = decide(SPAN, {1: [burst(1)], 2: []}, Policy(link_up=False), 0)
    assert not result.deliveries and rules(result) == [("link_down", 1, 1, (2,))]


def test_dropped_burst_is_lost_for_every_receiver():
    """Burst 5 is lost; bursts 4 and 6 arrive."""
    outboxes = {1: [burst(1, n, 3_000_000 + n) for n in (4, 5, 6)], 2: [], 3: []}
    result = decide(SPAN, outboxes, Policy(drops={(1, 5)}), 0)
    assert sorted({(d.sender, d.burst) for d in result.deliveries}) == [(1, 4), (1, 6)]
    assert ("drop", 1, 5, (2, 3)) in rules(result)


def test_channel_must_match():
    """A burst on channel 1 never reaches a receiver tuned to channel 2."""
    policy = Policy(tuning={2: Tuning(channel=2)})
    result = decide(SPAN, {1: [burst(1)], 2: [], 3: []}, policy, 0)
    assert [d.receiver for d in result.deliveries] == [3]
    assert ("channel", 1, 1, (2,)) in rules(result)


def test_timeslot_must_match_when_both_sides_know_it():
    """Known timeslots must match; an unknown timeslot does not filter."""
    policy = Policy(tuning={2: Tuning(ts=2), 3: Tuning(ts=1)})
    result = decide(SPAN, {1: [burst(1, ts=1)], 2: [], 3: []}, policy, 0)
    assert [d.receiver for d in result.deliveries] == [3]
    assert ("timeslot", 1, 1, (2,)) in rules(result)
    unknown = decide(SPAN, {1: [burst(1, ts=0)], 2: []}, policy, 0)
    assert [d.receiver for d in unknown.deliveries] == [2]


def test_color_code_must_match_when_both_sides_set_it():
    """Known color codes must match; an unspecified code does not filter."""
    policy = Policy(tuning={2: Tuning(cc=3), 3: Tuning(cc=1)})
    result = decide(SPAN, {1: [burst(1, cc=1)], 2: [], 3: [], 4: []}, policy, 0)
    assert [d.receiver for d in result.deliveries] == [3, 4]
    assert ("color_code", 1, 1, (2,)) in rules(result)


def test_disabled_link_is_one_direction():
    """Disabling the return path does not disable the forward path."""
    policy = Policy(down_links={(2, 1)})
    result = decide(SPAN, {1: [burst(1)], 2: []}, policy, 0)
    assert [d.receiver for d in result.deliveries] == [2]
    back = decide(SPAN, {1: [], 2: [burst(2)]}, policy, 0)
    assert not back.deliveries and rules(back) == [("link_disabled", 2, 1, (1,))]


def test_half_duplex_sending_radios_hear_nothing():
    """Both radios send overlapping bursts, so neither hears the other."""
    result = decide(SPAN, {1: [burst(1)], 2: [burst(2, t_us=3_000_500)]}, Policy(), 0)
    assert not result.deliveries
    assert rules(result) == [("half_duplex", 1, 1, (2,)), ("half_duplex", 2, 1, (1,))]


def test_collision_and_half_duplex_follow_air_overlap_not_the_span():
    """Bursts collide when they share air, wherever the span ends. Radio 2's burst starts 20 ms after radio 1's, in a
    later span: passed as context, it still blocks radio 1's burst at radio 3 (collision) and at radio 2 (half
    duplex). A burst that starts 29 999 us later (at radio 1's hand-over) or more shares no air that counts."""
    late = burst(2, t_us=3_020_000)
    result = decide(SPAN, {1: [burst(1)], 2: [], 3: []}, Policy(), 0, context=[late])
    assert rules(result) == [("collision", 1, 1, (3,)), ("half_duplex", 1, 1, (2,))]
    back = decide((3_020_000, 3_020_001), {1: [], 2: [late], 3: []}, Policy(), 0, context=[burst(1)])
    assert rules(back) == [("collision", 2, 1, (3,)), ("half_duplex", 2, 1, (1,))]
    for gap, heard in ((29_998, False), (29_999, True), (30_000, True)):
        result = decide(SPAN, {1: [burst(1)], 2: [], 3: []}, Policy(), 0,
                        context=[burst(2, t_us=SPAN[0] + gap)])
        assert (len(result.deliveries) == 2) is heard, gap
    with pytest.raises(AirError, match="WORLD_DUPLICATE_BURST"):
        decide(SPAN, {1: [burst(1)]}, Policy(), 0, context=[burst(1)])
    assert overlap(burst(1), burst(2, t_us=3_029_998)) and not overlap(burst(1), burst(2, t_us=3_029_999))


def test_collision_two_senders_nobody_hears():
    """Radio 3 hears two senders at once, so neither burst is delivered."""
    result = decide(SPAN, {1: [burst(1)], 2: [burst(2)], 3: []}, Policy(), 0)
    assert not result.deliveries
    assert ("collision", 1, 1, (3,)) in rules(result) and ("collision", 2, 1, (3,)) in rules(result)


def test_collision_ignores_senders_the_receiver_cannot_reach():
    """Disabled links and other channels do not create collisions."""
    policy = Policy(down_links={(2, 3)})
    result = decide(SPAN, {1: [burst(1)], 2: [burst(2)], 3: []}, policy, 0)
    assert [(d.receiver, d.sender) for d in result.deliveries] == [(3, 1)]
    other = decide(SPAN, {1: [burst(1)], 2: [burst(2, channel=2)], 3: []}, Policy(), 0)
    assert [(d.receiver, d.sender) for d in other.deliveries] == [(3, 1)]


def test_seeded_loss_matches_the_documented_formula():
    """SHA-256 of pack("!QIIQ", seed, sender id, receiver id, burst), first 8 bytes, lost when
    sample * 10**6 < rate * 2**64. Recomputed independently for 200 bursts."""
    policy = Policy(loss_ppm={(1, 2): 400_000}, identities={1: 1001, 2: 1002})
    outboxes = {1: [burst(1, n, 3_000_000 + n) for n in range(1, 201)], 2: []}
    result = decide(SPAN, outboxes, policy, 7)
    expected = set()
    for n in range(1, 201):
        digest = hashlib.sha256(struct.pack("!QIIQ", 7, 1001, 1002, n)).digest()
        if int.from_bytes(digest[:8], "big") * 1_000_000 < 400_000 * (1 << 64):
            expected.add(n)
    assert {d.burst for d in result.decisions if d.rule == "loss"} == expected
    assert {d.burst for d in result.deliveries} == set(range(1, 201)) - expected
    assert 40 < len(expected) < 120
    assert decide(SPAN, outboxes, policy, 7) == result
    assert decide(SPAN, outboxes, policy, 8) != result
    assert not lost(loss_sample(0, 1, 2, 1), 0) and lost(loss_sample(0, 1, 2, 1), 1_000_000)


def test_delay_moves_the_start_on_air():
    """A delay for one sender, receiver and burst leaves other deliveries on time."""
    policy = Policy(delays_us={(1, 2, 1): 60_000})
    result = decide(SPAN, {1: [burst(1)], 2: [], 3: []}, policy, 0)
    assert [(d.receiver, d.air_start_us) for d in result.deliveries] == [(3, 3_000_000), (2, 3_060_000)]
    assert ("delay", 1, 1, (2,)) in rules(result)


def test_corrupt_xors_a_data_burst_and_refuses_voice():
    """XOR preserves data length; this policy refuses corruption of synthetic voice."""
    mask = bytes([0xFF]) + bytes(32)
    policy = Policy(corruptions={(1, 2, 1): mask})
    result = decide(SPAN, {1: [burst(1)], 2: []}, policy, 0)
    (delivery,) = result.deliveries
    assert delivery.corrupted and delivery.payload == bytes([0xFF]) + PAYLOAD[1:]
    assert ("corrupt", 1, 1, (2,)) in rules(result)
    with pytest.raises(AirError, match="WORLD_CORRUPT_NEEDS_DATA"):
        decide(SPAN, {1: [burst(1, kind="voice")], 2: []}, policy, 0)
    short = decide(SPAN, {1: [burst(1)], 2: []}, Policy(corruptions={(1, 2, 1): b"\x01"}), 0)
    assert short.deliveries[0].payload == bytes([1]) + PAYLOAD[1:]
    long = decide(SPAN, {1: [burst(1, payload=b"\x00\x00")], 2: []}, Policy(corruptions={(1, 2, 1): b"\x01" * 5}), 0)
    assert long.deliveries[0].payload == b"\x01\x01"


def test_duplicate_delivers_twice_the_damaged_copy_when_corrupted():
    """Both duplicate deliveries contain the corrupted payload."""
    mask = bytes(32) + b"\x01"
    policy = Policy(duplicates={(1, 3, 6)}, corruptions={(1, 3, 6): mask})
    result = decide(SPAN, {1: [burst(1, 6, kind="data")], 2: [], 3: []}, policy, 0)
    copies = [(d.receiver, d.copy, d.corrupted) for d in result.deliveries]
    assert copies == [(2, 0, False), (3, 0, True), (3, 1, True)]
    voice = decide(SPAN, {1: [burst(1, 6, kind="voice")], 3: []}, Policy(duplicates={(1, 3, 6)}), 0)
    assert len(voice.deliveries) == 2 and ("duplicate", 1, 6, (3,)) in rules(voice)


def test_rule_order_channel_before_half_duplex_before_collision():
    """The first rejection wins: channel mismatch precedes half duplex and collision."""
    policy = Policy(tuning={2: Tuning(channel=2)})
    result = decide(SPAN, {1: [burst(1)], 2: [burst(2, channel=2)], 3: [burst(3)]}, policy, 0)
    assert ("channel", 1, 1, (2,)) in rules(result)
    assert ("half_duplex", 1, 1, (3,)) in rules(result)


def test_bad_inputs_are_refused():
    """Malformed inputs fail with named errors."""
    with pytest.raises(AirError, match="WORLD_BURST_OUTSIDE_SPAN"):
        decide(SPAN, {1: [burst(1, t_us=SPAN[1])]}, Policy(), 0)
    with pytest.raises(AirError, match="WORLD_BAD_BURST"):
        decide(SPAN, {2: [burst(1)]}, Policy(), 0)
    with pytest.raises(AirError, match="WORLD_DUPLICATE_BURST"):
        decide(SPAN, {1: [burst(1), burst(1)]}, Policy(), 0)
    with pytest.raises(AirError, match="WORLD_BAD_SPAN"):
        decide((5, 5), {}, Policy(), 0)
    with pytest.raises(AirError, match="WORLD_BAD_LOSS_RATE"):
        Policy(loss_ppm={(1, 2): 1_000_001})


def scenario(rng):
    radios = rng.randint(2, 5)
    outboxes = {}
    for radio in range(1, radios + 1):
        count = rng.choice((0, 0, 1, 1, 2))
        outboxes[radio] = [burst(radio, n, rng.randrange(*SPAN), kind=rng.choice(("data", "voice")),
                                 channel=rng.choice((1, 1, 2)), cc=rng.choice((None, 1, 2)), ts=rng.randint(0, 2),
                                 payload=bytes(rng.randrange(256) for _ in range(33)))
                           for n in range(1, count + 1)]
    links = [(s, r) for s in outboxes for r in outboxes if s != r]
    data = [(b.radio, r, b.ordinal) for bs in outboxes.values() for b in bs if b.kind == "data"
            for r in outboxes if r != b.radio]
    every = [(b.radio, r, b.ordinal) for bs in outboxes.values() for b in bs for r in outboxes if r != b.radio]
    policy = Policy(
        link_up=rng.random() > 0.05,
        down_links=set(rng.sample(links, rng.randint(0, min(2, len(links))))),
        drops={(b.radio, b.ordinal) for bs in outboxes.values() for b in bs if rng.random() < 0.1},
        loss_ppm={link: rng.choice((0, 300_000, 1_000_000)) for link in rng.sample(links, rng.randint(0, min(3, len(links))))},
        delays_us={key: rng.choice((10_000, 60_000)) for key in rng.sample(every, min(2, len(every)))},
        corruptions={key: bytes([rng.randrange(1, 256)]) + bytes(32) for key in rng.sample(data, min(2, len(data)))},
        duplicates=set(rng.sample(every, min(2, len(every)))),
        tuning={radio: Tuning(rng.choice((1, 2)), rng.choice((None, 1, 2)), rng.randint(0, 2))
                for radio in rng.sample(sorted(outboxes), rng.randint(0, radios))})
    return outboxes, policy


def shuffled(outboxes, rng):
    keys = list(outboxes)
    rng.shuffle(keys)
    result = {}
    for key in keys:
        bursts = list(outboxes[key])
        rng.shuffle(bursts)
        result[key] = bursts
    return result


def test_property_same_inputs_same_output_any_order():
    """Collect every sender before deciding (ADR 0005). Over 200 fixed seeds, repeated and shuffled inputs
    give the same result."""
    decided = 0
    for seed in range(200):
        rng = random.Random(seed)
        outboxes, policy = scenario(rng)
        first = decide(SPAN, outboxes, policy, seed)
        assert decide(SPAN, outboxes, policy, seed) == first, seed
        for _ in range(3):
            assert decide(SPAN, shuffled(outboxes, rng), policy, seed) == first, seed
        assert list(first.deliveries) == sorted(first.deliveries)
        assert all(isinstance(d, Decision) for d in first.decisions)
        decided += len(first.decisions)
    assert decided > 200
