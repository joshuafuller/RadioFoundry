#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""VFO channel-switch bug on the real firmware binary (a supported board) in Renode: does the DMR VFO keep its color code across a channel switch?

VFO channel-switch bug (tests/hil/test_vfo_dmr.py::test_vfo_dmr_settings): with the voice codec installed, the DMR VFO set to color
code 7, slot 2, private target 3107777 comes back with color code 2 (the channel's) after "channel and back".

This script boots the unchanged build/<board>/rfoundry_<board>.elf the way tools/emu/emu_check.py does (flash image,
hostport.py debug link, keys.py keys) and, unless --no-codec, loads tools/emu/codec_stub.py so the codec looks present.
Then, with a debug link command (RADIO ops) or a key press for each step, it follows the issue:
    boot -> VFO on -> DMR mode, CC 7, slot 2 -> private target 3107777 -> channel -> VFO again
After every step it reads the core and dmr view sections (GET_VIEW) and saves a screenshot. The last step is judged:
REPRODUCED when the VFO comes back with another color code, slot or target than it was given, else NOT REPRODUCED.

    python3 tools/emu/vfo_channel_check.py --out /tmp/vfo-channel          # codec present (the stub)
    python3 tools/emu/vfo_channel_check.py --board <board> --out /tmp/vfo-channel-board   # the board of the report
    python3 tools/emu/vfo_channel_check.py --no-codec --out /tmp/vfo-channel-nocodec
    python3 tools/emu/vfo_channel_check.py --keys --out /tmp/vfo-channel-keys   # channel switch by the keypad (see KEY_STEPS)

Exit status: 0 not reproduced, 1 reproduced, 2 the run itself failed. No renode process is left.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import emu_check as ec  # noqa: E402
import flashimg  # noqa: E402
rd = ec.rd

SECTIONS = ("core", "dmr")
FIELDS = ("vfo", "mode", "temporary", "dmr.active", "dmr.cc", "dmr.ts", "dmr.target_id", "dmr.target_private",
          "dmr.target_manual", "channel", "zone")


def line(v):
    return " ".join(f"{k}={v.get(k)}" for k in FIELDS if k in v)


class Run:
    def __init__(self, link, r, screen, out, deadline, key_bits):
        self.d, self.r, self.screen, self.out, self.deadline, self.key_bits = link, r, screen, out, deadline, key_bits
        self.n = 0
        self.log = []

    def settle(self, ms=800):
        ec.wait_firmware(self.d, ms, self.deadline)
        ec.wait_settled(self.d, self.screen, 0, 500, self.deadline)

    def look(self, name):
        v = self.d.view(SECTIONS)
        self.n += 1
        png = f"{self.out}-{self.n:02d}-{name}.png"
        frame = self.screen.shot(png)
        text = f"[{self.n:02d} {name}] {line(v)}"
        print(text)
        print(f"     screen {png} crc={ec.crc_of(frame)}")
        self.log.append((name, v, png))
        return v

    def wait(self, cond, what, sec=8.0):
        end = time.monotonic() + sec * 1  # wall seconds; the firmware answers at about real time
        while time.monotonic() < min(end, self.deadline):
            v = self.d.view(SECTIONS)
            if cond(v):
                return v
            time.sleep(0.2)
        print(f"     (still waiting for: {what}; last {line(self.d.view(SECTIONS))})")

    def key(self, bit_name, hold_ms=100, after_ms=600):
        ec.press(self.r, "keys_key", self.d, self.key_bits[bit_name], self.deadline, hold_ms, after_ms)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--board", default=ec.default_board("renode"))
    ap.add_argument("--out", required=True, help="prefix of the PNG files: <out>-NN-<step>.png")
    ap.add_argument("--no-codec", action="store_true", help="do not load codec_stub.py (codec absent)")
    ap.add_argument("--keys", action="store_true", help="also try the keypad where the UI has a path (see KEY_STEPS)")
    ap.add_argument("--channel-op", choices=("vfo-off", "step"), default="vfo-off",
                    help="how to go to a channel: RADIO OP_VFO off (as the HIL test) or a channel step")
    ap.add_argument("--storage", default="dmr-group")
    ap.add_argument("--radio-index", type=int, default=1)
    ap.add_argument("--renode", default=ec.default_renode())
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--scale", type=int, default=2)
    a = ap.parse_args()

    b = a.board
    elf = os.path.join(ec.ROOT, f"build/{b}/rfoundry_{b}.elf")
    config = ec.BOARDS[b]
    directory = config["directory"]
    repl, resc = (os.path.join(directory, config[k]) for k in ("repl", "resc"))
    for path, what in ((a.renode, "Renode"), (elf, "ELF"), (repl, "platform file"), (resc, "Renode script")):
        if not os.path.exists(path):
            print(f"FAIL: no {what} at {path}")
            return 2
    work = tempfile.mkdtemp(prefix="vfo_channel_check_")
    image = os.path.join(work, "flash.bin")
    flashimg.build_image(image, a.storage, a.radio_index)
    with open(os.path.join(directory, config["flash"])) as f:
        text = f.read().replace("{image}", image)
    with open(os.path.join(work, "flash.repl"), "w") as f:
        f.write(text)
    frame_log = os.path.join(work, "frames.txt")
    terminal = os.path.join(work, "debuglink")
    script = (f"$elf=@{elf}; $tcp=false; $repl=@{repl}; $flashrepl=@{work}/flash.repl; include @{resc}; "
              f"lcd FrameLog @{frame_log}; ")
    if not a.no_codec:
        script += f"include @{HERE}/codec_stub.py; setup_codec_stub; "
    layout = f'keys_layout "{b}"; '
    script += (f'include @{ec.KEYS_PY}; {layout}keys_attach; '
               f'emulation CreateUartPtyTerminal "debuglink" @{terminal}; '
               "connector Connect sysbus.hostport debuglink; start")
    log_path = os.path.join(work, "renode.log")
    t0 = time.monotonic()
    deadline = t0 + a.timeout
    r = ec.Renode(a.renode, script, log_path, int(a.timeout) + 30)
    verdict = 2
    try:
        if not r.wait_terminal(terminal, deadline):
            raise ec.Check(f"Renode never opened PTY {terminal}")
        with rd.DebugLink(terminal, timeout=3.0) as d:
            v = None
            while v is None and time.monotonic() < deadline:
                try:
                    v = d.version()
                except rd.DebugLinkError:
                    pass
            if v is None:
                raise ec.Check("no GET_VERSION reply")
            print(f"board {v.get('board')} version {v.get('version')} codec stub: {'no' if a.no_codec else 'yes'}")
            screen = ec.Screen(r, frame_log, a.scale, deadline)
            ec.wait_settled(d, screen, 2500, 500, deadline)
            run = Run(d, r, screen, a.out, deadline, ec.KEY_BITS)
            feats = d.view(("features",))
            print("features: " + " ".join(f"{k}={val}" for k, val in feats.items()
                                          if "vfo" in k.lower() or "codec" in k.lower() or "dmr" in k.lower()))
            run.look("boot")

            d.vfo(True)
            run.settle()
            run.look("vfo-on")
            d.dmr_fields(mode="dmr", cc=7, ts=2)
            run.settle()
            run.wait(lambda v: v.get("mode") == "dmr" and v.get("dmr.cc") == "7" and v.get("dmr.ts") == "2",
                     "DMR VFO CC 7 slot 2")
            given = run.look("dmr-cc7-ts2")
            d.dmr_target(3107777, private=True)
            run.settle()
            run.wait(lambda v: v.get("dmr.target_id") == "3107777", "private target")
            given = run.look("target-3107777")
            if a.channel_op == "vfo-off":
                d.vfo(False)
            else:
                d.channel_step(1)
            run.settle(1500)
            run.wait(lambda v: v.get("vfo") == "0", "a channel")
            run.look("channel")
            d.vfo(True)
            run.settle(1500)
            back = run.wait(lambda v: v.get("vfo") == "1", "the VFO again") or run.d.view(SECTIONS)
            back = run.look("vfo-again")
            keep = ("dmr.cc", "dmr.ts", "dmr.target_id", "dmr.target_private")
            lost = [f"{k}: gave {given.get(k)}, got {back.get(k)}" for k in keep if given.get(k) != back.get(k)]
            if back.get("mode") != "dmr":
                lost.append(f"mode: gave dmr, got {back.get('mode')}")
            if lost:
                print("REPRODUCED: " + "; ".join(lost))
                verdict = 1
            else:
                print("NOT REPRODUCED: " + line(back))
                verdict = 0
    except ec.Check as e:
        print(f"FAIL: {e}")
    finally:
        clean = r.stop()
        left = subprocess.run(["pgrep", "-g", str(r.pgid)], capture_output=True, text=True).stdout.split()
        print(f"renode: stopped, processes left in its group: {len(left)}; wall {time.monotonic() - t0:.0f} s")
        if verdict == 2 or not clean or left:
            print(f"renode log {log_path}")
            verdict = 2
        else:
            for name in os.listdir(work):
                os.unlink(os.path.join(work, name))
            os.rmdir(work)
    return verdict


if __name__ == "__main__":
    sys.exit(main())
