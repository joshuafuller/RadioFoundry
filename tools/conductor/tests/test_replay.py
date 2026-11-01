# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Replay a two-radio recording in fresh processes; a changed input stops at its first difference, named."""

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil

import pytest

from tools.conductor import replay
from tools.conductor.channel import Kind
from tools.conductor.conductor import install_parts, scenario_config
from tools.conductor.parts import air, dlcache, dlmux, recorder, resolve, tcppeer, webgw
from tools.conductor.tests.conftest import RadioStub
from tools.conductor.world import Config, Input, World

SCENARIO = Path(__file__).resolve().parents[1] / "scenarios/air_pair.json"


@pytest.fixture(scope="module")
def pair(vhost_elf, tmp_path_factory):
    """The air_pair world, recorded once on this build. Real firmware; needs Unix sockets."""
    runtime = tmp_path_factory.mktemp("recorded")
    world = air.scenario_world(SCENARIO)
    try:
        world.api.bind("pair", runtime)
        world.start()
        world.run("max")
        while world.state != "Ended":
            world.advance()
    finally:
        world.close()
    folder = runtime / "rfoundry-vr/pair"
    assert json.loads((folder / "recording/scenario.json").read_text())["end"]["complete"] is True
    return folder


def edited(pair, tmp_path, change):
    """A copy of the recorded world folder whose scenario.json went through change(value)."""
    copy = tmp_path / "copy"
    shutil.copytree(pair, copy)
    path = copy / "recording/scenario.json"
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return copy


def run(recording, tmp_path, vhost_elf, **options):
    lines = []
    tool = replay.Replay(recording, executable=vhost_elf, runtime=tmp_path, out=lines.append, **options)
    return tool.run(), lines


@pytest.fixture
def mixed_recording(tmp_path, stub_world):
    """Real recording metadata from two different files; no sockets or firmware build needed."""
    executables = [tmp_path / "first build.elf", tmp_path / "second=build.elf"]
    for index, path in enumerate(executables):
        path.write_bytes(f"build {index}".encode())
    world = stub_world(radios=2, radio_executables=[str(path) for path in executables], end_us=1000)
    folder = tmp_path / "recorded"
    folder.mkdir(mode=0o700)
    world.api.directory = folder
    recorder.install(world)
    resolve.install(world)
    world.start()
    world.run("max")
    while world.state != "Ended":
        world.advance()
    return folder, executables


@pytest.mark.parametrize("all_builds", [False, True])
def test_replay_selects_each_build_and_keeps_it_on_restart(mixed_recording, all_builds):
    folder, executables = mixed_recording
    selected = []

    class SelectedRadio(RadioStub):
        def __init__(self, executable, *args):
            selected.append(executable)
            super().__init__(executable, *args)

    builds = {2: executables[1]}
    default = executables[0]
    if all_builds:
        builds[1] = executables[0]
        default = folder / "unused.elf"
    tool = replay.Replay(folder, executable=default, builds=builds, channel_factory=SelectedRadio)
    world = tool.make_world()
    try:
        world.start()
        world.restart(2)
        world.restart(1)
        assert selected == [str(executables[i]) for i in (0, 1, 1, 0)]
    finally:
        world.close()


@pytest.mark.parametrize("selection", ["omitted", "wrong", "missing"])
def test_replay_missing_build_names_radio_hash_and_option(mixed_recording, selection):
    folder, executables = mixed_recording
    builds = {} if selection == "omitted" else {2: executables[0] if selection == "wrong" else folder / "missing"}
    expected = resolve.build_identity(executables[1])["sha256"]
    with pytest.raises(replay.Refused) as caught:
        replay.Replay(folder, executable=executables[0], builds=builds)
    message = str(caught.value)
    assert "radio-2" in message and expected in message and "--build 2=PATH" in message


@pytest.mark.parametrize("target", ["top-level", "radio-2"])
def test_replay_still_checks_conductor_hash(mixed_recording, target):
    folder, executables = mixed_recording
    path = folder / "recording/scenario.json"
    value = json.loads(path.read_text())
    build = value["build"] if target == "top-level" else value["radios"][1]["build"]
    build["conductor_sha256"] = "0" * 64
    path.write_text(json.dumps(value))
    with pytest.raises(replay.Refused, match="build differs, use compare.*conductor sha256"):
        replay.Replay(folder, executable=executables[0], builds={2: executables[1]})


def test_replay_legacy_build_fallback_and_compare(mixed_recording):
    folder, executables = mixed_recording
    compared = replay.Replay(folder, mode="compare", executable=executables[0])
    assert compared.executables == (str(executables[0]),) * 2
    path = folder / "recording/scenario.json"
    value = json.loads(path.read_text())
    for radio in value["radios"]:
        del radio["build"]
    path.write_text(json.dumps(value))
    legacy = replay.Replay(folder, executable=executables[0])
    assert legacy.executables == compared.executables
    with pytest.raises(replay.Refused, match="build differs, use compare.*radio-2"):
        replay.Replay(folder, executable=executables[0], builds={2: executables[1]})


def test_replay_build_cli_passes_each_selection(mixed_recording, monkeypatch):
    folder, executables = mixed_recording
    calls = []

    def run_selected(tool):
        calls.append(tool.executables)
        return {"same": True}

    monkeypatch.setattr(replay.Replay, "run", run_selected)
    assert replay.main(["replay", str(folder), "--executable", str(folder / "unused"),
                        "--build", f"1={executables[0]}", "--build", f"2={executables[1]}"]) == 0
    assert calls == [tuple(str(path) for path in executables)]


@pytest.mark.parametrize("builds", [["old.elf"], ["0=old.elf"], ["a=old.elf"], ["1="],
                                    ["1=bad\0path"], ["1=first.elf", "1=second.elf"]])
def test_replay_bad_build_arguments_are_refused(builds, capsys):
    with pytest.raises(SystemExit) as caught:
        replay.main(["replay", "unused", *[arg for build in builds for arg in ("--build", build)]])
    assert caught.value.code == 2
    assert "--build" in capsys.readouterr().err


def test_replay_build_radio_must_exist(mixed_recording, capsys):
    folder, executables = mixed_recording
    assert replay.main(["replay", str(folder), "--build", f"3={executables[0]}"]) == 2
    assert "REPLAY_BAD_BUILD" in capsys.readouterr().out


def test_mixed_build_recording_replays_in_fresh_processes(vhost_elf, tmp_path):
    """Needs sockets. A trailing ELF tag changes the binary hash without changing the firmware's behavior."""
    other = tmp_path / "other.elf"
    shutil.copy2(vhost_elf, other)
    with other.open("ab") as output:
        output.write(b"mixed-build replay test\n")
    world = air.scenario_world(SCENARIO)
    world.config = replace(world.config, executable=str(vhost_elf), radio_executables=(None, str(other)))
    try:
        world.api.bind("mixed", tmp_path)
        world.start()
        original_pids = {slot.channel.process.pid for slot in world.radios.values()}
        world.run("max")
        while world.state != "Ended":
            world.advance()
    finally:
        world.close()
    folder = world.api.directory
    recorded = json.loads((folder / "recording/scenario.json").read_text())
    assert recorded["radios"][0]["build"]["sha256"] != recorded["radios"][1]["build"]["sha256"]
    result, lines = run(folder, tmp_path, vhost_elf, builds={2: other})
    assert result["same"], result["first_difference"]
    assert len(result["pids"]) == 2 and not original_pids & {pid for _, _, pid in result["pids"]}
    replayed = json.loads((Path(result["world"]) / "recording/scenario.json").read_text())
    for field in ("radios", "events", "checkpoints", "end"):
        assert replayed[field] == recorded[field], field
    for hashes in result["traces"].values():
        assert hashes["recorded"] == hashes["replayed"]
    print("\n".join(lines))


@pytest.mark.parametrize("ratio", ["max", 1])
def test_pair_replays_the_same_run(pair, tmp_path, vhost_elf, ratio, capsys):
    result, lines = run(pair, tmp_path, vhost_elf, ratio=ratio)
    with capsys.disabled():
        print("\n" + "\n".join(lines))
    assert result["same"], result["first_difference"]
    for radio, hashes in result["traces"].items():
        assert hashes["recorded"] == hashes["replayed"], radio
    recorded = json.loads((pair / "recording/scenario.json").read_text())
    replayed = json.loads((Path(result["world"]) / "recording/scenario.json").read_text())
    assert replayed["checkpoints"] == recorded["checkpoints"]
    assert replayed["events"] == recorded["events"]
    assert replayed["end"] == recorded["end"]
    for name in ("air-deliveries.jsonl", "air-decisions.jsonl"):
        assert (Path(result["world"]) / "recording" / name).read_bytes() == (pair / "recording" / name).read_bytes()
    # Fresh processes: two radios, one boot each, each with its own new process id
    pids = [pid for _, _, pid in result["pids"]]
    assert len(pids) == 2 and len(set(pids)) == 2 and all(pids)
    assert pair not in Path(result["world"]).parents and Path(result["world"]) != pair


def test_two_replays_use_new_processes(pair, tmp_path, vhost_elf):
    first, _ = run(pair, tmp_path / "a", vhost_elf)
    second, _ = run(pair, tmp_path / "b", vhost_elf)
    assert first["same"] and second["same"]
    assert not {p for _, _, p in first["pids"]} & {p for _, _, p in second["pids"]}


def test_input_moved_by_one_microsecond_stops_and_names_it(pair, tmp_path, vhost_elf):
    def move(value):
        key = next(e for e in value["events"] if "key" in e and e["t_us"] == 3_000_000)
        key["t_us"] += 1
        value["events"].sort(key=lambda e: (e["t_us"], e["round"], e["class"], e["seq"]))
    result, lines = run(edited(pair, tmp_path, move), tmp_path, vhost_elf)
    assert not result["same"]
    first = result["first_difference"]
    # time, radio, record kind and both values
    assert first.startswith("at 3000.") and " radio-1 " in first, first
    assert any(f" {kind} " in first for kind in replay.RANK), first
    assert " was " in first and ", now " in first, first
    assert result["difference_count"] == 1  # a replay stops at the first one
    assert any("DIFFERS, first difference" in line for line in lines)


def test_changed_build_hash_is_refused(pair, tmp_path, vhost_elf):
    def other(value):
        value["build"]["sha256"] = "0" * 64
    with pytest.raises(replay.Refused, match="not a replay: build differs, use compare"):
        replay.Replay(edited(pair, tmp_path, other), executable=vhost_elf, runtime=tmp_path)


def test_incomplete_recording_never_passes(pair, tmp_path, vhost_elf):
    def cut(value):
        value["end"] = {"t_us": 2_000_000, "reason": "RECORD_STORAGE_INPUT:debug link CODEPLUG write",
                        "complete": False}
    recording = edited(pair, tmp_path, cut)
    for mode in ("replay", "compare"):
        with pytest.raises(replay.Refused, match="no complete end.*can never pass"):
            replay.Replay(recording, mode=mode, executable=vhost_elf, runtime=tmp_path)
    assert replay.main(["replay", str(recording), "--executable", str(vhost_elf), "--runtime", str(tmp_path)]) == 2


def test_changed_time_model_is_refused(pair, tmp_path, vhost_elf):
    def model(value):
        value["time_model"] = "adr-0005-draft"
    with pytest.raises(replay.Refused, match="time model 'adr-0005-draft' is not the current 'adr-0005'"):
        replay.Replay(edited(pair, tmp_path, model), executable=vhost_elf, runtime=tmp_path)


def test_changed_class_table_and_board_are_refused(pair, tmp_path, vhost_elf):
    with pytest.raises(replay.Refused, match="class table"):
        replay.Replay(edited(pair, tmp_path / "c", lambda v: v.update(class_table=2)), executable=vhost_elf)

    def board(value):
        value["radios"][0]["board"]["keymap_sha256"] = "1" * 64
    with pytest.raises(replay.Refused, match="board profile differs: radio-1 board keymap_sha256"):
        replay.Replay(edited(pair, tmp_path / "b", board), executable=vhost_elf)


def test_compare_on_the_same_build_finds_no_view_difference(pair, tmp_path, vhost_elf):
    result, lines = run(pair, tmp_path, vhost_elf, mode="compare")
    assert result["same"], result["differences"]
    assert lines[-1] == "compare: VIEW records and assertions are the same"


def test_grant_longer_than_the_proven_lookahead_is_refused(pair, vhost_elf):
    with pytest.raises(replay.Refused, match="REPLAY_BAD_GRANT"):
        replay.Replay(pair, executable=vhost_elf, grant_us=replay.lookahead.VDMR_LOOKAHEAD_US + 1)


def test_eth_plan_keeps_bytes_and_the_full_stamp():
    peer = tcppeer.Peer(b"replay-eth")
    peer.start_dhcp(0)
    frame, = peer.output(0)
    event = {"t_us": 1_600_000, "round": 2, "class": 30, "seq": 17, "source": "source-512",
             "radio": "radio-2", "eth": {"hex": frame.hex()}}
    inputs, world_events = replay.plan({"radios": [{"id": "radio-1"}, {"id": "radio-2"}], "events": [event]})
    assert inputs == [Input(2, Kind.ETH_IN, frame, 1_600_000, 512, 2, 30, 17)]
    assert world_events == []


def test_gateway_browser_session_records_and_replays(vhost_elf, tmp_path, capsys):
    """One authenticated browser, a key press and release, then the timed end. Needs sockets."""
    from tools.conductor.tests.test_webgw import (
        Ws, grant, hello_of, http, login, mint, pump, run_until, sent_to_radio,
    )

    world = World(Config(executable=str(vhost_elf), end_us=30_000_000))
    secrets = []
    ws = None
    try:
        install_parts(world)
        gateway = webgw.get(world, enabled=True)
        response = world.api.dispatch({"version": 1, "command": "air_config", "groups": [[1]], "seed": 0,
                                       "radios": [{"radio": 1, "storage": "blank", "radio_index": 1,
                                                   "identity": 3100001, "channel": 1, "cc": None, "ts": 0}]})
        assert response["ok"], response
        world.api.bind("eth", tmp_path)
        world.start()
        world.run("max")
        run_until(world, lambda: gateway._link(1) is not None and gateway._link(1).state == "up")
        world.pause()
        token = mint(world)
        port = gateway.servers[1].port

        def browser():
            session, cookie, _ = login(token, port)
            secrets.extend((session["url"].split("token=")[1], cookie.split("=", 1)[1]))
            assert http(port, "/", headers=[f"Cookie: {cookie}"])[0] == 200
            connection = Ws(port, cookie)
            assert hello_of(connection.next())["hello"] == 1
            return session["principal"], connection

        principal, ws = pump(world, browser)
        world.run("max")  # a lease is granted only while the world runs
        assert grant(world, principal)["ok"]
        for command in ("d3", "u3"):
            count = len(gateway.forwarded)
            pump(world, ws.send, command)
            run_until(world, lambda: len(gateway.forwarded) > count and sent_to_radio(gateway, command))
        ws.close()
        ws = None
        while world.state != "Ended":
            world.advance()
    finally:
        if ws is not None:
            ws.close()
        world.close()
    folder = tmp_path / "rfoundry-vr/eth"
    recorded = json.loads((folder / "recording/scenario.json").read_text())
    assert recorded["end"] == {"t_us": 30_000_000, "reason": "WORLD_END_TIME", "complete": True}
    assert any("eth" in event for event in recorded["events"])
    assert resolve.recording_lint.main([str(folder / "recording")]) == 0
    for path in folder.rglob("*"):
        if path.is_file():
            data = path.read_bytes().lower()
            for secret in secrets:
                assert secret.encode().lower() not in data and secret.encode().hex().encode() not in data, path
    result, lines = run(folder, tmp_path, vhost_elf)
    assert result["same"], result["first_difference"]
    replayed = json.loads((Path(result["world"]) / "recording/scenario.json").read_text())
    for field in ("events", "checkpoints", "end"):
        assert replayed[field] == recorded[field], field
    with capsys.disabled():
        print(f"\ngateway end={recorded['end']}; lint=passed; session secrets absent")
        print("\n".join(lines))


# --- time travel: replay to T, pause with the web gateway up ---

COMMITTED = Path(__file__).resolve().parents[3] / "tests/recordings/two-radio-dmr.json"
T_US = 1_500_000    # inside radio 1's PTT, 1.0 to 2.0 s, in the two_radio_dmr scenario


def test_travel_reads_views_after_a_recorded_network_switch():
    """In network mode UART commands arrive, but their replies go over UDP (hostlink_write). No sockets."""
    class NetworkRadio(RadioStub):
        network = True

        def send(self, message):
            if message.kind in (Kind.GRANT, Kind.DRAIN):
                for event in self.inputs:
                    if event.time_us > message.time_us or (event.time_us == message.time_us
                                                         and message.kind != Kind.DRAIN):
                        continue
                    for item in dlmux.Parser(dlmux.REQUEST_MAX).feed(event.payload):
                        if item[0] != "frame":
                            continue
                        _, cmd, seq, payload = item
                        if cmd == webgw.RADIO and payload == bytes((webgw.OP_USB_NETWORK, 0)):
                            self.network = False
                            self.outputs.append((Kind.TRACE, b"000000000000 0000000000 BOARD net.select 0"))
                        elif cmd == dlcache.GET_VIEW and not self.network:
                            view = b"dmr.state=rx\n" if payload == bytes((replay.DMR,)) else b"call.src=3100001\n"
                            self.outputs.append((Kind.BYTES_OUT,
                                                 dlmux.frame(cmd | 0x80, seq, b"\0" * 5 + b"\xff" + view)))
            super().send(message)

    world = World(Config(radios=2, radio_args=("--host-port", "on")), channel_factory=NetworkRadio)
    tool = replay.Replay.__new__(replay.Replay)
    tool.pending, tool.world_events = [], []
    tool.input_index = tool.world_index = 0
    try:
        install_parts(world)
        world.start()
        world.run("max")
        travel = replay.Travel(tool, world, {})
        assert travel.read_views(0) == {radio: {"dmr.state": "rx", "call.src": "3100001"} for radio in (1, 2)}
    finally:
        world.close()


@pytest.fixture(scope="module")
def dmr(vhost_elf, tmp_path_factory):
    """tools/conductor/scenarios/two_radio_dmr.json recorded once on this build, as interaction_check.py records it."""
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "checks/interaction_check.py"
    spec = importlib.util.spec_from_file_location("interaction_check", path)
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    folder, _ = check.live_run(str(vhost_elf), tmp_path_factory.mktemp("dmr"), out=lambda line: None)
    return folder


@pytest.fixture(scope="module")
def gateway_dmr(vhost_elf, tmp_path_factory):
    """The launcher recording that exposed the bug: its gateway switches USB to network mode at 1.6 s."""
    path = Path(__file__).resolve().parents[1] / "scenarios/two_radio_dmr.json"
    data = json.loads(path.read_text())
    runtime = tmp_path_factory.mktemp("gateway-dmr")
    world = World(scenario_config(path, radios=data["radios"], executable=str(vhost_elf)))
    try:
        install_parts(world)
        response = world.api.dispatch({**data["air"], "version": 1, "command": "air_config"})
        assert response["ok"], response
        gateway = webgw.get(world, enabled=True)
        world.api.bind("gateway-dmr", runtime)
        world.start()
        world.run("max")
        while world.state != "Ended":
            world.api.poll(0)
            world.advance()
        assert all(link.state == "up" for link in gateway.links.values())
    finally:
        world.close()
    folder = runtime / "rfoundry-vr/gateway-dmr"
    assert json.loads((folder / "recording/scenario.json").read_text())["end"]["complete"] is True
    return folder


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with open(f"/proc/{pid}/stat") as stat:   # a zombie is no longer running
        return stat.read().split(") ", 1)[1][0] != "Z"


def open_page(travel, radio):
    """A browser visit to the printed address: the token sets a cookie, the page then answers 200."""
    from tools.conductor.tests.test_webgw import http, pump, exchange
    url = travel.addresses[radio]
    port = int(url.split("/")[2].rsplit(":", 1)[1])

    def browser():
        status, head, _ = exchange(port, url.split("#rf-token=")[1])
        assert status == 200, head
        cookie = next(line for line in head.split(b"\r\n") if line.startswith(b"Set-Cookie: "))
        return http(port, "/", headers=[f"Cookie: {cookie[12:].split(b';')[0].decode()}"])[0]
    return pump(travel.world, browser)


@pytest.mark.parametrize("stop_at_ms", [1500, 2000, 2500, 3000, 4000])
@pytest.mark.parametrize("recording", ["dmr", "gateway_dmr"])
def test_travel_pauses_at_t_with_the_radios_open(request, recording, tmp_path, vhost_elf, capsys, stop_at_ms):
    lines = []
    tool = replay.Replay(request.getfixturevalue(recording), executable=vhost_elf, runtime=tmp_path, out=lines.append)
    t_us = stop_at_ms * 1000
    with tool.travel(t_us) as travel:
        with capsys.disabled():
            print("\n" + "\n".join(lines))
        result, world = travel.result, travel.world
        assert result["same"], result["first_difference"]
        # The run before T is the recorded one, record for record: the same hashes, and none of them empty
        names = set(result["prefix"])
        assert {f"radio-{r} {k}" for r in (1, 2) for k in ("trace lines", "VIEW", "FRAME", "STORE")} <= names
        assert {"air AIR_DELIVERY", "air AIR_DECISION"} <= names
        for name, (count, recorded, replayed) in result["prefix"].items():
            assert recorded == replayed, name
        assert all(result["prefix"][f"radio-{r} trace lines"][0] > 1000 for r in (1, 2))
        assert t_us < travel.view_us < t_us + replay.OPEN_LIMIT_US
        assert world.state == "Paused" and travel.view_us < travel.paused_us <= t_us + replay.OPEN_LIMIT_US
        if recording == "dmr" and t_us == T_US:
            # At 1.5 s radio 2 is in the call from radio 1; the pages open before the PTT release.
            assert travel.view_us < t_us + 50_000
            assert travel.views[2]["call.src"] == "3100001" and travel.views[2]["dmr.state"] == "rx", travel.views
            assert travel.views[1]["dmr.state"] == "tx", travel.views
            assert travel.paused_us < 2_000_000
        status = world.api.dispatch({"version": 1, "command": "webgw.status"})["result"]
        assert {radio: entry["state"] for radio, entry in status.items()} == {"1": "up", "2": "up"}
        assert set(travel.addresses) == {1, 2}
        assert {radio: open_page(travel, radio) for radio in travel.addresses} == {1: 200, 2: 200}
        # A world client can step and resume; the recording's later keys (radio 1's PTT release at 2 s) go on
        keys = []
        world.on_input(lambda event: keys.append((event.time_us, event.radio, event.payload)) if event.kind == Kind.KEY
                       else None)
        now = world.now
        assert world.api.dispatch({"version": 1, "command": "step"})["ok"]
        while world.state == "Stepping":
            world.advance()
        assert world.state == "Paused" and world.now > now
        resume_us = max(2_600_000, world.now + 100_000)
        assert world.api.dispatch({"version": 1, "command": "run", "ratio": "max", "until_us": resume_us})["ok"]
        while world.state != "Paused":
            world.api.poll(0)
            tool.feed(world)
            world.advance()
        assert world.now >= resume_us
        assert keys == ([(2_000_000, 1, bytes((4, 0)))] if recording == "dmr" and t_us == T_US else []), keys
        pids = [pid for _, _, pid in tool.pids]
    assert world.state == "Ended" and pids and not any(alive(pid) for pid in pids)


def test_committed_recording_travels_with_compare(tmp_path, vhost_elf):
    """The committed shareable file has no traces and an older build: compare checks VIEW up to T, then opens."""
    lines = []
    tool = replay.Replay(COMMITTED, mode="compare", executable=vhost_elf, runtime=tmp_path, out=lines.append)
    with tool.travel(T_US) as travel:
        assert travel.result["same"], travel.result["first_difference"]
        assert set(travel.result["prefix"]) == {"radio-1 VIEW", "radio-2 VIEW"}
        assert all(r == p and n for n, r, p in travel.result["prefix"].values())
        assert travel.views[2]["call.src"] == "3100001", travel.views
        assert open_page(travel, 1) == 200
    with pytest.raises(replay.Refused, match="REPLAY_MISSING_FILE"):
        replay.Replay(COMMITTED, executable=vhost_elf)


def test_travel_with_a_changed_trace_opens_nothing(dmr, tmp_path, vhost_elf):
    copy = tmp_path / "copy"
    shutil.copytree(dmr, copy)
    trace = copy / "recording/radio-2.trace"
    lines = trace.read_bytes().split(b"\n")
    index = next(i for i, line in enumerate(lines) if line.startswith(b"000000500000 "))
    lines[index] += b" changed"
    trace.write_bytes(b"\n".join(lines))
    tool = replay.Replay(copy, executable=vhost_elf, runtime=tmp_path, out=lambda line: None)
    travel = tool.travel(T_US)
    first = travel.result["first_difference"]
    assert not travel.result["same"] and first.startswith("at 500.") and " radio-2 TRACE " in first, first
    assert travel.world.state == "Ended" and not travel.addresses
    count, recorded, replayed = travel.result["prefix"]["radio-2 trace lines"]
    assert recorded != replayed
    with pytest.raises(replay.Refused, match="REPLAY_BAD_STOP"):
        tool.travel(10_000_000)
