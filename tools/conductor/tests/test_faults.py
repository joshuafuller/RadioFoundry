# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""World fault boundaries, research retirements, and child startup diagnostics."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest

from tools.conductor import channel
from tools.conductor.channel import Channel, ChannelError, Kind, Message, STDERR_LIMIT
from tools.conductor.parts import airctl, audit, faults, input as keys
from tools.conductor.parts.view import HEADER as VIEW_HEADER
from tools.conductor.world import Config, World, WorldError


def request(world, kind="loss", **fields):
    return world.api.dispatch({"version": 1, "command": "fault", "kind": kind,
                               "radio": 1, "time_us": 10, **fields})


def fault_world(stub_world, **options):
    world = stub_world(radios=2, **options)
    airctl.install(world)
    keys.install(world)
    faults.install(world)
    world.start()
    world.run("max")
    return world


def test_loss_changes_only_after_the_preceding_span(stub_world):
    world = stub_world(radios=2, end_us=30)
    airctl.install(world)
    spans = []
    world.on_grant_done(lambda at, _: spans.append((at, dict(airctl.policy(world).loss_ppm))))
    faults.install(world)
    world.start()
    world.run("max")
    assert request(world, target=2, rate_ppm=1000000)["ok"]
    assert request(world, time_us=20, target=2, rate_ppm=0)["ok"]
    assert not airctl.policy(world).loss_ppm
    while world.state != "Ended":
        world.advance()
    assert spans == [(0, {}), (10, {}), (20, {(1, 2): 1000000}), (30, {}), (30, {})]


def test_fault_at_next_start_and_restart_boundary(stub_world):
    world = fault_world(stub_world, end_us=20)
    old = world.radios[1].channel
    # start() drained zero; the next input belongs to 1 us.
    assert request(world, time_us=0, target=2, rate_ppm=0)["error"] == "WORLD_PAST_INPUT"
    assert request(world, time_us=1, target=2, rate_ppm=1000000)["ok"]
    assert request(world, "restart")["ok"]
    world.advance()
    assert world.now == 1 and dict(airctl.policy(world).loss_ppm) == {(1, 2): 1000000}
    world.advance()
    assert world.now == 10 and world.boots == {1: 2, 2: 1}
    assert old.closed and world.radios[1].origin == 10
    # A scenario input submitted after the restart is addressed to the new boot.
    world.submit(1, Kind.KEY, b"\x04\x01", 1, time_us=10)
    new = world.radios[1].channel
    world.advance()
    assert any(m.kind == Kind.KEY and m.time_us == 0 for m in new.sent)


def test_step_applies_previously_accepted_faults(stub_world):
    world = fault_world(stub_world)
    assert request(world, target=2, rate_ppm=1000000)["ok"]
    assert request(world, "restart")["ok"]
    world.pause()
    world.step(10)
    world.advance()
    assert world.state == "Paused" and world.now == 10
    assert world.boots == {1: 2, 2: 1}
    assert dict(airctl.policy(world).loss_ppm) == {(1, 2): 1000000}


@pytest.mark.parametrize("fields,reason", [
    ({"radio": True}, "WORLD_UNKNOWN_RADIO"),
    ({"time_us": True}, "WORLD_BAD_TIME"),
    ({"time_us": -1}, "WORLD_BAD_TIME"),
    ({"target": 1}, "WORLD_BAD_LINK"),
    ({"target": 3}, "WORLD_UNKNOWN_RADIO"),
    ({"rate_ppm": True}, "WORLD_BAD_LOSS_RATE"),
    ({"rate_ppm": 1000001}, "WORLD_BAD_LOSS_RATE"),
    ({"kind": "unknown"}, "WORLD_UNKNOWN_FAULT"),
])
def test_fault_refusals_do_not_change_policy(stub_world, fields, reason):
    world = fault_world(stub_world)
    assert request(world, **{"target": 2, "rate_ppm": 1, **fields})["error"] == reason
    assert not airctl.policy(world).loss_ppm


@pytest.mark.parametrize("kind", faults.RETIRED_INPUTS)
def test_retired_inputs_have_named_refusals(stub_world, kind):
    world = fault_world(stub_world)
    result = request(world, kind)
    assert not result["ok"]
    assert result["error"] == f"WORLD_INPUT_RETIRED: {kind}; {faults.RETIRED_INPUTS[kind]}"


def test_paused_fault_refused(stub_world):
    world = fault_world(stub_world)
    world.pause()
    assert request(world, target=2, rate_ppm=1)["error"] == "WORLD_PAUSED"


@pytest.fixture
def exited_channel(monkeypatch):
    # Real child and stderr, with only the forbidden socket replaced.
    parent = Mock()
    parent.fileno.return_value = 3
    parent.recvmsg.return_value = (b"", [], 0, None)
    child = open(os.devnull, "rb")
    monkeypatch.setattr(channel.socket, "socketpair", lambda *args: (parent, child))
    popen = subprocess.Popen

    def exited(*args, **kwargs):
        process = popen(*args, **kwargs)
        process.wait(timeout=5)
        return process

    monkeypatch.setattr(channel.subprocess, "Popen", exited)
    return parent


def test_startup_stderr_in_error_and_audit(exited_channel, tmp_path):
    world = World(Config(executable=sys.executable, radio_args=("-c",
        "import sys; sys.stderr.write('startup fixture failed\\n'); sys.exit(2)")))
    faults.install(world)
    world.api.directory = tmp_path
    with pytest.raises(WorldError, match="startup fixture failed"):
        world.start()
    lines = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["kind"] == "radio_failure"
    assert "startup fixture failed" in lines[0]["reason"]
    assert world.state == "Ended"


def test_noisy_stderr_does_not_block_and_keeps_a_bounded_tail(exited_channel):
    with Channel(sys.executable, "-c",
                 "import sys; sys.stderr.write('x' * 200000 + '\\nlast error \\ufffd\\n'); sys.exit(2)") as radio:
        with pytest.raises(ChannelError, match="last error") as raised:
            radio.collect()
        text = str(raised.value)
        assert "[stderr truncated]" in text and len(text) < STDERR_LIMIT + 100
    assert radio._stderr.closed and "last error" in radio.stderr_text()


@pytest.mark.parametrize("operation", ["send", "receive"])
def test_socket_errors_include_stderr(exited_channel, operation):
    with Channel(sys.executable, "-c", "import sys; sys.stderr.write('radio failed\\n')") as radio:
        getattr(exited_channel, "send" if operation == "send" else "recvmsg").side_effect = OSError("broken socket")
        with pytest.raises(ChannelError, match="radio failed"):
            if operation == "send":
                radio.send(Message(Kind.GRANT))
            else:
                radio.receive()


def test_start_failure_audit_still_redacts_registered_secrets(exited_channel, tmp_path):
    world = World(Config(executable=sys.executable, radio_args=("-c",
        "import sys; sys.stderr.write('secret-value startup failure\\n'); sys.exit(2)")))
    faults.install(world)
    audit.get(world).register_secret("secret-value")
    world.api.directory = tmp_path
    with pytest.raises(WorldError):
        world.start()
    line = json.loads((tmp_path / "audit.jsonl").read_text())
    assert line["reason"] == audit.REFUSED and "reason" in line["refused"]


def test_real_socket_start_failure_stderr(tmp_path):
    world = World(Config(executable=sys.executable, radio_args=("-c",
        "import sys; sys.stderr.write('real startup failure\\n'); sys.exit(2)")))
    faults.install(world)
    world.api.directory = tmp_path
    try:
        with pytest.raises(WorldError, match="real startup failure"):
            world.start()
        assert "real startup failure" in (tmp_path / "audit.jsonl").read_text()
    finally:
        world.close()


def scenario_modules():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "digital-twin"))
    import scenario
    from scenarios import WORLD_SCENARIOS
    return scenario, WORLD_SCENARIOS


def test_scenario_fault_mapping_and_inputs_after_restart(tmp_path):
    scenario, cases = scenario_modules()
    from scenario_test import ScenarioChannel

    class RestartChannel(ScenarioChannel):
        def __init__(self, executable, *args):
            super().__init__(executable, *args)
            boot = int(args[args.index("--boot-id") + 1])
            self.wire[0] = Message(Kind.VIEW,
                VIEW_HEADER.pack(self.radio, boot, 0, 0, 0, 0, 0, 0) + b"home\0\0\0\0")

    value = copy.deepcopy(cases["air_pair"])
    value["duration_us"] = 5000
    stamp = {"round": 0, "source": "scenario", "radio": "a"}
    value["events"] = [
        {**stamp, "t_us": 0, "class": 10, "seq": 1, "loss": {"target": "b", "rate_ppm": 1000000}},
        {**stamp, "t_us": 1000, "class": 10, "seq": 2, "restart": {}},
        {**stamp, "t_us": 1000, "class": 30, "seq": 3, "key": {"bit": 4, "down": True}},
        {**stamp, "t_us": 5000, "class": 10, "seq": 4, "loss": {"target": "b", "rate_ppm": 0}},
        {**stamp, "t_us": 5000, "class": 30, "seq": 5, "key": {"bit": 4, "down": False}},
    ]
    value["assertions"] = []
    binary = tmp_path / "vhost"
    binary.write_bytes(b"fixture")
    ScenarioChannel.made = []
    ScenarioChannel.fail_radio = None
    with patch("tools.conductor.world.World", side_effect=lambda config: World(config, channel_factory=RestartChannel)):
        result = scenario.run(value, binary)
    assert result["status"] == "passed", result["execution_error"]
    first, peer, restarted = ScenarioChannel.made
    assert not any(m.kind == Kind.KEY for m in first.sent)
    assert [(m.time_us, m.payload) for m in restarted.sent if m.kind == Kind.KEY] == [
        (0, b"\x04\x01"), (4000, b"\x04\x00")]
    assert [e["time_us"] for e in result["trace"] if e["kind"] == "world_fault"] == [0, 1000, 5000]
    assert first.closed and peer.closed and restarted.closed


def test_virtual_ptt_loss_and_restart(vhost_elf):
    scenario, cases = scenario_modules()
    value = copy.deepcopy(cases["air_pair"])
    value["duration_us"] = 8000000
    value["assertions"] = []
    stamp = {"round": 0, "source": "scenario", "radio": "a"}
    actions = [(3000000, 10, {"loss": {"target": "b", "rate_ppm": 1000000}}),
               (3000000, 30, {"key": {"bit": 4, "down": True}}),
               (3400000, 10, {"loss": {"target": "b", "rate_ppm": 0}}),
               (4000000, 30, {"key": {"bit": 4, "down": False}}),
               (4500000, 10, {"restart": {}}),
               (7500000, 30, {"key": {"bit": 4, "down": True}}),
               (7800000, 30, {"key": {"bit": 4, "down": False}})]
    value["events"] = [{**stamp, "seq": i, "t_us": at, "class": cls, **payload}
                       for i, (at, cls, payload) in enumerate(actions, 1)]
    result = scenario.run(value, vhost_elf)
    assert result["status"] == "passed", result["execution_error"]
    assert any(e["kind"] == "burst" and e["radio"] == "a" and 3000000 <= e["t_us"] < 3400000
               for e in result["trace"])
    deliveries = [e for e in result["trace"] if e["kind"] == "air_delivery" and e["sender"] == 1]
    assert not any(3000000 <= e["air_start_us"] < 3400000 for e in deliveries)
    assert any(3400000 <= e["air_start_us"] < 4000000 for e in deliveries)
    assert any(7500000 <= e["air_start_us"] < 7800000 for e in deliveries)
    assert any(e["kind"] == "radio_started" and e["radio"] == 1 and e["boot"] == 2
               and e["time_us"] == 4500000 for e in result["trace"])


def test_a_loss_window_restores_the_rate_from_before_it_and_refuses_overlap(stub_world):
    world = stub_world(radios=2, end_us=60)
    airctl.install(world)
    spans = []
    world.on_grant_done(lambda at, _: spans.append((at, dict(airctl.policy(world).loss_ppm))))
    control = faults.install(world)
    world.start()
    world.run("max")
    assert request(world, time_us=5, target=2, rate_ppm=250000)["ok"]
    assert request(world, time_us=10, target=2, rate_ppm=1000000, until_us=10)["error"] == "WORLD_BAD_WINDOW"
    assert request(world, time_us=10, target=2, rate_ppm=1000000, until_us=30)["ok"]
    assert request(world, time_us=40, target=2, rate_ppm=500000, until_us=50)["error"] == "WORLD_FAULT_OVERLAP"
    assert request(world, time_us=40, target=2, rate_ppm=500000)["error"] == "WORLD_FAULT_OVERLAP"
    assert request(world, time_us=20, radio=2, target=1, rate_ppm=500000, until_us=40)["ok"]   # other direction
    while world.state != "Ended":
        world.advance()
    # A span's entry is the policy that held during the span ending at that time.
    rates = {at: policy.get((1, 2)) for at, policy in spans}
    assert (rates[10], rates[20], rates[30], rates[40]) == (250000, 1000000, 1000000, 250000)   # restored
    back = {at: policy.get((2, 1)) for at, policy in spans}
    assert (back[20], back[30], back[40], back[60]) == (None, 500000, 500000, None)
    assert control.windows == {}
