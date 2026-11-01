# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""A failed cleanup watcher must not leave its child or pipe behind."""

import os
import signal
import subprocess

import pytest

from tools.emu import renode_guard


@pytest.mark.parametrize("failure", ["pipe", "watcher"])
def test_initialization_failure_reaps_child(monkeypatch, failure):
    children = []
    original_init = subprocess.Popen.__init__

    def fail(*args, **kwargs):
        raise OSError("injected cleanup setup failure")

    def started(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        children.append(self)
        # Popen also uses pipe; inject only after the actual child starts.
        monkeypatch.setattr(renode_guard.os if failure == "pipe" else renode_guard.subprocess,
                            "pipe" if failure == "pipe" else "Popen", fail)

    monkeypatch.setattr(subprocess.Popen, "__init__", started)
    try:
        with pytest.raises(OSError, match="injected cleanup setup failure"):
            renode_guard.GuardedPopen(["sleep", "30"])
        assert children
        with pytest.raises(ChildProcessError):
            os.waitpid(children[0].pid, os.WNOHANG)
        assert children[0]._owner_end is None
    finally:
        for child in children:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
