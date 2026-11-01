# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The live events.jsonl: stamps, mode, conductor events and the recording filter hook."""

import json
import stat

import pytest

from tools.conductor.channel import Kind
from tools.conductor.parts import audit, recorder
from tools.conductor.parts.recorder import StopRecording


def read(world, name="events.jsonl"):
    return [json.loads(line) for line in (world.api.directory / name).read_text().splitlines()]


@pytest.fixture
def live(stub_world, tmp_path):
    world = stub_world()
    recorder.install(world)
    world.api.bind("rec", tmp_path)
    world.start()
    return world


def test_bytes_in_line_has_stamp_and_mode(live, capsys):
    live.submit(1, Kind.BYTES_IN, b"\x01\xab", 7, time_us=5000, internal=True)
    line = [row for row in read(live) if row["type"] == "input"][0]
    assert line["time_us"] == 5000 and line["round"] == 0 and line["class"] == 30
    assert line["sequence"] == 1 and line["source"] == 7 and line["payload_hex"] == "01ab"
    assert line["kind"] == "BYTES_IN" and line["radio"] == 1
    path = live.api.directory / "events.jsonl"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    with capsys.disabled():
        print(f"\nevents line={line}\nmode={mode:o}")


def test_starts_with_world_and_records_conductor_events(live):
    rows = read(live)
    assert [r["event"] for r in rows if r["type"] == "conductor"][:1] == ["radio_started"]
    live.power_off(1)
    assert "radio_off" in [r.get("event") for r in read(live)]


def test_lines_before_bind_are_kept(stub_world, tmp_path):
    world = stub_world()
    recorder.install(world)
    world.start()  # no folder yet
    assert world.recorder.lines == 0 and world.recorder.pending
    world.api.bind("late", tmp_path)
    world.recorder.flush()
    assert read(world)[0]["event"] == "radio_started"


def test_restart_reason_is_a_conductor_event(stub_world, tmp_path):
    world = stub_world()
    recorder.install(world)
    world.api.bind("rr", tmp_path)
    world.start()
    world.radios[1].channel.outputs.append((Kind.RESTART_REASON, b"watchdog"))
    world.run("max")
    world.advance()
    row = [r for r in read(world) if r.get("event") == "radio_restart_reason"][0]
    assert row["reason"] == "watchdog" and row["radio"] == 1


def test_filter_edits_and_stops(live):
    live.recorder.add_filter(lambda line: {**line, "seen": True})
    live.submit(1, Kind.BYTES_IN, b"a", 1, time_us=500, internal=True)

    def refuse(line):
        if line["type"] == "input" and line["payload_hex"] == "62":
            raise StopRecording("RECORD_STORAGE_BYTES")
        return line

    live.recorder.add_filter(refuse)
    live.submit(1, Kind.BYTES_IN, b"b", 1, time_us=1000, internal=True)
    live.submit(1, Kind.BYTES_IN, b"c", 1, time_us=2000, internal=True)
    rows = read(live)
    assert rows[-1]["type"] == "recording_stopped" and rows[-1]["reason"] == "RECORD_STORAGE_BYTES"
    assert any(r.get("seen") for r in rows) and not any(r.get("payload_hex") == "63" for r in rows)


def test_secret_in_input_ends_recording(live):
    audit.get(live).register_secret("s3cretValue")
    live.submit(1, Kind.BYTES_IN, b"x s3cretValue", 1, time_us=500, internal=True)
    rows = read(live)
    assert rows[-1]["reason"] == "RECORD_SECRET_IN_INPUT"
    assert b"s3cretValue" not in (live.api.directory / "events.jsonl").read_bytes()
