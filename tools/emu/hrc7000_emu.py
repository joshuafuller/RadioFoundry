#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Boot an unchanged hrc7000 (C-SKY CK803S) board ELF in the XuanTie QEMU fork and bridge its debug link.

Runs qemu-system-cskyv2 as a separate tool (it is GPL; nothing of it is copied here) with the ck803s CPU on a machine
whose plain RAM covers both the SRAM (0x00010000) and the XIP window (0x0300C000), stopped at reset with its private GDB Unix socket
open. tools/emu/hrc7000_rsp.py drives it over that socket:

- the host port, like tools/emu/hostport.py does for STM32 boards in Renode: a breakpoint on each of the five
  functions in hrc_host_uart_stream (platform/hrc7000/uart.c). At a stop the bridge does the function's job against a
  local TCP socket, puts the return value in r0 and jumps to r15 (the link register). The ELF is never patched; the
  five addresses are read out of the loaded table.
- --stub FUNC=VALUE: the same trick on any function, to step past hardware the machine does not have (a PLL lock wait,
  a busy-wait on a timer). Each one is printed, so the report shows what was skipped.
- one virtual clock (hrc7000_soc.VirtualClock) for all time: it moves only while the CPU runs, never past the next
  interrupt due, and the time functions, the RTOS tick and the 30 ms DMR slots all read it.
- --irq (the default with --boot on virt): the PIC and timers 1-6 (hrc7000_soc.py, from docs/spec/hardware-dm32.md
  3.4 and 4.1) see each write the firmware makes to their registers (write watchpoints), and the bridge gives the
  CPU their interrupts the way the CK803 takes one. The firmware's own hrc_irq_entry and vPortTickHandler run the tick.
- every --hang-s seconds with no hook, the PC is printed with its function name, so a hang names itself.
- the RF chip's 3-wire bus (drivers/rf/rfbus/rfbus.c) at the register access, with --boot: a stop at the entry of
  write_3w and read_3w keeps a register file (hrc7000_soc.RfBus3w) and answers "locked, quiet", so an access costs one
  stop instead of one per bit.
- --screen / --key: a DCS display controller (hrc7000_soc.DcsPanel) behind the firmware's ST7789 bus functions, read
  from the st7789_t the firmware passes to st7789_init. It keeps the frame memory and prints a FRAME line (the virtual
  radio's checkpoint, platform/host/vframe.c) for every window the firmware fills. With --boot the key matrix and PTT
  inputs read idle (hrc7000_soc.KeyMatrix, wiring from boards/<board>/pins.h and input_<board>.c), and --key holds one
  key for 100 ms of radio time.

    python3 tools/emu/hrc7000_emu.py --board <board> --boot --get-version   # boot, ask GET_VERSION over the bridge
    python3 tools/emu/hrc7000_emu.py --board <board> --boot --timeout 30    # run, then report the clock and the tick
    python3 tools/emu/hrc7000_emu.py --board <board> --boot --machine deimos   # the old way: the idle task's soft tick
    python3 tools/emu/hrc7000_emu.py --board <board> --boot --get-version --screen home.png --key ok   # screen and a key
    python3 tools/emu/hrc7000_emu.py --board <board> --boot --flash dmr-pair --modem --dmr-send call_alert:3100002
        # a codeplug on SPI0 (hrc7000_flash.py), the DMR modem (hrc7000_modem.py): a CSBK out as an air record

Exit 0 with --get-version only when the reply names the board. QEMU runs under timeout and is killed on the way out.
"""
import argparse
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from hrc7000_rsp import Rsp  # noqa: E402
from renode_guard import GuardedPopen  # noqa: E402
import hrc7000_panels  # noqa: E402
from board_config import BOARDS  # noqa: E402
from hrc7000_soc import (GPIO_DR, GPIO_EXT, PIC_BASE, PIC_MASK_1_OFF, PIC_MASK_OFF, SLOT_S, TIMER_BASE,  # noqa: E402
                         TIMER_STRIDE, VECTOR_OF_SOURCE, DcsPanel, gpio_bank, KeyMatrix, RfBus3w,
                         RFBUS_QUIET_READS, Soc, Timers, VirtualClock)

DEFAULT_QEMU = os.path.expanduser("~/tools/xuantie-qemu/src/build/qemu-system-cskyv2")
CROSS = os.environ.get("CSKY_CROSS", os.path.expanduser("~/toolchains/csky-elf/bin/csky-elf-"))
# hal_stream_t field order (hal/hal_stream.h): start, connected, read, wait_rx, write
STREAM_FIELDS = ("start", "connected", "read", "wait_rx", "write")
R0, LR = 0, 15
# Control registers in QEMU's csky g packet: the order of its target description (csky-abiv2-softmmu-core.xml, read
# with qXfer:features:read): pc, ar0..ar15, psr, vbr, epsr, fpsr, epc. The PC slot is found at reset (130 here), and
# the others follow it. Checked: VBR reads 0x00010000 (the firmware's `vectors`) after hrc_irq_init, and EPC, EPSR and
# PSR read back what a G packet writes
PSR_OFF, VBR_OFF, EPSR_OFF, EPC_OFF = 17, 18, 19, 21
PSR_S, PSR_EE, PSR_IE = 1 << 31, 1 << 8, 1 << 6     # supervisor, exceptions enabled, interrupts enabled
# What --boot steps past, each a function whose hardware this machine does not have, and its return value:
# hal_delay_us   busy-waits on timer 2, which never counts here (time.c); a delay has nothing to wait for
# hrc_ssi_xfer   the SPI controllers (ssi.c): each byte polls SSI_SR to its spin limit and fails; fail at once instead
# bus_pixels     a display driver's pixel stream, one SPI or bit-banged byte at a time: a frame took minutes
BOOT_STUBS = (("hal_delay_us", 0), ("hrc_ssi_xfer", 0), ("bus_pixels", 0))
# The conductor's key names (tools/conductor/parts/input.py KEYS) as the firmware's key codes (core/input/keys.h):
# ok is the green (menu) key, back the red one. ptt is the PTT pin, not a matrix key
CONDUCTOR_KEYS = {"ok": "KEY_GREEN", "select": "KEY_GREEN", "back": "KEY_RED", "up": "KEY_UP", "down": "KEY_DOWN",
                  **{str(d): f"KEY_{d}" for d in range(10)}}
SETTLE_QUIET_S = 0.5        # radio seconds with no new FRAME line before a screen counts as settled (as emu_check)
SETTLE_MAX_S = 10.0         # after settle_ms, take the screen anyway after this long and say so
KEY_HOLD_S, KEY_AFTER_S = 0.1, 0.6
# Backlight: PWM0 on PTA15 (platform/hrc7000/backlight.c, hrc_backlight): IO_DIPLEX0 bit 17 set = the pin is a GPIO
IO_DIPLEX0_ADDR, PWM0_PER, PWM0_FP = 0x11000034, 0x140C0008, 0x140C000C


def board_keypad(board):
    """The key matrix wiring of boards/<board> as a hrc7000_soc.KeyMatrix, or None when the board has none this model
    reads: rows from PIN_KEY_ROW0, columns from PIN_KEY_COL1 (KEY_ROWS and KEY_COLS consecutive pins, pins.h), PTT from
    PIN_PTT, encoder lines from PIN_ENC_A / PIN_ENC_B, and the [row][column] key table `map[]` from input_<board>.c."""
    d = os.path.join(ROOT, "boards", board)
    try:
        with open(os.path.join(d, "pins.h")) as f:
            pins = {k: int(v, 0) for k, v in re.findall(r"(?m)^#define\s+(\w+)\s+(0x[0-9A-Fa-f]+|\d+)\b", f.read())}
        with open(os.path.join(d, f"input_{board}.c")) as f:
            body = re.search(r"static const uint8_t map\[[^\]]*\]\s*=\s*\{(.*?)\};", f.read(), re.S)
    except OSError:
        return None
    need = ("PIN_KEY_ROW0", "PIN_KEY_COL1", "KEY_ROWS", "KEY_COLS")
    if body is None or not all(k in pins for k in need):
        return None
    nr, nc = pins["KEY_ROWS"], pins["KEY_COLS"]
    names = [t.strip() for t in body.group(1).split(",") if t.strip()]
    if len(names) != nr * nc:
        return None
    where = {n: divmod(i, nc) for i, n in enumerate(names)}
    keymap = {k: where[code] for k, code in CONDUCTOR_KEYS.items() if code in where}
    return KeyMatrix(range(pins["PIN_KEY_ROW0"], pins["PIN_KEY_ROW0"] + nr),
                     range(pins["PIN_KEY_COL1"], pins["PIN_KEY_COL1"] + nc), keymap, ptt=pins.get("PIN_PTT"),
                     pull_up=[pins[k] for k in ("PIN_ENC_A", "PIN_ENC_B") if k in pins])


def symbols(elf):
    out = subprocess.run([CROSS + "nm", elf], capture_output=True, text=True, check=True).stdout
    syms, by_addr = {}, []
    for line in out.splitlines():
        p = line.split()
        if len(p) == 3:
            a = int(p[0], 16)
            syms[p[2]] = a
            if p[1] in "tTwW":
                by_addr.append((a, p[2]))
    by_addr.sort()
    return syms, by_addr


def elf_bytes(elf, addr, size):
    """The bytes the ELF loads at addr (from its PT_LOAD program headers), or None."""
    with open(elf, "rb") as f:
        data = f.read()
    phoff, = struct.unpack_from("<I", data, 0x1C)
    phentsize, phnum = struct.unpack_from("<HH", data, 0x2A)
    for i in range(phnum):
        p_type, off, vaddr, _paddr, filesz = struct.unpack_from("<IIIII", data, phoff + i * phentsize)
        if p_type == 1 and vaddr <= addr and addr + size <= vaddr + filesz:
            return data[off + addr - vaddr:off + addr - vaddr + size]
    return None


# Where the debugger sees the guest's memory: at its own address (deimos), or through the CPU's kernel segment at
# 0x80000000 up (virt; found by stepping the reset code and searching for its bytes)
MEM_BASES = (0, 0x80000000, 0xA0000000)


def find_mem_base(r, elf, entry):
    want = elf_bytes(elf, entry, 16)
    for base in MEM_BASES:
        r.mem_base = base
        try:
            if r.read(entry, 16) == want:
                return base
        except RuntimeError:
            pass
    r.mem_base = 0
    return None


def name_of(by_addr, pc):
    best = None
    for a, n in by_addr:
        if a > pc:
            break
        best = (a, n)
    return f"{best[1]}+0x{pc - best[0]:x}" if best else "?"


class HostBuffer:
    """The in-process controller's host port bytes."""

    def __init__(self):
        self.conn = None
        self.rx = bytearray()
        self.lock = threading.Lock()
        self.data = threading.Event()
        self.unread = bytearray()

    def take(self, n):
        with self.lock:
            out, self.rx[:] = bytes(self.rx[:n]), self.rx[n:]
            if not self.rx:
                self.data.clear()
            return out

    def send(self, b):
        if self.conn:
            try:
                self.conn.sendall(b)
            except OSError:
                pass
        elif len(self.unread) < 4096:
            self.unread += b    # nobody listening (a fault trace before any client): kept for the report


class HostSocket(HostBuffer):
    """The standalone bridge's TCP side: one debug-link client at a time."""

    def __init__(self, port):
        super().__init__()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(1)
        self.closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closed:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.conn = c
            while True:
                try:
                    b = c.recv(4096)
                except OSError:
                    b = b""
                if not b:
                    break
                with self.lock:
                    self.rx += b
                self.data.set()
            self.conn = None

    def close(self):
        self.closed = True
        self.srv.close()


def main(argv=None, control=None):
    """control: a hrc7000_lockstep.Lockstep, to run as a world radio stepped by grants (tools/conductor/parts/emu.py)."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--board", required=True)
    ap.add_argument("--elf", help="default build/<board>/rfoundry_<board>.elf")
    ap.add_argument("--qemu", default=os.environ.get("CSKY_QEMU", DEFAULT_QEMU))
    ap.add_argument("--machine", default="virt",
                    help="QEMU machine: virt (RAM over 0..0x4FFFFFFF, so the firmware's peripheral writes are kept and "
                         "--irq works) or deimos (RAM over 0..0x07FFFFFF only; peripheral accesses are dropped)")
    ap.add_argument("--cpu", default="ck803s")
    ap.add_argument("--stub", action="append", default=[], help="FUNC=VALUE: return VALUE at once from FUNC")
    ap.add_argument("--get-version", action="store_true", help="ask GET_VERSION over the bridged host port")
    ap.add_argument("--timeout", type=float, default=60.0, help="limit for the whole run, seconds")
    ap.add_argument("--hang-s", type=float, default=5.0, help="interrupt and print the PC after this long")
    ap.add_argument("--max-stops", type=int, default=200000)
    ap.add_argument("--log", help="write QEMU's -d guest_errors log (one line per unmapped access) here and sum it up")
    ap.add_argument("--log-max-mb", type=int, default=200, help="stop the run when the -d log passes this size")
    ap.add_argument("--wallclock", action="store_true",
                    help="answer the time functions (platform/hrc7000/time.c) from the virtual clock: timer 2 is missing")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="radio seconds per host second the virtual clock aims for (it slows down when the bridge lags)")
    ap.add_argument("--apb-hz", type=float, default=84e6, help="timer 2's count rate (HRC_APB_HZ)")
    ap.add_argument("--boot", action="store_true",
                    help="--wallclock, the tick (--irq on virt, --soft-tick on deimos) and the stubs BOOT_STUBS: what "
                         "every hrc7000 board needs to boot here")
    ap.add_argument("--soft-tick", action="store_true",
                    help="give the RTOS its tick from the idle task (the old way, without the timer and PIC models)")
    ap.add_argument("--irq", action="store_true",
                    help="model the PIC and timers 1-6 (hrc7000_soc.py) and give their interrupts to the CPU, so the "
                         "firmware's own timer 1 interrupt is the tick. Needs a machine with RAM at the peripherals (virt)")
    ap.add_argument("--time-hooks", action="store_true",
                    help="with --irq, still answer hal_millis, hal_micros and hrc_micros at a breakpoint (one stop each) "
                         "instead of letting them read the timer 2 model")
    ap.add_argument("--slot-irq", action="store_true",
                    help="with --irq, raise the modem's 30 ms timeslot interrupts (sources 30, 31) on the same clock")
    ap.add_argument("--no-rfbus", action="store_true",
                    help="with --boot, leave the RF chip's 3-wire bus to run bit by bit (one stop per bit, as before)")
    ap.add_argument("--screen", metavar="OUT.png",
                    help="model the display: print a FRAME line per window the firmware fills, and once the screen is "
                         "settled (--settle-ms, then 0.5 s with no new FRAME line) save it as a PNG")
    ap.add_argument("--key", metavar="NAME", help="after the settled screen, hold this conductor key (ok, back, up, down, "
                    "0-9, ptt) for 100 ms of radio time; the FRAME crc must change")
    ap.add_argument("--settle-ms", type=int, default=2500, help="radio ms before the first screen")
    ap.add_argument("--png-scale", type=int, default=1, help="PNG pixels per panel pixel")
    ap.add_argument("--modem", action="store_true", help="with --irq, model the DMR modem at burst level "
                    "(hrc7000_modem.py): its slot and RF TX interrupts, and each TX burst out as an air record")
    ap.add_argument("--flash", metavar="FIXTURE", help="model the SPI0 data flash (hrc7000_flash.py) with this "
                    "flashimg.py fixture (blank, dmr-pair, dmr-group) or a 16 MB image file")
    ap.add_argument("--radio-index", type=int, default=1, help="the --flash fixture member")
    ap.add_argument("--dmr-send", metavar="WHAT:DST", help="with --modem, ask the radio for a CSBK over the debug "
                    "link once booted (call_alert:DST or radio_check:DST) and wait for its air record")
    ap.add_argument("--air-out", metavar="FILE", help="write each air record the modem sends here (JSON lines)")
    ap.add_argument("--air-in", metavar="FILE", help="with --modem, receive these air records (JSON lines, as "
                    "--air-out writes them), the first at --air-in-at radio seconds")
    ap.add_argument("--air-in-at", type=float, default=2.8)
    ap.add_argument("--lockstep", action="store_true", help="with --irq, radio time moves only by what the firmware "
                    "does (hrc7000_lockstep.py free mode): the screen, the key and the debug link replies land at the "
                    "same radio times in every run, whatever the host load")
    a = ap.parse_args(argv)
    if a.boot:
        a.wallclock = True
        if not a.soft_tick and not a.irq:
            a.irq = a.machine == "virt"
            a.soft_tick = not a.irq
    if a.irq and a.soft_tick:
        print("FAIL: --irq and --soft-tick are two ways to give the same tick; choose one")
        return 2
    if a.lockstep and a.irq and control is None:
        from hrc7000_lockstep import Lockstep
        control = Lockstep(free=True)
        a.scale, a.time_hooks = 0.0, True
    if a.irq and a.machine != "virt":
        print(f"FAIL: --irq needs the firmware's timer and PIC writes in RAM; machine {a.machine} drops them (use virt)")
        return 2

    elf = os.path.abspath(a.elf or os.path.join(ROOT, f"build/{a.board}/rfoundry_{a.board}.elf"))
    for path, what in ((a.qemu, "QEMU"), (elf, "ELF"), (CROSS + "nm", "csky-elf-nm")):
        if not os.path.exists(path):
            print(f"FAIL: no {what} at {path}")
            return 2
    syms, by_addr = symbols(elf)
    entry = syms.get("Reset_Handler")
    private = tempfile.TemporaryDirectory(prefix="rf-qemu-")
    gdb = os.path.join(private.name, "gdb.sock")
    log = a.log
    cmd = ["timeout", "-k", "2", str(int(a.timeout) + 10), a.qemu, "-M", a.machine, "-cpu", a.cpu,
           "-kernel", elf, "-nographic", "-monitor", "none", "-serial", "none", "-S",
           "-gdb", f"unix:{gdb},server=on,wait=off"]
    cmd += ["-d", "guest_errors,unimp", "-D", log] if log else []
    print("qemu:", " ".join(cmd[4:]))
    q = host = None
    t0 = time.monotonic()
    deadline = t0 + a.timeout
    r = None
    ok = False
    try:
        q = GuardedPopen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, umask=0o077)
        host = HostBuffer() if control else HostSocket(0)
        hport = host.srv.getsockname()[1] if isinstance(host, HostSocket) else None
        while time.monotonic() < deadline and q.poll() is None:
            try:
                r = Rsp(gdb, timeout=max(0.01, min(5.0, deadline - time.monotonic())))
                break
            except OSError:
                time.sleep(0.05)
        if r is None:
            print("FAIL: QEMU never opened its GDB endpoint")
            return 1
        print("qSupported:", r.cmd("qSupported:swbreak+").decode()[:120])
        regs = r.regs()
        print(f"registers in the g packet: {len(regs)}")
        # The PC's index in the g packet: QEMU stops at the ELF entry, so the slot holding Reset_Handler is the PC
        pc_idx = next((i for i, v in enumerate(regs) if v == entry), None)
        print(f"reset: entry Reset_Handler 0x{entry:08x}, PC slot {pc_idx}")
        if pc_idx is None:
            print("FAIL: no register holds the entry address; values:", " ".join(f"{v:x}" for v in regs[:40]))
            return 1
        PSR, VBR, EPSR, EPC = (pc_idx + o for o in (PSR_OFF, VBR_OFF, EPSR_OFF, EPC_OFF))

        base = find_mem_base(r, elf, entry)
        if base is None:
            print(f"FAIL: the debugger shows none of the ELF's bytes at 0x{entry:08x} (tried bases "
                  + ", ".join(f"0x{b:08x}" for b in MEM_BASES) + ")")
            return 1
        print(f"memory: the debugger sees guest address A at A + 0x{base:08x}")

        hooks = {}
        table = syms.get("hrc_host_uart_stream")
        if table:
            ptrs = [r.read32(table + 4 * i) for i in range(5)]
            for n, p in zip(STREAM_FIELDS, ptrs):
                hooks[p] = ("host", n)
            print("host port hooks:", ", ".join(f"{n}=0x{p:08x}" for n, p in zip(STREAM_FIELDS, ptrs)))
        stubs = list(a.stub)
        want_panel = bool(a.screen or a.key)
        other = hrc7000_panels.find_driver(syms)     # the ST7567 or ST7735 boards
        if want_panel and "st7789_init" not in syms and not other:
            print("FAIL: --screen / --key: no display model for this firmware's panel driver (needs st7789_init)")
            return 2
        if a.boot:
            stubs += [f"{fn}={v}" for fn, v in BOOT_STUBS if fn in syms and not any(x.startswith(fn + "=") for x in stubs)
                      and not (want_panel and fn == "bus_pixels")]
        for s in stubs:
            fn, _, val = s.partition("=")
            if fn not in syms:
                print(f"FAIL: no symbol {fn}")
                return 2
            hooks[syms[fn]] = ("stub", fn, int(val or "0", 0))
        if a.wallclock:
            # cycles_now is what every reader of the time base calls; the three readers are hooked too, so a stop
            # there names the caller that waits (and costs one stop instead of two)
            # With --irq the time functions are left to run: they read timer 2 (hrc_cycles), whose count the bridge
            # writes from the clock at every stop (one memory write in place of a stop per call). --time-hooks keeps
            # the hooks on hal_millis, hal_micros and hrc_micros, which name their callers
            for fn, scale in (("cycles_now", 1.0), ("hal_millis", 1e-3), ("hal_micros", 1e-6), ("hrc_micros", 1e-6)):
                if fn in syms and not (a.irq and (fn == "cycles_now" or not a.time_hooks)):
                    hooks[syms[fn]] = ("time", fn, scale)
        rf = None
        if a.boot and not a.no_rfbus and "write_3w" in syms and "read_3w" in syms:
            rf = RfBus3w({**RFBUS_QUIET_READS, **dict(BOARDS[a.board].get("rfbus_id_reads", []))})
            hooks[syms["write_3w"]] = ("rfbus", "write_3w")
            hooks[syms["read_3w"]] = ("rfbus", "read_3w")
        flash = None
        if a.flash:
            from hrc7000_flash import FlashHook, SpiNand, SpiNor, codeplug_image, nand_pages
            if BOARDS[a.board]["flash"]["kind"] == "nand":
                flash = FlashHook(SpiNand(nand_pages(a.flash, a.radio_index, a.board)))
            else:
                img = open(a.flash, "rb").read() if os.path.exists(a.flash) else codeplug_image(a.flash, a.radio_index,
                                                                                                a.board)
                flash = FlashHook(SpiNor(img))
            hooks[syms["hrc_ssi_select"]] = ("flash", "select")
            hooks[syms["hrc_ssi_xfer"]] = ("flash", "xfer")
        panel = None
        if want_panel:
            # the bus functions are read from the st7789_t the firmware passes (r0) when st7789_init is reached
            hooks[syms["st7789_init" if "st7789_init" in syms else other[0]]] = ("panel", "init")
        keypad = (board_keypad(a.board) or hrc7000_panels.board_keypad(a.board)) if a.boot else None
        if a.key:
            if keypad is None:
                print(f"FAIL: --key: no key matrix model for board {a.board} (boards/{a.board}/pins.h, input_{a.board}.c)")
                return 2
            if a.key not in keypad.names():
                print(f"FAIL: --key {a.key}: not one of {', '.join(keypad.names())}")
                return 2
        read_fn = getattr(keypad, "read_fn", None)
        if a.key and read_fn in syms:      # the board reads its inputs through one function: answer at its entry
            hooks[syms[read_fn]] = ("keyread", "read")
        ext_now = {}

        def write_ext(levels):
            for bank, v in levels.items():
                if ext_now.get(bank) != v:
                    r.write(bank + GPIO_EXT, struct.pack("<I", v))
                    ext_now[bank] = v

        if keypad:
            write_ext(keypad.idle_ext())
            print("inputs idle: " + ", ".join(f"GPIO_EXT 0x{b + GPIO_EXT:08x} = 0x{v:08x}" for b, v in ext_now.items())
                  + f" (key matrix rows {keypad.rows[0]:#x}.., columns {keypad.cols[0]:#x}.., PTT {keypad.ptt:#x})")
        for addr in hooks:
            r.break_at(addr)

        counts = {}
        callers = {}    # who called a time or stub hook, a map of where the boot spends its stops
        version = {}
        asked = False

        link_lock = threading.Lock()   # one debug link client at a time (HostSocket takes one)

        def ask_version():
            import rfoundry_debug as rd
            if control and control.free:      # in-process, at this stop (hrc7000_lockstep.py free mode)
                control.request(rd.VERSION, b"", lambda st, p: version.update(
                    rd.parse_kv(p.decode("ascii", "replace")) if st == 0 else {"error": f"status {st}"}))
                return
            with link_lock:
                try:
                    with rd.DebugLink(f"socket://127.0.0.1:{hport}", timeout=5.0) as d:
                        version.update(d.version())
                except Exception as e:  # noqa: BLE001 - printed as the result
                    version["error"] = repr(e)

        probes = []     # (label, thread, result): the firmware's own view of the UI and keys at each screen

        class Answer:
            """A probe answered in-process (free lockstep): alive until its reply came."""
            def __init__(self, res):
                self.res = res

            def is_alive(self):
                return "text" not in self.res

            def join(self, timeout=None):
                pass

        def probe(label):
            res = {}
            if control and control.free:
                import rfoundry_debug as rd

                def got(st, p):
                    kv = rd.parse_kv(p[5:].decode("ascii", "replace")) if st == 0 else {}
                    res["text"] = f"ui.screen={kv.get('ui.screen')} input.raw_keys={kv.get('input.raw_keys')}"
                control.request(rd.GET_VIEW, bytes((rd.VIEW_SECTIONS.index("link"),)), got)
                probes.append((label, Answer(res), res))
                return

            def run():
                import rfoundry_debug as rd
                with link_lock:
                    try:
                        with rd.DebugLink(f"socket://127.0.0.1:{hport}", timeout=5.0) as d:
                            kv = d.view_section("link")[2]
                            res["text"] = f"ui.screen={kv.get('ui.screen')} input.raw_keys={kv.get('input.raw_keys')}"
                    except Exception as e:  # noqa: BLE001 - printed as the result
                        res["text"] = f"no view: {e!r}"
            t = threading.Thread(target=run, daemon=True)
            t.start()
            probes.append((label, t, res))

        # The one clock (hrc7000_soc.VirtualClock). Every time hook answers from it, the RTOS tick is given on it and
        # the 30 ms slots count on it, so they agree even when the bridge is slower than the host clock
        clock = VirtualClock(a.scale)
        modem = dmr = None
        if a.modem and a.irq:
            from hrc7000_modem import WATCHES, MODEM_BASE, Modem, ModemSoc
            modem = Modem(r, clock)
            if a.air_in:
                from hrc7000_modem import read_records
                modem.feed(read_records(a.air_in), a.air_in_at)
            soc = ModemSoc(clock, modem)
            for off in WATCHES:
                r.watch_writes(MODEM_BASE + off, 4)
        else:
            soc = Soc(clock) if a.irq else None

        # The software tick (--soft-tick, the old way). When the idle task asks the time (vApplicationIdleHook calls
        # hal_millis), the bridge makes the idle task call the kernel the way the tick interrupt would:
        # xTaskIncrementTick once per millisecond of radio time gone by (at most 50 at a time), then vPortYield
        # (freertos/portasm.S) if a task woke. Each call returns to the entry of hal_millis, which has a breakpoint
        # already; a stop there with r15 == hal_millis is such a return. The idle task's real return address is kept
        # per task (pxCurrentTCB) until it is done. The clock may run at most 2 ms ahead of the ticks given.
        HM, INC, YIELD = syms["hal_millis"], syms["xTaskIncrementTick"], syms["vPortYield"]
        idle_lo = syms.get("vApplicationIdleHook", 0)
        tcb_ptr = syms["pxCurrentTCB"]
        tick = {"done": 0, "woke": 0}
        pending = {}

        def soft_tick(g, lr):
            tcb = r.read32(tcb_ptr)
            st = pending.get(tcb)
            if lr == HM and st:
                if st["phase"] == "inc":
                    tick["done"] += 1
                    st["yield"] |= g[R0] != 0
                    st["left"] -= 1
                    if st["left"] > 0:
                        g[pc_idx] = INC
                        return True
                    if st["yield"]:
                        tick["woke"] += 1
                        st["phase"] = "yield"
                        g[pc_idx] = YIELD
                        return True
                # done: hal_millis answers as usual, to the idle hook
                del pending[tcb]
                g[R0], g[LR], g[pc_idx] = clock.ms(), st["lr"], st["lr"]
                return True
            if not (idle_lo <= lr < idle_lo + 0x100):
                return False
            due = clock.ms() - tick["done"]
            if due <= 0:
                return False
            tick.setdefault("first", clock.now - due / 1000)
            pending[tcb] = {"lr": lr, "phase": "inc", "left": min(due, 50), "yield": False}
            g[LR], g[pc_idx] = HM, INC
            return True

        # The interrupt path (--irq). The CPU takes an interrupt the way the CK803 does it in hardware: EPC = PC,
        # EPSR = PSR, PSR.VEC = vector, PSR.IE = PSR.EE = 0, PC = the word at VBR + 4 * vector. The bridge does exactly
        # that in the register file, then the firmware's own entry (hrc_irq_entry in freertos/portasm.S) saves the
        # task, acks the PIC, calls hrc_irq_table[source] (vPortTickHandler for timer 1) and returns with rte. Only
        # when PSR.IE is set and PSR.VEC is 0: interrupts on and not already in one (the firmware never nests)
        injected = {}
        handlers = {}
        sleeping = {}   # task (TCB) -> return address, for a host port wait_rx turned into vTaskDelay
        DELAY = syms.get("vTaskDelay")
        proof = {}
        TICK_HANDLER = syms.get("vPortTickHandler")
        IRQ_ENTRY = syms.get("hrc_irq_entry", 0)

        def inject(g):
            psr = g[PSR]
            if not psr & PSR_IE or (psr >> 16) & 0xFF:
                return False
            src = soc.pic.take()
            if src is None:
                return False
            vec = src + VECTOR_OF_SOURCE
            key = (g[VBR], vec)
            if key not in handlers:
                handlers[key] = r.read32(g[VBR] + 4 * vec)
            g[EPC], g[EPSR] = g[pc_idx], psr
            g[PSR] = (psr & ~(0xFF << 16) & ~(PSR_IE | PSR_EE)) | (vec << 16) | PSR_S
            g[pc_idx] = handlers[key]
            injected[src] = injected.get(src, 0) + 1
            if src == 1 and TICK_HANDLER and "armed" not in proof:
                proof["armed"] = clock.now
                r.break_at(TICK_HANDLER)
            return True

        tmr2 = {"c": None}

        def resume(g):
            """Before the CPU runs on: timer 2's count from the clock (CURRENTVALUE counts down, hrc_cycles reads
            ~CURRENTVALUE), then the registers."""
            if soc:
                c = ~clock.cycles(a.apb_hz) & 0xFFFFFFFF
                if c != tmr2["c"]:
                    r.write(TIMER_BASE + TIMER_STRIDE + 4, struct.pack("<I", c))
                    tmr2["c"] = c
            r.put_regs(g)

        def program_soc():
            soc.program_timers(r.read(TIMER_BASE, Timers.span()))
            soc.pic.program(r.read32(PIC_BASE + PIC_MASK_OFF), r.read32(PIC_BASE + PIC_MASK_1_OFF))

        if a.irq:
            # the firmware's writes to the timer and PIC mask registers stop the CPU, so the models see each one
            r.watch_writes(TIMER_BASE, Timers.span())
            r.watch_writes(PIC_BASE + PIC_MASK_OFF, 4)
            r.watch_writes(PIC_BASE + PIC_MASK_1_OFF, 4)
            soc.slots.enabled = a.slot_irq

        frames = []         # (radio time, FRAME line)
        shot = {"phase": "settle" if want_panel else "done", "shots": [], "scans_low": 0}

        def backlight():
            ch = other[2] if other else 0       # the backlight PWM channel: PTA15 + ch (backlight.c)
            if r.read32(IO_DIPLEX0_ADDR) & (1 << (17 + ch)):
                return 100 if r.read32(0x14020000 + GPIO_DR) & (1 << (15 + ch)) else 0
            per = r.read32(PWM0_PER + 0x20 * ch)
            return min(100, round(r.read32(PWM0_FP + 0x20 * ch) * 100 / per)) if per else 0

        def take_shot(path, label):
            line = frames[-1][1] if frames else None
            if path:
                with open(path, "wb") as f:
                    f.write(panel.png(a.png_scale))
            shot["shots"].append((label, path, line, panel.crc() if panel else None))
            print(f"[{clock.host_elapsed():6.1f} s] screen {label}: radio {clock.now:.3f} s, {len(frames)} FRAME lines"
                  + (f", saved {path}" if path else ""))
            probe(label)

        def screen_step():
            """The screen and key steps, all on radio time (clock.now)."""
            ph, now = shot["phase"], clock.now
            last = frames[-1][0] if frames else None
            if other and panel is not None and panel.touched is not None:
                last = max(last or 0.0, panel.touched)     # a window still streaming is not a settled screen
            quiet = last is not None and now - last >= SETTLE_QUIET_S
            if ph in ("settle", "after") and panel is not None:
                start = max(a.settle_ms / 1000, BOARDS[a.board].get("settle_min_s", 0)) if ph == "settle" else shot["t"] + KEY_HOLD_S + KEY_AFTER_S
                if now < start or not (quiet or now > start + SETTLE_MAX_S):
                    return
                if not quiet:
                    print(f"screen: not settled {SETTLE_MAX_S} s after {start:.1f} s (FRAME lines keep coming); taken anyway")
                if ph == "settle":
                    take_shot(a.screen, "home")
                    shot["phase"] = "press" if a.key else "done"
                else:
                    stem, ext = os.path.splitext(a.screen) if a.screen else (None, None)
                    take_shot(f"{stem}-key-{a.key}{ext or '.png'}" if a.screen else None, f"after key {a.key}")
                    shot["phase"] = "done"
            elif ph == "press" and not any(t.is_alive() for _, t, _ in probes):
                keypad.held.add(a.key)
                shot["t"], shot["phase"] = now, "held"
                print(f"[{clock.host_elapsed():6.1f} s] key {a.key} down at radio {now:.3f} s")
            elif ph == "held" and now >= shot["t"] + KEY_HOLD_S:
                keypad.held.discard(a.key)
                write_ext(keypad.idle_ext())
                shot["phase"] = "after"
                print(f"[{clock.host_elapsed():6.1f} s] key {a.key} up at radio {now:.3f} s; {shot['scans_low']} key "
                      f"scan steps saw it")

        def finished():
            return ((not a.get_version or version) and shot["phase"] == "done" and (not dmr or "done" in dmr)
                    and not any(t.is_alive() for _, t, _ in probes))

        stops = kicks = watches = 0
        last_real = time.monotonic()
        if control:
            control.attach(r, clock, soc, modem, host, q, syms)
        while time.monotonic() < deadline and stops < a.max_stops:
            if control and not control.gate():
                break
            if want_panel:
                screen_step()
            if (a.get_version or want_panel or dmr) and finished():
                break
            if log and stops % 1000 == 0 and os.path.exists(log) and os.path.getsize(log) > a.log_max_mb << 20:
                print(f"stop: the -d log passed {a.log_max_mb} MB")
                break
            wait = a.hang_s
            if control:
                wait = control.wait
            elif soc:
                due = soc.next_due()
                if due is not None:
                    wait = min(wait, max(2e-4, (due - clock.now) / a.scale))
            clock.resume()
            reply = r.cont(timeout=wait)
            kicked = reply is None
            if kicked:
                # nothing stopped the CPU before the next interrupt is due: stop it to give the interrupt
                reply = r.interrupt()
                kicks += 1
            clock.pause()
            stops += 1
            if not reply.startswith(b"T") and not reply.startswith(b"S"):
                print("stop:", reply[:80])
                break
            g = r.regs()
            pc = g[pc_idx]
            h = hooks.get(pc)
            # a stubbed delay takes its own length of radio time (r0 = microseconds)
            extra = g[R0] / 1e6 if h and h[0] == "stub" and h[1] == "hal_delay_us" else 0.0
            if control:
                extra = control.extra(h, g[LR], kicked, extra)
            if soc:
                if b"watch:" in reply:
                    watches += 1
                    m = re.search(rb"watch:([0-9a-fA-F]+)", reply)
                    waddr = int(m.group(1), 16) if m else None
                    if modem and waddr is not None and waddr >> 24 == MODEM_BASE >> 24:
                        modem.on_watch(waddr)
                    else:
                        program_soc()
                soc.step(extra)
            elif a.soft_tick:
                clock.advance((tick["done"] + 2) / 1000, extra)
            else:
                clock.advance(None, extra)
            now = time.monotonic()
            if h is None and (kicked or b"watch:" in reply):
                if now - last_real > a.hang_s:
                    last_real = now
                    print(f"[{clock.host_elapsed():6.1f} s] no hook for {a.hang_s} s; PC 0x{pc:08x} "
                          f"{name_of(by_addr, pc)} LR 0x{g[LR]:08x} {name_of(by_addr, g[LR])}")
                if soc:
                    inject(g)
                    resume(g)
                continue
            last_real = now
            if h is None and pc == TICK_HANDLER and "seen" not in proof:
                # the proof that the firmware's own tick interrupt runs: vPortTickHandler, called from hrc_irq_entry
                proof["seen"] = (g[LR], name_of(by_addr, g[LR]), (g[PSR] >> 16) & 0xFF, clock.now)
                r.unbreak_at(TICK_HANDLER)
                continue
            if h is None:
                print(f"stop at 0x{pc:08x} {name_of(by_addr, pc)}: {reply[:40]!r}")
                break
            key = h[1]
            if counts.get(key, 0) == 0:
                print(f"[{clock.host_elapsed():6.1f} s] first call: {h[0]} {key}")
            counts[key] = counts.get(key, 0) + 1
            lr = g[LR]
            if h[0] == "stub":
                who = key + "<-" + name_of(by_addr, lr).split("+")[0]
                callers[who] = callers.get(who, 0) + 1
                if keypad and keypad.held and key == "hal_delay_us":
                    # the key scan drives a row, then waits (hal_delay_us), then reads the columns: give the columns
                    # the levels the held key makes with the rows as they are now
                    rows_dr = r.read32(keypad.row_bank() + GPIO_DR)
                    levels = keypad.ext(rows_dr)
                    if levels != keypad.idle_ext():
                        shot["scans_low"] += 1
                    write_ext(levels)
                g[R0], g[pc_idx] = h[2], lr
            elif h[0] == "rfbus":
                if key == "write_3w":
                    rf.write(g[0], g[1], g[2])
                else:
                    g[R0] = rf.read(g[0], g[1])
                g[pc_idx] = lr
            elif h[0] == "flash":
                if key == "select":
                    flash.select(g)
                else:
                    g[R0] = flash.xfer(r, g)
                g[pc_idx] = lr
            elif h[0] == "keyread":
                levels = keypad.ext(r.read32(keypad.row_bank() + GPIO_DR) if keypad.held else 0)
                g[R0] = sum(1 << i for i, pin in enumerate(keypad.cols) if not levels[gpio_bank(pin)] >> (pin & 31) & 1)
                g[pc_idx] = lr
            elif h[0] == "panel":
                if other and panel is not None:
                    panel.touched = clock.now
                if key == "init" and "st7789_init" not in syms:
                    panel, found = other[1](r, g[0])
                    for addr, nm in found.items():
                        hooks[addr] = ("panel", nm)
                        r.break_at(addr)
                    del hooks[pc]
                    r.unbreak_at(pc)     # the init function itself runs on
                    print(f"panel: {panel.w}x{panel.h} ({type(panel).__name__}) through "
                          + ", ".join(f"{nm}=0x{a:08x} {name_of(by_addr, a)}" for a, nm in found.items()))
                elif key == "end":
                    g[pc_idx] = lr
                    if panel.end():
                        line = panel.frame_line(1, 1, len(frames) + 1, clock.us(), backlight())
                        frames.append((clock.now, line))
                        print(line)
                elif key == "init":
                    # r0 = the st7789_t: bus table pointer, then width and height (drivers/display/st7789/st7789.h)
                    pbus = r.read32(g[0])
                    w, hh = struct.unpack("<HH", r.read(g[0] + 4, 4))
                    begin, end, cmd, data, pixels = struct.unpack("<5I", r.read(pbus, 20))
                    panel = DcsPanel(w, hh)
                    for addr, nm in ((cmd, "cmd"), (data, "data"), (pixels, "pixels")):
                        hooks[addr] = ("panel", nm)
                        r.break_at(addr)
                    del hooks[pc]
                    r.unbreak_at(pc)     # st7789_init itself runs on
                    print(f"panel: {w}x{hh} through cmd=0x{cmd:08x} {name_of(by_addr, cmd)}, data=0x{data:08x}, "
                          f"pixels=0x{pixels:08x} {name_of(by_addr, pixels)}")
                elif key == "cmd":
                    panel.cmd(g[0] & 0xFF)
                    g[pc_idx] = lr
                elif key == "data":
                    if g[1]:
                        panel.data(r.read(g[0], g[1]))
                    g[pc_idx] = lr
                else:
                    n = g[1]
                    done = n and panel.pixels(struct.unpack("<%dH" % n, r.read(g[0], 2 * n)))
                    g[pc_idx] = lr
                    if done:
                        line = panel.frame_line(1, 1, len(frames) + 1, clock.us(), backlight())
                        frames.append((clock.now, line))
                        print(line)
            elif h[0] == "time":
                if not (a.soft_tick and h[1] == "hal_millis" and soft_tick(g, lr)):
                    who = h[1] + "<-" + name_of(by_addr, lr).split("+")[0]
                    callers[who] = callers.get(who, 0) + 1
                    c = clock.cycles(a.apb_hz) if h[2] == 1.0 else int(clock.now / h[2])
                    g[R0], g[1], g[pc_idx] = c & 0xFFFFFFFF, c >> 32, lr
            elif key in ("start", "connected"):
                g[R0], g[pc_idx] = 1, lr
                if a.get_version and not asked:
                    asked = True
                    if control and control.free:
                        ask_version()
                    else:
                        threading.Thread(target=ask_version, daemon=True).start()
                if modem and (a.dmr_send or a.air_in) and dmr is None:
                    from hrc7000_modem import send_driver
                    dmr = {}
                    threading.Thread(target=send_driver, daemon=True, args=(
                        f"socket://127.0.0.1:{hport}", link_lock, clock, modem, a.dmr_send,
                        a.settle_ms / 1000, dmr)).start()
            elif key == "wait_rx":
                tcb = r.read32(tcb_ptr) if a.irq else None
                if a.irq and lr == pc and tcb in sleeping:
                    # back from the vTaskDelay below: answer the wait, to the real caller
                    g[R0] = 1 if host.data.is_set() else 0
                    g[LR] = g[pc_idx] = sleeping.pop(tcb)
                elif a.irq and not host.data.is_set() and DELAY:
                    # No UART model, so the receive interrupt never gives the semaphore. With a real tick the task can
                    # sleep as the firmware's wait_rx does (xSemaphoreTake with the timeout in r0, ms): make it call
                    # vTaskDelay(r0 ticks) and return here, so the other tasks and the idle task run meanwhile
                    sleeping[tcb] = lr
                    g[R0], g[LR], g[pc_idx] = max(g[R0], 1), pc, DELAY
                else:
                    # without the tick nothing would wake a sleeping task: hold the CPU until bytes come (0.5 s at most)
                    g[R0], g[pc_idx] = (1 if host.data.wait(0 if a.irq else 0.5) else 0), lr
            elif key == "read":
                buf, size = g[0], g[1]
                b = host.take(size)
                if b:
                    r.write(buf, b)
                g[R0], g[pc_idx] = len(b), lr
            elif key == "write":
                buf, n = g[0], g[1]
                host.send(r.read(buf, n) if n else b"")
                g[R0], g[pc_idx] = n, lr
            if soc:
                inject(g)
            resume(g)
            if version and not want_panel:
                break
        el = clock.host_elapsed()
        print(f"clock: {clock.now:.3f} s of radio time in {el:.2f} s of host time (scale {clock.now / el:.3f}; the CPU "
              f"ran {clock.ran:.2f} s of it, asked scale {a.scale}); {stops} stops ({stops / el:.0f} a second), "
              f"{kicks} of them to give an interrupt")
        print(f"slots: {soc.slots.slot(clock.now) if soc else int(clock.now / SLOT_S)} timeslots of 30 ms"
              + (f", {soc.slots.count} raised" if soc and soc.slots.enabled else ""))
        if "xTickCount" in syms:
            kt = r.read32(syms["xTickCount"])
            # the tick starts with the scheduler, so the ratio counts from timer 1's start (from the first tick
            # given, without --irq)
            t1 = soc.timers.t[0].started if soc else tick.get("first")
            since = clock.now - t1 if t1 is not None else 0.0
            print(f"kernel: xTickCount {kt} in {since * 1000:.0f} ms of radio time since the tick started: "
                  f"{kt / max(since * 1000, 1):.3f} ticks per ms (hal_millis {clock.ms()}); {kt / el:.0f} ticks a host second")
        if host.unread:
            print("host port output with no client:", bytes(host.unread[:600]).decode(errors="replace"))
        if "hrc_fault_info" in syms:
            vec, epc, epsr = struct.unpack("<III", r.read(syms["hrc_fault_info"], 12))
            if vec or epc:
                print(f"FAULT: CPU exception vector {vec} at 0x{epc:08x} {name_of(by_addr, epc)}, EPSR 0x{epsr:08x}")
        if soc:
            print("interrupts given (PIC source: count):", ", ".join(f"{s}: {n}" for s, n in sorted(injected.items()))
                  or "none", f"; {watches} register writes seen")
            for tm in soc.timers.t:
                if tm.ctrl or tm.load:
                    print(f"  timer {tm.n}: LOADCOUNT {tm.load} CONTROLREG 0x{tm.ctrl:x} expiries {tm.expiries} "
                          f"lost {tm.overruns}")
            print(f"  PIC mask 0x{soc.pic.mask:016x}, waiting 0x{soc.pic.pending:016x}")
            if "seen" in proof:
                lr, who, vec, t = proof["seen"]
                print(f"firmware timer interrupt: vPortTickHandler reached at {t:.3f} s, called from {who} "
                      f"(0x{lr:08x}), PSR.VEC {vec}")
            else:
                print("firmware timer interrupt: not seen")
        if a.soft_tick:
            print(f"soft tick: {tick['done']} ticks given, {tick['woke']} woke a task")
        if callers:
            top = sorted(callers.items(), key=lambda kv: -kv[1])[:8]
            print("callers:", ", ".join(f"{k}={v}" for k, v in top))
        print("hook calls:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none")
        if rf:
            print(f"rf bus: {rf.writes} register writes, {rf.reads} reads answered at the access ({len(rf.regs)} "
                  "registers written)")
        if flash:
            print(flash.report())
        if modem:
            from hrc7000_modem import decode_line
            for line in modem.report():
                print(line)
            out = open(a.air_out, "w") if a.air_out else None
            for start, wire, b in modem.sent:
                rec, text = decode_line(start, wire)
                print(f"{text} (armed at radio {b['armed_at']:.4f} s, SEND_TYPE 0x{b['send_type']:x})")
                if out:
                    out.write(json.dumps({"t_us": rec.t_us, "kind": rec.kind, "cc": rec.cc, "ts": rec.ts,
                                          "wire": wire.hex()}) + "\n")
            if out:
                out.close()
            if dmr is not None:
                for k in ("view", "asked_at", "diag", "after", "error"):
                    if k in dmr:
                        print(f"dmr-send {k}: {dmr[k]}")
                if a.dmr_send and not modem.sent:
                    print("FAIL: --dmr-send: the modem sent no burst")
        screen_ok = not (a.dmr_send and modem and not modem.sent)
        if want_panel:
            for label, t, res in probes:
                t.join(5.0)
            texts = {label: res.get("text", "no view: timeout") for label, t, res in probes}
            if panel is None:
                print("FAIL: the firmware never reached st7789_init")
                screen_ok = False
            elif shot["phase"] != "done":
                print(f"FAIL: screen step '{shot['phase']}' not done when the run ended ({len(frames)} FRAME lines)")
                screen_ok = False
            for label, path, line, crc in shot["shots"]:
                print(f"screen {label}: {path or '(not saved)'}; firmware view {texts.get(label)}")
                print(line or "FAIL: no FRAME line")
                screen_ok = screen_ok and line is not None
            if a.key and len(shot["shots"]) == 2 and screen_ok:
                c1, c2 = shot["shots"][0][3], shot["shots"][1][3]
                if c1 == c2:
                    print(f"FAIL: key {a.key}: the screen did not change (crc {c1:08x})")
                    screen_ok = False
                else:
                    print(f"key {a.key}: screen changed, crc {c1:08x} -> {c2:08x}")
        if a.get_version:
            if not version:
                print("FAIL: no GET_VERSION reply" + ("" if asked else " (the host port was never started)"))
            elif "error" in version:
                print("FAIL: GET_VERSION:", version["error"])
            else:
                print("GET_VERSION:")
                for k, v in version.items():
                    print(f"  {k}={v}")
                want = re.sub(r"[^a-z0-9]", "", a.board.lower())
                ok = want in re.sub(r"[^a-z0-9]", "", str(version.get("board", "")).lower())
        else:
            ok = True
        return 0 if ok and screen_ok else 1
    finally:
        if control:
            control.done()
        if r:
            r.close()
        if isinstance(host, HostSocket):
            host.close()
        if q is not None:
            try:
                os.killpg(q.pid, 9)
            except ProcessLookupError:
                pass
            q.wait()
            out = q.stdout.read().decode(errors="replace").strip()
            q.stdout.close()
            if out:
                print("qemu output:", out[-800:])
        private.cleanup()
        # The log holds one line per access to an address the machine does not map (the SoC's peripherals): count
        # them per 64 KB block, which is one peripheral each (platform/hrc7000/platform.h)
        n, blocks, other = 0, {}, []
        if log:
            with open(log, errors="replace") as f:
                for line in f:
                    n += 1
                    m = re.match(r"Invalid (read|write) at addr 0x([0-9A-Fa-f]+)", line)
                    if m:
                        k = (int(m.group(2), 16) & ~0xFFFF, m.group(1))
                        blocks[k] = blocks.get(k, 0) + 1
                    elif len(other) < 8:
                        other.append(line.rstrip())
            print(f"qemu -d log {log}: {n} lines; unmapped accesses per 64 KB block:")
            for (blk, rw), c in sorted(blocks.items()):
                print(f"  0x{blk:08x} {rw:5s} {c}")
            for line in other:
                print("  other: " + line)


if __name__ == "__main__":
    sys.exit(main())
