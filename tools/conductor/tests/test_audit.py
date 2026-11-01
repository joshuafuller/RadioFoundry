# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The audit stream and its token filter."""

import json
import stat
import subprocess

import pytest

from tools.conductor.parts import audit, recorder

TOKEN = "k3JxQ9vTn2LmZp8Rw4Yd7Ub1Hc6Sf0Ae"


@pytest.fixture
def live(stub_world, tmp_path):
    world = stub_world()
    recorder.install(world)
    world.api.bind("aud", tmp_path)
    world.start()
    return world


def grep_count(token, path):
    result = subprocess.run(["grep", "-c", token, str(path)], text=True, stdout=subprocess.PIPE)
    return result.stdout.strip()


def test_emit_writes_both_files(live):
    line = audit.emit(live, "lease_acquire", "uid:1000", 1, "accepted", "OK", layer="debug", lease_generation=3)
    assert line["time_us"] == 0 and line["host_ns"] > 0 and line["lease_generation"] == 3
    mine = [json.loads(x) for x in (live.api.directory / "audit.jsonl").read_text().splitlines()]
    events = [json.loads(x) for x in (live.api.directory / "events.jsonl").read_text().splitlines()]
    assert mine[0]["kind"] == "lease_acquire" and mine[0]["result"] == "accepted"
    assert [e for e in events if e["type"] == "audit"][0]["principal"] == "uid:1000"
    assert stat.S_IMODE((live.api.directory / "audit.jsonl").stat().st_mode) == 0o600


def test_token_never_reaches_either_file(live, capsys):
    audit.get(live).register_secret("browserOneTime")
    line = audit.emit(live, "web_login", "uid:1000", 1, "refused", f"bad {TOKEN}",
                      session_token=TOKEN, detail={"x": TOKEN}, note="browserOneTime", command="key")
    assert line["refused"] == ["detail.x", "note", "reason", "session_token"]
    assert line["command"] == "key"
    results = []
    for name in ("audit.jsonl", "events.jsonl"):
        results.append(grep_count(TOKEN, live.api.directory / name))
        results.append(grep_count("browserOneTime", live.api.directory / name))
    assert results == ["0", "0", "0", "0"]
    with capsys.disabled():
        print(f"\ngrep -c token (audit, events): {results}; refused={line['refused']}")


def test_plain_names_pass(live):
    line = audit.emit(live, "key", "uid:1000", 1, "refused", "WORLD_NOT_LEASE_HOLDER", capability="maintenance")
    assert "refused" not in line and line["capability"] == "maintenance"


def test_bytes_value_is_refused(live):
    line = audit.emit(live, "send", "uid:1000", 1, "accepted", "OK", payload=b"\x01secret")
    assert line["payload"] == audit.REFUSED
