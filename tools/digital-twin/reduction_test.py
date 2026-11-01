# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Focused reducer check against two actual firmware builds, supplied explicitly."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from scenario_test import VALID


def check(bad_worker, good_worker):
    scenario = copy.deepcopy(VALID)
    packet = scenario["events"][0]
    scenario["events"] = [
        {"at_ms": 0, "radio": "a", "action": "refuse_tx", "enabled": True}, packet,
        {"at_ms": 1000, "radio": "a", "action": "refuse_tx", "enabled": False},
        {**packet, "at_ms": 1500, "payload_hex": b"recovered".hex()},
        {"at_ms": 3000, "radio": "b", "action": "sync_loss"},
        {"at_ms": 4000, "radio": "a", "action": "queue_limits", "wait_ms": 100, "max_age_ms": 100},
    ]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "input.json"
        source.write_text(json.dumps(scenario))
        command = [sys.executable, str(Path(__file__).with_name("scenario.py")), "minimize", str(source),
                   "--worker", str(bad_worker), "--reference-worker", str(good_worker), "--out", str(root / "reduction")]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        report = json.loads(proc.stdout)
        assert report["status"] == "passed" and report["one_event_minimal"]
        reduced = json.loads((root / "reduction/minimized.json").read_text())
        assert reduced["events"] == scenario["events"][:4], reduced
        assert report["original_events"] == 6 and report["remaining_events"] == 4
        for name, expected in (("final-failing", "failed"), ("final-reference", "passed")):
            result = json.loads((root / "reduction" / name / "result.json").read_text())
            assert result["status"] == expected
            assert (root / "reduction" / name / "replay.json").exists()
        assert json.loads((root / "reduction/result.json").read_text()) == report
        bounded = command.copy()
        bounded[bounded.index("--out") + 1] = str(root / "bounded")
        bounded.extend(["--max-attempts", "1"])
        limited = subprocess.run(bounded, capture_output=True, text=True, timeout=30)
        assert limited.returncode == 1, limited.stdout + limited.stderr
        limit_report = json.loads(limited.stdout)
        assert limit_report["status"] == "not_run" and not limit_report["one_event_minimal"]
        assert limit_report["attempts"] == 1 and limit_report["remaining_events"] == 6
        passing = command.copy()
        passing[passing.index("--out") + 1] = str(root / "passing")
        passing[passing.index("--worker") + 1] = str(good_worker)
        rejected = subprocess.run(passing, capture_output=True, text=True, timeout=30)
        assert rejected.returncode == 2, rejected.stdout + rejected.stderr
        assert "requires assertion failure" in json.loads(rejected.stdout)["error"]
    print("PASS: real startup regression reduced from six events to four while preserving failing/passing builds")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bad-worker", required=True, type=Path)
    parser.add_argument("--good-worker", required=True, type=Path)
    args = parser.parse_args()
    check(args.bad_worker.resolve(), args.good_worker.resolve())
