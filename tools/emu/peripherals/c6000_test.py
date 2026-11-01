#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""DMR on an emulated HR_C6000 board: the unchanged ELF with a fresh dmr-group flash image.

The board's emulator description supplies the boot script, platform, flash template and keypad.
Voice TX uses tools/emu/codec_stub.py; no real codec is included.
AIR records are checked with the conductor's Record codec, and debug link replies through the in-process console.
Run: python3 -m pytest tools/emu/peripherals/c6000_test.py -v
"""
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "emu"))
from tools.conductor.parts.air import Record  # noqa: E402
from tools.emu.renode_guard import GuardedPopen  # noqa: E402
import rfoundry_debug as rd  # noqa: E402
import flashimg  # noqa: E402

RENODE = os.environ.get("RENODE") or os.path.expanduser("~/tools/renode/renode")
from board_config import BOARDS
BOARD, CONFIG = next((b, c) for b, c in BOARDS.items() if c.get("chip") == "sysbus.spi2.c6000")
ELF = CONFIG["elf"]
SLOT = 30000
VOICE = 0x10
OWN_ID = 3100001            # radio index 1 of the dmr-group fixture (spinor_image.c): 0x2F4D61
TG = 9
CALLER = 1234567            # someone else, on talkgroup 9

pytestmark = pytest.mark.skipif(not (os.path.exists(RENODE) and os.path.exists(ELF)),
                                reason=f"Renode or {ELF} missing")

CONSOLE = '''python """
console = self.Machine["sysbus.hostport"]
replies_file = open(r'{replies}', 'wb')
def receive(value):
    replies_file.write(chr(value))
    replies_file.flush()
console.CharReceived += receive
"""
'''


class Run:
    """One Renode run's outputs: the chip trace, the debug link replies (by sequence number) and the Renode log."""

    def __init__(self, trace, replies, log):
        self.trace = trace
        self.replies = replies
        self.log = log

    def find(self, pattern):
        rx = re.compile(pattern)
        return [ln for ln in self.trace if rx.search(ln)]

    def air_out(self):
        return [(t_of(ln), Record.decode(bytes.fromhex(ln.split()[-1]))) for ln in self.find(r" AIR_OUT ")]

    def view(self, seq):
        return rd.parse_kv(self.replies[seq][5:].decode("ascii"))


def t_of(line):
    return int(line.split()[0][2:])


def debug(seq, command, payload=b""):
    """Monitor line that writes one debug link request into the console"""
    return f'python "for value in {list(rd.frame(command, seq, payload))}: console.WriteChar(value)"'


def get_view(seq, section):
    return debug(seq, rd.GET_VIEW, bytes((rd.VIEW_SECTIONS.index(section),)))


def parse_replies(data):
    replies = {}
    while data:
        assert data[:2] == b"\xA5\x5A", data[:16]
        command, sequence, length = struct.unpack_from("<BBH", data, 2)
        packet, data = data[:8 + length], data[8 + length:]
        assert len(packet) == length + 8 and rd.crc16(packet[2:-2]) == struct.unpack("<H", packet[-2:])[0]
        if command >= 0x80:     # the virtual console echoes the requests too
            assert packet[6] == rd.ST_OK, packet
            replies[sequence] = packet[7:-2]
    return replies


def renode_run(body, limit_s=240, codec=False):
    """Boot the board on a fresh dmr-group image, run body (monitor lines), quit. Nothing outlives the call.
    codec: hook the codec stub in (DMR voice TX)."""
    d = tempfile.mkdtemp(prefix="c6000-")
    try:
        image = os.path.join(d, "flash.bin")
        flashimg.build_image(image, "dmr-group", 1)
        flash_repl = os.path.join(d, "flash.repl")
        with open(os.path.join(CONFIG["directory"], CONFIG["flash"])) as f:
            text = f.read().replace("{image}", image)
        with open(flash_repl, "w") as f:
            f.write(text)
        trace = os.path.join(d, "trace.txt")
        replies = os.path.join(d, "replies.bin")
        script = "\n".join([
            "$tcp=false",
            f"$flashrepl=@{flash_repl}",
            f"$elf=@{ELF}",
            f"include @{CONFIG['directory']}/{CONFIG['resc']}",
            *([f"include @{ROOT}/tools/emu/codec_stub.py", "setup_codec_stub"] if codec else []),
            f"sysbus.spi2.c6000 TracePath @{trace}",
            CONSOLE.format(replies=replies),
            body,
            'python "replies_file.close()"',
            "quit",
            "",
        ])
        path = os.path.join(d, "run.resc")
        with open(path, "w") as f:
            f.write(script)
        log = os.path.join(d, "renode.log")
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(d, "config"))
        with open(log, "w") as out:
            p = GuardedPopen(["timeout", "-s", "KILL", str(limit_s), RENODE, "--disable-xwt", "--console",
                             "--plain", path], cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT)
            try:
                p.wait(limit_s + 10)
            finally:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        text = open(log).read()
        assert "Could not compile" not in text and "There was an error" not in text, text[-3000:]
        lines = open(trace).read().splitlines() if os.path.exists(trace) else []
        data = open(replies, "rb").read() if os.path.exists(replies) else b""
        assert lines, text[-3000:]
        return Run(lines, parse_replies(data), text)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- Helpers for the checks ---

def crc_ccitt(info, mask):
    """ETSI TS 102 361-1 B.3.8: CRC-CCITT over the first 10 bytes, inverted, xor the data type's mask"""
    c = 0
    for x in info[:10]:
        c ^= x << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return (~c ^ mask) & 0xFFFF


def lc_bytes(dst, src):
    """Group voice LC (FLCO 0, FID 0, no options) to dst from src, 9 bytes (TS 102 361-2 7.1.1)"""
    return bytes(3) + dst.to_bytes(3, "big") + src.to_bytes(3, "big")


def gf_mul(a, b):
    p = 0
    while b:
        if b & 1:
            p ^= a
        a = ((a << 1) ^ (0x1D if a & 0x80 else 0)) & 0xFF
        b >>= 1
    return p


def rs_lc(lc, mask):
    """LC + RS(12,9) parity xor the mask (ETSI TS 102 361-1 B.3.6; core/dmr/call/dmr_lc.c dmr_lc_encode_full)"""
    r2 = r1 = r0 = 0
    for x in lc:
        fb = x ^ r2
        r2, r1, r0 = r1 ^ gf_mul(fb, 0x0E), r0 ^ gf_mul(fb, 0x38), gf_mul(fb, 0x40)
    return bytes(lc) + bytes((r2 ^ mask, r1 ^ mask, r0 ^ mask))


EXTI_CHECK = '''python """
bus = self.Machine.SystemBus
c = self.Machine["sysbus.spi2.c6000"]
bus.WriteDoubleWord(0x40021018, 0x3)          # PE0, PE1 high: both LEDs on
c.TsInt.Set(True)                             # idle level first: the pause may fall inside a timeslot pulse
bus.WriteDoubleWord(0x40013C14, 0x7)          # clear EXTI0-2 pending
c.TsInt.Set(False)
c.TsInt.Set(True)
print("EXTI PR=%X" % bus.ReadDoubleWord(0x40013C14))
"""
'''


# --- Tests ---

def test_boot_config_port_exti_and_ptt():
    """The unchanged ELF brings the chip up on a DMR channel through the bit-banged port; the firmware's reads through
    that port are bit exact (a received burst's report: it writes back the 0x82 value it read) with the keypad driver's
    read hook on the same port (peripherals/keys.py, board layout); the green key still opens the menu; EXTI0 sees the timeslot line with the
    LEDs on; PTT through the keys driver reaches the radio, which refuses DMR voice because this build has no codec."""
    r = renode_run("\n".join([
        f"include @{HERE}/keys.py",
        f'keys_layout "{BOARD}"',
        "keys_attach",
        'emulation RunFor "4"',
        get_view(1, "core"),
        'emulation RunFor "0.2"',
        "keys_key 4 1",
        'emulation RunFor "0.6"',
        get_view(2, "core"),
        'emulation RunFor "0.2"',
        "keys_key 4 0",
        'emulation RunFor "0.5"',
        get_view(3, "link"),
        'emulation RunFor "0.1"',
        "keys_key 0 1",
        'emulation RunFor "0.1"',
        "keys_key 0 0",
        'emulation RunFor "0.6"',
        get_view(4, "link"),
        'emulation RunFor "0.1"',
        f'sysbus.spi2.c6000 AirInAfter 10000 "{Record(1, 1, 0, 1, 0, 1, 0, bytes(9), lc_bytes(TG, CALLER)).encode().hex()}"',
        'emulation RunFor "0.1"',
        "sysbus.spi2.c6000 PeekRegister 4 0x10",
        "sysbus.spi2.c6000 PeekRegister 4 0x40",
        "sysbus.spi2.c6000 PeekRegister 4 0x1F",
        EXTI_CHECK,
    ]))
    assert not r.find("CFG BAD"), r.find("CFG BAD")[:3]
    assert len(r.find(r" CFG W p4 ")) > 100
    # Power-on init (hrc6000.c init_clock, init_step13) and DMR entry (dmr_enter 0x10 = 0x6E, passive RX 0x40 = 0xC3)
    assert r.find(r"CFG W p4 00B \[1\] 28") and r.find(r"CFG W p4 00C \[1\] 33")
    assert r.find(r"CFG W p4 010 \[1\] 6E") and r.find(r"CFG W p4 040 \[1\] C3")
    assert r.find(r" REPORT flags=08 ") and r.find(r"CFG R p4 082 \[1\] 08"), r.find("REPORT|AIR_IN")
    assert r.find(r"CFG W p4 083 \[1\] 08"), r.find(r"CFG . p4 08")
    assert re.search(r"^0x6E\s*$", r.log, re.M) and re.search(r"^0xC3\s*$", r.log, re.M)
    m = re.search(r"EXTI PR=(\w+)", r.log)
    assert m and int(m.group(1), 16) & 1, r.log[-3000:]
    before, during = r.view(1), r.view(2)
    assert before["mode"] == "dmr" and before["tx_deny"] == "none", before
    assert during["state"] == "tx_denied" and during["tx_deny"] == "no_codec", during
    assert not r.air_out()
    assert r.view(3)["ui.screen"] == "home" and r.view(4)["ui.screen"] != "home", (r.view(3), r.view(4))
    print(f"\nreport read back 0x82=08; EXTI0 pending with PE0 high; PTT: {during['tx_deny']}")


def test_dmr_data_bursts_out_decode_with_air_py():
    """A call alert (a CSBK) and a group text message (preamble CSBKs, a data header, rate 1/2 blocks) asked for over
    the debug link go out as AIR records that air.py decodes: our color code, MS sync, the MCU's own CRC sent as
    written (CRC_MCU_Control, host-CRC flag), our ID as the source, one RF TX pulse per burst, bursts 60 ms apart."""
    r = renode_run("\n".join([
        'emulation RunFor "4"',
        debug(1, rd.DMR_DATA, bytes([rd.DATA_OP_CALL_ALERT]) + struct.pack("<I", CALLER)),
        'emulation RunFor "1.5"',
        debug(2, rd.DMR_DATA, bytes([rd.DATA_OP_SMS]) + struct.pack("<IB", TG, 1) + b"hello"),
        'emulation RunFor "1.0"',
    ]))
    out = r.air_out()
    assert len(out) >= 8, out
    assert len(r.find(r" RFTX$")) >= len(out)
    masks = {3: 0xA5A5, 6: 0xCCCC}
    for t, rec in out:
        assert rec.cc == 1 and rec.sync == 1 and rec.ts == 0 and rec.flags == 1, rec
        assert t == rec.t_us
        if rec.kind in masks:
            p = rec.payload
            assert len(p) == 12 and crc_ccitt(p, masks[rec.kind]) == int.from_bytes(p[10:12], "big"), rec
    alert = out[0][1]
    assert alert.kind == 3 and alert.payload[0] == 0x9F, alert                     # CSBK, call alert (LB set)
    assert alert.payload[4:7] == CALLER.to_bytes(3, "big") and alert.payload[7:10] == OWN_ID.to_bytes(3, "big")
    sms = [rec for _, rec in out[1:]]
    kinds = [rec.kind for rec in sms]
    assert 6 in kinds and kinds.count(7) >= 2, kinds                               # data header, rate 1/2 blocks
    header = sms[kinds.index(6)]
    assert header.payload[2:5] == TG.to_bytes(3, "big") and header.payload[5:8] == OWN_ID.to_bytes(3, "big")
    starts = [t for t, rec in out[1:] if rec.kind != 3 or rec.payload[0] != 0x9F]
    assert all((b - a) % (2 * SLOT) == 0 for a, b in zip(starts, starts[1:])), starts
    for t, rec in out:
        print(f"AIR t={t} kind={rec.kind:#x} cc={rec.cc} sync={rec.sync} flags={rec.flags} {rec.payload.hex()}")


def test_dmr_rx_call_reaches_the_stack():
    """A group call fed in as AIR records (3 voice LC headers, two superframes A-F, a terminator; one slot in two, as
    a direct mode call is) is taken by the chip model and reported, and the radio's DMR stack sees the call: view
    call.src is the caller, call.dst the talkgroup, voice bursts are counted, and the call ends with the terminator."""
    lc = lc_bytes(TG, CALLER)
    recs = [Record(1, 1, 0, 1, 0, 1, 0, bytes(9), lc) for _ in range(3)]
    for _ in range(2):
        for n in range(6):
            recs.append(Record(1, VOICE, 0, 1, n, 1 if n == 0 else 0, 2, lc,
                               bytes((n * 16 + j) & 0xFF for j in range(27))))
    recs.append(Record(1, 2, 0, 1, 0, 1, 0, bytes(9), lc))
    lines = ['emulation RunFor "4"']
    lines += [f'sysbus.spi2.c6000 AirInAfter {10000 + 2 * SLOT * i} "{rec.encode().hex()}"'
              for i, rec in enumerate(recs)]
    lines += ['emulation RunFor "0.6"', get_view(1, "dmr_call"), 'emulation RunFor "0.1"', get_view(2, "dmr"),
              'emulation RunFor "0.7"', get_view(3, "dmr_call"), 'emulation RunFor "0.1"']
    r = renode_run("\n".join(lines))
    taken = r.find(r"AIR_IN taken")
    assert len(taken) == len(recs), r.find("AIR_IN")
    reports = r.find(r" REPORT flags=08 ")
    assert len(reports) == len(recs)
    # The MCU read each report bit exact: it writes back what it read from 0x82 (sys_service)
    assert len(r.find(r"CFG W p4 083 \[1\] 08")) >= len(recs)
    during, state, after = r.view(1), r.view(2), r.view(3)
    assert during["call.src"] == str(CALLER) and during["call.dst"] == str(TG) and during["call.group"] == "1", during
    assert state["dmr.phase"] == "rx_voice", state
    assert after["last.src"] == str(CALLER) and after["last.dst"] == str(TG) and int(after["last.ms"]) > 0, after
    print(f"\nview during: call.src={during['call.src']} call.dst={during['call.dst']} dmr.phase={state['dmr.phase']}; "
          f"after: last.src={after['last.src']}")



def test_dmr_voice_ptt_bursts_decode_with_air_py():
    """With the codec stub (tools/emu/codec_stub.py, as tools/conductor/parts/emu.py runs the HR_C5000 board), PTT on the side
    key keys a DMR group call: voice LC headers, whole superframes of voice A-F, then a terminator, as AIR records that
    air.py decodes. The LC (headers, terminator, the embedded LC of the voice bursts) is a group call to talkgroup 9
    from our ID with a valid RS(12,9) parity; voice A carries the voice sync, B-F none; bursts are 60 ms apart."""
    r = renode_run("\n".join([
        'emulation RunFor "4"',
        "sysbus.gpioPortE OnGPIO 11 false",
        'emulation RunFor "1.2"',
        get_view(1, "core"),
        'emulation RunFor "0.1"',
        "sysbus.gpioPortE OnGPIO 11 true",
        'emulation RunFor "1.0"',
        get_view(2, "core"),
        'emulation RunFor "0.1"',
    ]), codec=True)
    out = r.air_out()
    kinds = [rec.kind for _, rec in out]
    print("\n" + " ".join("H" if k == 1 else "T" if k == 2 else "ABCDEF"[rec.voice_index] if k == VOICE else hex(k)
                          for k, (_, rec) in zip(kinds, out)))
    assert r.view(1)["state"] != "tx_denied", r.view(1)
    assert kinds[:3] == [1, 1, 1] and kinds[-1] == 2, kinds
    voice = [rec for _, rec in out if rec.kind == VOICE]
    assert len(voice) >= 6 and len(voice) % 6 == 0, kinds
    assert [rec.voice_index for rec in voice] == [i % 6 for i in range(len(voice))]
    assert set(kinds) == {1, 2, VOICE}, kinds
    lc = lc_bytes(TG, OWN_ID)
    for t, rec in out:
        assert rec.cc == 1 and rec.ts == 0 and t == rec.t_us, rec
        if rec.kind in (1, 2):
            assert rec.sync == 1 and len(rec.payload) == 12 and rec.payload[:9] == lc, rec
            assert rec.payload == rs_lc(lc, 0x96 if rec.kind == 1 else 0x99), rec
        else:
            assert len(rec.payload) == 27 and rec.sync == (1 if rec.voice_index == 0 else 0), rec
            assert rec.flags & 2 and rec.lc == lc, rec      # the embedded LC the chip spreads over B-E
    starts = [t for t, _ in out]
    assert all(b - a == 2 * SLOT for a, b in zip(starts, starts[1:])), starts
    assert r.view(2)["state"] == "idle", r.view(2)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))
