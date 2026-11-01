# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Stage 1 emulation: a board's unchanged ELF answers GET_VERSION and a view read in Renode.

Opt-in: skips without Renode ($RENODE or ~/tools/renode/renode) or without the board ELF (make BOARD=<board>), and the debug link tests skip without pyserial.
Run: python3 -m pytest -q tools/emu/test_emu_check.py
"""
import importlib.util
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RENODE = os.environ.get("RENODE") or os.path.expanduser("~/tools/renode/renode")
sys.path.insert(0, HERE)
from board_config import BOARDS, default_board

BOARD = default_board("renode")
CONFIG = BOARDS[BOARD]
ELF = CONFIG["elf"]
QEMU_BOARDS = [board for board, config in BOARDS.items() if config["engine"] == "qemu"]
# emu_check.py talks to the emulator over a socket:// URL, which needs pyserial in this interpreter (sys.executable).
# uv run --with pytest alone does not have it: add --with pyserial
HAS_SERIAL = importlib.util.find_spec("serial") is not None
needs_serial = pytest.mark.skipif(not HAS_SERIAL, reason="no pyserial in this Python (uv run --with pyserial)")


@pytest.mark.skipif(not os.path.exists(RENODE), reason="needs Renode")
@pytest.mark.parametrize("board", [b for b, c in BOARDS.items() if c["engine"] == "renode"])
def test_standalone_boot_has_no_tcp_listener(board, tmp_path):
    import time
    from pathlib import Path
    import flashimg
    from tools.conductor.parts.emu import GuardedPopen, Monitor, check_emulator_listeners

    config = BOARDS[board]
    if not os.path.exists(config["elf"]):
        pytest.skip("needs firmware ELF")
    directory = Path(config["directory"])
    image = tmp_path / "flash.bin"
    flashimg.build_image(str(image), "dmr-group", 1)
    flash = tmp_path / "flash.repl"
    flash.write_text((directory / config["flash"]).read_text().replace("{image}", str(image)))
    process = GuardedPopen([RENODE, "--disable-xwt", "--console", "-P", "-1", "--plain", "--hide-log"],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=ROOT)
    monitor = None
    try:
        monitor = Monitor(process, time.monotonic() + 20)
        for command in (f"$elf=@{config['elf']}", f"$flashrepl=@{flash}", "$port=0",
                        f"include @{directory / config['resc']}"):
            reply = monitor.command(command)
            assert "error" not in reply.lower(), reply
        check_emulator_listeners(process.pid)
    finally:
        process.kill()
        process.wait(10)
        if monitor:
            monitor.close()


@pytest.mark.parametrize("board", QEMU_BOARDS)
@pytest.mark.parametrize("status", [0, 1, 2])
@pytest.mark.parametrize("overrides", [False, True])
def test_csky_dispatch(monkeypatch, tmp_path, board, status, overrides):
    import emu_check

    args = ["emu_check.py", "--board", board, "--renode", "/no/renode"]
    elf = os.path.join(ROOT, f"build/{board}/rfoundry_{board}.elf")
    timeout = "180.0"
    log_args = []
    if overrides:
        monkeypatch.chdir(tmp_path)
        args += ["--elf", "custom.elf", "--timeout", "12.5", "--log", "qemu.log"]
        elf, timeout, log_args = str(tmp_path / "custom.elf"), "12.5", ["--log", "qemu.log"]
    expected = [sys.executable, os.path.join(HERE, "hrc7000_emu.py"), "--board", board,
                "--elf", elf, "--boot", "--lockstep", "--get-version", "--timeout", timeout, *log_args]
    calls = []

    def run(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, status)

    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(emu_check.subprocess, "run", run)
    assert emu_check.main() == status
    assert calls == [expected]


@pytest.mark.parametrize("board", QEMU_BOARDS)
@pytest.mark.parametrize(("options", "forwarded"), [
    (["--screen", "home.png"], ["--screen", "HOME"]),
    (["--key", "ok"], ["--key", "ok"]),
    (["--screen", "home.png", "--key", "down"], ["--screen", "HOME", "--key", "down"])])
def test_csky_screen_and_key_forwarded(monkeypatch, tmp_path, board, options, forwarded):
    import emu_check

    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(sys, "argv", ["emu_check.py", "--board", board, *options])
    monkeypatch.setattr(emu_check.subprocess, "run",
                        lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(cmd, 2))
    assert emu_check.main() == 2
    tail = ["--settle-ms", "2500", "--png-scale", "2"] + [str(tmp_path / "home.png") if x == "HOME" else x
                                                         for x in forwarded]
    assert calls[0][-len(tail):] == tail


@pytest.mark.parametrize("board", QEMU_BOARDS)
@needs_serial
def test_csky_answers_debug_link(board):
    import boards
    reported = boards.board_t_names(os.path.join(ROOT, "boards", board))[0][0]
    import hrc7000_emu

    elf = os.path.join(ROOT, f"build/{board}/rfoundry_{board}.elf")
    qemu = os.environ.get("CSKY_QEMU", hrc7000_emu.DEFAULT_QEMU)
    for path in (qemu, hrc7000_emu.CROSS + "nm", elf):
        if not os.path.exists(path):
            pytest.skip(f"no {path}")
    r = subprocess.run([sys.executable, os.path.join(HERE, "emu_check.py"), "--board", board],
                       capture_output=True, text=True, timeout=210)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "GET_VERSION:" in r.stdout
    assert f"  board={reported}" in r.stdout.splitlines()


@pytest.mark.skipif(not os.path.exists(RENODE), reason=f"no Renode at {RENODE}")
@pytest.mark.skipif(not os.path.exists(ELF), reason=f"no {ELF} (make BOARD={BOARD})")
@needs_serial
def test_renode_answers_debug_link():
    r = subprocess.run([sys.executable, os.path.join(HERE, "emu_check.py"), "--section", "core", "--speed-s", "2",
                        "--timeout", "120"], capture_output=True, text=True, timeout=180)
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    import emu_check
    assert emu_check.board_matches(BOARD, next(line.split("=", 1)[1] for line in out.splitlines()
                                               if line.startswith("  board=")))
    assert "GET_VIEW section core" in out and "  section=core" in out
    assert "processes left in its group: 0" in out


@pytest.mark.skipif(not os.path.exists(RENODE), reason=f"no Renode at {RENODE}")
@pytest.mark.skipif(not os.path.exists(ELF), reason=f"no {ELF} (make BOARD={BOARD})")
@needs_serial
def test_renode_screen_and_key(tmp_path):
    png = tmp_path / "home.png"
    r = subprocess.run([sys.executable, os.path.join(HERE, "emu_check.py"), "--screen", str(png), "--key", "ok",
                        "--speed-s", "0"], capture_output=True, text=True, timeout=300)
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert "\nFRAME radio=1 " in out and "key ok: screen changed" in out and "ui.screen=hub" in out
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and (tmp_path / "home-key-ok.png").exists()


@pytest.mark.skipif(not os.path.exists(RENODE), reason=f"no Renode at {RENODE}")
def test_renode_exti0_from_port_c_only(tmp_path):
    """A falling edge on PC0 (the HR_C5000 timeslot line) sets EXTI0 pending while PE0 (an LED) is high."""
    script = tmp_path / "exti.resc"
    script.write_text(
        f"include @{HERE}/stm32_stubs.cs\ninclude @{HERE}/peripherals/lcd_st7735.cs\n"
        f"include @{HERE}/peripherals/c5000.cs\nmach create\nmachine LoadPlatformDescription @{CONFIG['directory']}/{CONFIG['repl']}\n"
        "sysbus WriteDoubleWord 0x40013C00 0x1\n"      # EXTI IMR: line 0
        "sysbus WriteDoubleWord 0x40013C0C 0x1\n"      # EXTI FTSR: falling edge on line 0
        "sysbus WriteDoubleWord 0x40021000 0x5\n"      # GPIOE MODER: PE0, PE1 outputs
        "sysbus WriteDoubleWord 0x40021018 0x1\n"      # GPIOE BSRR: PE0 high
        "sysbus.gpioPortC OnGPIO 0 true\nsysbus.gpioPortC OnGPIO 0 false\n"
        "sysbus ReadDoubleWord 0x40013C14\nquit\n")   # EXTI PR
    r = subprocess.run(["timeout", "-s", "KILL", "90", RENODE, "--disable-xwt", "--console", "--plain", "-e",
                        f"include @{script}"], capture_output=True, text=True, stdin=subprocess.DEVNULL, cwd=ROOT)
    values = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("0x")]
    assert values == ["0x00000001"], r.stdout
