# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Leases, the maintenance capability and release of held keys, with a fake front end."""

import json
import re

import pytest

from tools.conductor.channel import Kind
from tools.conductor.parts import audit, holds, lease, recorder
from tools.conductor.tests.conftest import RadioStub
from tools.conductor.world import Config, World, WorldError

PTT = 4


class KeyRadio(RadioStub):
    """A stub radio whose input task traces the key pins when a KEY input runs, like keypad_vhost.c."""

    def __init__(self, executable, *args):
        super().__init__(executable, *args)
        self.pins = 0

    def send(self, message):
        if message.kind in (Kind.GRANT, Kind.DRAIN):
            end, inclusive = message.time_us, message.kind == Kind.DRAIN
            for event in self.inputs:
                if event.kind == Kind.KEY and (event.time_us < end or inclusive and event.time_us == end):
                    bit, down = event.payload
                    self.pins = self.pins | (1 << bit) if down else self.pins & ~(1 << bit)
                    line = "%012d %010d BOARD key pins=%04x" % (end, end // 1000, self.pins)
                    self.outputs.append((Kind.TRACE, line.encode()))
        super().send(message)



def secret_of(world, radio):
    """The current grant secret, as the caller of lease.grant would have it (None when not held)."""
    held = lease.get(world).held.get(radio)
    return held.secret if held is not None else None


class FakeFrontEnd:
    """What a debug link or browser front end does: check the lease, send the key, track the hold."""

    def __init__(self, world):
        self.world = world
        self.leases = lease.get(world)
        self.holds = holds.get(world)
        self.holds.register("key", self.release, self.confirms)

    @staticmethod
    def release(radio, hold):
        return Kind.KEY, bytes((int(hold.name), 0))

    @staticmethod
    def confirms(radio, hold, line):
        found = re.search(r" BOARD key pins=([0-9a-f]{4})$", line)
        return bool(found) and not int(found.group(1), 16) & (1 << int(hold.name))

    def press(self, radio, principal, generation, bit):
        self.leases.check(radio, principal, generation=generation, secret=secret_of(self.world, radio),
                          command="key_down")
        self.world.submit(radio, Kind.KEY, bytes((bit, 1)), 9, internal=True)
        self.holds.down(radio, "key", str(bit), principal)


@pytest.fixture
def live(tmp_path):
    world = World(Config(radios=2), channel_factory=KeyRadio)
    for part in (audit, holds, lease, recorder):
        part.install(world)
    world.api.bind("lease", tmp_path)
    world.start()
    world.run("max")
    world.front = FakeFrontEnd(world)
    yield world
    world.close()


def audit_lines(world):
    return [json.loads(line) for line in (world.api.directory / "audit.jsonl").read_text().splitlines()]


def last(world, kind):
    return [line for line in audit_lines(world) if line["kind"] == kind][-1]


def request(world, command, **fields):
    return world.api.dispatch({"version": 1, "command": command, **fields})


def run_until(world, done, steps=20):
    for _ in range(steps):
        if done():
            return
        world.advance()
    pytest.fail("condition not reached")


def test_no_lease_by_connection_order(live):
    # Two clients "connect" and try to send; neither has a lease, so both are refused.
    for principal in ("debug:c1", "browser:s1"):
        with pytest.raises(WorldError, match="LEASE_NOT_HOLDER"):
            live.front.press(1, principal, 1, PTT)
        line = last(live, "control")
        assert (line["principal"], line["radio"], line["result"], line["reason"]) == \
            (principal, 1, "refused", "LEASE_NOT_HOLDER")
    assert live.leases.held == {} and not live.holds.open
    assert request(live, "lease.status")["result"] == {}
    granted = request(live, "lease.grant", radio=1, principal="hil:radio0")
    assert granted["ok"] and granted["result"]["generation"] == 1
    assert last(live, "lease_grant")["result"] == "accepted"


def test_second_grant_while_held_is_refused(live):
    assert request(live, "lease.grant", radio=1, principal="hil:radio0")["ok"]
    answer = request(live, "lease.grant", radio=1, principal="browser:s1")
    assert answer == {"version": 1, "id": None, "ok": False, "error": "LEASE_HELD"}
    line = last(live, "lease_grant")
    assert (line["principal"], line["result"], line["reason"], line["holder"]) == \
        ("browser:s1", "refused", "LEASE_HELD", "hil:radio0")
    # Radio 2 is a separate lease.
    assert request(live, "lease.grant", radio=2, principal="browser:s1")["ok"]


def test_old_generation_fails(live):
    first = live.leases.grant(1, "debug:c1")["generation"]
    live.leases.revoke(1)
    second = live.leases.grant(1, "debug:c1")["generation"]
    assert (first, second) == (1, 2)
    with pytest.raises(WorldError, match="LEASE_STALE_GENERATION"):
        live.leases.check(1, "debug:c1", generation=first, secret=secret_of(live, 1), command="key_down")
    line = last(live, "control")
    assert (line["result"], line["reason"], line["lease_generation"], line["current_generation"]) == \
        ("refused", "LEASE_STALE_GENERATION", 1, 2)
    with pytest.raises(WorldError, match="LEASE_STALE_GENERATION"):
        live.leases.check(1, "debug:c1", generation=None, secret=secret_of(live, 1))
    assert live.leases.check(1, "debug:c1", generation=second, secret=secret_of(live, 1), command="key_down")
    assert last(live, "control")["result"] == "accepted"


def test_revoke_releases_holds_in_order(live, capsys):
    generation = live.leases.grant(1, "debug:c1")["generation"]
    for bit in (PTT, 2, 7):
        live.front.press(1, "debug:c1", generation, bit)
    run_until(live, lambda: live.radios[1].channel.pins == (1 << PTT) | (1 << 2) | (1 << 7))
    result = request(live, "lease.revoke", radio=1)["result"]
    assert result["pending"] is True
    # Successor refused until the input task traced every key up.
    assert request(live, "lease.grant", radio=1, principal="browser:s1")["error"] == "LEASE_RELEASE_PENDING"
    assert last(live, "lease_grant")["reason"] == "LEASE_RELEASE_PENDING"
    run_until(live, lambda: not live.holds.unconfirmed(1))
    assert live.radios[1].channel.pins == 0
    assert request(live, "lease.grant", radio=1, principal="browser:s1")["ok"]
    events = [json.loads(x) for x in (live.api.directory / "events.jsonl").read_text().splitlines()]
    releases = [e for e in events if e["type"] == "input" and e["source"] == holds.CONDUCTOR_SOURCE]
    assert [e["payload_hex"] for e in releases] == ["0400", "0200", "0700"]
    sent = [a for a in audit_lines(live) if a["kind"] == "hold_release"]
    confirmed = [a for a in audit_lines(live) if a["kind"] == "hold_confirmed"]
    assert [(a["hold"], a["result"], a["cause"]) for a in sent] == \
        [("4", "sent", "LEASE_REVOKED"), ("2", "sent", "LEASE_REVOKED"), ("7", "sent", "LEASE_REVOKED")]
    assert [a["sequence"] for a in sent] == [e["sequence"] for e in releases]
    assert sorted(a["hold"] for a in confirmed if a["result"] == "confirmed") == ["2", "4", "7"]
    kinds = [a["kind"] for a in audit_lines(live)]
    assert kinds.index("lease_revoke") < kinds.index("hold_release") < kinds.index("hold_confirmed")
    with capsys.disabled():
        print(f"\nreleases in order: {[e['payload_hex'] for e in releases]}; successor admitted after confirm")


def test_dropped_release_waits_for_restart(live, capsys):
    generation = live.leases.grant(1, "debug:c1")["generation"]
    live.front.press(1, "debug:c1", generation, PTT)
    run_until(live, lambda: live.radios[1].channel.pins == 1 << PTT)
    live.holds.drop_releases(1)
    live.leases.revoke(1)
    assert last(live, "hold_release")["result"] == "dropped"
    for _ in range(5):
        live.advance()
    assert live.radios[1].channel.pins == 1 << PTT
    assert request(live, "lease.grant", radio=1, principal="browser:s1")["error"] == "LEASE_RELEASE_PENDING"
    assert last(live, "lease_grant")["reason"] == "LEASE_RELEASE_PENDING"
    live.restart(1)  # A new process: every key up, queued input cleared.
    assert live.radios[1].channel.pins == 0
    assert request(live, "lease.grant", radio=1, principal="browser:s1")["ok"]
    cleared = last(live, "hold_confirmed")
    assert (cleared["result"], cleared["reason"], cleared["boot"]) == ("cleared", "HOLD_RADIO_RESTARTED", 2)
    assert last(live, "lease_grant")["result"] == "accepted"
    with capsys.disabled():
        print("\ndropped release: successor LEASE_RELEASE_PENDING until restart, then admitted")



def test_only_the_grant_secret_proves_the_holder(live):
    granted = request(live, "lease.grant", radio=1, principal="debug:c1")["result"]
    assert set(granted) == {"radio", "principal", "generation", "secret"}
    assert request(live, "lease.status")["result"] == {"1": {"principal": "debug:c1", "generation": 1,
                                                             "maintenance": False}}
    for wrong in (None, "", granted["secret"][:-1], 5, "\u00e9" * 43):
        with pytest.raises(WorldError, match="LEASE_BAD_SECRET"):
            live.leases.check(1, "debug:c1", generation=1, secret=wrong, command="key_down")
        line = last(live, "control")
        assert (line["principal"], line["result"], line["reason"], line["lease_generation"]) == \
            ("debug:c1", "refused", "LEASE_BAD_SECRET", 1)
    assert live.leases.check(1, "debug:c1", generation=1, secret=granted["secret"], command="key_down")
    # A new grant has a new secret; the old one is worth nothing.
    live.leases.revoke(1)
    again = live.leases.grant(1, "debug:c1")
    assert again["secret"] != granted["secret"]
    with pytest.raises(WorldError, match="LEASE_BAD_SECRET"):
        live.leases.check(1, "debug:c1", generation=2, secret=granted["secret"])

def test_maintenance_without_lease_is_refused(live):
    assert request(live, "lease.grant_maintenance", radio=1, principal="debug:c1",
                   generation=1)["error"] == "LEASE_NOT_HOLDER"
    assert last(live, "maintenance_grant")["reason"] == "LEASE_NOT_HOLDER"
    generation = live.leases.grant(1, "debug:c1")["generation"]
    with pytest.raises(WorldError, match="LEASE_NO_MAINTENANCE"):
        live.leases.check(1, "debug:c1", True, generation=generation, secret=secret_of(live, 1), command="REBOOT")
    line = last(live, "control")
    assert (line["reason"], line["command"], line["needs_maintenance"]) == ("LEASE_NO_MAINTENANCE", "REBOOT", True)
    assert request(live, "lease.grant_maintenance", radio=1, principal="debug:c2",
                   generation=generation)["error"] == "LEASE_NOT_HOLDER"
    assert request(live, "lease.grant_maintenance", radio=1, principal="debug:c1", generation=generation,
                   secret=secret_of(live, 1))["ok"]
    assert last(live, "maintenance_grant")["result"] == "accepted"
    assert live.leases.check(1, "debug:c1", True, generation=generation, secret=secret_of(live, 1), command="REBOOT")
    # Maintenance ends with the lease generation.
    live.leases.revoke(1)
    generation = live.leases.grant(1, "debug:c1")["generation"]
    with pytest.raises(WorldError, match="LEASE_NO_MAINTENANCE"):
        live.leases.check(1, "debug:c1", True, generation=generation, secret=secret_of(live, 1), command="REBOOT")


def test_browser_never_gets_maintenance(live):
    generation = live.leases.grant(1, "browser:s1")["generation"]
    assert request(live, "lease.grant_maintenance", radio=1, principal="browser:s1",
                   generation=generation)["error"] == "LEASE_BROWSER_NO_MAINTENANCE"
    line = last(live, "maintenance_grant")
    assert (line["principal"], line["result"], line["reason"]) == \
        ("browser:s1", "refused", "LEASE_BROWSER_NO_MAINTENANCE")
    with pytest.raises(WorldError, match="LEASE_BROWSER_NO_MAINTENANCE"):
        live.leases.check(1, "browser:s1", True, generation=generation, secret=secret_of(live, 1), command="READ_MEM")
    assert last(live, "control")["reason"] == "LEASE_BROWSER_NO_MAINTENANCE"
    assert live.leases.held[1].maintenance is False
    # The browser still controls normally.
    assert live.leases.check(1, "browser:s1", generation=generation, secret=secret_of(live, 1), command="key_down")


def test_pause_revokes_and_waits_for_release(live):
    generation = live.leases.grant(1, "browser:s1")["generation"]
    live.front.press(1, "browser:s1", generation, PTT)
    run_until(live, lambda: live.radios[1].channel.pins == 1 << PTT)
    live.pause()
    assert live.state == "Pausing" and live.leases.held == {}
    assert last(live, "lease_revoke")["reason"] == "WORLD_PAUSE"
    run_until(live, lambda: live.state == "Paused")
    assert live.radios[1].channel.pins == 0
    assert last(live, "hold_confirmed")["result"] == "confirmed"
    with pytest.raises(WorldError, match="LEASE_WORLD_NOT_RUNNING"):
        live.leases.grant(1, "browser:s1")
    assert last(live, "lease_grant")["reason"] == "LEASE_WORLD_NOT_RUNNING"
    live.run("max")
    assert live.leases.grant(1, "browser:s1")["generation"] == generation + 1


def test_bad_principal_and_disconnect(live):
    with pytest.raises(WorldError, match="LEASE_BAD_PRINCIPAL"):
        live.leases.grant(1, "root")
    assert last(live, "lease_grant")["reason"] == "LEASE_BAD_PRINCIPAL"
    revoked = []
    live.leases.on_revoke(lambda *args: revoked.append(args))
    generation = live.leases.grant(2, "debug:c1")["generation"]
    live.front.press(2, "debug:c1", generation, 1)
    live.leases.disconnected("debug:c1")
    assert revoked == [(2, "debug:c1", generation, "LEASE_DISCONNECT")]
    assert last(live, "lease_revoke")["reason"] == "LEASE_DISCONNECT"
    assert last(live, "hold_release")["cause"] == "LEASE_DISCONNECT"


def test_maintenance_command_needs_capability_whatever_the_caller_says(live):
    generation = live.leases.grant(1, "browser:s1")["generation"]
    with pytest.raises(WorldError, match="LEASE_BROWSER_NO_MAINTENANCE"):
        live.leases.check(1, "browser:s1", generation=generation, secret=secret_of(live, 1), command="READ_MEM")
    line = last(live, "control")
    assert (line["command"], line["needs_maintenance"], line["result"]) == ("READ_MEM", True, "refused")


def test_press_after_restart_is_a_new_hold(live):
    generation = live.leases.grant(1, "debug:c1")["generation"]
    live.front.press(1, "debug:c1", generation, PTT)
    run_until(live, lambda: live.radios[1].channel.pins == 1 << PTT)
    live.restart(1)
    live.front.press(1, "debug:c1", generation, PTT)
    run_until(live, lambda: live.radios[1].channel.pins == 1 << PTT)
    live.leases.revoke(1)
    assert last(live, "hold_release")["result"] == "sent"
    assert request(live, "lease.grant", radio=1, principal="browser:s1")["error"] == "LEASE_RELEASE_PENDING"
    run_until(live, lambda: not live.holds.unconfirmed(1))
    assert live.radios[1].channel.pins == 0


def test_failed_release_still_blocks_successor(live):
    generation = live.leases.grant(1, "debug:c1")["generation"]
    live.front.press(1, "debug:c1", generation, PTT)
    run_until(live, lambda: live.radios[1].channel.pins == 1 << PTT)
    live.holds.kinds["key"] = (lambda radio, hold: (Kind.FRAME, b""), live.front.confirms)  # refused by submit
    live.leases.revoke(1)
    line = last(live, "hold_release")
    assert (line["result"], line["reason"]) == ("failed", "WORLD_BAD_INPUT_KIND")
    assert request(live, "lease.grant", radio=1, principal="browser:s1")["error"] == "LEASE_RELEASE_PENDING"
    live.restart(1)
    assert request(live, "lease.grant", radio=1, principal="browser:s1")["ok"]


def test_missing_request_field_is_audited(live):
    assert request(live, "lease.grant", radio=1)["error"] == "LEASE_BAD_PRINCIPAL"
    assert last(live, "lease_grant")["reason"] == "LEASE_BAD_PRINCIPAL"
    assert request(live, "lease.revoke")["error"] == "WORLD_UNKNOWN_RADIO"
    assert last(live, "lease_revoke")["reason"] == "WORLD_UNKNOWN_RADIO"
