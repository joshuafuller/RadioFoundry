#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Boot the unchanged release ELF through mduv380.resc and exercise its debug link.

Run from the repository root after make BOARD=mduv380 and creating a new image with
python3 tools/emu/flashimg.py build/mduv380/flash.bin:
    python3 boards/mduv380/emu/boot_test.py

Needs PTYs, pyserial and Renode ($RENODE or ~/tools/renode/renode).
Leaves the Renode log, home/menu PNGs and FRAME lines in build/mduv380/emu-check/.
"""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/emu"))
import emu_check
from renode_guard import GuardedPopen



def main():
    out = ROOT / "build/mduv380/emu-check"
    out.mkdir(parents=True, exist_ok=True)
    for name in ("home.png", "menu.png", "frames.txt"):
        (out / name).unlink(missing_ok=True)
    private = tempfile.TemporaryDirectory(prefix="rf-boot-")
    terminal = str(Path(private.name) / "debuglink")
    deadline = time.monotonic() + 120
    with (out / "renode.log").open("w") as log:
        proc = GuardedPopen(
            [emu_check.default_renode(), "--disable-xwt", "--console", "--plain", "-P", "-1", "-e",
             f'$tcp=false; include @boards/mduv380/emu/radio.resc; '
             f'emulation CreateUartPtyTerminal "debuglink" @{terminal}; '
             'connector Connect sysbus.hostport debuglink; '
             f'sysbus.lcd FrameLog @{out}/frames.txt; start'],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, text=True,
            env=dict(os.environ, XDG_CONFIG_HOME=str(out / "config")),
        )

        def monitor(command):
            proc.stdin.write(command + "\n")
            proc.stdin.flush()

        try:
            version = None
            while time.monotonic() < deadline:
                assert proc.poll() is None, f"Renode exited; see {out}/renode.log"
                try:
                    with emu_check.rd.DebugLink(terminal, timeout=2) as link:
                        version = link.version()
                    break
                except emu_check.rd.DebugLinkError:
                    time.sleep(0.1)
            assert version is not None, f"No GET_VERSION; see {out}/renode.log"
            assert version["board"] == "MD-UV380", version
            print(f'GET_VERSION: board={version["board"]}')
            with emu_check.rd.DebugLink(terminal, timeout=3) as link:
                # ui.screen reads home already during the boot splash, whose keys the UI drops (core/ui/ui.c
                # ui_get_status, ui_task). ui.draws counts frames from after that drain, so wait for one
                while time.monotonic() < deadline:
                    _, _, view = link.view_section("link")
                    if view.get("ui.screen") == "home" and int(view.get("ui.draws", "0")) > 0:
                        break
                assert view.get("ui.screen") == "home" and int(view.get("ui.draws", "0")) > 0, view
                monitor(f"pause; sysbus.lcd SavePng @{out}/home.png 1; start")
                link.key("green")
                while time.monotonic() < deadline:
                    _, _, view = link.view_section("link")
                    if view.get("ui.screen") != "home":
                        break
                assert view.get("ui.screen") not in (None, "home"), view
                print(f'KEY green: home -> {view["ui.screen"]}')
                monitor(f"pause; sysbus.lcd SavePng @{out}/menu.png 1; sysbus.lcd LastFrame")
        finally:
            try:
                monitor("quit")
                proc.wait(timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)
            proc.stdin.close()
            private.cleanup()
    assert proc.returncode == 0, f"Renode exit {proc.returncode}; see {out}/renode.log"
    assert (out / "home.png").read_bytes() != (out / "menu.png").read_bytes(), "screen did not change"
    frames = (out / "frames.txt").read_text().splitlines()
    assert frames and frames[-1].startswith("FRAME radio="), frames
    print(frames[-1])
    print(f"PASS: GET_VERSION, home PNG and key-driven screen change; outputs {out}")


if __name__ == "__main__":
    sys.exit(main())
