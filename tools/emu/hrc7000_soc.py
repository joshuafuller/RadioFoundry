# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The HR_C7000 blocks the emulator does not have, as plain Python models for tools/emu/hrc7000_emu.py.

Written from docs/spec/hardware-dm32.md (3.4 timers, 4.1 PIC, 4.2 vectors) and platform/hrc7000/platform.h. No
emulator code is in here: the bridge reads the registers the firmware writes (on QEMU's virt machine they are plain
RAM) and hands them to these models, and the models say which interrupt is due and when.

- VirtualClock: the one clock every part of the emulated radio reads. It runs at `scale` times the host clock, but
  never past the next interrupt that is due and not yet taken (`limit`). So the RTOS tick, the 30 ms DMR slots and
  hal_millis() all count the same time, even when the bridge is too slow to keep up with the host clock.
- Timers: timers 1 to 6 (DesignWare style, 0x14 bytes each, counting at APB 84 MHz).
- Pic: the 64-source interrupt controller at 0x17000000: mask bits and pending requests.
- SlotClock: the modem's 30 ms timeslot clock (docs/spec/modem-hrc7000.md section 3), for the modem model to come.
- RfBus3w: the RF chip's registers behind the 3-wire bus (drivers/rf/rfbus/rfbus.c), one stop per access.
- DcsPanel: a DCS-class display controller (CASET, RASET, RAMWR) with its frame memory, FRAME lines and PNGs.
- KeyMatrix: the GPIO input levels of a key matrix and a PTT pin, idle or with keys held.
"""
import struct
import time
import zlib

APB_HZ = 84_000_000             # timers count at APB = CPU / 2 (platform/hrc7000/clock_hz.h)
TIMER_BASE = 0x14000000
TIMER_STRIDE = 0x14
TIMERS = 6
CTRL_EN, CTRL_PERIODIC, CTRL_INT_MASK = 0x01, 0x02, 0x04
PIC_BASE = 0x17000000
PIC_MASK_OFF, PIC_MASK_1_OFF = 0x08, 0x68
PIC_SOURCES = 64
VECTOR_OF_SOURCE = 32           # vector = PIC source + 32 (hardware-dm32.md 4.2)
SLOT_S = 0.030                  # one DMR timeslot (modem-hrc7000.md 3)


class VirtualClock:
    """Seconds of radio time. It moves only while the CPU runs: the bridge calls resume() before it lets the CPU go
    and pause() at the stop, and advance() adds the run time gone by times `scale`, plus `extra` (a stubbed delay's
    own length), capped at `limit`. The time the bridge spends at a stop is not radio time, so a stop costs the
    firmware nothing.

    Example: scale 1.0, the CPU ran 3 ms of host time since the last stop, and the next tick is due 1 ms from now.
    The clock moves 1 ms, not 3: the tick is taken at exactly its time, and the other 2 ms are dropped. The radio
    then runs slower than the host, but every part of it agrees on the time."""

    def __init__(self, scale=1.0, host=time.monotonic):
        self.scale = scale
        self.host = host
        self.t0 = host()
        self.mark = None
        self.run = 0.0          # host seconds the CPU ran since the last advance
        self.ran = 0.0          # and in all
        self.now = 0.0
        self.dropped = 0.0      # radio seconds the cap threw away

    def resume(self):
        self.mark = self.host()

    def pause(self):
        if self.mark is not None:
            d = self.host() - self.mark
            self.run += d
            self.ran += d
            self.mark = None

    def advance(self, limit=None, extra=0.0):
        want = self.now + self.run * self.scale + extra
        self.run = 0.0
        if limit is not None and want > limit:
            self.dropped += want - max(limit, self.now)
            want = max(limit, self.now)
        self.now = want
        return self.now

    def host_elapsed(self):
        return self.host() - self.t0

    def ms(self):
        return int(self.now * 1000)

    def us(self):
        return int(self.now * 1e6)

    def cycles(self, hz=APB_HZ):
        return int(self.now * hz)


class Timer:
    """One timer as the firmware programs it (platform/hrc7000/time.c): LOADCOUNT, CONTROLREG; EOI and INTSTATUS
    are read side effects the bridge does not see, so a request is cleared when its interrupt is taken."""

    def __init__(self, n):
        self.n = n
        self.load = 0
        self.ctrl = 0
        self.next = None        # radio time of the next expiry, None when stopped
        self.started = None     # radio time it was first started
        self.expiries = 0
        self.overruns = 0       # expiries while the last request was still waiting: lost, as on the chip

    @property
    def source(self):
        return self.n           # IRQ_TIMER1 = 1 .. IRQ_TIMER6 = 6 (platform.h)

    def period(self):
        if self.ctrl & CTRL_PERIODIC:
            return max(self.load, 1) / APB_HZ
        return (1 << 32) / APB_HZ   # free-running mode: reloads 0xFFFFFFFF after the first expiry

    def program(self, load, ctrl, now):
        """New register values from the firmware. Starting (EN 0 -> 1) or a new LOADCOUNT while running restarts
        the count from LOADCOUNT."""
        was_on = bool(self.ctrl & CTRL_EN)
        on = bool(ctrl & CTRL_EN)
        restart = on and (not was_on or load != self.load)
        self.load, self.ctrl = load, ctrl
        if not on:
            self.next = None
        elif restart:
            self.next = now + max(load, 1) / APB_HZ
            if self.started is None:
                self.started = now

    def interrupts(self):
        return bool(self.ctrl & CTRL_EN) and not self.ctrl & CTRL_INT_MASK


class Timers:
    def __init__(self, count=TIMERS):
        self.t = [Timer(n) for n in range(1, count + 1)]

    @staticmethod
    def span():
        return TIMER_STRIDE * TIMERS

    def program_block(self, raw, now):
        """raw: the bytes of the timer block from TIMER_BASE (span() bytes)."""
        for tm in self.t:
            off = TIMER_STRIDE * (tm.n - 1)
            if off + 12 > len(raw):
                break
            load, _value, ctrl = struct.unpack_from("<III", raw, off)
            tm.program(load, ctrl, now)

    def running(self):
        return [tm for tm in self.t if tm.next is not None]


class Pic:
    """Mask bits as the firmware writes them (1 = masked; reset all masked) and the requests the models raise."""

    def __init__(self):
        self.mask = (1 << PIC_SOURCES) - 1
        self.pending = 0
        self.taken = {}

    def program(self, mask_lo, mask_hi):
        self.mask = (mask_lo & 0xFFFFFFFF) | (mask_hi & 0xFFFFFFFF) << 32

    def masked(self, source):
        return bool(self.mask >> source & 1)

    def request(self, source):
        """Returns False when that source already had a request waiting (the new one is lost)."""
        bit = 1 << source
        had = bool(self.pending & bit)
        self.pending |= bit
        return not had

    def clear(self, source):
        self.pending &= ~(1 << source)

    def next_source(self):
        """The lowest unmasked source with a request (the firmware leaves every priority at its reset value)."""
        ready = self.pending & ~self.mask
        if not ready:
            return None
        return (ready & -ready).bit_length() - 1

    def take(self):
        s = self.next_source()
        if s is not None:
            self.clear(s)
            self.taken[s] = self.taken.get(s, 0) + 1
        return s


class SlotClock:
    """The 30 ms timeslot clock: slot k starts at radio time k * 30 ms. It reads the same VirtualClock, so the slots
    keep step with the RTOS tick. `sources` are the PIC sources the modem raises at each slot boundary (30 TX slot,
    31 RX slot); the bridge only raises them once a modem model exists (--slot-irq)."""

    def __init__(self, sources=(30, 31)):
        self.sources = sources
        self.enabled = False
        self.next = SLOT_S
        self.count = 0

    def slot(self, now):
        return int(now / SLOT_S)


class Soc:
    """Timers + PIC + slot clock on one VirtualClock. The bridge calls step() at every stop."""

    def __init__(self, clock, max_hold=2000):
        self.clock = clock
        self.timers = Timers()
        self.pic = Pic()
        self.slots = SlotClock()
        self.max_hold = max_hold    # stops the clock may wait on an interrupt the CPU will not take (interrupts off)
        self.held = 0
        self.lost = 0

    def program_timers(self, raw):
        """The timer block's bytes as the firmware left them. A timer stopped or masked loses its waiting request:
        on the chip the firmware's EOI read clears it (hrc_timer_stop in platform/hrc7000/time.c), so a PA one-shot
        stopped from a task must not fire later."""
        self.timers.program_block(raw, self.clock.now)
        for tm in self.timers.t:
            if not tm.interrupts():
                self.pic.clear(tm.source)

    def _events(self):
        """(time, source, owner) of every upcoming expiry that raises an interrupt."""
        ev = [(tm.next, tm.source, tm) for tm in self.timers.t if tm.next is not None and tm.interrupts()]
        if self.slots.enabled:
            ev += [(self.slots.next, s, self.slots) for s in self.slots.sources]
        return ev

    def limit(self):
        """(time, held): the radio time the clock may not pass now, and whether a waiting request sets it.

        The next expiry whose interrupt the PIC lets through holds the clock: it is taken at exactly its time. A masked
        source does not hold it (nothing would ever take it). A source whose last request still waits (the CPU has
        interrupts off) holds the clock just short of its next expiry, so no tick is lost, for max_hold stops at most;
        after that the clock goes on and the expiry counts as lost, as on the chip (a busy wait with interrupts off
        must still see time pass)."""
        lim, held = None, False
        for t, s, _ in self._events():
            if self.pic.masked(s):
                continue
            if self.pic.pending >> s & 1:
                if self.held >= self.max_hold:
                    continue
                t -= 1e-9
                if lim is None or t < lim:
                    lim, held = t, True
                continue
            if lim is None or t < lim:
                lim, held = t, False
        return lim, held

    def step(self, extra=0.0):
        """Advance the clock (by the CPU's run time, plus `extra` radio seconds) and raise what fell due. Returns
        the radio time."""
        lim, held = self.limit()
        now = self.clock.advance(lim, extra)
        self.held = self.held + 1 if held and now >= lim else 0
        for tm in self.timers.t:
            while tm.next is not None and tm.next <= now:
                tm.expiries += 1
                if tm.interrupts() and not self.pic.request(tm.source):
                    tm.overruns += 1
                    self.lost += 1
                tm.next += tm.period()
        while self.slots.enabled and self.slots.next <= now:
            self.slots.count += 1
            for s in self.slots.sources:
                self.pic.request(s)
            self.slots.next += SLOT_S
        return now

    def next_due(self):
        """Radio time of the next event that could interrupt the CPU (for the bridge's wake-up), or None."""
        ev = [t for t, s, _ in self._events() if not self.pic.masked(s)]
        return min(ev) if ev else None


# --- The RF chip's 3-wire register bus (drivers/rf/rfbus/rfbus.c), modeled at the register access ---------------------

# What a quiet, locked FD6818 answers (docs/spec/hardware-dm32.md 8.4): RSSI 0x67 = (dBm + 160) * 2, so -120 dBm
# reads 80; noise 0x65 low byte 55 (no signal reads 43-63); CSS status 0x0C 0 (no tone found)
RFBUS_QUIET_READS = {0x67: 80, 0x65: 55, 0x0C: 0}


class RfBus3w:
    """The register file behind one or more 3-wire buses. The bridge stops at the entry of write_3w and read_3w
    (rfbus.c) and answers here, so one register access costs one stop instead of one stop per bit (each bit calls
    hal_delay_us). A read returns the quiet value above, else the last value written, else 0.

    Example: write_3w(bus, 0x30, 0x1D38) then read_3w(bus, 0x30) gives 0x1D38; read_3w(bus, 0x67) gives 80."""

    def __init__(self, reads=RFBUS_QUIET_READS):
        self.fixed = dict(reads)
        self.regs = {}          # (bus address, register) -> last value written
        self.writes = 0
        self.reads = 0

    def write(self, bus, reg, val):
        self.writes += 1
        self.regs[(bus, reg & 0x7F)] = val & 0xFFFF

    def read(self, bus, reg):
        self.reads += 1
        reg &= 0x7F
        if reg in self.fixed:
            return self.fixed[reg]
        return self.regs.get((bus, reg), 0)


# --- A display controller of the MIPI DCS command set (ST7789 / ST7735 class) ----------------------------------------

DCS_CASET, DCS_RASET, DCS_RAMWR, DCS_MADCTL = 0x2A, 0x2B, 0x2C, 0x36
DCS_DISPOFF, DCS_DISPON, DCS_INVOFF, DCS_INVON = 0x28, 0x29, 0x20, 0x21


class DcsPanel:
    """The panel's frame memory as the firmware's window commands see it (drivers/display/st7789/st7789.c), from the
    public DCS command set: CASET 2Ah and RASET 2Bh set the window (start and end, both inclusive, high byte first),
    RAMWR 2Ch starts the pixel stream, which fills the window row by row and wraps at its end. One RGB565 pixel per
    address. MADCTL is recorded but not applied: the firmware sets it so its own window coordinates are the picture
    the user sees (MADCTL 0xC0 turns a panel mounted upside down), so the frame memory is kept in those coordinates.

    A checkpoint is due each time a window has been filled to its last pixel: the driver sends one window per flush
    band (st7789_write_rows_indexed), so that is one checkpoint per band, as the Renode display model
    (tools/emu/peripherals) takes one per session.

    Example: CASET 0,0,0,239; RASET 0,10,0,11; RAMWR; 480 pixels fill rows 10 and 11, and the 480th returns True."""

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.gram = [0] * (width * height)
        self.cmd_byte = None
        self.params = bytearray()
        self.x0, self.x1, self.y0, self.y1 = 0, width - 1, 0, height - 1
        self.x, self.y = 0, 0
        self.madctl = 0
        self.on = False
        self.inverted = False
        self.written = 0        # pixels since RAMWR

    def cmd(self, c):
        self.cmd_byte, self.params = c & 0xFF, bytearray()
        if c == DCS_RAMWR:
            self.x, self.y, self.written = self.x0, self.y0, 0
        elif c in (DCS_DISPON, DCS_DISPOFF):
            self.on = c == DCS_DISPON
        elif c in (DCS_INVON, DCS_INVOFF):
            self.inverted = c == DCS_INVON

    def data(self, b):
        self.params += b
        p = self.params
        if self.cmd_byte in (DCS_CASET, DCS_RASET) and len(p) >= 4:
            lo, hi = p[0] << 8 | p[1], p[2] << 8 | p[3]
            if self.cmd_byte == DCS_CASET:
                self.x0, self.x1 = lo, hi
            else:
                self.y0, self.y1 = lo, hi
        elif self.cmd_byte == DCS_MADCTL and p:
            self.madctl = p[0]

    def area(self):
        return max(0, self.x1 - self.x0 + 1) * max(0, self.y1 - self.y0 + 1)

    def pixels(self, px):
        """RGB565 values in stream order. True when this call wrote the window's last pixel (a checkpoint is due)."""
        if self.cmd_byte != DCS_RAMWR:
            return False
        done = False
        area = self.area()
        for v in px:
            if self.x < self.w and self.y < self.h:
                self.gram[self.y * self.w + self.x] = v & 0xFFFF
            self.written += 1
            self.x += 1
            if self.x > self.x1:
                self.x = self.x0
                self.y += 1
                if self.y > self.y1:
                    self.y = self.y0
            if area and self.written % area == 0:
                done = True
        return done

    def panel_bytes(self):
        """What the FRAME crc covers: every pixel little endian, row after row (as the Renode display model and vhost)."""
        return struct.pack("<%dH" % len(self.gram), *self.gram)

    def crc(self):
        return zlib.crc32(self.panel_bytes()) & 0xFFFFFFFF

    def frame_line(self, radio, boot, frame_id, us, backlight):
        """One FRAME line, field for field platform/host/vframe.c. palette is '-': the bus carries RGB565."""
        return (f"FRAME radio={radio} boot={boot} id={frame_id} us={us} crc={self.crc():08x} palette=- "
                f"backlight={backlight}")

    def png(self, scale=1):
        """The picture as PNG bytes (zlib and struct only, no imaging library), each pixel scale x scale."""
        scale = max(1, int(scale))
        rows = []
        for y in range(self.h):
            line = bytearray()
            for v in self.gram[y * self.w:(y + 1) * self.w]:
                r, g, b = (v >> 11) & 0x1F, (v >> 5) & 0x3F, v & 0x1F
                line += bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))) * scale
            rows += [b"\x00" + bytes(line)] * scale

        def chunk(kind, body):
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

        ihdr = struct.pack(">IIBBBBB", self.w * scale, self.h * scale, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
                + chunk(b"IEND", b""))


# --- A key matrix on GPIO inputs ---------------------------------------------------------------------------------------

GPIO_BANKS = (0x14020000, 0x14100000, 0x14110000)   # pins 0-31, 32-63, 64-95 (platform/hrc7000/platform.h)
GPIO_DR, GPIO_EXT = 0x00, 0x50                      # output levels, input levels


def gpio_bank(pin):
    return GPIO_BANKS[pin >> 5]


class KeyMatrix:
    """The input levels a board's keys give, from its wiring: row pins the firmware drives low one at a time, column
    pins it reads (low = a key closed, idle high by the pull-ups), and a PTT pin (low = pressed). On QEMU's virt
    machine the GPIO input register (GPIO_EXT) is plain RAM that starts at 0, so with no model every column and the
    PTT read as pressed. `idle_ext` gives the levels of the banks this model owns with no key held, `ext(rows_dr)` the
    levels while keys are held, given the row bank's output register.

    `pull_up` lists further input pins that idle high (encoder lines resting at A == B).

    Example (rows 0x47-0x4B, columns 0x31-0x34): green is row 0, column 1. Held, column 1 (bank B bit 17) reads low
    only while row 0 (bank C bit 7) is driven low."""

    def __init__(self, rows, cols, keymap, ptt=None, pull_up=()):
        self.rows, self.cols = list(rows), list(cols)
        self.keymap = keymap        # key name -> (row index, column index)
        self.ptt = ptt
        self.pull_up = list(pull_up)
        self.held = set()

    def _idle(self):
        out = {}
        for pin in self.cols + self.pull_up + ([self.ptt] if self.ptt is not None else []):
            out[gpio_bank(pin)] = out.get(gpio_bank(pin), 0) | 1 << (pin & 31)
        return out

    def idle_ext(self):
        return self._idle()

    def row_bank(self):
        return gpio_bank(self.rows[0])

    def ext(self, rows_dr):
        out = self._idle()
        for name in self.held:
            if name == "ptt" and self.ptt is not None:
                out[gpio_bank(self.ptt)] &= ~(1 << (self.ptt & 31))
                continue
            r, c = self.keymap[name]
            if not rows_dr >> (self.rows[r] & 31) & 1:
                pin = self.cols[c]
                out[gpio_bank(pin)] &= ~(1 << (pin & 31))
        return out

    def names(self):
        return sorted(self.keymap) + (["ptt"] if self.ptt is not None else [])
