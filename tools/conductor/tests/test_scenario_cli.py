# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Saved scenario artifacts through the public CLI, using real host firmware."""

import json
from pathlib import Path
import stat
import subprocess
import sys

import pytest

CLI = Path(__file__).resolve().parents[2] / "digital-twin/scenario.py"


def call(*args):
    return subprocess.run([sys.executable, str(CLI), *map(str, args)],
                          capture_output=True, text=True, timeout=100, umask=0o022)


@pytest.mark.parametrize("command", ["run", "suite", "minimize"])
def test_export_modes_are_private(vhost_elf, tmp_path, command):
    output = tmp_path / "new-parent" / "export"
    args = [command, "@air_pair", "--out", output, "--worker", vhost_elf]
    if command == "suite":
        args.insert(2, "@missing")  # The failure artifact must be private too.
    if command == "minimize":
        args += ["--reference-worker", vhost_elf]
    result = call(*args)
    assert result.returncode == {"run": 0, "suite": 1, "minimize": 2}[command], result.stdout + result.stderr
    assert (output / ("initial-failing/result.json" if command == "minimize" else "result.json")).is_file()
    for path in [output.parent, *output.parent.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600), path


def test_run_result_round_trips_through_compare(vhost_elf, tmp_path):
    output = tmp_path / "run"
    run = call("run", "@air_pair", "--worker", vhost_elf, "--out", output)
    assert run.returncode == 0, run.stdout + run.stderr
    saved = output / "result.json"
    compared = call("compare", saved, saved)
    assert compared.returncode == 0, compared.stdout + compared.stderr
    assert json.loads(compared.stdout)["comparison"] == "identical"
