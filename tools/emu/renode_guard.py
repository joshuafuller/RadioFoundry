# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""An emulator never outlives the Python process that started it.

The problem. Every check starts Renode in its own session (start_new_session=True), so its kill of the process group
gets Renode and its timeout wrapper and nothing else. That cleanup is Python code: a finally block or close(). When
the Python process itself is killed (the landing script's timeout, subprocess.run(timeout=...) on emu_check.py, a
SIGKILL), no Python code runs, and Renode is in a session the killer does not reach. It then runs on until its own
timeout -s KILL limit: 1800 s for tools/conductor/parts/emu.py, no limit for boards/mduv380/emu/boot_test.py.

The fix. GuardedPopen starts the command exactly as before, in its own session (its pid is the group id, so
os.killpg(p.pid, ...) in the callers keeps working). Next to it, it starts this file as a small watcher:

    python (owner) --pipe--> renode_guard.py <fd> <pgid>      (own session too)
                   --------> timeout -s KILL N renode ...   (group <pgid>)

The owner keeps the write end of a pipe and never writes to it. The kernel closes it when the owner exits, however it
exits; GuardedPopen also closes it once the command has ended. The watcher then sees end of file and kills group
<pgid>. It is a sibling, not a parent, because GNU timeout moves itself into a new process group when it is not a
session leader, so a parent's own group would not hold Renode. The watcher also exits when the group is gone.

leftovers() lists the Renode, timeout and guard processes below a pid. The pytest fixture in conftest.py calls it
after every test and fails the test if any are still there.
"""

import os
import select
import signal
import subprocess
import sys
import time

GUARD = os.path.abspath(__file__)
NAMES = ("renode", "qemu-system-cskyv2")


class GuardedPopen(subprocess.Popen):
    """subprocess.Popen for emulator commands: own session, and its group is killed when this process dies."""

    def __init__(self, args, **kwargs):
        kwargs["start_new_session"] = True
        self._owner_end = self._watcher = None
        read_end = None
        super().__init__(args, **kwargs)
        try:
            read_end, self._owner_end = os.pipe()  # only the watcher inherits read_end
            self._watcher = subprocess.Popen([sys.executable, "-I", GUARD, str(read_end), str(self.pid)],
                                             pass_fds=(read_end,), start_new_session=True, stdin=subprocess.DEVNULL)
        except BaseException:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.wait()
            raise
        finally:
            if read_end is not None:
                os.close(read_end)

    def _release(self):
        if getattr(self, "_owner_end", None) is not None:
            os.close(self._owner_end)
            self._owner_end = None
            if self._watcher is not None:
                self._watcher.wait(10)

    def poll(self):
        code = super().poll()
        if code is not None:
            self._release()
        return code

    def wait(self, timeout=None):
        code = super().wait(timeout)
        self._release()
        return code

    def __del__(self):
        try:
            self._release()
        except Exception:
            pass
        super().__del__()


def _children():
    """{ppid: [pid, ...]} for every process now."""
    tree = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                stat = f.read()
        except OSError:
            continue
        ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        tree.setdefault(ppid, []).append(int(name))
    return tree


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def leftovers(root=None):
    """[(pid, command line)] of emulator, timeout and guard descendants of root (default: this process).
    Other callers' processes are never listed."""
    root = os.getpid() if root is None else root
    tree, found, todo = _children(), [], [root]
    while todo:
        for pid in tree.get(todo.pop(), ()):
            todo.append(pid)
            line = _cmdline(pid)
            words = line.split()
            exe = os.path.basename(words[0]) if words else ""
            if exe in NAMES or GUARD in line or (exe == "timeout" and any(name in line for name in NAMES)):
                found.append((pid, line))
    return found


def check_none_left(root=None, wait_s=10.0):
    """Wait up to wait_s for this process's emulators to end. Kill any still there and return them ([] when none)."""
    deadline = time.monotonic() + wait_s
    left = leftovers(root)
    while left and time.monotonic() < deadline:
        time.sleep(0.1)
        left = leftovers(root)
    for pid, _ in left:
        for kill in (os.killpg, os.kill):
            try:
                kill(os.getpgid(pid) if kill is os.killpg else pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    return left


def main(argv):
    """renode_guard.py <owner pipe fd> <pgid>: kill group pgid when the owner's pipe closes; exit once it is gone."""
    owner, pgid = int(argv[1]), int(argv[2])
    while True:
        ready, _, _ = select.select([owner], [], [], 0.5)
        try:
            os.killpg(pgid, signal.SIGKILL if ready else 0)    # the owner never writes: readable means it is gone
        except ProcessLookupError:
            return 0
        if ready:
            return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
