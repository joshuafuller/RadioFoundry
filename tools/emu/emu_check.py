#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Emulation check: a board's unchanged release ELF answers the debug link in Renode, shows its screen, takes a key.

Boots build/<board>/rfoundry_<board>.elf in Renode headless (boards/<board>/emu/boot.resc with boards/<board>/emu/platform.repl),
bridges the debug link to a private PTY, asks GET_VERSION and one view section with tools/rfoundry_debug.py, and
prints them with the boot time and the emulation speed. Then it quits Renode and checks that no Renode process is left.

    python3 tools/emu/emu_check.py                          # default board; Renode from $RENODE or ~/tools/renode
    python3 tools/emu/emu_check.py --section storage --speed-s 8
    python3 tools/emu/emu_check.py --screen /tmp/panel.png  # also save the panel and print its FRAME line
    python3 tools/emu/emu_check.py --key down               # press and release a key; the screen must change

When boards/<board>/emu/flash.repl exists, every run gets a fresh data flash image (tools/emu/flashimg.py, --storage
and --radio-index, default dmr-group and 1) and the .resc loads the filled-in template as $flashrepl.

--screen and --key need a display model named `lcd` with the interface of peripherals/lcd_st7735.cs (FrameLog,
SavePng). A FRAME line is the virtual radio's screen checkpoint (platform/host/vframe.c): its crc is CRC-32 over the
panel bytes, so two pictures are the same exactly when their crc is.

--key takes a name of the conductor's key map (tools/conductor/parts/input.py KEYS) and sends the same KEY payload the
conductor sends (bit u8, down u8): down, 100 ms of firmware time, up. The payload goes to keys_key from
tools/emu/peripherals/keys.py when that file exists. Otherwise the board's optional keys_fallback.py drives its keypad GPIO lines.

Exit status 0 when the reply names the board and every step passed. Needs pyserial (PTY support).

C-SKY boards (PLATFORM=hrc7000 in boards/<board>/board.mk) use hrc7000_emu.py --boot --get-version instead.
That runner prints the firmware reply and the hardware stubs it needs. --screen, --key, --settle-ms and --scale go to
its own display and key models (a firmware with no model for its panel or keys exits 2).
"""
import argparse
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, HERE)
import rfoundry_debug as rd  # noqa: E402
from renode_guard import GuardedPopen  # noqa: E402

from board_config import BOARDS, default_board  # noqa: E402

KEYS_PY = os.path.join(HERE, "peripherals", "keys.py")

# The conductor's key map: KEY payload bit by name (tools/conductor/parts/input.py KEYS, from
# boards/vhost/keypad_vhost.c). Repeated here so this check needs no conductor import.
KEY_BITS = {"ok": 0, "select": 0, "back": 1, "up": 2, "down": 3, "ptt": 4, "monitor": 5,
            **{str(digit): digit + 6 for digit in range(10)}}


def default_renode():
    return os.environ.get("RENODE") or os.path.expanduser("~/tools/renode/renode")


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def board_matches(board, reported):
    """Compare letters and digits only, ignoring case."""
    def norm(s):
        return re.sub(r"[^0-9a-z]", "", (s or "").lower())
    return norm(board) == norm(reported)


class Renode:
    """One headless Renode running <board>.resc, in its own process group so nothing outlives the check, even when
    this process is killed (renode_guard.py)."""

    def __init__(self, renode, script, log_path, limit_s):
        self.log_path = log_path
        self.log = open(log_path, "w")
        # From the repository root, where the .resc defaults (relative paths) work
        self.p = GuardedPopen(["timeout", "-s", "KILL", str(limit_s), renode, "--disable-xwt", "--console",
                              "--plain", "-P", "-1", "-e", script], stdin=subprocess.PIPE, stdout=self.log,
                              stderr=subprocess.STDOUT, text=True, cwd=ROOT)
        self.pgid = self.p.pid

    def monitor(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def wait_terminal(self, terminal, deadline):
        while time.monotonic() < deadline:
            if self.p.poll() is not None:
                return False
            if os.path.exists(terminal):
                os.chmod(terminal, 0o600)
                return True
            time.sleep(0.05)
        return False

    def stop(self):
        """quit through the monitor, then kill the group if it is still there. True when nothing is left."""
        try:
            self.monitor("quit")
        except (BrokenPipeError, OSError):
            pass
        try:
            self.p.wait(30)
        except subprocess.TimeoutExpired:
            os.killpg(self.pgid, signal.SIGKILL)
            self.p.wait(10)
        for _ in range(50):
            if not group_alive(self.pgid):
                break
            time.sleep(0.1)
        self.log.close()
        return not group_alive(self.pgid)


class Check(Exception):
    pass


class Screen:
    """The panel through the display model: FRAME lines from its FrameLog file, PNGs from SavePng."""

    def __init__(self, r, frame_log, scale, deadline):
        self.r, self.frame_log, self.scale, self.deadline = r, frame_log, scale, deadline

    def frames(self):
        try:
            with open(self.frame_log) as f:
                return [ln.strip() for ln in f if ln.startswith("FRAME ")]
        except FileNotFoundError:
            return []

    def shot(self, path):
        """Pause the machine, save the picture, read the last FRAME line, run on. Returns that line."""
        if os.path.exists(path):
            os.unlink(path)
        self.r.monitor("pause")
        self.r.monitor(f"lcd SavePng @{os.path.abspath(path)} {self.scale}")
        size = -1
        while time.monotonic() < self.deadline:     # the monitor runs commands in order: the file means paused
            if os.path.exists(path) and os.path.getsize(path) == size and size > 0:
                break
            size = os.path.getsize(path) if os.path.exists(path) else -1
            time.sleep(0.1)
        else:
            raise Check(f"no PNG at {path}")
        frames = self.frames()
        self.r.monitor("start")
        if not frames:
            raise Check("the display model wrote no FRAME line")
        return frames[-1]


def crc_of(frame):
    m = re.search(r"\bcrc=([0-9a-f]{8})\b", frame)
    return m.group(1) if m else None


def uptime(d):
    return int(d.version()["uptime_ms"])


def wait_firmware(d, ms, deadline):
    """Wait until ms of firmware time passed (debug link uptime). Returns the uptime then."""
    start = now = uptime(d)
    while now - start < ms:
        if time.monotonic() > deadline:
            raise Check(f"timeout waiting for {ms} ms of firmware time")
        time.sleep(0.2)
        now = uptime(d)
    return now


def wait_settled(d, screen, min_uptime, quiet_ms, deadline):
    """Wait until the firmware ran min_uptime ms and then quiet_ms with no new FRAME line."""
    now = uptime(d)
    if now < min_uptime:
        wait_firmware(d, min_uptime - now, deadline)
    while True:
        n = len(screen.frames())
        wait_firmware(d, quiet_ms, deadline)
        if len(screen.frames()) == n:
            return


def ui_input(d):
    """The firmware's own view of the UI and the keys (view section link: ui.screen, input.raw_keys)."""
    kv = d.view_section("link")[2]
    return f"ui.screen={kv.get('ui.screen')} input.raw_keys={kv.get('input.raw_keys')}"


def press(r, key_cmd, d, bit, deadline, hold_ms=100, after_ms=600):
    """KEY down, hold_ms of firmware time, KEY up, after_ms. Prints what the firmware saw at each step."""
    print(f"key: before   {ui_input(d)}")
    r.monitor(f"{key_cmd} {bit} 1")
    wait_firmware(d, hold_ms, deadline)
    print(f"key: held     {ui_input(d)}")
    r.monitor(f"{key_cmd} {bit} 0")
    wait_firmware(d, after_ms, deadline)
    print(f"key: released {ui_input(d)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--board", default=default_board("renode"), help="board to check (C-SKY uses QEMU, others use Renode)")
    ap.add_argument("--elf", help="default build/<board>/rfoundry_<board>.elf")
    ap.add_argument("--renode", default=default_renode())
    ap.add_argument("--section", default="core", help="view section to read (rfoundry_debug.VIEW_SECTIONS)")
    ap.add_argument("--speed-s", type=float, default=6.0, help="wall seconds to measure emulation speed over")
    ap.add_argument("--timeout", type=float, default=180.0, help="limit for the whole run, seconds")
    ap.add_argument("--log", help="Renode log file (default: a temporary file, kept and printed only on a failure)")
    ap.add_argument("--storage", default="dmr-group", help="flash image fixture (tools/emu/flashimg.py)")
    ap.add_argument("--radio-index", type=int, default=1, help="flash image radio index (tools/emu/flashimg.py)")
    ap.add_argument("--screen", metavar="OUT.png", help="save the settled screen as a PNG and print its FRAME line")
    ap.add_argument("--key", metavar="NAME", choices=sorted(KEY_BITS, key=lambda k: (KEY_BITS[k], k)),
                    help="press and release this conductor key; the FRAME crc must change")
    ap.add_argument("--scale", type=int, default=2, help="PNG pixels per panel pixel (1 to 8)")
    ap.add_argument("--settle-ms", type=int, default=2500,
                    help="firmware ms before the first screen (the boot splash drops keys for 1 s)")
    a = ap.parse_args()

    b = a.board
    elf = os.path.abspath(a.elf or os.path.join(ROOT, f"build/{b}/rfoundry_{b}.elf"))
    config = BOARDS.get(b, {})
    if config.get("engine") == "qemu":
        cmd = [sys.executable, os.path.join(HERE, "hrc7000_emu.py"), "--board", b,
               "--elf", elf, "--boot", "--lockstep", "--get-version", "--timeout", str(a.timeout)]
        if a.log:
            cmd += ["--log", a.log]
        if a.screen or a.key:
            cmd += ["--settle-ms", str(a.settle_ms), "--png-scale", str(a.scale)]
            cmd += ["--screen", os.path.abspath(a.screen)] if a.screen else []
            cmd += ["--key", a.key] if a.key else []
        return subprocess.run(cmd).returncode
    directory = os.path.join(ROOT, "boards", b, "emu")
    repl = os.path.join(directory, config.get("repl", "platform.repl"))
    resc = os.path.join(directory, config.get("resc", "boot.resc"))
    for path, what in ((a.renode, "Renode"), (elf, "ELF"), (repl, "platform file"), (resc, "Renode script")):
        if not os.path.exists(path):
            print(f"FAIL: no {what} at {path}")
            return 2
    key_cmd = None
    if a.key:
        if os.path.exists(KEYS_PY):
            key_cmd = "keys_key"
        elif config.get("key_fallback"):
            key_cmd = "emukey"
        else:
            print(f"FAIL: --key needs {KEYS_PY} for board {b} (no board fallback driver)")
            return 2

    work = tempfile.mkdtemp(prefix=f"emu_check_{b}_")
    terminal = os.path.join(work, "debuglink")
    script = f"$elf=@{elf}; $tcp=false; $repl=@{repl}; "
    template = os.path.join(directory, config.get("flash", "flash.repl"))
    if os.path.exists(template):
        import flashimg
        image = os.path.join(work, "flash.bin")
        flashimg.build_image(image, a.storage, a.radio_index)
        with open(template) as f:
            text = f.read().replace("{image}", image)
        with open(os.path.join(work, "flash.repl"), "w") as f:
            f.write(text)
        script += f"$flashrepl=@{work}/flash.repl; "
        print(f"flash: {image}, {a.storage}, radio {a.radio_index}")
    script += f"include @{resc}; "
    frame_log = os.path.join(work, "frames.txt")
    if a.screen or a.key:
        script += f"lcd FrameLog @{frame_log}; "
    if key_cmd == "keys_key":
        script += f'include @{KEYS_PY}; keys_layout "{b}"; keys_attach; '
    elif key_cmd == "emukey":
        driver = os.path.join(directory, config["key_fallback"])
        script += f"include @{driver}; "
    script += (f'emulation CreateUartPtyTerminal "debuglink" @{terminal}; '
               "connector Connect sysbus.hostport debuglink; start")
    if a.key:
        print(f"key: {a.key} is KEY bit {KEY_BITS[a.key]}, driver "
              f"{'peripherals/keys.py' if key_cmd == 'keys_key' else 'board fallback'}")

    log_path = a.log or os.path.join(work, "renode.log")
    t0 = time.monotonic()
    deadline = t0 + a.timeout
    r = Renode(a.renode, script, log_path, int(a.timeout) + 30)
    ok = False
    try:
        if not r.wait_terminal(terminal, deadline):
            raise Check(f"Renode never opened PTY {terminal}")
        t_port = time.monotonic() - t0
        with rd.DebugLink(terminal, timeout=3.0) as d:
            v = None
            while v is None and time.monotonic() < deadline:
                try:
                    v = d.version()
                except rd.DebugLinkError:
                    pass
            if v is None:
                raise Check("no GET_VERSION reply")
            t_reply = time.monotonic() - t0
            print(f"renode: PTY open after {t_port:.1f} s wall; first GET_VERSION reply after "
                  f"{t_reply:.1f} s wall, firmware uptime {v.get('uptime_ms')} ms")
            print("GET_VERSION:")
            for k, val in v.items():
                print(f"  {k}={val}")
            if not board_matches(b, v.get("board")):
                raise Check(f"GET_VERSION names board {v.get('board')!r}, not {b}")

            ms, nxt, kv = d.view_section(a.section)
            print(f"GET_VIEW section {a.section} (firmware ms {ms}, next {nxt}):")
            for k, val in kv.items():
                print(f"  {k}={val}")

            w1, u1 = time.monotonic(), uptime(d)
            time.sleep(a.speed_s)
            w2, u2 = time.monotonic(), uptime(d)
            speed = (u2 - u1) / 1000.0 / (w2 - w1)
            print(f"speed: {u2 - u1} ms of firmware time in {w2 - w1:.2f} s wall = {speed:.2f}x real time")

            if a.screen or a.key:
                screen = Screen(r, frame_log, a.scale, deadline)
                wait_settled(d, screen, a.settle_ms, 500, deadline)
                before_png = a.screen or os.path.join(work, "screen.png")
                before = screen.shot(before_png)
                print(f"screen: {before_png} at firmware {uptime(d)} ms, {len(screen.frames())} FRAME lines so far")
                print(before)
                if a.key:
                    press(r, key_cmd, d, KEY_BITS[a.key], deadline)
                    wait_settled(d, screen, 0, 500, deadline)
                    stem, ext = os.path.splitext(before_png)
                    after_png = f"{stem}-key-{a.key}{ext or '.png'}"
                    after = screen.shot(after_png)
                    print(f"screen after key {a.key}: {after_png}")
                    print(after)
                    if crc_of(before) == crc_of(after):
                        raise Check(f"key {a.key}: the screen did not change (crc {crc_of(before)})")
                    print(f"key {a.key}: screen changed, crc {crc_of(before)} -> {crc_of(after)}")
        r.monitor("emulation GetTimeSourceInfo")
        time.sleep(0.5)
        ok = True
    except Check as e:
        print(f"FAIL: {e}")
    finally:
        clean = r.stop()
        with open(log_path) as f:
            info = [ln.strip() for ln in f if ln.startswith(("Elapsed", "Quantum", "Advance immediately"))]
        for ln in info:
            print(f"renode: {ln}")
        left = subprocess.run(["pgrep", "-g", str(r.pgid)], capture_output=True, text=True).stdout.split()
        print(f"renode: stopped, processes left in its group: {len(left)}")
        ok = ok and clean and not left
        if ok and not a.log:
            for name in os.listdir(work):   # temporary files are kept only when something failed
                os.unlink(os.path.join(work, name))
            os.rmdir(work)
        else:
            print(f"renode: log {log_path}, files in {work}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
