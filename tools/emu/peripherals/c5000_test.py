#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Standalone checks of the HR_C5000 Renode model (c5000.cs) against our own driver's register sequences.

Each test starts one headless Renode (c5000_test.repl: the stock STM32F4, the chip wiring from the board description, and the
bench MCU c5000_bench.cs), runs a scenario in virtual time, quits, and reads the model's trace. AIR records are
checked with the conductor's own codec (tools/conductor/parts/air.py Record), so the layout is the one the virtual
board and the conductor use. FEC values are checked against core/dmr/call/dmr_lc.c, compiled on the host.

    python3 -m pytest tools/emu/peripherals/c5000_test.py -v      # Renode from $RENODE or ~/tools/renode/renode
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
from tools.conductor.parts.air import Record  # noqa: E402
from tools.emu.board_config import BOARDS, load
from tools.emu.renode_guard import GuardedPopen  # noqa: E402

BOARD = next(b for b, c in BOARDS.items() if c.get("chip") == "sysbus.spi2.c5000")

RENODE = os.environ.get("RENODE") or os.path.expanduser("~/tools/renode/renode")
SLOT = 30000
VOICE = 0x10
LC = bytes.fromhex("000000000009" "12D687")          # group call to TG 9 from 1234567 (TS 102 361-2 7.1.1)
OWN_ID = 1234567

pytestmark = pytest.mark.skipif(not os.path.exists(RENODE), reason="Renode not installed")


def renode_run(body, limit_s=100):
    """Run one scenario; return the model's trace lines. Nothing outlives the call."""
    d = tempfile.mkdtemp(prefix="c5000-")
    trace = os.path.join(d, "trace.txt")
    script = "\n".join([
        f"include @{HERE}/c5000.cs",
        f"include @{HERE}/c5000_bench.cs",
        'mach create "c5000"',
        f"machine LoadPlatformDescription @{HERE}/c5000_test.repl",
        # A CPU that only spins, so virtual time runs: SP, reset vector, "b ."
        "sysbus WriteDoubleWord 0x08000000 0x20001000",
        "sysbus WriteDoubleWord 0x08000004 0x08000009",
        "sysbus WriteWord 0x08000008 0xE7FE",
        "cpu PC 0x08000008",
        f"sysbus.spi2.c5000 TracePath @{trace}",
        body,
        "quit",
        "",
    ])
    path = os.path.join(d, "run.resc")
    with open(path, "w") as f:
        f.write(script)
    log = os.path.join(d, "renode.log")
    with open(log, "w") as out:
        p = GuardedPopen(["timeout", "-s", "KILL", str(limit_s), RENODE, "--disable-xwt", "--console", "--plain",
                         "-e", f"include @{path}"], stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.STDOUT)
        try:
            p.wait(limit_s + 10)
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    text = open(log).read()
    assert "Could not compile" not in text and "error" not in text.lower().replace("errors: 0", ""), text[-3000:]
    lines = open(trace).read().splitlines() if os.path.exists(trace) else []
    shutil.rmtree(d, ignore_errors=True)
    assert lines, text[-3000:]
    return lines


def t_of(line):
    return int(line.split()[0][2:])


def find(lines, pattern):
    rx = re.compile(pattern)
    return [ln for ln in lines if rx.search(ln)]


def air_out(lines):
    return [(t_of(ln), Record.decode(bytes.fromhex(ln.split()[-1]))) for ln in find(lines, r" AIR_OUT ")]


def bench(cmd):
    return "sysbus.bench " + cmd


def init(repeater=False, cc=1):
    return bench(f"Init {'true' if repeater else 'false'} {cc} {OWN_ID}")


def voice_frame(n):
    return bytes((n * 16 + i) & 0xFF for i in range(27))


# --- FEC reference: our own C code ---

_ref = {}


def c_reference():
    """RS(12,9) of LC with the header mask, and the BS activation CSBK, from core/dmr/call/dmr_lc.c."""
    if not _ref:
        cc = shutil.which("gcc") or shutil.which("cc")
        if cc is None:
            pytest.skip("no host C compiler")
        d = tempfile.mkdtemp(prefix="c5000-ref-")
        src = os.path.join(d, "m.c")
        with open(src, "w") as f:
            f.write('#include <stdio.h>\n#include "dmr/call/dmr_lc.h"\nint main(void){unsigned char lc[9]={%s},o[12],'
                    'c[12];dmr_lc_encode_full(lc,0x96,o);for(int i=0;i<12;i++)printf("%%02X",o[i]);printf("\\n");'
                    'dmr_lc_encode_full(lc,0x99,o);for(int i=0;i<12;i++)printf("%%02X",o[i]);printf("\\n");'
                    'dmr_csbk_bs_activation(%d,c);for(int i=0;i<12;i++)printf("%%02X",c[i]);printf("\\n");return 0;}'
                    % (",".join(str(b) for b in LC), OWN_ID))
        exe = os.path.join(d, "m")
        subprocess.run([cc, "-I", os.path.join(ROOT, "core"), "-I", ROOT, "-I", os.path.join(ROOT, "hal"), "-o", exe,
                        src, os.path.join(ROOT, "core/dmr/call/dmr_lc.c"), os.path.join(ROOT, "core/dmr/fec/dmrFec.c")],
                       check=True)
        out = subprocess.run([exe], check=True, capture_output=True, text=True).stdout.split()
        shutil.rmtree(d, ignore_errors=True)
        _ref["header"], _ref["terminator"], _ref["csbk"] = (bytes.fromhex(x) for x in out)
    return _ref


# --- Tests ---

def test_config_port_bring_up():
    """Power-on, FM, DMR set-up through the bit-banged port: reads are bit-exact, the register file holds the driver's
    values, and every frame was whole."""
    lines = renode_run("\n".join([
        init(),
        bench("Peek 0 0x0B"), bench("Peek 0 0x0C"), bench("Peek 0 0x10"), bench("Peek 0 0x81"), bench("Peek 0 0x1F"),
        bench("Peek 0 0x14"), bench("Peek 0 0x16"), bench("Peek 0 0x2E"), bench("Peek 1 0x10"), bench("Peek 1 0x1F"),
        bench("Peek 1 0x04"), bench("Poke 0 0x50 0xA5"), bench("Peek 0 0x50"),
    ]))
    assert not find(lines, r"CFG BAD")
    # radio_md380.c reads 0x10 after hrc5000_power_on: FM receive leaves 0x80
    assert find(lines, r"BENCH r10=80$")
    assert find(lines, r"BENCH init done r10=6B r40=C3 r41=50 r81=19")
    peeks = dict(re.search(r"PEEK (p\d \w\w)=(\w\w)", ln).groups() for ln in find(lines, r"BENCH PEEK"))
    assert peeks == {
        "p0 0B": "28", "p0 0C": "33",                   # set_clock: PLLM 40, PLL on N 3 DO 3
        "p0 10": "6B", "p0 81": "19", "p0 1F": "10",    # DMR simplex, interrupt mask, color code 1
        "p0 14": "87", "p0 16": "12",                   # own ID 1234567 = 0x12D687, low byte first
        "p0 2E": "04",                                  # block_sys byte 0x1D of 44 (0x11 + 0x1D)
        "p1 10": "69", "p1 1F": "F0",                   # aux_masks first and 16th
        "p1 04": "D5",                                  # MS data sync, first byte
        "p0 50": "A5",
    }


def test_tx_call_becomes_air_records():
    """A direct-mode call as setup_burst writes it: three LC headers, one superframe A-F, the terminator, and a CSBK.
    Each burst comes out as one AIR record at its slot start, one frame apart, with the chip's FEC."""
    ref = c_reference()
    # Headers as the engine writes them without host CRC: 9 LC bytes and 3 zeros, the chip adds the RS(12,9) parity
    queue = [bench(f'Queue "01" 0 "{LC.hex()}"')] * 3
    queue += [bench(f'Queue "10" {i} "{voice_frame(i).hex()}"') for i in range(6)]
    queue += [bench(f'Queue "02" 0 "{ref["terminator"].hex()}"')]
    queue += [bench(f'Queue "03" 0 "{ref["csbk"][:10].hex()}"')]     # CSBK: the MCU writes 10 bytes, the chip the CRC
    lines = renode_run("\n".join([
        init(), 'emulation RunFor "0.1"', bench(f'EmbeddedLc "{LC.hex()}"'), bench("TxBegin"), *queue,
        'emulation RunFor "1.0"',
    ]))
    recs = air_out(lines)
    kinds = [(r.kind, r.voice_index) for _, r in recs]
    assert kinds == [(1, 0)] * 3 + [(VOICE, i) for i in range(6)] + [(2, 0), (3, 0)]
    starts = [r.t_us for _, r in recs]
    assert all(b - a == 2 * SLOT for a, b in zip(starts, starts[1:])), starts      # one burst per frame
    assert all(t == r.t_us for t, r in recs)                                         # emitted at the slot start
    for _, r in recs:
        assert r.cc == 1 and r.ts == 0 and r.flags & 1 == 0
    assert [r.sync for _, r in recs] == [1] * 4 + [0] * 5 + [1, 1]                  # MS sync; B-F carry EMB
    assert recs[0][1].payload == ref["header"]
    assert recs[9][1].payload == ref["terminator"]
    assert recs[10][1].payload == ref["csbk"]
    for i in range(6):
        v = recs[3 + i][1]
        assert v.payload == voice_frame(i)
        assert v.flags & 2 and v.lc == LC                                            # the embedded LC for B-E
    # RF TX interrupt 1 ms before each burst; the bench saw the TX slot in 0x42; back to receive
    rftx = [t_of(ln) for ln in find(lines, r"BENCH RFTX")]
    assert rftx == [s - 1000 for s in starts]
    assert len(find(lines, r"BENCH TICK \d+ r42=A0")) == len(recs)
    assert find(lines, r"BENCH TXDONE rftx=11")
    assert not find(lines, r"CFG BAD|TX BAD")


def rx_records(t0, header=True, cc=1, late_superframes=0):
    """A direct-mode call on the air, one burst per frame from t0: headers, voice A-F (embedded LC flagged on each,
    as the virtual modem sends it), terminator."""
    recs, t = [], t0
    if header:
        for _ in range(2):
            recs.append(Record(0, 1, t, cc, 0, 1, 0, bytes(9), c_reference()["header"]))
            t += 2 * SLOT
    for sf in range(1 + late_superframes):
        for i in range(6):
            recs.append(Record(0, VOICE, t, cc, i, 1 if i == 0 else 0, 2, LC, voice_frame(i + 6 * sf)))
            t += 2 * SLOT
    recs.append(Record(0, 2, t, cc, 0, 1, 0, bytes(9), c_reference()["terminator"]))
    return recs


def test_rx_call_becomes_registers_and_interrupts():
    """AIR records in: the headers and terminator come as system interrupts with 0x51 / 0x52 / page 2, the voice
    through the voice port at the tick after each burst while the decoder runs, the report 25 ms after that tick."""
    t0 = 300000
    recs = rx_records(t0)
    lines = renode_run("\n".join([init(), *[bench(f'Air "{r.encode().hex()}"') for r in recs],
                                  'emulation RunFor "1.0"']))
    assert len(find(lines, r"-> taken")) == len(recs), find(lines, r"BENCH air")
    # Passive timing: the clock starts on the first burst, its slot ends 30 ms after its start
    assert find(lines, rf"CLOCK passive slot={t0}")
    rxd = find(lines, r"BENCH RXD")
    assert [re.search(r"r51=(\w\w)", ln).group(1) for ln in rxd] == ["12", "12", "22"]
    assert all("r52=1" in ln for ln in rxd)                                          # color code 1
    assert re.search(r"payload=(\w+)", rxd[0]).group(1) == c_reference()["header"].hex().upper()
    assert re.search(r"payload=(\w+)", rxd[2]).group(1) == c_reference()["terminator"].hex().upper()
    assert t_of(rxd[0]) == t0 + SLOT + 25000                                         # RxReportDelayUs after the tick
    # Voice: each burst read at the tick that ends its slot
    for i in range(6):
        start = t0 + (2 + i) * 2 * SLOT
        tick = find(lines, rf"^t={start + SLOT} BENCH TICK")
        assert tick and f"voice={voice_frame(i).hex().upper()}" in tick[0], (start, tick)
    assert find(lines, r"DECODE start") and find(lines, r"DECODE stop")
    assert not find(lines, r"drop=")


def test_rx_late_entry_and_filters():
    """No header: the voice sync report alone is masked (0x81 = 0x19), the embedded LC at F raises late entry with the
    LC in page 2, and the decoder started then gets the next superframe. A burst with another color code is dropped."""
    t0 = 300000
    recs = rx_records(t0, header=False, late_superframes=1)
    other_cc = Record(0, 3, recs[-1].t_us + 2 * SLOT, 5, 0, 1, 0, bytes(9), c_reference()["csbk"])
    lines = renode_run("\n".join([init(), *[bench(f'Air "{r.encode().hex()}"') for r in recs + [other_cc]],
                                  'emulation RunFor "1.3"']))
    late = find(lines, r"BENCH LATE")
    assert len(late) == 1 and f"lc={LC.hex().upper()}" in late[0]
    f_start = t0 + 5 * 2 * SLOT
    assert t_of(late[0]) == f_start + SLOT + 25000
    # Superframe 2 reaches the voice port; superframe 1 did not (the decoder was off)
    for i in range(6):
        start = t0 + (6 + i) * 2 * SLOT
        assert f"voice={voice_frame(6 + i).hex().upper()}" in find(lines, rf"^t={start + SLOT} BENCH TICK")[0]
    assert not find(lines, r"VOICE_RX " + voice_frame(0).hex().upper())
    assert find(lines, r"drop=cc kind=03")


def test_round_trip_tx_records_received():
    """What the model sends, fed back as received air one second later (another radio's view): the LC and the voice
    come out of the registers unchanged."""
    ref = c_reference()
    queue = [bench(f'Queue "01" 0 "{ref["header"].hex()}"')] * 2
    queue += [bench(f'Queue "10" {i} "{voice_frame(i).hex()}"') for i in range(6)]
    queue += [bench(f'Queue "02" 0 "{ref["terminator"].hex()}"')]
    tx = renode_run("\n".join([init(), 'emulation RunFor "0.1"', bench(f'EmbeddedLc "{LC.hex()}"'), bench("TxBegin"),
                               *queue, 'emulation RunFor "0.8"']))
    sent = [r for _, r in air_out(tx)]
    assert len(sent) == 9
    shift = 2000000 - sent[0].t_us
    back = [Record(r.ts, r.kind, r.t_us + shift, r.cc, r.voice_index, r.sync, r.flags, r.lc, r.payload) for r in sent]
    rx = renode_run("\n".join([init(), *[bench(f'Air "{r.encode().hex()}"') for r in back], 'emulation RunFor "2.7"']))
    assert len(find(rx, r"-> taken")) == 9
    rxd = find(rx, r"BENCH RXD")
    assert len(rxd) == 3 and f"payload={ref['header'].hex().upper()}" in rxd[0]
    for i in range(6):
        assert find(rx, f"voice={voice_frame(i).hex().upper()}")


# --- The real firmware ---

def emu_root():
    """A tree with the emulation stage 1 platform and a built HR_C5000 board image: this one, or $C5000_EMU_ROOT."""
    for root in (os.environ.get("C5000_EMU_ROOT"), ROOT):
        if root and os.path.exists(os.path.join(root, "boards", BOARD, "emu", "platform.repl")) and \
                os.path.exists(load(root)[BOARD]["elf"]):
            return root
    pytest.skip(f"no platform and ELF for {BOARD}; set C5000_EMU_ROOT")


def test_real_firmware_bring_up_matches_bench():
    """The unchanged board image on the stage 1 platform plus this model: its HR_C5000 bring-up decodes whole, reads
    0x10 back as 0x80 (the diagnostics' "C5000 ok"), and is the sequence the bench plays (c5000_bench.cs PowerOnFmRx)."""
    root = emu_root()
    d = tempfile.mkdtemp(prefix="c5000-elf-")
    trace = os.path.join(d, "trace.txt")
    repl = os.path.join(d, "firmware_c5000.repl")
    with open(os.path.join(root, "boards", BOARD, "emu", "platform.repl")) as f:
        has_chip = any(ln.startswith("c5000:") for ln in f)
    if has_chip:    # The board platform wires the chip itself: declaring it again is Renode error E02
        repl = os.path.join(root, "boards", BOARD, "emu", "platform.repl")
    else:           # an older stage 1 tree ($C5000_EMU_ROOT): add the chip with the wiring of c5000_test.repl
        with open(os.path.join(HERE, "c5000_test.repl")) as f:
            wiring = f.read().split("using \"platforms/cpus/stm32f4.repl\"", 1)[1].split("bench:", 1)[0]
        with open(repl, "w") as f:
            f.write(f'using "{root}/boards/{BOARD}/emu/platform.repl"\n{wiring}')
    stubs = [os.path.join(root, "tools/emu/stm32_stubs.cs")]
    pdir = os.path.join(root, "tools/emu/peripherals")
    if os.path.isdir(pdir):
        stubs += [os.path.join(pdir, n) for n in sorted(os.listdir(pdir)) if n.endswith(".cs") and not n.startswith("c5000")]
    script = "\n".join([*[f"include @{s}" for s in stubs], f"include @{HERE}/c5000.cs",
                        'emulation SetSeed 1', 'emulation SetGlobalQuantum "0.0001"',
                        'emulation SetGlobalSerialExecution true', f'mach create "{BOARD}"',
                        f"machine LoadPlatformDescription @{repl}",
                        f"sysbus LoadELF @{load(root)[BOARD]['elf']}",
                        "cpu VectorTableOffset 0x0800C000", "cpu PerformanceInMips 72",
                        "sysbus.gpioPortE OnGPIO 11 true", "sysbus.gpioPortE OnGPIO 12 true",
                        "sysbus.gpioPortD OnGPIO 10 true", f"sysbus.spi2.c5000 TracePath @{trace}", "logLevel 3",
                        'emulation RunFor "1.5"', "quit", ""])
    path = os.path.join(d, "run.resc")
    with open(path, "w") as f:
        f.write(script)
    p = GuardedPopen(["timeout", "-s", "KILL", "200", RENODE, "--disable-xwt", "--console", "--plain", "-e",
                     f"include @{path}"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = p.communicate(timeout=210)
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert "There was an error" not in out and "Could not compile" not in out, out[-3000:]
    lines = open(trace).read().splitlines()
    shutil.rmtree(d, ignore_errors=True)
    cfg = [ln.split(" ", 1)[1] for ln in lines if " CFG " in ln]
    assert cfg and not find(lines, r"CFG BAD")
    read10 = cfg.index("CFG R p0 010 [1] 80")
    bench_lines = renode_run(init())
    want = [ln.split(" ", 1)[1] for ln in bench_lines if " CFG " in ln]
    assert cfg[:read10 + 1] == want[:want.index("CFG R p0 010 [1] 80") + 1]
