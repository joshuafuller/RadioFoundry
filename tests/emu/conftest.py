# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Every test here fails if a Renode it started is still running after it (tools/emu/renode_guard.py)."""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "renode_guard", Path(__file__).resolve().parents[2] / "tools/emu/renode_guard.py")
renode_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(renode_guard)


@pytest.fixture(autouse=True)
def no_renode_left():
    yield
    left = renode_guard.check_none_left()
    assert not left, f"Renode outlived the test (killed now): {left}"
