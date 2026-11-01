# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""World faults and air policy changes survive recording, lint and replay."""

import copy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from tools.conductor import replay
from tools.conductor.parts import air, airctl, faults, recorder, resolve
from tools.conductor.tests.conftest import RadioStub
from tools.conductor.world import Input
from tools.conductor.channel import Kind

import recording_lint
from scenario_test import VALID_V2


def command(world, name, **fields):
    reply = world.api.dispatch({"version": 1, "command": name, **fields})
    assert reply["ok"], reply


def advance(world):
    while world.state != "Ended":
        world.advance()


def recorded(folder):
    value = json.loads((folder / "recording/scenario.json").read_text())
    assert value["end"] == {"complete": True, "reason": "WORLD_END_TIME", "t_us": value["duration_us"]}
    assert json.loads((folder / "result.json").read_text())["lint_error"] is None
    assert recording_lint.main([str(folder / "recording")]) == 0
    return value


def install_recording_parts(world):
    """Socket-free unit world: real air, fault, recorder and fold logic, with RadioStub for the firmware."""
    for part in (air, airctl, faults, recorder, resolve):
        part.install(world)


def replay_stub(folder, destination, monkeypatch):
    def bind(tool, world):
        destination.mkdir(mode=0o700)
        world.api.directory = tool.directory = destination

    monkeypatch.setattr(replay.Replay, "bind", bind)
    monkeypatch.setattr(replay, "install_parts", install_recording_parts)
    tool = replay.Replay(folder, executable=__file__, channel_factory=RadioStub)
    result = tool.run()
    assert result["same"], result["first_difference"]
    return recorded(destination)


@pytest.mark.parametrize("until_us", [20, 30, 40])
def test_loss_window_records_to_end_and_replays_same(stub_world, tmp_path, monkeypatch, until_us):
    world = stub_world(executable=__file__, radios=2, end_us=30)
    install_recording_parts(world)
    folder = tmp_path / "original"
    folder.mkdir(mode=0o700)
    world.api.directory = folder
    world.start()
    world.run("max")
    command(world, "air_loss", sender=1, receiver=2, rate_ppm=250000)
    command(world, "fault", kind="loss", radio=1, target=2, rate_ppm=1000000,
            time_us=10, until_us=until_us)
    advance(world)
    value = recorded(folder)
    changes = [(e["t_us"], e["air_policy"]["policy"]["loss_ppm"]) for e in value["events"]]
    expected = [(0, [[1, 2, 250000]]), (10, [[1, 2, 1000000]])]
    if until_us <= 30:
        expected.append((until_us, [[1, 2, 250000]]))
    assert changes == expected
    lines = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
    assert any(line.get("event") == "world_fault" for line in lines)
    other = replay_stub(folder, tmp_path / "replayed", monkeypatch)
    assert other["events"] == value["events"]


def test_all_policy_changes_and_restart_keep_order(stub_world, tmp_path, monkeypatch):
    world = stub_world(executable=__file__, radios=2, end_us=30,
                       inputs=(Input(1, Kind.KEY, b"\x04\x01", 10, 100),))
    install_recording_parts(world)
    folder = tmp_path / "original"
    folder.mkdir(mode=0o700)
    world.api.directory = folder
    world.start()
    world.run("max")
    for enabled in (True, False):
        command(world, "air_link", up=enabled)
        command(world, "air_link", sender=1, receiver=2, up=enabled)
        command(world, "air_loss", sender=1, ordinal=2**64 - 1, dropped=enabled)
        command(world, "air_delay", sender=1, receiver=2, ordinal=2, delay_us=2**32 - 1 if enabled else 0)
        command(world, "air_corrupt", sender=1, receiver=2, ordinal=3, xor_hex="01" * 27 if enabled else "")
        command(world, "air_duplicate", sender=1, receiver=2, ordinal=4, enabled=enabled)
    command(world, "fault", kind="restart", radio=1, time_us=20)
    command(world, "fault", kind="loss", radio=1, target=2, rate_ppm=1000000, time_us=20, until_us=30)
    advance(world)
    value = recorded(folder)
    assert [next(kind for kind in ("restart", "air_policy") if kind in event)
            for event in value["events"] if event["t_us"] == 20] == ["restart", "air_policy"]
    other = replay_stub(folder, tmp_path / "replayed", monkeypatch)
    assert other["events"] == value["events"]


@pytest.mark.parametrize("field,value", [
    ("link_up", 1), ("unknown", []), ("down_links", None), ("down_links", [[1]]),
    ("down_links", [[1, 1]]), ("down_links", [[1, 3]]), ("down_links", [[True, 2]]),
    ("down_links", [[1, 2], [1, 2]]), ("drops", [[1, 0]]), ("drops", [[1, 2**64]]),
    ("loss_ppm", [[1, 2, -1]]), ("loss_ppm", [[1, 2, 0]]), ("loss_ppm", [[1, 2, 1000001]]),
    ("loss_ppm", [[1, 2, True]]), ("loss_ppm", [[1, 2, 1], [1, 2, 2]]),
    ("delays_us", [[1, 2, 1, 0]]), ("delays_us", [[1, 2, 1, 2**32]]),
    ("corruptions", [[1, 2, 1, "00"]]), ("corruptions", [[1, 2, 1, "01" * 28]]),
    ("corruptions", [[1, 2, 1, "0"]]), ("duplicates", [[1, 2, False]]),
])
def test_policy_lint_rejects_malformed_rules(field, value):
    scenario = copy.deepcopy(VALID_V2)
    scenario["radios"][0]["dmr_id"] = 3100001
    scenario["radios"][1]["dmr_id"] = scenario["radios"][1]["storage"][0]["id"] = 3100002
    policy = airctl.as_json(airctl.Policy())
    policy[field] = value
    scenario["events"] = [{"t_us": 1, "round": 0, "class": 10, "seq": 1, "source": "conductor",
                           "radio": "a", "air_policy": {"command": "air_loss", "policy": policy}}]
    with pytest.raises(ValueError, match="air_policy"):
        recording_lint.lint(scenario)


def test_firmware_loss_window_records_and_replays_same(vhost_elf, tmp_path):
    """Real firmware and real sockets; a loss window cuts an active call and then restores the link."""
    path = Path(__file__).resolve().parents[1] / "scenarios/air_pair.json"
    world = air.scenario_world(path)
    world.config = replace(world.config, executable=str(vhost_elf))
    try:
        world.api.bind("fault-pair", tmp_path)
        world.start()
        original_pids = {slot.channel.process.pid for slot in world.radios.values()}
        world.run("max")
        command(world, "fault", kind="loss", radio=1, target=2, rate_ppm=1000000,
                time_us=3_400_000, until_us=3_700_000)
        advance(world)
    finally:
        world.close()
    value = recorded(world.api.directory)
    assert [(event["t_us"], event["air_policy"]["policy"]["loss_ppm"])
            for event in value["events"] if "air_policy" in event] == [
                (3_400_000, [[1, 2, 1000000]]), (3_700_000, [])]
    decisions = [json.loads(line) for line in
                 (world.api.directory / "recording/air-decisions.jsonl").read_text().splitlines()]
    assert any(event["rule"] == "loss" for event in decisions)
    result = replay.Replay(world.api.directory, executable=vhost_elf, runtime=tmp_path).run()
    assert result["same"], result["first_difference"]
    assert not original_pids & {pid for _, _, pid in result["pids"]}
    assert recorded(Path(result["world"]))["events"] == value["events"]
