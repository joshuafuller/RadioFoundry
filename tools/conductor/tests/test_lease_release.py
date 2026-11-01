# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""lease release: a lease change releases a held PTT before anyone else gets control (ADR 0004 rule 2).

A controller holds PTT through the debug link multiplexer on the real vhost firmware. The lease is revoked.
The multiplexer's release ("PTT off") goes in as a stamped conductor input, and the release counts only once
the firmware's own words confirm it (dlmux.py, "Confirming a release"). Run with -s to see the evidence.
"""

import json

import pytest

from tools.conductor.channel import Kind
from tools.conductor.parts import audit, dlmux, lease, recorder
from tools.conductor.world import Config, World, WorldError

RELEVANT = (" BOARD radio.tx", " BOARD key.scan", " BOARD host.write")


@pytest.fixture
def live(tmp_path, vhost_elf):
    world = World(Config(executable=str(vhost_elf), end_us=20_000_000))
    recorder.install(world)
    mux = dlmux.get(world)
    world.api.bind("lease-release", tmp_path)
    world.audit_lines = []
    audit.get(world).add_sink(world.audit_lines.append)
    world.traces = []
    world.on_outbox(Kind.TRACE, lambda radio, message: world.traces.append(
        (message.time_us, message.payload.decode("utf-8", errors="replace"))))
    try:
        world.start()
        world.run("max")
        run_until(world, 1_400_000)
        yield world, mux
    finally:
        world.close()


def run_until(world, time_us, stop=lambda: False):
    while world.state not in ("Ended", "Paused") and world.now < time_us and not stop():
        world.advance()


def replies(data):
    return [item[1:] for item in dlmux.Parser(dlmux.REPLY_MAX).feed(data) if item[0] == "frame"]


def hold_ptt(world, mux, principal="script:first"):
    leases = lease.get(world)
    granted = leases.grant(1, principal)
    session = mux.connect(1, principal, "controller", granted["generation"], granted["secret"])
    mux.feed(session, dlmux.frame(0x07, 1, b"\x01"))
    run_until(world, world.now + 100_000)
    assert replies(session.take()) == [(0x87, 1, b"\0\x10\x27")]
    assert [(hold.kind, hold.name) for hold in world.holds.tracked(1)] == [("debuglink.ptt", "PTT")]
    assert any(" BOARD radio.tx " in line for _, line in world.traces)       # the radio transmits
    return leases, session


def show(title, lines):
    print(f"\n--- {title}")
    for line in lines:
        print(line if isinstance(line, str) else json.dumps(line, sort_keys=True))


def second_key(world, mux, leases):
    """The second client's KEY: admitted only with its own lease."""
    granted = leases.grant(1, "script:second")
    session = mux.connect(1, "script:second", "controller", granted["generation"], granted["secret"])
    mux.feed(session, dlmux.frame(0x06, 1, bytes((0, 5, 0, 0))))      # KEY select PRESS
    admitted = next(line for line in reversed(world.audit_lines)
                    if line["kind"] == "control" and line["principal"] == "script:second")
    run_until(world, world.now + 100_000)
    assert replies(session.take()) == [(0x86, 1, b"\0")]
    return admitted


def test_revoke_releases_ptt_before_a_second_client_is_admitted(live):
    world, mux = live
    leases, first = hold_ptt(world, mux)
    revoked_at = world.now
    result = leases.revoke(1)
    assert result["pending"] and first.closed                    # the raw client loses its connection
    release = next(line for line in world.audit_lines if line["kind"] == "hold_release")
    assert release["result"] == "sent"
    with pytest.raises(WorldError, match="LEASE_RELEASE_PENDING"):
        leases.grant(1, "script:second")
    run_until(world, revoked_at + 2_000_000, stop=lambda: not world.holds.unconfirmed(1))
    confirmed = next(line for line in world.audit_lines if line["kind"] == "hold_confirmed")
    assert confirmed["result"] == "confirmed"
    admitted = second_key(world, mux, leases)
    assert (admitted["result"], admitted["command"]) == ("accepted", "KEY")

    # The trace, in order: the PTT-off reply, the link view with nothing injected, two input task scans,
    # the confirm; the radio stopped transmitting before the second client's KEY.
    window = [(t, line) for t, line in world.traces if revoked_at <= t <= confirmed["seen_us"]
              and any(mark in line for mark in RELEVANT)]
    ptt_off = next(i for i, (_, line) in enumerate(window) if "host.write 11 @0 a55a87" in line)
    view = max(i for i, (_, line) in enumerate(window) if "host.write" in line and "@0 a55a89" in line
               and "73656374696f6e3d6c696e6b" in line)        # "section=link"
    scans = [i for i, (_, line) in enumerate(window) if "key.scan" in line and i > view]
    tx_off = next(t for t, line in world.traces if t >= revoked_at and "BOARD radio.tx_off" in line)
    assert ptt_off < view and len(scans) >= 2 and tx_off <= admitted["time_us"]
    assert release["release_us"] <= window[ptt_off][0] and confirmed["time_us"] <= admitted["time_us"]
    events = [json.loads(line) for line in (world.api.directory / "events.jsonl").read_text().splitlines()]
    recorded = [line for line in events if line.get("event") == "hold_release"]
    assert recorded and recorded[0]["hold_kind"] == "debuglink.ptt"
    hexes = [line for _, line in window[view:view + 6] if "host.write" in line]
    link = bytes.fromhex("".join(line.rsplit(" ", 1)[1] for line in hexes))
    injected = next(item for item in link.split(b"\n") if item.startswith(b"input.injected="))
    assert injected == b"input.injected=0x00000000"
    show("trace (release confirmed)", [line for _, line in window[ptt_off:ptt_off + 1]]
         + [line for _, line in window[view:view + 1]] + [f"  (decoded) {injected.decode()}"]
         + [window[i][1] for i in scans[:2]]
         + [line for t, line in world.traces if t == tx_off and "radio.tx_off" in line])
    show("audit", [release, confirmed, admitted])
    show("events.jsonl", recorded)


def test_dropped_release_refuses_the_second_client_until_restart(live):
    world, mux = live
    leases, _ = hold_ptt(world, mux)
    world.holds.drop_releases(1)
    leases.revoke(1)
    dropped = next(line for line in world.audit_lines if line["kind"] == "hold_release")
    assert (dropped["result"], dropped["reason"]) == ("dropped", "HOLD_TEST_DROP")
    run_until(world, world.now + 1_500_000)                     # six cache refreshes later: still waiting
    assert world.holds.unconfirmed(1)
    with pytest.raises(WorldError, match="LEASE_RELEASE_PENDING"):
        leases.grant(1, "script:second")
    refused = world.audit_lines[-1]
    assert not any("BOARD radio.tx_off" in line for _, line in world.traces)   # PTT really still held
    world.restart(1)                                            # a new process: every key up
    assert not world.holds.unconfirmed(1)
    cleared = next(line for line in world.audit_lines if line["kind"] == "hold_confirmed")
    assert (cleared["result"], cleared["reason"]) == ("cleared", "HOLD_RADIO_RESTARTED")
    run_until(world, world.now + 1_200_000)
    admitted = second_key(world, mux, leases)
    assert (admitted["result"], admitted["command"]) == ("accepted", "KEY")
    show("trace (dropped release)", [line for _, line in world.traces if " BOARD radio.tx" in line])
    show("audit", [dropped, refused, cleared, admitted])


def test_timed_select_hold_is_released_by_its_own_time_out(live):
    """KEY kind 3 holds SELECT for 2 s; no KEY up can end it (input_task.c:360-375). The successor waits."""
    world, mux = live
    leases = lease.get(world)
    granted = leases.grant(1, "script:first")
    session = mux.connect(1, "script:first", "controller", granted["generation"], granted["secret"])
    mux.feed(session, dlmux.frame(0x06, 1, bytes((3, 5)) + (2000).to_bytes(2, "little")))
    run_until(world, world.now + 50_000)
    assert replies(session.take()) == [(0x86, 1, b"\0")]
    pressed_at = world.now - 50_000
    assert [(hold.kind, hold.name) for hold in world.holds.tracked(1)] == [("debuglink.timed", "timed")]
    leases.revoke(1)
    release = next(line for line in world.audit_lines if line["kind"] == "hold_release")
    run_until(world, pressed_at + 4_000_000, stop=lambda: not world.holds.unconfirmed(1))
    confirmed = next(line for line in world.audit_lines if line["kind"] == "hold_confirmed")
    assert confirmed["result"] == "confirmed" and confirmed["seen_us"] >= pressed_at + 2_000_000
    admitted = second_key(world, mux, leases)
    assert admitted["result"] == "accepted"
    show("audit (timed hold)", [release, confirmed, admitted])
