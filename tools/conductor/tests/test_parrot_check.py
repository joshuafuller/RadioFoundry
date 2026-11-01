# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Parrot check expectations and prerequisite exit codes, plus the live reproduction."""

import sys

import pytest

from tools.conductor.checks import parrot_check as check


def echo_views(caller_plays, bystander_plays=True):
    timeline = {}
    copies = []
    for radio, played in ((1, caller_plays), (2, bystander_plays)):
        before = {"state": "idle", "src": "0", "rx_calls": 0, "played": 0, "frames": 0}
        during = {"state": "rx" if played else "rx_other", "src": check.OWN[1],
                  "rx_calls": 1, "played": int(played), "frames": 6 if played else 0}
        timeline[radio] = [(900_000, before), (1_300_000, during), (1_600_000, during)]
        copies.extend({"receiver": radio, "sender": 1, "element": "parrot", "air_start_us": start}
                      for start in (1_000_000, 1_570_000))
    return timeline, copies


@pytest.mark.parametrize("options, played, result", [
    ([], False, 0), ([], True, 1),
    (["--expect", "bug"], False, 0), (["--expect", "bug"], True, 1),
    (["--expect", "fixed"], True, 0), (["--expect", "fixed"], False, 1),
])
def test_echo_expectation_exit_codes(monkeypatch, capsys, options, played, result):
    monkeypatch.setattr(check, "live_run", lambda *a, **k: (None, *echo_views(played)))
    assert check.main(["--part", "echo", "--executable", sys.executable, *options]) == result
    output = capsys.readouterr().out
    if not options:
        from tools import demo
        verdict, evidence = demo.parrot_verdict("echo", result, output.splitlines())
        assert verdict == ("FAIL" if played else "PASS"), evidence
    assert f"parrot_check: {'PASS' if result == 0 else 'FAIL'} --part echo" in output
    if result == 1:
        assert "echo FAIL expected" in output
    elif not played:
        assert "reproduced caller echo bug" in output


@pytest.mark.parametrize("fault", ["bystander", "copies", "views"])
def test_incomplete_echo_is_not_a_successful_reproduction(monkeypatch, capsys, fault):
    timeline, copies = echo_views(False, bystander_plays=fault != "bystander")
    if fault == "copies":
        copies.clear()
    if fault == "views":
        timeline[1].clear()
    monkeypatch.setattr(check, "live_run", lambda *a, **k: (None, timeline, copies))
    assert check.main(["--part", "echo", "--executable", sys.executable]) == 1
    assert "parrot_check: FAIL" in capsys.readouterr().out


@pytest.mark.parametrize("part", ["mixed", "all"])
@pytest.mark.parametrize("fixed", ["absent", "missing", "directory"])
def test_mixed_without_fixed_build_skips(part, fixed, tmp_path, monkeypatch, capsys):
    options = [] if fixed == "absent" else ["--fixed", str(tmp_path if fixed == "directory" else tmp_path / "missing")]
    monkeypatch.setattr(check, "live_run", lambda *a, **k: pytest.fail("unavailable mixed run started"))
    assert check.main(["--part", part, *options]) == 2
    assert capsys.readouterr().out == (
        "parrot_check: SKIP mixed: no fixed build; pass --fixed PATH to an existing fixed executable\n")


def test_mixed_with_fixed_build_runs(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(check, "part_mixed", lambda fixed, executable, runtime: calls.append((fixed, executable)))
    assert check.main(["--part", "mixed", "--fixed", sys.executable, "--executable", sys.executable]) == 0
    assert calls == [(sys.executable, sys.executable)]
    assert "parrot_check: PASS --part mixed" in capsys.readouterr().out


def test_live_echo_reproduces_bug(vhost_elf):
    assert check.main(["--part", "echo", "--executable", str(vhost_elf)]) == 0
