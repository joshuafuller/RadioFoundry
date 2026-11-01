# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Tests of the HR_C7000 modem and SPI0 flash models (hrc7000_modem.py, hrc7000_flash.py) and, opt-in, of a board
boot that sends a CSBK in XuanTie QEMU.

The model tests need nothing but Python. The boot test skips without QEMU, without the board ELF or
without a host C compiler (the flash image is built by flashimg.py). Run:
python3 -m unittest tools/emu/test_hrc7000_modem.py
"""
import os
import shutil
import struct
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from conductor.parts.air import Record  # noqa: E402
from hrc7000_flash import JEDEC, SpiNor  # noqa: E402
from hrc7000_modem import (CSBK_CRC_MASK, KIND_CSBK, KIND_LC_HEADER, KIND_VOICE, LAYER2_CONTROL,  # noqa: E402
                           LAYER2_SEND_TYPE, LAYER2_SLOTON, LAYER2_STATUS, LAYER2_TXRX_CTRL, LOCAL_CC, MODEM_BASE,
                           MODEM_RAM, RF_CONTROL, RX_RAM, RX_TYPE_INFO, SEND_DATA_SYNC_H, SEND_DATA_SYNC_L,
                           SRC_RF_TX, SRC_SYS, SRC_TS_RX, SRC_TS_TX, SYS_INTERP_CLEAR, SYS_INTERP_LIST,
                           SYS_INTERP_MASK, WORK_MODE, Modem, ModemSoc, crc_ccitt, decode_line, encode_record,
                           kind_of, lc_record)
from hrc7000_soc import VirtualClock  # noqa: E402

QEMU = os.environ.get("CSKY_QEMU") or os.path.expanduser("~/tools/xuantie-qemu/src/build/qemu-system-cskyv2")
from board_config import BOARDS, default_board
BOARD = default_board("qemu")
ELF = BOARDS[BOARD]["elf"]
# A call alert from 3100001 to 3100002 (core/dmr/dmrDataService.c signalTransmit): the 10 bytes the driver puts in
# TX RAM, and the CRC the stack's dmr_crc16_append gives them with the CSBK mask (core/dmr/call/dmr_lc.c
# dmr_crc_ccitt compiled on the host and run on these 10 bytes: 0xA50A)
CALL_ALERT = bytes.fromhex("9f0000002f4d622f4d61")
CALL_ALERT_CRC = 0xA50A
# The private call LC 3100002 -> 3100001 with its RS(12,9) parity, header and terminator masks: core/dmr/call/dmr_lc.c
# dmr_lc_encode_full compiled on the host gives these
LC_HEADER = bytes.fromhex("0300002f4d612f4d62eb778f")
LC_TERMINATOR = bytes.fromhex("0300002f4d612f4d62e47880")


class FakeRsp:
    """Memory the model reads, as a dict of address -> byte."""

    def __init__(self):
        self.mem = {}

    def put32(self, addr, v):
        for i, b in enumerate(struct.pack("<I", v)):
            self.mem[addr + i] = b

    def write(self, addr, data):
        for i, b in enumerate(data):
            self.mem[addr + i] = b

    def read(self, addr, n):
        return bytes(self.mem.get(addr + i, 0) for i in range(n))


class Host:
    t = 0.0

    def __call__(self):
        return self.t


class ModemTest(unittest.TestCase):
    def test_send_type_codes(self):
        self.assertEqual(kind_of(0x30), (KIND_CSBK, 0))
        self.assertEqual(kind_of(0x60), (6, 0))
        self.assertEqual(kind_of(0x2B), (KIND_VOICE, 2))     # voice C with LCSS 3 (section 2.6)
        self.assertEqual(kind_of(0x08), (KIND_VOICE, 0))

    def test_crc_is_the_stacks(self):
        self.assertEqual(crc_ccitt(CALL_ALERT) ^ 0xA5A5, CALL_ALERT_CRC)

    def setup_modem(self):
        r, host = FakeRsp(), Host()
        clock = VirtualClock(1.0, host=host)
        m = Modem(r, clock)
        soc = ModemSoc(clock, m)
        soc.pic.program(0, 0)       # everything unmasked
        r.put32(MODEM_BASE + RF_CONTROL, 0x0004041A)    # 0.4 ms RF TX lead
        r.put32(MODEM_BASE + CSBK_CRC_MASK, 0xA5A5)
        r.put32(MODEM_BASE + LOCAL_CC, 1)
        r.put32(MODEM_BASE + SEND_DATA_SYNC_H, 0xD5D7F7)
        r.put32(MODEM_BASE + SEND_DATA_SYNC_L, 0x7FD757)
        return r, host, clock, m, soc

    def test_no_slot_interrupts_on_passive_timing(self):
        r, host, clock, m, soc = self.setup_modem()
        r.put32(MODEM_BASE + LAYER2_CONTROL, 0xC0)
        m.on_watch(MODEM_BASE + LAYER2_CONTROL)
        host.t = 0.2
        clock.resume(), clock.pause()
        soc.step()
        self.assertEqual(m.raised, {})

    def test_active_timing_ticks_and_a_csbk_goes_out_at_the_next_edge(self):
        r, host, clock, m, soc = self.setup_modem()
        clock.now = 0.010
        r.put32(MODEM_BASE + LAYER2_CONTROL, 0xC3)
        m.on_watch(MODEM_BASE + LAYER2_CONTROL)
        self.assertAlmostEqual(soc.next_due(), 0.030)
        clock.now = 0.030
        soc.step()
        self.assertEqual(soc.pic.take(), SRC_TS_TX)
        # the TX timeslot interrupt arms a CSBK
        r.write(MODEM_RAM, CALL_ALERT)
        r.put32(MODEM_BASE + LAYER2_SEND_TYPE, 0x30)
        r.put32(MODEM_BASE + LAYER2_TXRX_CTRL, 0x80)
        m.on_watch(MODEM_BASE + LAYER2_TXRX_CTRL)
        self.assertAlmostEqual(soc.next_due(), 0.060 - 0.0004)   # RF TX, 0.4 ms ahead of the burst
        clock.now = 0.0596
        soc.step()
        self.assertEqual(soc.pic.take(), SRC_RF_TX)
        self.assertEqual(m.sent, [])
        clock.now = 0.060
        soc.step()
        self.assertEqual(len(m.sent), 1)
        rec, text = decode_line(*m.sent[0][:2])
        self.assertEqual((rec.kind, rec.cc, rec.ts, rec.t_us, rec.flags), (KIND_CSBK, 1, 0, 60000, 1))
        self.assertEqual(rec.payload, CALL_ALERT + struct.pack(">H", CALL_ALERT_CRC))
        self.assertIn("src 3100001", text)
        self.assertIn("crc good", text)
        self.assertEqual(Record.decode(rec.encode()), rec)


class ReceiveTest(unittest.TestCase):
    def setup_rx(self):
        r, host = FakeRsp(), Host()
        clock = VirtualClock(1.0, host=host)
        m = Modem(r, clock)
        soc = ModemSoc(clock, m)
        soc.pic.program(0, 0)
        for off, v in ((WORK_MODE, 0x6E), (LOCAL_CC, 1), (SYS_INTERP_MASK, 0x1187F), (CSBK_CRC_MASK, 0xA5A5),
                       (LAYER2_CONTROL, 0xC0), (LAYER2_TXRX_CTRL, 0x40)):
            r.put32(MODEM_BASE + off, v)
        m.on_watch(MODEM_BASE + LAYER2_CONTROL)
        m.on_watch(MODEM_BASE + LAYER2_TXRX_CTRL)
        return r, clock, m, soc

    def run_to(self, clock, soc, t):
        clock.now = t
        soc.step()

    def call_alert(self, cc=1, flags=1, crc=CALL_ALERT_CRC):
        return encode_record(0, KIND_CSBK, 0, cc, 0, 1, flags, bytes(9), CALL_ALERT + struct.pack(">H", crc))

    def test_rs_is_the_stacks(self):
        self.assertEqual(Record.decode(lc_record(1, 3100002, 3100001, group=False)).payload, LC_HEADER)
        self.assertEqual(Record.decode(lc_record(2, 3100002, 3100001, group=False)).payload, LC_TERMINATOR)

    def test_a_csbk_is_deframed_then_the_rx_slot_clock_runs(self):
        r, clock, m, soc = self.setup_rx()
        m.feed([self.call_alert()], 0.010)      # first even slot edge at or after 10 ms: 60 ms (slot 2)
        self.run_to(clock, soc, 0.060)
        self.assertEqual(soc.pic.take(), None)  # no timing yet: no RX slot interrupt
        self.assertAlmostEqual(soc.next_due(), 0.060 + 0.0275)
        self.run_to(clock, soc, 0.0875)
        self.assertEqual(soc.pic.take(), SRC_SYS)
        self.assertEqual(r.read(MODEM_RAM + RX_RAM, 12), CALL_ALERT + struct.pack(">H", CALL_ALERT_CRC))
        # data type 3, sync type data, cc 1, MS sync, chip slot 0, no CRC flag
        self.assertEqual(struct.unpack("<I", r.read(MODEM_BASE + RX_TYPE_INFO, 4))[0], 0x312)
        self.assertEqual(struct.unpack("<I", r.read(MODEM_BASE + SYS_INTERP_LIST, 4))[0], 8)
        # the ISR writes the list back to SYS_INTERP_CLEAR: the list reads 0 and the ISR leaves its loop
        r.put32(MODEM_BASE + SYS_INTERP_CLEAR, 8)
        m.on_watch(MODEM_BASE + SYS_INTERP_CLEAR)
        self.assertEqual(struct.unpack("<I", r.read(MODEM_BASE + SYS_INTERP_LIST, 4))[0], 0)
        self.run_to(clock, soc, 0.090)
        self.assertEqual(soc.pic.take(), SRC_TS_RX)
        self.assertEqual(struct.unpack("<I", r.read(MODEM_BASE + LAYER2_STATUS, 4))[0] >> 30 & 1, 1)   # slot 3
        # the driver's restart releases the RX slot sync: the RX slot interrupts stop
        r.put32(MODEM_BASE + LAYER2_TXRX_CTRL, 0xC00000)
        m.on_watch(MODEM_BASE + LAYER2_TXRX_CTRL)
        self.run_to(clock, soc, 0.200)
        self.assertEqual(soc.pic.take(), None)

    def test_another_color_code_is_not_deframed(self):
        r, clock, m, soc = self.setup_rx()
        m.feed([self.call_alert(cc=2)], 0.0)
        self.run_to(clock, soc, 0.0275)
        self.assertEqual(soc.pic.take(), None)
        self.assertIn("color code", m.received[0][3])

    def test_a_bad_crc_or_a_corrupted_record_sets_the_crc_flag(self):
        for wire in (self.call_alert(crc=0x1234), self.call_alert(flags=1 | 4)):
            r, clock, m, soc = self.setup_rx()
            m.feed([wire], 0.0)
            self.run_to(clock, soc, 0.0275)
            self.assertEqual(struct.unpack("<I", r.read(MODEM_BASE + RX_TYPE_INFO, 4))[0], 0x312 | 1 << 26)

    def test_lc_header_on_an_odd_slot_for_ts2(self):
        r, clock, m, soc = self.setup_rx()
        rec = Record.decode(lc_record(KIND_LC_HEADER, 3100002, 3100001, group=False))
        m.feed([Record(2, rec.kind, 0, 1, 0, 4, 1, rec.lc, rec.payload).encode()], 0.0)   # TS2 sync, ts 2
        self.run_to(clock, soc, 0.030 + 0.0275)
        self.assertEqual(struct.unpack("<I", r.read(MODEM_BASE + RX_TYPE_INFO, 4))[0], 1 << 19 | 0x11E)

    def test_an_aligned_start_takes_the_tx_slots_from_the_receiver(self):
        r, clock, m, soc = self.setup_rx()
        m.feed([self.call_alert()], 0.0)
        self.run_to(clock, soc, 0.0275)
        self.run_to(clock, soc, 0.030)
        soc.pic.take(), soc.pic.take()
        r.put32(MODEM_BASE + LAYER2_SLOTON, 0x25)
        m.on_watch(MODEM_BASE + LAYER2_SLOTON)
        self.run_to(clock, soc, 0.060)
        self.assertEqual(soc.pic.take(), SRC_TS_RX)             # the slot right after the strobe: no TX tick
        self.run_to(clock, soc, 0.090)
        self.assertEqual(sorted((soc.pic.take(), soc.pic.take())), [SRC_TS_TX, SRC_TS_RX])
        # b31: a burst armed now goes out in slot 4, chip slot 0, the received burst's
        self.assertEqual(struct.unpack("<I", r.read(MODEM_BASE + LAYER2_STATUS, 4))[0] >> 31, 0)


class LockstepTest(unittest.TestCase):
    def test_the_bridge_pauses_at_each_grant_end_and_inputs_land_there(self):
        import math
        import threading
        from hrc7000_lockstep import Lockstep

        class Box:
            pass
        ctl, clock, modem, host = Lockstep(), Box(), Box(), Box()
        clock.now, seen = 0.0, []
        ctl.attach(None, clock, None, modem, host, None, {"vApplicationIdleHook": 0x100})
        self.assertIs(host.conn, ctl)
        # an idle point (hal_millis called from the idle hook) jumps to the limit; any other stop adds its own time
        self.assertEqual(ctl.extra(("time", "hal_millis", 1e-3), 0x120, False, 0.0), math.inf)
        self.assertEqual(ctl.extra(("time", "hal_millis", 1e-3), 0x400, False, 0.0), 0.0)
        self.assertEqual(ctl.extra(("stub", "hal_delay_us", 0), 0x400, False, 5e-6), 5e-6)

        def bridge():       # the loop of hrc7000_emu.main: run (here: jump to the limit) until gate() says stop
            while ctl.gate():
                clock.now = modem.stop_at
            ctl.done()
        t = threading.Thread(target=bridge)
        t.start()
        ctl.wait_paused()
        ctl.call(lambda: seen.append(("input", clock.now)))
        ctl.run_to(0.001)
        self.assertEqual((ctl.paused_at, seen), (0.001, [("input", 0.0)]))
        ctl.call(lambda: seen.append(("input", clock.now)))
        ctl.run_to(0.002)
        self.assertEqual(seen[-1], ("input", 0.001))
        ctl.close()
        t.join(5)
        self.assertTrue(ctl.finished)


class FlashTest(unittest.TestCase):
    def test_jedec_read_program_erase(self):
        f = SpiNor()
        f.select(True)
        self.assertEqual(f.feed(b"\x9f\0\0\0"), b"\xff" + JEDEC)
        f.select(False)
        for cmd in (b"\x06", b"\x02\x00\x01\x00\x12\x34"):
            f.select(True)
            f.feed(cmd)
            f.select(False)
        f.select(True)
        self.assertEqual(f.feed(b"\x03\x00\x01\x00\0\0\0"), b"\xff" * 4 + b"\x12\x34\xff")
        f.select(False)
        f.select(True)
        f.feed(b"\x02\x00\x01\x00\x00")      # without write enable: no effect
        f.select(False)
        self.assertEqual(f.mem[0x100], 0x12)
        for cmd in (b"\x06", b"\x20\x00\x01\x00"):
            f.select(True)
            f.feed(cmd)
            f.select(False)
        self.assertEqual(f.mem[0x100], 0xFF)


@unittest.skipUnless(os.path.exists(QEMU) and os.path.exists(ELF) and shutil.which(os.environ.get("CC", "cc")),
                     "needs XuanTie QEMU, a board ELF and a host C compiler")
class QemuBootTest(unittest.TestCase):
    def test_firmware_call_alert_comes_out_as_an_air_record(self):
        out = subprocess.run([sys.executable, os.path.join(HERE, "hrc7000_emu.py"), "--board", BOARD, "--boot",
                              "--flash", "dmr-pair", "--modem", "--dmr-send", "call_alert:3100002", "--timeout", "90"],
                             capture_output=True, text=True, timeout=150).stdout
        self.assertIn("kind=3 (csbk) cc=1", out, out[-3000:])
        self.assertIn("dst 3100002 src 3100001 crc good", out)
        self.assertIn("modem init: 11 of 11", out)

    def boot(self, *args):
        return subprocess.run([sys.executable, os.path.join(HERE, "hrc7000_emu.py"), "--board", BOARD, "--boot",
                               "--flash", "dmr-pair", "--modem", "--timeout", "90", *args],
                              capture_output=True, text=True, timeout=150).stdout

    def test_firmware_receives_a_call_alert_and_answers(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "in.jsonl")
            with open(path, "w") as f:   # the call alert 3100001 -> 3100002 as the board sent it (M2)
                f.write('{"wire": "%s"}\n' % encode_record(0, KIND_CSBK, 2730000, 1, 0, 1, 1, bytes(9), CALL_ALERT
                                                           + struct.pack(">H", CALL_ALERT_CRC)).hex())
            out = self.boot("--radio-index", "2", "--air-in", path)
        self.assertIn("kind=3 (csbk) cc=1 len=12 payload=9f0000002f4d622f4d61a50a: deframed", out, out[-3000:])
        self.assertIn("'data.call_alerts_received': '1'", out)
        self.assertIn("opcode 0x20 fid 0x00 dst 3100001 src 3100002 crc good", out)   # its Call Alert ACK

    def test_firmware_takes_a_voice_lc_header(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "in.jsonl")
            subprocess.run([sys.executable, os.path.join(HERE, "hrc7000_modem.py"), "lc", "3100002", "3100001", path,
                            "--private"], check=True, capture_output=True)
            out = self.boot("--air-in", path)
        self.assertIn("'call.src': '3100002'", out, out[-3000:])
        self.assertIn("'call.rx_calls': '1'", out)
        self.assertIn("V0 H3 T1", out)


if __name__ == "__main__":
    unittest.main()
