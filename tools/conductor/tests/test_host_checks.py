# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The stall gate remains bounded even if the firmware watchdog never returns."""

import os
from pathlib import Path
import signal
import subprocess

import pytest


@pytest.mark.parametrize("step", ["host", "python", "firmware"])
def test_ci_attempts_checkout_and_stops_on_failure(tmp_path, step):
    root = Path(__file__).resolve().parents[3]
    script = tmp_path / "tools/ci.sh"
    script.parent.mkdir()
    script.write_bytes((root / "tools/ci.sh").read_bytes())
    checkout = script.parent / "checkout-submodules.sh"
    checkout.write_text("#!/bin/sh\necho checkout-attempted\nexit 1\n")
    checkout.chmod(0o700)
    result = subprocess.run(["sh", str(script), "--steps", step], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert "checkout-attempted" in result.stdout
    assert "ci: submodule checkout failed" in result.stderr


@pytest.mark.parametrize("present", [False, True])
def test_ci_host_initializes_only_missing_submodules(tmp_path, present):
    root = Path(__file__).resolve().parents[3]
    script = tmp_path / "tools/ci.sh"
    script.parent.mkdir()
    script.write_bytes((root / "tools/ci.sh").read_bytes())
    header = tmp_path / "third_party/FreeRTOS-Kernel/include/FreeRTOS.h"
    header.parent.mkdir(parents=True)
    if present:
        header.touch()
    checkout = script.parent / "checkout-submodules.sh"
    checkout.write_text("#!/bin/sh\necho checkout-attempted\ntouch third_party/FreeRTOS-Kernel/include/FreeRTOS.h\n")
    checkout.chmod(0o700)
    host = tmp_path / "tests/host"
    host.mkdir(parents=True)
    (host / "Makefile").write_text("all:\n\t@test -f ../../third_party/FreeRTOS-Kernel/include/FreeRTOS.h\n")
    result = subprocess.run(["sh", str(script), "--steps", "host"], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("checkout-attempted" in result.stdout) is not present
    assert "all checks passed" in result.stdout


def test_host_without_submodules_skips_virtual_board(tmp_path):
    root = Path(__file__).resolve().parents[3]
    (tmp_path / "Makefile").write_text(f"ROOT := {tmp_path}\ninclude {root}/boards/vhost/tests/host.mk\n")
    result = subprocess.run(["make", "run-boards"], cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipped, FreeRTOS missing; run tools/checkout-submodules.sh" in result.stdout


def test_stall_gate_has_an_outer_timeout(tmp_path):
    root = Path(__file__).resolve().parents[3]
    stuck = tmp_path / "stuck"
    stuck.write_text("#!/bin/sh\nexec sleep 60\n")
    stuck.chmod(0o700)
    # Use the real recipe with an isolated output folder and no build prerequisite.
    recipe = (root / "boards/vhost/tests/mk/stall.mk").read_text()
    recipe = recipe[recipe.index("vhost-stall:"):recipe.index(".PHONY:")]
    recipe = recipe.replace("vhost-stall: vhost-elf", "vhost-stall:")
    (tmp_path / "Makefile").write_text("VSTALL_BUDGET := 5\n" + recipe)
    process = subprocess.Popen(["make", "vhost-stall", f"VHOST_ELF={stuck}", "VSTALL_TIMEOUT=0.2"],
                               cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=3)
        assert process.returncode != 0
        assert "outer timeout" in output
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnknownMarkWarning")
def test_hub_count_uses_key_metadata(tmp_path, monkeypatch):
    import importlib.util
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "tests/hil"))
    spec = importlib.util.spec_from_file_location("hub_test", root / "tests/hil/test_ui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "keys.c"
    source.write_text("const keymap_entry_t keys[] = {\n {0, ACT_SELECT},\n};\n")
    assert module._hub_count(tmp_path) == 8
    source.write_text("const keymap_entry_t keys[] = {\n {0, ACT_CONTEXT},\n};\n")
    assert module._hub_count(tmp_path) == 7
