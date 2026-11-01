# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Tests of the HR_C7000 models (hrc7000_soc.py) and, opt-in, of a board boot in XuanTie QEMU.

The model tests need nothing but Python. The boot test skips without QEMU (~/tools/xuantie-qemu or $CSKY_QEMU) or
without the board ELF (make BOARD=<board>), or without pyserial (the debug link over socket://).
Run: python3 -m unittest tools/emu/test_hrc7000_soc.py
"""
import importlib.util
import os
import re
import struct
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
from hrc7000_soc import (APB_HZ, CTRL_EN, CTRL_INT_MASK, CTRL_PERIODIC, SLOT_S, DcsPanel, KeyMatrix,  # noqa: E402
                         Pic, RfBus3w, Soc, Timers, VirtualClock)

QEMU = os.environ.get("CSKY_QEMU") or os.path.expanduser("~/tools/xuantie-qemu/src/build/qemu-system-cskyv2")
from board_config import BOARDS, default_board
BOARD = default_board("qemu")
ELF = BOARDS[BOARD]["elf"]


class FakeHost:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def run_for(clock, host, seconds):
    clock.resume()
    host.t += seconds
    clock.pause()


def block(timers):
    """The timer block's bytes as the firmware leaves them: (LOADCOUNT, CONTROLREG) per timer."""
    raw = bytearray(Timers.span())
    for n, (load, ctrl) in timers.items():
        struct.pack_into("<III", raw, 0x14 * (n - 1), load, 0, ctrl)
    return bytes(raw)


class ClockTest(unittest.TestCase):
    def test_only_run_time_counts(self):
        host = FakeHost()
        c = VirtualClock(1.0, host)
        run_for(c, host, 0.003)
        host.t += 5.0           # the bridge works at a stop: not radio time
        self.assertAlmostEqual(c.advance(), 0.003)

    def test_scale_and_limit(self):
        host = FakeHost()
        c = VirtualClock(0.5, host)
        run_for(c, host, 0.010)
        self.assertAlmostEqual(c.advance(), 0.005)
        run_for(c, host, 0.010)
        self.assertAlmostEqual(c.advance(limit=0.006), 0.006)  # capped at the next interrupt; 4 ms dropped
        self.assertAlmostEqual(c.dropped, 0.004)

    def test_extra_is_a_delay(self):
        c = VirtualClock(1.0, FakeHost())
        self.assertAlmostEqual(c.advance(extra=5e-6), 5e-6)
        self.assertEqual(c.cycles(), 420)   # 5 us at 84 MHz


class TimerPicTest(unittest.TestCase):
    def test_tick_timer_as_time_c_programs_it(self):
        # hrc_tick_start(1000): LOADCOUNT 84000, CONTROLREG periodic | enable, interrupt on
        t = Timers()
        t.program_block(block({1: (APB_HZ // 1000, CTRL_PERIODIC | CTRL_EN)}), 0.0)
        tm = t.t[0]
        self.assertTrue(tm.interrupts())
        self.assertAlmostEqual(tm.next, 0.001)
        self.assertAlmostEqual(tm.period(), 0.001)

    def test_clock_timer_is_masked(self):
        # hrc_time_init: timer 2 free-running, interrupt masked
        t = Timers()
        t.program_block(block({2: (0xFFFFFFFF, CTRL_INT_MASK | CTRL_EN)}), 0.0)
        self.assertFalse(t.t[1].interrupts())

    def test_pic_lowest_unmasked_first(self):
        p = Pic()
        p.request(3)
        p.request(1)
        self.assertIsNone(p.take())     # reset: all masked
        p.program(~((1 << 1) | (1 << 3)) & 0xFFFFFFFF, 0xFFFFFFFF)
        self.assertEqual(p.take(), 1)
        self.assertEqual(p.take(), 3)
        self.assertIsNone(p.take())

    def test_second_request_while_waiting_is_lost(self):
        p = Pic()
        self.assertTrue(p.request(1))
        self.assertFalse(p.request(1))


class SocTest(unittest.TestCase):
    def setUp(self):
        self.host = FakeHost()
        self.soc = Soc(VirtualClock(1.0, self.host), max_hold=3)
        self.soc.timers.program_block(block({1: (APB_HZ // 1000, CTRL_PERIODIC | CTRL_EN)}), 0.0)
        self.soc.pic.program(~(1 << 1) & 0xFFFFFFFF, 0xFFFFFFFF)

    def test_tick_taken_at_its_time(self):
        run_for(self.soc.clock, self.host, 0.0035)
        self.assertAlmostEqual(self.soc.step(), 0.001)      # held at the expiry, the rest dropped
        self.assertEqual(self.soc.pic.take(), 1)
        for _ in range(9):
            run_for(self.soc.clock, self.host, 0.0035)
            self.soc.step()
            self.assertEqual(self.soc.pic.take(), 1)
        self.assertAlmostEqual(self.soc.clock.now, 0.010)
        self.assertEqual(self.soc.timers.t[0].overruns, 0)

    def test_waiting_tick_holds_then_is_lost(self):
        run_for(self.soc.clock, self.host, 0.001)
        self.soc.step()                                     # tick 1 raised, the CPU does not take it
        for _ in range(3):
            run_for(self.soc.clock, self.host, 0.005)
            self.assertLess(self.soc.step(), 0.002)         # held short of tick 2: nothing lost
        self.assertEqual(self.soc.timers.t[0].overruns, 0)
        run_for(self.soc.clock, self.host, 0.005)
        self.soc.step()                                     # max_hold stops: time goes on, as on the chip
        run_for(self.soc.clock, self.host, 0.005)
        self.soc.step()
        self.assertGreater(self.soc.timers.t[0].overruns, 0)

    def test_pa_one_shot_as_time_c_programs_it(self):
        # hrc_timer_oneshot(3, 2000): CONTROLREG = INT_MASK, LOADCOUNT = 2000 us * 84, CONTROLREG = EN
        self.soc.pic.program(~((1 << 1) | (1 << 3)) & 0xFFFFFFFF, 0xFFFFFFFF)
        tmrs = {1: (APB_HZ // 1000, CTRL_PERIODIC | CTRL_EN)}
        for ctrl in (CTRL_INT_MASK, CTRL_EN):
            tmrs[3] = (2000 * 84, ctrl)
            self.soc.program_timers(block(tmrs))
        threes = 0
        while self.soc.clock.now < 0.0035:
            run_for(self.soc.clock, self.host, 0.0003)
            self.soc.step()
            while (s := self.soc.pic.take()) is not None:
                if s == 3:
                    threes += 1
                    self.assertAlmostEqual(self.soc.clock.now, 0.002)   # at its time, in step with the ticks
                    tmrs[3] = (2000 * 84, CTRL_INT_MASK)    # the handler stops it (hrc_timer_stop)
                    self.soc.program_timers(block(tmrs))
        self.assertEqual(threes, 1)

    def test_stopped_timer_loses_its_waiting_request(self):
        self.soc.pic.program(~((1 << 1) | (1 << 3)) & 0xFFFFFFFF, 0xFFFFFFFF)
        tmrs = {1: (APB_HZ // 1000, CTRL_PERIODIC | CTRL_EN), 3: (500 * 84, CTRL_EN)}
        self.soc.program_timers(block(tmrs))
        run_for(self.soc.clock, self.host, 0.0006)
        self.soc.step()
        self.assertTrue(self.soc.pic.pending >> 3 & 1)
        tmrs[3] = (500 * 84, CTRL_INT_MASK)                 # stopped from a task before the CPU took it
        self.soc.program_timers(block(tmrs))
        self.assertFalse(self.soc.pic.pending >> 3 & 1)

    def test_masked_source_does_not_hold(self):
        self.soc.pic.program(0xFFFFFFFF, 0xFFFFFFFF)
        run_for(self.soc.clock, self.host, 0.0035)
        self.assertAlmostEqual(self.soc.step(), 0.0035)

    def test_slots_on_the_same_clock(self):
        self.soc.slots.enabled = True
        self.soc.pic.program(~((1 << 1) | (1 << 30) | (1 << 31)) & 0xFFFFFFFF, 0xFFFFFFFF)
        while self.soc.clock.now < 0.0905:
            run_for(self.soc.clock, self.host, 0.0004)
            self.soc.step()
            while self.soc.pic.take() is not None:
                pass
        self.assertEqual(self.soc.slots.count, 3)            # 30, 60 and 90 ms
        self.assertEqual(self.soc.pic.taken[30], 3)
        self.assertEqual(self.soc.pic.taken[1], 90)          # and 90 ticks in the same 90 ms
        self.assertEqual(self.soc.slots.slot(self.soc.clock.now), int(self.soc.clock.now / SLOT_S))


class RfBus3wTest(unittest.TestCase):
    def test_reads_give_last_write_or_the_quiet_values(self):
        rf = RfBus3w()
        rf.write(0x100, 0x30, 0x1D38)
        self.assertEqual(rf.read(0x100, 0x30), 0x1D38)
        self.assertEqual(rf.read(0x100, 0xB0), 0x1D38)        # bit 7 is the read flag, not the register
        self.assertEqual(rf.read(0x200, 0x30), 0)             # another bus has its own registers
        self.assertEqual(rf.read(0x100, 0x67), 80)            # RSSI -120 dBm
        self.assertEqual(rf.read(0x100, 0x65), 55)            # noise, no signal
        self.assertEqual((rf.writes, rf.reads), (1, 5))


class DcsPanelTest(unittest.TestCase):
    def window(self, p, x0, x1, y0, y1):
        p.cmd(0x2A)
        p.data(bytes((x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF)))
        p.cmd(0x2B)
        p.data(bytes((y0 >> 8, y0 & 0xFF)))
        p.data(bytes((y1 >> 8, y1 & 0xFF)))                   # parameters may come in more than one call
        p.cmd(0x2C)

    def test_window_fills_row_by_row_and_reports_the_last_pixel(self):
        p = DcsPanel(4, 3)
        self.window(p, 1, 2, 1, 2)
        self.assertFalse(p.pixels([0x1111, 0x2222, 0x3333]))
        self.assertTrue(p.pixels([0x4444]))
        self.assertEqual(p.gram, [0, 0, 0, 0, 0, 0x1111, 0x2222, 0, 0, 0x3333, 0x4444, 0])

    def test_crc_and_frame_line_cover_the_panel_bytes_little_endian(self):
        import zlib
        p = DcsPanel(2, 1)
        self.window(p, 0, 1, 0, 0)
        p.pixels([0xF800, 0x001F])
        self.assertEqual(p.panel_bytes(), bytes((0x00, 0xF8, 0x1F, 0x00)))
        line = p.frame_line(1, 1, 7, 1234, 100)
        self.assertEqual(line, f"FRAME radio=1 boot=1 id=7 us=1234 crc={zlib.crc32(p.panel_bytes()):08x} "
                               "palette=- backlight=100")

    def test_png_is_a_valid_rgb_image(self):
        import zlib
        p = DcsPanel(2, 1)
        self.window(p, 0, 1, 0, 0)
        p.pixels([0xF800, 0x07E0])
        png = p.png(2)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        w, h = struct.unpack(">II", png[16:24])
        self.assertEqual((w, h), (4, 2))
        idat_len = struct.unpack(">I", png[33:37])[0]
        raw = zlib.decompress(png[41:41 + idat_len])
        self.assertEqual(raw[:13], b"\x00" + bytes((255, 0, 0)) * 2 + bytes((0, 255, 0)) * 2)


class KeyMatrixTest(unittest.TestCase):
    def setUp(self):
        # rows 0x47-0x4B (bank C bits 7-11), columns 0x31-0x34 (bank B bits 17-20), PTT 0x2B (bank B bit 11)
        self.k = KeyMatrix(range(0x47, 0x4C), range(0x31, 0x35), {"ok": (0, 0), "down": (4, 1)}, ptt=0x2B)
        self.bank_b = 0x14100000

    def test_idle_reads_every_column_and_ptt_high(self):
        self.assertEqual(self.k.idle_ext(), {self.bank_b: (0xF << 17) | (1 << 11)})

    def test_a_held_key_pulls_its_column_low_only_while_its_row_is_driven_low(self):
        self.k.held.add("ok")
        all_high = 0x1F << 7
        self.assertEqual(self.k.ext(all_high), self.k.idle_ext())
        self.assertEqual(self.k.ext(all_high & ~(1 << 7))[self.bank_b], (0xE << 17) | (1 << 11))
        self.assertEqual(self.k.ext(all_high & ~(1 << 8)), self.k.idle_ext())   # another row: no effect

    def test_ptt_held_reads_low(self):
        self.k.held.add("ptt")
        self.assertEqual(self.k.ext(0x1F << 7)[self.bank_b], 0xF << 17)


@unittest.skipUnless(os.path.exists(QEMU), f"no XuanTie QEMU at {QEMU}")
@unittest.skipUnless(os.path.exists(ELF), "no the board ELF (make BOARD=<board>)")
@unittest.skipUnless(importlib.util.find_spec("serial"), "no pyserial in this Python (uv run --with pyserial)")
class QemuBootTest(unittest.TestCase):
    def test_firmware_tick_is_the_firmware_interrupt(self):
        r = subprocess.run([sys.executable, os.path.join(HERE, "hrc7000_emu.py"), "--board", BOARD, "--boot",
                            "--get-version", "--timeout", "90"], capture_output=True, text=True, timeout=150)
        out = r.stdout
        self.assertEqual(r.returncode, 0, out + r.stderr)
        self.assertIn("-gdb unix:", out)
        import boards
        reported = boards.board_t_names(os.path.join(ROOT, "boards", BOARD))[0][0]
        self.assertIn(f"  board={reported}", out)
        self.assertRegex(out, r"vPortTickHandler reached at .* called from hrc_irq_entry\+0x[0-9a-f]+ .*PSR.VEC 33")
        ratio = float(re.search(r"([0-9.]+) ticks per ms", out).group(1))
        self.assertGreater(ratio, 0.95)
        self.assertNotRegex(out, r"(?m)^FAULT:|^host port output with no client")

    def test_firmware_home_screen_and_ok_key(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            png = os.path.join(d, "home.png")
            r = subprocess.run([sys.executable, os.path.join(HERE, "hrc7000_emu.py"), "--board", BOARD, "--boot",
                                "--lockstep", "--get-version", "--timeout", "90", "--screen", png, "--key", "ok"],
                               capture_output=True, text=True, timeout=150)
            out = r.stdout
            self.assertEqual(r.returncode, 0, out + r.stderr)
            self.assertTrue(os.path.getsize(png) > 1000)
        self.assertRegex(out, r"(?m)^FRAME radio=1 boot=1 id=\d+ us=\d+ crc=[0-9a-f]{8} palette=- backlight=\d+$")
        self.assertIn("ui.screen=home input.raw_keys=0x00000000", out)
        self.assertIn("ui.screen=hub", out)
        self.assertIn("key ok: screen changed", out)


if __name__ == "__main__":
    unittest.main()
