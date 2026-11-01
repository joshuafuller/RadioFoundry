#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Test of the ST7735 LCD model (lcd_st7735.cs) in Renode, headless.

Pattern test (always): a platform with only the model. A script inside Renode plays the bus traffic of our own
driver, transcribed from drivers/display/st7735/st7735.c (type 3, the unlock-variant init) and
boards/md380/display_md380.c (panel ID read, bus_end's parking write), then a known picture. The test checks the
ID bytes, the FRAME lines (format of platform/host/vframe.c, crc = zlib CRC-32 of the expected panel bytes) and
the PNG pixels.

ELF test (--elf): the firmware image on the stock STM32F4 platform plus the model, run for --seconds of
virtual time; it checks that FRAME lines came out and writes the firmware's own screen as a PNG.

    python3 tools/emu/peripherals/lcd_test.py
    python3 tools/emu/peripherals/lcd_test.py --elf build/md380/rfoundry_md380.elf --out /tmp/lcd

Plain python3, no pytest: every Renode run has a timeout and ends with quit.
"""

import argparse
import os
import re
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL = HERE / "lcd_st7735.cs"
RENODE = Path(os.environ.get("RENODE", Path.home() / "tools/renode/renode"))
W, H = 160, 128
CMD, DATA = 0x60000000, 0x60040000
FRAME = re.compile(r"^FRAME radio=(\d+) boot=(\d+) id=(\d+) us=(\d+) crc=([0-9a-f]{8}) palette=(-|[0-9a-f]{64}) "
                   r"backlight=(\d+)$")

# ---- the bus traffic of our driver ----


class Bus:
    """Collects bus operations as lines for the replay script: c/d byte, r (read), cs/rst level."""

    def __init__(self):
        self.ops = []

    def cs(self, level):
        self.ops.append(f"cs {int(level)}")

    def rst(self, level):
        self.ops.append(f"rst {int(level)}")

    def cmd(self, c):
        self.ops.append(f"c {c}")

    def data(self, *bs):
        self.ops.extend(f"d {b}" for b in bs)

    def read(self):
        self.ops.append("r")

    # display_md380.c bus_begin / bus_end: select, ..., deselect, then park the lines with a 0x00 data write
    def begin(self):
        self.cs(0)

    def end(self):
        self.cs(1)
        self.data(0x00)

    def pixels(self, px):
        for v in px:
            self.data(v >> 8, v & 0xFF)  # high byte first (bus_pixels)

    def window(self, x0, y0, x1, y1):  # st7735.c set_window, x1 and y1 exclusive
        self.cmd(0x2A)
        self.data(x0 >> 8, x0 & 0xFF, (x1 - 1) >> 8, (x1 - 1) & 0xFF)
        self.cmd(0x2B)
        self.data(y0 >> 8, y0 & 0xFF, (y1 - 1) >> 8, (y1 - 1) & 0xFF)
        self.cmd(0x2C)


# st7735.c init_unlock_a and init_unlock_b, { command, count, parameters }; command 0 is a pause
UNLOCK_A = [(0xFE, []), (0xEF, []), (0xB4, [0x00]), (0xFF, [0x16])]
UNLOCK_B = [(0xA4, [0x70]), (0xE7, [0x94, 0x88]), (0xEA, [0x3A]), (0xED, [0x11]), (0xE4, [0xC5]), (0xE2, [0x80]),
            (0xA3, [0x12]), (0xE3, [0x07]), (0xE5, [0x10]), (0xF0, [0x00]), (0xF1, [0x55]), (0xF2, [0x05]),
            (0xF3, [0x53]), (0xF4, [0x00]), (0xF5, [0x00]), (0xF7, [0x27]), (0xF8, [0x22]), (0xF9, [0x77]),
            (0xFA, [0x35]), (0xFB, [0x00]), (0xFC, [0x00]), (0xFE, []), (0xEF, []), (0xE9, [0x00]), (0x00, [20])]


def run_table(b, table):
    for c, params in table:
        if c == 0x00:  # WAIT: the session ends while the panel settles
            b.end()
            b.begin()
            continue
        b.cmd(c)
        b.data(*params)


def driver_init(b):
    """display_md380.c init (ID read) and st7735_init for type 3, then the clear and DISPON."""
    b.rst(0)
    b.rst(1)
    for c, n in ((0x04, 3), (0xDA, 1), (0xDB, 1), (0xDC, 1)):  # read_reg: command, dummy, n bytes
        b.begin()
        b.cmd(c)
        for _ in range(n + 1):
            b.read()
        b.end()
    b.rst(1)
    b.rst(0)
    b.rst(1)
    b.begin()
    run_table(b, UNLOCK_A)
    b.cmd(0xFD)
    b.data(0x40)  # type 3
    run_table(b, UNLOCK_B)
    b.cmd(0x36)
    b.data(0xA0)  # madctl_by_type[3]
    b.cmd(0x3A)
    b.data(0x05)
    b.cmd(0x11)
    b.end()
    b.begin()  # st7735_fill_raw of the whole panel, black
    b.window(0, 0, W, H)
    b.pixels([0] * (W * H))
    b.end()
    b.begin()
    b.cmd(0x29)
    b.end()


BARS = [0xFFFF, 0xFFE0, 0x07FF, 0x07E0, 0xF81F, 0xF800, 0x001F, 0x0000]  # white yellow cyan green magenta red blue black


def expected_picture():
    pic = [[BARS[x * 8 // W] for x in range(W)] for y in range(H)]
    for y in range(4, 14):  # a 10 x 10 orange marker near the top left: shows orientation
        for x in range(2, 12):
            pic[y][x] = 0xFC00
    return pic


def picture(b):
    """Two flushes: the full color bars, then the marker through a small window (st7735_write_rows style)."""
    b.begin()
    b.window(0, 0, W, H)
    b.pixels([BARS[x * 8 // W] for y in range(H) for x in range(W)])
    b.end()
    b.begin()
    b.window(2, 4, 12, 14)
    b.pixels([0xFC00] * 100)
    b.end()
    # Not selected: a controller ignores these. Taken, they would be a pixel at the marker window's start (2, 4)
    b.data(0xAB, 0xCD)


def panel_bytes(pic):  # the vhost rgb565 profile: little endian, row after row
    return b"".join(struct.pack("<H", v) for row in pic for v in row)


def read_png(path):
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, idat, width, height = 8, b"", 0, 0
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        assert zlib.crc32(kind + body) == struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0], "chunk CRC"
        if kind == b"IHDR":
            width, height = struct.unpack(">II", body[:8])
            assert body[8:10] == b"\x08\x02", "RGB8 expected"
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = 1 + width * 3
    return width, height, [[tuple(raw[y * stride + 1 + x * 3:y * stride + 4 + x * 3]) for x in range(width)]
                           for y in range(height)]


def rgb8(v):
    r, g, b = v >> 11, (v >> 5) & 0x3F, v & 0x1F
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


# ---- Renode ----

REPLAY = '''python """
lcd = self.Machine["sysbus.lcd"]
bus = self.Machine.SystemBus
out = open(r"{reads}", "w")
for line in open(r"{ops}"):
    op, _, v = line.strip().partition(" ")
    if op == "c":
        bus.WriteByte({cmd}, int(v))
    elif op == "d":
        bus.WriteByte({data}, int(v))
    elif op == "r":
        out.write("%d\\n" % bus.ReadByte({data}))
    elif op == "cs":
        lcd.OnGPIO(0, v == "1")
    elif op == "rst":
        lcd.OnGPIO(1, v == "1")
out.close()
"""
'''


OWN_GROUPS = []


def renode(script, workdir, timeout):
    resc = Path(workdir) / "run.resc"
    resc.write_text(script + "quit\n")
    # Its own session, so the leak check at the end looks only at our Renode, not another run on the same machine
    proc = subprocess.Popen([str(RENODE), "--disable-xwt", "--console", "--plain", "-e", f"include @{resc}"],
                            cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    OWN_GROUPS.append(proc.pid)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, 9)
        out, err = proc.communicate()
    log = out + err
    (Path(workdir) / "renode.log").write_text(log)
    if "Could not compile" in log or "There was an error" in log or "rror executing" in log:
        sys.exit("renode error:\n" + log[-3000:])
    return log


def frames(path):
    lines = Path(path).read_text().splitlines() if Path(path).exists() else []
    parsed = []
    for line in lines:
        m = FRAME.match(line)
        assert m, f"bad FRAME line: {line!r}"
        parsed.append(m.groups())
    return lines, parsed


def pattern_test(out):
    b = Bus()
    driver_init(b)
    picture(b)
    ops = out / "ops.txt"
    ops.write_text("\n".join(b.ops) + "\n")
    reads, flog, png = out / "reads.txt", out / "frames.txt", out / "pattern.png"
    for f in (reads, flog, png):
        f.unlink(missing_ok=True)
    script = (f"include @{MODEL}\n"
              "mach create\n"
              'machine LoadPlatformDescriptionFromString "lcd: RadioFoundry.St7735Lcd @ sysbus 0x60000000"\n'
              f"lcd FrameLog @{flog}\n"
              "lcd BacklightAddress 0\n"
              "lcd Backlight 100\n"
              + REPLAY.format(reads=reads, ops=ops, cmd=CMD, data=DATA) +
              f"lcd SavePng @{png} 1\n")
    renode(script, out, 120)

    got = [int(v) for v in reads.read_text().split()]
    # 0x04: dummy, 00 91 02; 0xDA-0xDC: dummy and one each (hardware-md380.md 7, "One radio")
    assert got == [0, 0x00, 0x91, 0x02, 0, 0x00, 0, 0x91, 0, 0x02], f"ID reads {got}"

    lines, parsed = frames(flog)
    black = zlib.crc32(bytes(W * H * 2))
    bars = [[BARS[x * 8 // W] for x in range(W)] for _ in range(H)]
    crcs = [int(p[4], 16) for p in parsed]
    # The clear is one checkpoint (black: the display is still off). DISPON shows the cleared, black RAM: the
    # picture is unchanged, so no checkpoint. Then one checkpoint for each flush of the picture.
    want = [black, zlib.crc32(panel_bytes(bars)), zlib.crc32(panel_bytes(expected_picture()))]
    assert crcs == want, f"FRAME crcs {[hex(c) for c in crcs]} want {[hex(c) for c in want]}\n" + "\n".join(lines)
    assert [int(p[2]) for p in parsed] == [1, 2, 3], "ids count up from 1"
    assert all(p[0] == "1" and p[1] == "1" and p[5] == "-" and p[6] == "100" for p in parsed), "\n".join(lines)

    width, height, pix = read_png(png)
    assert (width, height) == (W, H)
    pic = expected_picture()
    bad = [(x, y) for y in range(H) for x in range(W) if pix[y][x] != rgb8(pic[y][x])]
    assert not bad, f"{len(bad)} PNG pixels differ, first {bad[:5]}"
    print(f"pattern: ok ({len(b.ops)} bus operations, {len(lines)} FRAME lines, PNG {png})")
    for line in lines:
        print("  " + line)


ELF_REPL = """using "platforms/cpus/stm32f4.repl"
ccm: Memory.MappedMemory @ sysbus 0x10000000
    size: 0x10000
bkpsram: Memory.MappedMemory @ sysbus 0x40024000
    size: 0x1000
dwt: Miscellaneous.DWT @ sysbus 0xE0001000
    frequency: 72000000
fsmcBank1: @none
lcd: RadioFoundry.St7735Lcd @ sysbus 0x60000000
gpioPortD:
    6 -> lcd@0
    13 -> lcd@1
"""


def elf_test(out, elf, seconds, stubs, stubs_repl):
    flog, png = out / "elf-frames.txt", out / "elf-boot.png"
    for f in (flog, png):
        f.unlink(missing_ok=True)
    repl = out / "lcd.repl"
    repl.write_text(ELF_REPL + (stubs_repl or ""))
    script = (f"include @{MODEL}\n" + (f"include @{stubs}\n" if stubs else "") +
              "emulation SetSeed 1\n"
              'mach create "lcd"\n'
              f"machine LoadPlatformDescription @{repl}\n"
              f"sysbus LoadELF @{elf}\n"
              "cpu VectorTableOffset 0x0800C000\n"
              "cpu PerformanceInMips 72\n"
              "sysbus.gpioPortE OnGPIO 11 true\n"
              "sysbus.gpioPortE OnGPIO 12 true\n"
              "sysbus.gpioPortD OnGPIO 10 true\n"
              f"lcd FrameLog @{flog}\n"
              "logLevel 3\n"
              f'emulation RunFor "{seconds}"\n'
              f"lcd SavePng @{png} 2\n"
              "lcd Madctl\nlcd Colmod\nlcd DisplayOn\nlcd PixelsWritten\nlcd PanelId\n")
    log = renode(script, out, 60 + 30 * seconds)
    lines, parsed = frames(flog)
    tail = "\n".join(log.splitlines()[-12:])
    assert lines, "no FRAME lines from the firmware\n" + tail
    print(f"elf: ok, {len(lines)} FRAME lines in {seconds} s virtual, PNG {png}")
    for line in lines[:3] + (["  ..."] if len(lines) > 6 else []) + lines[max(3, len(lines) - 3):]:
        print("  " + line)
    print(tail)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, help="keep outputs here (default: a temporary folder)")
    ap.add_argument("--elf", type=Path, help="also run the firmware image with the model attached")
    ap.add_argument("--seconds", type=int, default=3, help="virtual seconds for --elf (default 3)")
    ap.add_argument("--stubs", type=Path, help="extra C# peripherals to include for --elf (for example stage 1's)")
    ap.add_argument("--stubs-repl", help="repl lines that attach --stubs")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        out = (args.out or Path(tmp)).resolve()
        out.mkdir(parents=True, exist_ok=True)
        pattern_test(out)
        if args.elf:
            elf_test(out, args.elf.resolve(), args.seconds, args.stubs and args.stubs.resolve(),
                     args.stubs_repl and args.stubs_repl.encode().decode("unicode_escape"))
    left = [p for g in OWN_GROUPS
            for p in subprocess.run(["pgrep", "-g", str(g)], capture_output=True, text=True).stdout.split()]
    assert not left, f"renode still running: {left}"
    print("lcd_test: all checks passed")


if __name__ == "__main__":
    main()
