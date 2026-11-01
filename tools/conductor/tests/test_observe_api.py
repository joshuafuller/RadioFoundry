# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""observe.status and observe.events: read-only history for observers (parts/observe_api.py)."""

import json

import pytest

from tools.conductor.channel import Kind
from tools.conductor.parts import audit, observe_api
from tools.conductor.world import Input


def request(world, command, **fields):
    return world.api.dispatch({"version": 1, "command": command, **fields})


def events(world, after=0, **fields):
    reply = request(world, "observe.events", after=after, **fields)
    assert reply["ok"], reply
    return reply["result"]


def test_status_reports_clock_ratio_air_and_radios(stub_world):
    world = stub_world(radios=2)
    observe = observe_api.install(world)
    world.notify({"event": "air_config", "time_us": 0, "groups": [[1, 2]], "seed": 7, "radios": []})
    world.start()
    world.run(0.5)
    status = request(world, "observe.status")["result"]
    assert (status["state"], status["ratio"], status["air"]["seed"], status["air"]["groups"]) == \
        ("Running", 0.5, 7, [[1, 2]])
    assert status["radios"] == [{"radio": 1, "on": True, "boot": 1}, {"radio": 2, "on": True, "boot": 1}]
    world.set_ratio("max")
    assert request(world, "observe.status")["result"]["ratio"] == "max"
    assert status["cursor"] == observe.sequence and status["first"] == 1


def test_inputs_are_summaries_never_payload_bytes(stub_world):
    world = stub_world()
    observe_api.install(world)
    world.start()
    world.run("max")
    secret = b"grant-secret-k3Jx9QpL2mN8vR4tW7yZ"
    world.submit(1, Kind.BYTES_IN, secret, 0x100)
    world.submit(1, Kind.KEY, bytes((4, 1)), 0)
    world.submit(1, Kind.AIR_IN, b"x", 2, cls=20)
    found = [e for e in events(world)["events"] if e["event"] == "input"]
    assert [e["kind"] for e in found] == ["BYTES_IN", "KEY"]
    assert found[0]["bytes"] == len(secret)
    assert (found[1]["key"], found[1]["down"], found[1]["radio"]) == ("ptt", True, 1)
    assert secret.decode() not in json.dumps(events(world)) and secret.hex() not in json.dumps(events(world))


def test_air_events_faults_and_audit_refusals_are_kept(stub_world):
    world = stub_world(radios=2)
    observe = observe_api.install(world)
    world.start()
    world.notify({"event": "air_delivery", "time_us": 59999, "sender": 1, "receiver": 2, "burst": 1,
                  "air_start_us": 30000, "payload": "ab" * 27, "input_us": 59999})
    world.notify({"event": "air_decision", "time_us": 59999, "t_us": 30000, "sender": 1, "burst": 1,
                  "rule": "collision", "receivers": [2], "reason": "senders 1,2 on air at once"})
    world.notify({"event": "lease_changed", "time_us": 0})              # not an observer notice
    audit.emit(world, "world_fault", None, 1, "accepted", "OK",
               fault={"kind": "loss", "radio": 1, "target": 2, "rate_ppm": 5, "time_us": 90000})
    audit.emit(world, "webgw_request", None, 1, "refused", "WEBGW_BAD_HOST")
    names = [e["event"] for e in events(world)["events"]]
    assert "lease_changed" not in names
    delivery = next(e for e in events(world)["events"] if e["event"] == "air_delivery")
    assert "payload" not in delivery and delivery["input_us"] == 59999
    assert any(e["event"] == "audit" and e["reason"] == "WEBGW_BAD_HOST" for e in events(world)["events"])
    assert observe.status()["faults"] == [{"kind": "loss", "radio": 1, "target": 2, "rate_ppm": 5, "time_us": 90000}]


def test_paging_is_bounded_and_ordered(stub_world):
    world = stub_world()
    observe = observe_api.install(world)
    for index in range(1500):
        world.notify({"event": "air_error", "time_us": index, "reason": "x" * 100})
    first = events(world)
    assert len(json.dumps(first)) < observe_api.PAGE_BYTES + 2048
    assert first["events"][0]["seq"] == 1 and first["cursor"] == first["events"][-1]["seq"] < first["latest"]
    seqs, cursor = [], 0
    while True:
        page = events(world, after=cursor)
        if not page["events"]:
            break
        seqs += [e["seq"] for e in page["events"]]
        cursor = page["cursor"]
    assert seqs == list(range(1, observe.sequence + 1))
    assert len(events(world, after=10, limit=3)["events"]) == 3


def test_ring_overflow_says_lost(stub_world):
    world = stub_world()
    observe_api.install(world)
    for index in range(observe_api.RING + 10):
        world.notify({"event": "air_error", "time_us": index, "reason": ""})
    page = events(world, after=0)
    assert page["lost"] and page["events"][0]["seq"] == 11
    assert not events(world, after=page["latest"])["lost"]


@pytest.mark.parametrize("after", [-1, "1", None, True])
def test_bad_cursor_is_refused(stub_world, after):
    world = stub_world()
    observe_api.install(world)
    assert request(world, "observe.events", after=after)["error"] == "WORLD_BAD_CURSOR"
    assert request(world, "observe.events", after=0, limit=0)["error"] == "WORLD_BAD_CURSOR"


@pytest.mark.parametrize("state", ["Pausing", "Stepping"])
def test_read_only_messages_answer_a_busy_world_without_audit(stub_world, tmp_path, state):
    world = stub_world()
    world.api.directory = tmp_path
    observe_api.install(world)
    world.start()
    if state == "Pausing":
        world.on_pausing(lambda: lambda: False)
        world.run("max")
        world.pause()
    else:
        world.step()
    assert world.state == state
    assert request(world, "observe.status")["result"]["state"] == state
    assert request(world, "observe.events", after=0)["ok"]
    assert not (tmp_path / "audit.jsonl").exists()
    assert request(world, "run")["error"] == "WORLD_BUSY"         # control is still refused and audited
    assert (tmp_path / "audit.jsonl").read_text().count("WORLD_BUSY") == 1


def test_observe_changes_no_input_stream(stub_world):
    def run(with_observer):
        world = stub_world()
        if with_observer:
            observe_api.install(world)
        world.start()
        world.run("max")
        world.submit(1, Kind.KEY, bytes((4, 1)), 0, time_us=5000)
        for _ in range(5):
            if with_observer:
                request(world, "observe.status")
                request(world, "observe.events", after=0)
            world.advance()
        return [(m.kind, m.payload, m.time_us) for m in world.radios[1].channel.sent]

    assert run(False) == run(True)


def test_assertions_are_set_by_the_harness_and_only_read_by_observers(stub_world):
    world = stub_world()
    observe = observe_api.install(world)
    observe.add_assertion("radio 2 heard radio 1", "air_delivery 1 to 2")
    assert observe.status()["assertions"][0]["passed"] is None
    observe.set_assertion("radio 2 heard radio 1", True, "at 3.06 s")
    assert observe.status()["assertions"][0] == {"name": "radio 2 heard radio 1", "passed": True,
                                                 "detail": "at 3.06 s", "time_us": 0}
    with pytest.raises(ValueError):
        observe.add_assertion("radio 2 heard radio 1")
    with pytest.raises(ValueError):
        observe.set_assertion("unknown", True)
    assert request(world, "observe.assert")["error"] == "WORLD_UNKNOWN_COMMAND"
    assert [e["passed"] for e in events(world)["events"] if e["event"] == "assertion"] == [None, True]


def test_input_record_is_frozen_input(stub_world):
    world = stub_world()
    observe = observe_api.install(world)
    observe._input(Input(1, Kind.ENC, (-2).to_bytes(4, "little", signed=True), 10, 0))
    assert observe.events[-1]["detents"] == -2
