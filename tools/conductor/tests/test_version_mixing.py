# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Per-radio builds and recording identities.

The live test needs RFOUNDRY_OLD_VHOST pointing to a different virtual-radio build.
It never creates a checkout or requires unpublished history.
"""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

import pytest

from tools import vradio
from tools.conductor.channel import Kind
from tools.conductor.checks.interaction_check import DMR_CALL, read_view
from tools.conductor.conductor import install_parts
from tools.conductor.parts import dlcache, recorder, resolve
from tools.conductor.tests.conftest import RadioStub
from tools.conductor.world import Config, Input, World, WorldError

import recording_lint  # on sys.path through parts.resolve
import scenario


def test_each_radio_keeps_its_executable_on_restart(stub_world):
    selected = []

    class SelectedRadio(RadioStub):
        def __init__(self, executable, *args):
            selected.append(executable)
            super().__init__(executable, *args)

    overrides = [None, "old.elf"]
    world = stub_world(executable="current.elf", radios=2, radio_executables=overrides)
    overrides[1] = "changed.elf"
    world.channel_factory = SelectedRadio
    world.start()
    world.restart(2)
    world.restart(1)
    assert selected == ["current.elf", "old.elf", "old.elf", "current.elf"]
    assert world.boots == {1: 2, 2: 2}
    assert replace(Config(radios=2), executable="new-default.elf").executable_for(2) == "new-default.elf"


@pytest.mark.parametrize("executables", ["old.elf", {2: "old.elf"}, None, ["old.elf"],
                                          [None, ""], [None, "bad\0path"], [None, 7]])
def test_bad_radio_executables_are_refused(executables):
    with pytest.raises(WorldError, match="WORLD_BAD_EXECUTABLES"):
        Config(radios=2, radio_executables=executables)


def test_launcher_build_overrides_keep_default_and_scenario(tmp_path):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"version": 1, "inputs": [], "end_us": 3000000}))
    args = vradio.arguments(["up", "--radios", "3", "--scenario", str(path), "--until-ms", "2000",
                             "--build", "2=old version.elf", "--build", "3=build=three.elf"])
    config = vradio.configuration(args, Path("current.elf"))
    assert [config.executable_for(radio) for radio in (1, 2, 3)] == [
        "current.elf", "old version.elf", "build=three.elf"]
    assert config.end_us == 2000000
    args = vradio.arguments(["up", "--radios", "2"])
    assert vradio.configuration(args, Path("current.elf")) == Config(executable="current.elf", radios=2)


@pytest.mark.parametrize("builds", [["old.elf"], ["0=old.elf"], ["3=old.elf"], ["a=old.elf"],
                                    ["1="], ["1=bad\0path"], ["1=first.elf", "1=second.elf"]])
def test_launcher_rejects_bad_or_duplicate_builds(builds, capsys):
    with pytest.raises(SystemExit) as caught:
        vradio.arguments(["up", "--radios", "2", *[arg for build in builds for arg in ("--build", build)]])
    assert caught.value.code == 2
    assert "--build" in capsys.readouterr().err


def test_recording_pins_each_radio_and_accepts_legacy_recordings(stub_world, tmp_path):
    executables = []
    for name in ("old", "current"):
        directory = tmp_path / name
        directory.mkdir()
        executable = directory / "radio.elf"
        executable.write_bytes(name.encode())
        (directory / "version").write_text(f"{name} release 1234\n")
        executables.append(str(executable))
    world = stub_world(executable="unused-default.elf", radios=2, radio_executables=executables, end_us=1000)
    world.api.directory = tmp_path
    recorder.install(world)
    resolve.install(world)
    world.start()
    world.run("max")
    while world.state != "Ended":
        world.advance()
    value = json.loads((tmp_path / "recording/scenario.json").read_text())
    identities = [radio["build"] for radio in value["radios"]]
    assert [build["sha256"] for build in identities] == [
        hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in executables]
    assert [build["version"] for build in identities] == ["old", "current"]
    assert all(build["commit_time"] == 1234 for build in identities)
    assert identities[0]["conductor_sha256"] == identities[1]["conductor_sha256"]
    assert value["build"] == identities[0]
    assert value["end"]["complete"]
    assert str(tmp_path) not in json.dumps(value)
    recording_lint.lint(value)
    assert json.loads((tmp_path / "result.json").read_text())["lint_error"] is None
    for radio in value["radios"]:
        del radio["build"]
    assert scenario.validate_v2(value) == value


@pytest.mark.parametrize("change", [{"sha256": "bad"}, {"conductor_sha256": "bad"},
                                    {"version": ""}, {"commit_time": -1}, {"path": "/private/radio.elf"}])
def test_per_radio_build_uses_the_same_strict_schema(change):
    from scenario_test import RECORDING
    value = json.loads(json.dumps(RECORDING))
    value["radios"][1]["build"] = {**value["build"], **change}
    with pytest.raises(ValueError, match="radio 1 build"):
        scenario.validate_v2(value)


def test_build_identity_requires_a_recording():
    from scenario_test import RECORDING, VALID_V2
    value = json.loads(json.dumps(VALID_V2))
    value["radios"][0]["build"] = RECORDING["build"]
    with pytest.raises(ValueError, match="only a recording holds build identities"):
        scenario.validate_v2(value)


@pytest.fixture(scope="session")
def old_vhost_elf():
    path = os.environ.get("RFOUNDRY_OLD_VHOST")
    if not path:
        pytest.skip("set RFOUNDRY_OLD_VHOST to a different virtual-radio build for version mixing")
    return Path(path)


def test_old_and_current_firmware_share_air(vhost_elf, old_vhost_elf, tmp_path):
    old = old_vhost_elf
    assert old.is_file(), old
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (vhost_elf, old)]
    assert hashes[0] != hashes[1], "version mixing needs two different builds"
    path = Path(__file__).resolve().parents[1] / "scenarios/air_pair.json"
    args = vradio.arguments(["up", "--radios", "2", "--scenario", str(path), "--build", f"2={old}"])
    inputs = tuple(Input(radio, Kind.KEY, bytes((4, down)), time_us, 100) for radio, down, time_us in (
        (1, 1, 1000000), (1, 0, 2000000), (2, 1, 3000000), (2, 0, 4000000)))
    world = World(replace(vradio.configuration(args, vhost_elf), inputs=inputs, end_us=4500000))
    views = {}
    try:
        install_parts(world)
        response = world.api.dispatch({**vradio.air_settings(args), "version": 1, "command": "air_config"})
        assert response["ok"], response
        world.add_grant_limit(lambda now: next((t for t in (1900000, 3900000) if t > now), None))
        dlcache.get(world, cadence_us=500000)
        world.api.bind("version-mixing", tmp_path)
        world.start()
        world.run("max")
        observers = {r: world.dlmux.connect(r, "debug:version-mixing", "observer") for r in (1, 2)}
        while world.state != "Ended":
            for time_us, receiver in ((1900000, 2), (3900000, 1)):
                if world.now == time_us:
                    views[receiver] = read_view(world, observers[receiver], DMR_CALL)
            world.api.poll(0)
            world.advance()
        assert world.reason == "WORLD_END_TIME", world.reason
    finally:
        world.close()
    assert views[2].get("call.src") == "3100001", views
    assert views[1].get("call.src") == "3100002", views
    folder = world.api.directory
    deliveries = [json.loads(line) for line in (folder / "recording/air-deliveries.jsonl").read_text().splitlines()]
    assert {(event["sender"], event["receiver"]) for event in deliveries} == {(1, 2), (2, 1)}
    value = json.loads((folder / "recording/scenario.json").read_text())
    assert [radio["build"]["sha256"] for radio in value["radios"]] == hashes
    assert value["end"]["complete"]
    recording_lint.lint(value)
    assert json.loads((folder / "result.json").read_text())["lint_error"] is None
    print(f"old/current calls received both ways: {views}; build sha256={hashes}; recording={folder}")
