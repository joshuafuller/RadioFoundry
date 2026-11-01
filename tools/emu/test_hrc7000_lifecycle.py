# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Owner termination must also terminate the actual QEMU process group."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_standalone_debug_ports_remain_reserved():
    import socket
    from tools.emu.hrc7000_emu import HostSocket

    first, second = HostSocket(0), HostSocket(0)
    try:
        addresses = [host.srv.getsockname() for host in (first, second)]
        assert addresses[0] != addresses[1]
        for address in addresses:
            assert address[0] == "127.0.0.1" and address[1] != 0
            with socket.socket() as contender:
                with pytest.raises(OSError):
                    contender.bind(address)
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("default_index", [False, True])
def test_qemu_owner_death(tmp_path, default_index):
    from tools.conductor.parts.emu import PLATFORMS
    from tools.emu.hrc7000_emu import DEFAULT_QEMU

    elf = next(c["elf"] for c in PLATFORMS.values() if c["engine"] == "qemu")
    if not os.path.exists(os.environ.get("CSKY_QEMU", DEFAULT_QEMU)) or not os.path.exists(elf):
        pytest.skip("needs QEMU and firmware ELF")
    # A separate subreaper owns every descendant, including cleanup after an expected failure.
    result = subprocess.run([sys.executable, __file__, elf, str(int(default_index))],
                            env=dict(os.environ, TMPDIR=str(tmp_path)), capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def owner_death(elf, default_index):
    import ctypes
    import signal
    import time

    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    owner = subprocess.Popen([sys.executable, "-c", """
import sys
from tools.conductor.parts.emu import HrcChannel
args = () if int(sys.argv[2]) else ('--radio-index', '1')
channel = HrcChannel(sys.argv[1], *args)
print('GROUP', channel.process.pid, flush=True)
sys.stdin.read()
""", elf, str(int(default_index))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    group = None
    try:
        for line in owner.stdout:
            if line.startswith("GROUP "):
                group = int(line.split()[1])
                break
        assert group is not None, "channel failed before its initial pause"
        owner.kill()
        owner.wait(10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                while os.waitpid(-1, os.WNOHANG)[0]:
                    pass
            except ChildProcessError:
                pass
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        raise AssertionError(f"QEMU group {group} survived its owner's SIGKILL")
    finally:
        owner.kill()
        owner.wait(10)
        owner.stdin.close()
        owner.stdout.close()
        if group:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            while True:
                os.waitpid(-1, 0)
        except ChildProcessError:
            pass


if __name__ == "__main__":
    # Direct execution puts this directory, rather than the repository root, on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    owner_death(sys.argv[1], bool(int(sys.argv[2])))
