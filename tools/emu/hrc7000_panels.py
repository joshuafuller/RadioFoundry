# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""ST7567 and ST7735 screens and board-described keys for tools/emu/hrc7000_emu.py.

The panels draw through a bus table of small functions (boards/<board>/display_<board>.c: bus_cmd, bus_data, and
bus_end or bus_pixels). The bridge stops at those functions and takes the byte values from the registers, so the
bit-banged or SPI wire is never run. That is why a frame is fast.

Clean room: written from this repo's own drivers and specs and the public ST7567 and ST7735 command sets only.
"""
import os
import re
import struct

from board_config import BOARDS

from hrc7000_soc import DcsPanel, KeyMatrix

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

# RGB565 of the two pixel states in the PNG: dark ink on a pale LCD
INK, PAPER = 0x1082, 0xCF3A


class MonoPanel(DcsPanel):
    """A page-addressed monochrome LCD (ST7567 / UC1701 command set) with its display RAM.

    Commands the model follows: 0xB0 + n page, 0x10 + n column high nibble, 0x00 + n column low nibble, 0xAE and 0xAF
    off and on, 0xA6 and 0xA7 normal and inverse, 0xA0 and 0xA1 segment direction, 0xC0 and 0xC8 common direction,
    and 0x81 followed by one more byte (contrast, read and dropped). The column address advances by one for each data
    byte. The firmware's own picture is what the user sees, as with DcsPanel and MADCTL: the segment and common
    directions are recorded but not applied. The one thing the direction changes is the first visible column: the
    controller has 132 columns, and with the segments reversed (0xA1) the visible ones start at 4
    (hardware-dm1801pro.md 7), with them normal at 0.

    The checkpoint (`end()` returning True) is due when the bus session ends after data bytes were written, which is
    one flush band: st7567_write_rows sends its pages between begin and end.

    The FRAME crc is CRC-32 over the packed 1 bit per pixel frame buffer the firmware draws (rows of width / 8 bytes,
    leftmost pixel in bit 7, 1 = on), the format of `gfx` for a 1 bpp display.

    Example: B0 (page 0), 04 and 10 (column 4), data 01 sets the pixel at x=0, y=0 after 0xA1."""

    COLUMNS = 132

    def __init__(self, width=128, height=64):
        super().__init__(width, height)
        self.mem = [[0] * self.COLUMNS for _ in range(height // 8)]
        self.page, self.col = 0, 0
        self.seg_reverse = False
        self.com_reverse = False
        self.contrast = None
        self.param_next = False
        self.dirty = False
        self.gram = [PAPER] * (width * height)

    def cmd(self, c):
        c &= 0xFF
        if self.param_next:
            self.param_next, self.contrast = False, c
        elif c == 0x81:
            self.param_next = True
        elif 0xB0 <= c <= 0xB7:
            self.page = c & 7
        elif c & 0xF0 == 0x10:
            self.col = (self.col & 0x0F) | (c & 0x0F) << 4
        elif c & 0xF0 == 0x00:
            self.col = (self.col & 0xF0) | (c & 0x0F)
        elif c in (0xA0, 0xA1):
            self.seg_reverse = c == 0xA1
        elif c in (0xC0, 0xC8):
            self.com_reverse = c == 0xC8
        elif c in (0xAE, 0xAF):
            self.on = c == 0xAF
        elif c in (0xA6, 0xA7):
            self.inverted = c == 0xA7
        elif c == 0xE2:                      # reset
            self.page = self.col = 0
        self.cmd_byte = c

    def data(self, b):
        first = 4 if self.seg_reverse else 0
        for v in b:
            if self.col < self.COLUMNS:
                self.mem[self.page][self.col] = v
                x = self.col - first
                if 0 <= x < self.w:
                    for bit in range(8):
                        self.gram[(self.page * 8 + bit) * self.w + x] = INK if v >> bit & 1 else PAPER
                self.col += 1
                self.dirty = True

    def end(self):
        """The bus session ended: True when it wrote anything (a checkpoint is due)."""
        done, self.dirty = self.dirty, False
        return done

    def panel_bytes(self):
        out = bytearray(self.h * self.w // 8)
        for y in range(self.h):
            for x in range(self.w):
                if self.gram[y * self.w + x] == INK:
                    out[y * (self.w // 8) + (x >> 3)] |= 0x80 >> (x & 7)
        return bytes(out)


def _touchable(panel):
    """`touched` is the radio time of the last bus access, set by hrc7000_emu.py: a screen is not settled while a
    window is still streaming, even if no window has completed yet."""
    panel.touched = None
    return panel


def _attach_mono(r, r0):
    """st7567_init(const st7567_t *p): p->bus is the first field. The bus table is begin, end, cmd, data, reset,
    delay_ms (drivers/display/st7567/st7567.h)."""
    pbus = r.read32(r0)
    begin, end, cmd, data = struct.unpack("<4I", r.read(pbus, 16))
    return _touchable(MonoPanel()), {cmd: "cmd", data: "data", end: "end"}


def _attach_tft(r, r0):
    """st7735_init(const st7735_t *p): p->bus first, then type (u8), width and height (u16 at +6 and +8)
    (drivers/display/st7735/st7735.h). The bus table is begin, end, cmd, data, pixels, reset, delay_ms."""
    pbus = r.read32(r0)
    w, h = struct.unpack("<HH", r.read(r0 + 6, 4))
    begin, end, cmd, data, pixels = struct.unpack("<5I", r.read(pbus, 20))
    return _touchable(DcsPanel(w, h)), {cmd: "cmd", data: "data", pixels: "pixels"}


# init symbol -> (attach function, PWM channel of the backlight). The bridge handles ST7789 itself.
DRIVERS = {"st7567_init": (_attach_mono, 0), "st7735_init": (_attach_tft, 1)}


def find_driver(syms):
    """(init symbol, attach function, backlight PWM channel) for the display driver this firmware links, or None."""
    for sym, (fn, ch) in DRIVERS.items():
        if sym in syms:
            return sym, fn, ch
    return None


# --- Keys ---------------------------------------------------------------------------------------------------------

def board_keypad(board):
    """A KeyMatrix from the board's pin names and firmware key map, or None without a description."""
    spec = BOARDS.get(board, {}).get("keypad")
    if spec is None:
        return None
    d = os.path.join(ROOT, "boards", board)
    try:
        with open(os.path.join(d, "pins.h")) as f:
            pins = {k: int(v, 0) for k, v in re.findall(r"(?m)^#define\s+(\w+)\s+(0x[0-9A-Fa-f]+|\d+)\b", f.read())}
        with open(os.path.join(d, f"input_{board}.c")) as f:
            body = re.search(r"static const uint8_t map\[[^\]]*\]\s*=\s*\{(.*?)\};", f.read(), re.S)
    except OSError:
        return None
    if body is None:
        return None
    ns, ni = pins[spec["strobe_count"]], pins[spec["input_count"]]
    rows = [pins[name] for name in spec["strobes"]]
    rows += [rows[-1] + i + 1 for i in range(ns - len(rows))]
    cols = [pins[spec["first_input"]] + i for i in range(ni)]
    codes = [t.strip() for t in body.group(1).split(",") if t.strip()]
    if len(codes) != ns * ni:
        return None
    where = {n: divmod(i, ni) for i, n in enumerate(codes)}
    wanted = {**spec["names"], **{str(n): f"KEY_{n}" for n in range(10)}}
    keymap = {k: where[code] for k, code in wanted.items() if code in where}
    km = KeyMatrix(rows, cols, keymap, ptt=pins[spec["ptt"]], pull_up=[pins[p] for p in spec["pull_up"]])
    km.read_fn = spec.get("read_fn")
    return km
