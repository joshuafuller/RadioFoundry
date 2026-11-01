# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The fold: a live world becomes a version 2 recording, with no storage bytes in any file."""

import hashlib
import json
from pathlib import Path
import stat
import struct
import zlib

import pytest

from tools.conductor.channel import Kind, Message
from tools.conductor.parts import air, audit, recorder, resolve, tcppeer, udp, webgw
import rfoundry_debug

import recording_lint  # on sys.path through parts.resolve
import scenario

SCENARIO = Path(__file__).resolve().parents[1] / "scenarios/air_pair.json"
SENTINEL = b"RFSENTINEL-CODEPLUG-7f6312"


def folder_files(directory):
    return [path for path in sorted(directory.rglob("*")) if path.is_file()]


def stub(stub_world, tmp_path, name, **options):
    world = stub_world(**options)
    recorder.install(world)
    resolve.install(world)
    world.api.bind(name, tmp_path)
    world.start()
    world.run("max")
    return world


def recording(world):
    return json.loads((world.api.directory / "recording/scenario.json").read_text())


def test_two_radio_world_folds_to_a_valid_recording(vhost_elf, tmp_path, capsys):
    """Real firmware, two radios on one air (air_pair.json). Needs Unix sockets."""
    world = air.scenario_world(SCENARIO)
    try:
        world.api.bind("pair", tmp_path)
        world.start()
        world.run("max")
        while world.state != "Ended":
            world.advance()
    finally:
        world.close()
    directory = tmp_path / "rfoundry-vr/pair"
    value = json.loads((directory / "recording/scenario.json").read_text())
    scenario.validate_v2(value)
    assert value["end"] == {"t_us": 5_000_000, "reason": "WORLD_END_TIME", "complete": True}
    assert [radio["dmr_id"] for radio in value["radios"]] == [3100001, 3100002]
    for radio in value["radios"]:
        channel = next(step for step in radio["storage"] if step["op"] == "channel")
        assert channel["contact"] == 1
        stores = [record for record in value["checkpoints"]
                  if record["kind"] == "STORE" and record["radio"] == radio["id"]]
        assert [store["t_us"] for store in stores] == [0, 5_000_000]
    assert value["build"]["sha256"] == hashlib.sha256(vhost_elf.read_bytes()).hexdigest()
    kinds = {record["kind"] for record in value["checkpoints"]}
    assert kinds == {"VIEW", "FRAME", "STORE"}
    # The debug link cache's periodic view reads are inputs too; the keys are the two edges
    assert [event["key"] for event in value["events"] if "key" in event] == [{"bit": 4, "down": True},
                                                                          {"bit": 4, "down": False}]
    assert (directory / "recording/air-deliveries.jsonl").read_text()
    assert str(Path.home()).encode() not in (directory / "recording/scenario.json").read_bytes()
    assert recording_lint.main([str(directory / "recording")]) == 0
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in [directory, *directory.rglob("*")]}
    assert all(mode == (0o700 if path.is_dir() else 0o600) for path, mode in modes.items()), modes
    result = json.loads((directory / "result.json").read_text())
    assert result["lint_error"] is None and set(result["traces"]) == {"radio-1", "radio-2"}
    with capsys.disabled():
        print(f"\nfolder={directory}\nend={value['end']}\ncheckpoints={result['checkpoints']}")


def test_killed_radio_gives_an_incomplete_end(stub_world, tmp_path):
    world = stub(stub_world, tmp_path, "killed", radios=2, end_us=100_000)
    world.radios[2].channel.outputs.append((Kind.RESTART_REASON, b"killed"))
    world.advance()
    assert world.state == "Ended"
    end = recording(world)["end"]
    assert end["complete"] is False and end["reason"] == "WORLD_RADIO_FAILURE:2:killed"
    scenario.validate_v2(recording(world))


def test_unknown_codeplug_write_stops_and_leaves_no_bytes(stub_world, tmp_path):
    world = stub(stub_world, tmp_path, "codeplug", end_us=100_000)
    world.submit(1, Kind.BYTES_IN, b"\x00", 5, time_us=1000, internal=True)  # undecoded: stops too
    world.close()
    assert recording(world)["end"]["reason"].startswith("RECORD_INPUT_UNDECODED:")

    world = stub(stub_world, tmp_path, "codeplug2", end_us=100_000)
    world.submit(1, Kind.BYTES_IN, rfoundry_debug.frame(0x1c, 1, b"\x04" + SENTINEL), 5,
                 time_us=1000, internal=True)
    world.submit(1, Kind.BYTES_IN, rfoundry_debug.frame(0x01, 2), 5, time_us=2000, internal=True)
    while world.state != "Ended":
        world.advance()
    world.close()
    value = recording(world)
    assert value["end"] == {"t_us": 0, "reason": "RECORD_STORAGE_INPUT:debug link CODEPLUG write",
                            "complete": False}
    assert value["events"] == []
    for path in folder_files(world.api.directory):
        data = path.read_bytes().lower()
        for needle in (SENTINEL, SENTINEL.hex().encode(), SENTINEL.hex().upper().encode()):
            assert needle.lower() not in data, path


def test_known_generator_bytes_become_a_reference(stub_world, tmp_path):
    crc = zlib.crc32(b"\xff" * 4096)
    payloads = (b"\x03\x01", b"\x04" + struct.pack("<IBHI", 0x20000, 2, 0, crc), b"\x06", b"\x07")
    data = b"".join(rfoundry_debug.frame(0x1c, 9 + i, p) for i, p in enumerate(payloads))
    world = stub(stub_world, tmp_path, "generator", end_us=10_000)
    world.submit(1, Kind.BYTES_IN, data, 5, time_us=1000, internal=True)
    while world.state != "Ended":
        world.advance()
    world.close()
    value = recording(world)
    host = value["events"][0]["host"]
    assert host["generator"] == {"name": "codeplug-blank-block", "params": {"address": 0x20000, "seq": 9},
                                 "sha256": hashlib.sha256(data).hexdigest()}
    assert value["end"]["complete"] is True
    recording_lint.lint(value)
    assert data.hex() not in "".join(p.read_text(errors="replace") for p in folder_files(world.api.directory))


def test_storage_steps_keep_channel_contact():
    steps, notes = resolve.storage_steps("dmr-pair", 2)
    assert {"op": "dmr_id", "id": 3100002} in steps and notes == []
    assert steps[0]["contact"] == 1
    scenario.validate_storage(steps, "storage")
    text, _ = scenario.start_storage({"storage": steps})
    assert resolve.storage_steps(text, 2) == (steps, [])
    assert resolve.storage_steps("blank", 1) == ("blank", [])


def test_storage_steps_group_fixture():
    """dmr-group member 7: its own identity, the shared channel and the talkgroup 9 contact (storage_vhost.c)."""
    steps, notes = resolve.storage_steps("dmr-group", 7)
    assert notes == [] and {"op": "dmr_id", "id": 3100007} in steps
    assert {"op": "contact", "index": 1, "name": "TG9", "id": 9, "type": "group", "timeslot": 1} in steps
    assert steps[0]["contact"] == 1 and steps[0]["color_code"] == 1 and steps[0]["timeslot"] == 1
    scenario.validate_storage(steps, "storage")
    text, _ = scenario.start_storage({"storage": steps})
    assert resolve.storage_steps(text, 7) == (steps, [])


def test_timed_end_folds_boot_and_closing_store(stub_world, tmp_path):
    world = stub_world(executable=__file__, end_us=1000)

    class ClosingRadio(world.channel_factory):
        def __init__(self, *args):
            super().__init__(*args)
            self.wire.appendleft(Message(Kind.TRACE, b"000000000000 0000000000 STORE " + b"a" * 64))

        def close(self):
            if self.closed:
                return ()
            super().close()
            return (Message(Kind.TRACE, b"000000001000 0000000001 STORE " + b"b" * 64, 1000),)

    world.channel_factory = ClosingRadio
    world.api.directory = tmp_path
    recorder.install(world)
    resolve.install(world)
    world.start()
    world.run("max")
    while world.state != "Ended":
        world.advance()
    value = recording(world)
    scenario.validate_v2(value)
    assert value["end"] == {"t_us": 1000, "reason": "WORLD_END_TIME", "complete": True}
    assert [(r["t_us"], r["sha256"]) for r in value["checkpoints"] if r["kind"] == "STORE"] == [
        (0, "a" * 64), (1000, "b" * 64)]


def network_peer():
    peer = tcppeer.Peer(b"recording-eth")
    peer.ip, peer.radio_ip = bytes((192, 168, 10, 2)), bytes((192, 168, 10, 1))
    peer.radio_mac = udp.RADIO_MAC
    return peer


def tcp_frame(data, port=80):
    peer = network_peer()
    conn = peer.connect(port, 0)
    peer._send_tcp(conn, tcppeer.PSH | tcppeer.ACK, conn.snd_nxt, data)
    return peer.frames[0]


def test_eth_setup_and_web_control_fold_and_pass_lint(stub_world, tmp_path):
    peer = network_peer()
    peer.start_dhcp(0)
    frames = peer.output(0)
    peer.dhcp, peer.dhcp_due, peer.offer, peer.server = "requesting", 0, peer.ip, peer.radio_ip
    frames += peer.output(0)
    peer.dhcp, peer.dhcp_due = "renewing", 0
    frames += peer.output(0)
    peer._eth(peer.radio_mac, tcppeer.ETH_ARP,
              struct.pack("!HHBBH", 1, 0x800, 6, 4, 2) + peer.mac + peer.ip + peer.radio_mac + peer.radio_ip)
    frames += peer.frames
    conn = peer.connect(80, 0)
    peer.frames = []
    peer._send_tcp(conn, tcppeer.SYN, conn.iss, options=struct.pack("!BBH", 2, 4, tcppeer.MSS))
    frames += peer.frames
    frames += [tcp_frame(data) for data in (
        b"", b"GET / HTTP/1.1\r\nHost: 192.168.10.1\r\nAccept-Encoding: gzip\r\nConnection: close\r\n\r\n",
        b"GET /ws HTTP/1.1\r\nHost: 192.168.10.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg==\r\nSec-WebSocket-Version: 13\r\n\r\n",
        b"".join(webgw.client_frame(1, command, b"mask") for command in (b"s1", b"d3", b"u3", b"e-1")),
        webgw.client_frame(10, b"", b"mask"), webgw.client_frame(8, b"\x03\xe8", b"mask"))]
    frames.append(udp.ethernet({"source_ip": "192.168.10.2", "destination": "192.168.10.1",
                               "source_port": 40000, "port": 3334, "payload_hex": rfoundry_debug.frame(1, 0).hex()}))
    world = stub_world(executable=__file__, end_us=20_000)
    world.api.directory = tmp_path
    recorder.install(world)
    resolve.install(world)
    world.start()
    world.run("max")
    for index, frame in enumerate(frames, 1):
        world.submit(1, Kind.ETH_IN, frame, webgw.SOURCE, time_us=index * 1000, internal=True)
    while world.state != "Ended":
        world.advance()
    value = recording(world)
    assert value["end"] == {"t_us": 20_000, "reason": "WORLD_END_TIME", "complete": True}
    assert [event["eth"]["hex"] for event in value["events"]] == [frame.hex() for frame in frames]
    assert all(event["class"] == 30 and event["source"] == f"source-{webgw.SOURCE}" for event in value["events"])
    assert recording_lint.main([str(tmp_path / "recording")]) == 0


@pytest.mark.parametrize("frame", [
    udp.ethernet({"source_ip": "192.168.10.2", "destination": "192.168.10.1", "source_port": 40000,
                  "port": port, "payload_hex": data.hex()})
    for port, data in ((69, b"\0\2codeplug.bin\0octet\0"), (40001, b"\0\3\0\1" + SENTINEL),
                       (3334, rfoundry_debug.frame(0x1c, 1, b"\4" + SENTINEL)), (3334, b"opaque"))
] + [tcp_frame(data) for data in (
    b"GET /?token=secret HTTP/1.1\r\nHost: 192.168.10.1\r\n\r\n",
    b"GET / HTTP/1.1\r\nHost: 192.168.10.1\r\nCookie: secret\r\n\r\n",
    b"POST /codeplug HTTP/1.1\r\n\r\n" + SENTINEL,
    webgw.client_frame(2, SENTINEL, b"mask"), webgw.client_frame(1, b"token=secret", b"mask"),
    webgw.client_frame(1, b"d3", b"mask")[:-1],
)] + [tcp_frame(b"opaque", port=4532)])
def test_eth_storage_opaque_and_secret_payloads_are_refused_by_recorder_and_lint(frame):
    with pytest.raises(recorder.StopRecording, match="RECORD_ETH_INPUT:"):
        resolve.storage_filter({"type": "input", "kind": "ETH_IN", "payload_hex": frame.hex()})
    from scenario_test import VALID_V2
    value = json.loads(json.dumps(VALID_V2))
    value["radios"][0]["dmr_id"] = 3100001
    value["radios"][1]["dmr_id"] = value["radios"][1]["storage"][0]["id"] = 3100002
    value["events"] = [{"t_us": 1000, "round": 0, "class": 30, "seq": 1, "source": "source-512",
                        "radio": "a", "eth": {"hex": frame.hex()}}]
    with pytest.raises(ValueError):
        recording_lint.lint(value)


def test_eth_refusal_leaves_no_storage_or_session_secret_on_disk(stub_world, tmp_path):
    for name, payload, reason in (
            ("storage", b"\0\3\0\1" + SENTINEL, "RECORD_ETH_INPUT:"),
            ("secret", b"session-token-must-not-be-recorded", "RECORD_SECRET_IN_INPUT")):
        directory = tmp_path / name
        directory.mkdir()
        world = stub_world(executable=__file__, end_us=1000)
        world.api.directory = directory
        recorder.install(world)
        resolve.install(world)
        if name == "secret":
            audit.get(world).register_secret(payload)
        world.start()
        world.run("max")
        frame = udp.ethernet({"source_ip": "192.168.10.2", "destination": "192.168.10.1",
                              "source_port": 40000, "port": 69, "payload_hex": payload.hex()})
        world.submit(1, Kind.ETH_IN, frame, 512, time_us=1000, internal=True)
        while world.state != "Ended":
            world.advance()
        value = recording(world)
        assert not value["end"]["complete"] and value["end"]["reason"].startswith(reason)
        assert value["events"] == []
        for path in folder_files(directory):
            data = path.read_bytes().lower()
            assert payload.lower() not in data and payload.hex().encode() not in data


def test_identity_is_the_running_binary_even_after_rebuild(vhost_elf, tmp_path, monkeypatch):
    import shutil
    from tools.conductor.world import Config, World

    executable = tmp_path / "radio.elf"
    shutil.copy2(vhost_elf, executable)
    original = hashlib.sha256(executable.read_bytes()).hexdigest()
    monkeypatch.setattr(resolve, "conductor_sha256", lambda: "a" * 64)
    world = World(Config(executable=str(executable)))
    recorder.install(world)
    resolve.install(world)
    world.api.bind("identity", tmp_path)
    try:
        world.start()
        replacement = tmp_path / "replacement.elf"
        shutil.copy2(executable, replacement)
        with replacement.open("ab") as output:
            output.write(b"replacement")
        replacement.replace(executable)
        monkeypatch.setattr(resolve, "conductor_sha256", lambda: "b" * 64)
    finally:
        world.close()
    identity = recording(world)["radios"][0]["build"]
    assert identity["sha256"] == original
    assert identity["conductor_sha256"] == "a" * 64


def test_rebuild_between_boots_stops_recording(stub_world, tmp_path):
    executable = tmp_path / "radio.elf"
    executable.write_bytes(b"first")
    world = stub(stub_world, tmp_path, "rebuild", executable=str(executable))
    executable.write_bytes(b"second")
    world.pause()
    world.restart(1)
    world.close()
    value = recording(world)
    assert value["radios"][0]["build"]["sha256"] == hashlib.sha256(b"first").hexdigest()
    assert value["end"]["complete"] is False
    assert value["end"]["reason"] == "RECORD_BUILD_CHANGED:radio-1:boot-2"


def test_recording_history_stays_off_heap_through_shutdown(stub_world, tmp_path):
    import gc
    import tracemalloc
    from tools.conductor.parts import frames, view
    from tools.conductor.tests.test_frames import chunks
    from tools.conductor.tests.test_view import payload

    world = stub_world(executable=__file__, end_us=100_000)
    world.api.directory = tmp_path
    recorder.install(world)
    resolve.install(world)
    frames.install(world)
    view.install(world)
    world.start()
    tracemalloc.start()
    try:
        baseline = tracemalloc.get_traced_memory()[0]
        for i in range(12000):
            world.resolver.trace(1, Message(Kind.TRACE, (str(i) + "x" * 1000).encode()))
            world.notify({"event": "air_decision", "ordinal": i, "reason": "x" * 500})
            world.view_checkpoints.receive(1, Message(Kind.VIEW, payload(frame=i, time_us=0)))
            for chunk in chunks(frame_id=i, width=1, height=1):
                world.frames.receive(1, chunk)
        gc.collect()
        assert tracemalloc.get_traced_memory()[0] - baseline < 4_000_000
        world.close()
        assert tracemalloc.get_traced_memory()[1] - baseline < 8_000_000
    finally:
        tracemalloc.stop()
        world.close()
    result = json.loads((tmp_path / "result.json").read_text())
    assert "fold_error" not in result, result
    assert result["traces"]["radio-1"]["lines"] == 12000
    assert result["air"]["decisions"] == 12000
    assert result["checkpoints"] == {"VIEW": 12000, "FRAME": 12000, "STORE": 0}
    assert result["lint_error"] is None
