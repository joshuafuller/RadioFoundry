#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Check boot, hostport framing and LCD output inside Renode, without a TCP terminal.

Uses the same boot setup as mduv380.resc. This supplements mduv380_test.py; it does
not prove the TCP connection used by conductor. Run after the build and flash-image
commands in mduv380.resc: python3 boards/mduv380/emu/console_test.py
"""

import os
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib

import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/emu"))
import emu_check
import flashimg

rd = emu_check.rd


def check(out, flashrepl=None):
    out.mkdir(parents=True, exist_ok=True)
    for name in ("reply.bin", "home.png", "menu.png", "key.png", "menu.rgb565", "key.rgb565", "frames.txt"):
        (out / name).unlink(missing_ok=True)
    commands = [f"$flashrepl=@{flashrepl}"] if flashrepl else []
    commands += [
        "include @boards/mduv380/emu/boot.resc",
        "include @tools/emu/peripherals/keys.py",
        "keys_attach",
        f"sysbus.hostport CreateFileBackend @{out}/reply.bin true",
        f"sysbus.lcd FrameLog @{out}/frames.txt",
        'emulation RunFor "4"',
    ]

    def request(cmd, seq, payload=b""):
        commands.extend([
            'python "for b in %r: self.Machine[\'sysbus.hostport\'].WriteChar(b)"'
            % list(rd.frame(cmd, seq, payload)),
            'emulation RunFor "0.5"',
        ])

    request(rd.VERSION, 1)
    request(rd.GET_VIEW, 2, bytes([rd.VIEW_SECTIONS.index("link")]))
    commands.append(f"sysbus.lcd SavePng @{out}/home.png 1")
    request(rd.KEY, 3, struct.pack("<BBBb", 0, rd.ACTIONS["green"], rd.PRESS, 0))
    request(rd.GET_VIEW, 4, bytes([rd.VIEW_SECTIONS.index("link")]))
    commands.extend([
        f"sysbus.lcd SavePng @{out}/menu.png 1",
        f"sysbus.lcd SavePanel @{out}/menu.rgb565",
        "keys_key 1 1",  # conductor KEY back down, then up after the firmware debounce interval
        'emulation RunFor "0.1"',
        "keys_key 1 0",
        'emulation RunFor "0.6"',
    ])
    request(rd.GET_VIEW, 5, bytes([rd.VIEW_SECTIONS.index("link")]))
    request(rd.GET_VIEW, 6, bytes([rd.VIEW_SECTIONS.index("core")]))
    commands.extend([f"sysbus.lcd SavePng @{out}/key.png 1",
                     f"sysbus.lcd SavePanel @{out}/key.rgb565", "quit"])
    script = out / "check.resc"
    script.write_text("\n".join(commands) + "\n")
    with (out / "renode.log").open("w") as log:
        result = subprocess.run(
            [emu_check.default_renode(), "--disable-xwt", "--console", "--plain", str(script)],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=180,
            env=dict(os.environ, XDG_CONFIG_HOME=str(out / "config")))
    assert result.returncode == 0, f"see {out}/renode.log"
    data = (out / "reply.bin").read_bytes()
    replies = {}
    while data:
        start = data.find(bytes([rd.SOF1, rd.SOF2]))
        assert start >= 0 and len(data) >= start + 8, data
        data = data[start:]
        size = int.from_bytes(data[4:6], "little")
        assert len(data) >= size + 8, data
        body, crc = data[2:6 + size], data[6 + size:8 + size]
        assert rd.crc16(body) == int.from_bytes(crc, "little"), "reply CRC"
        # The file backend includes echoed requests as well as firmware replies.
        if data[2] & 0x80:
            assert size > 0 and data[6] == rd.ST_OK, data
            replies[data[3]] = (data[2], data[7:6 + size])
        data = data[8 + size:]
    for seq, cmd in ((1, rd.VERSION), (2, rd.GET_VIEW), (3, rd.KEY), (4, rd.GET_VIEW),
                     (5, rd.GET_VIEW), (6, rd.GET_VIEW)):
        assert replies[seq][0] == cmd | 0x80, replies
    version = rd.parse_kv(replies[1][1].decode("ascii"))
    before = rd.parse_kv(replies[2][1][5:].decode("ascii"))
    after = rd.parse_kv(replies[4][1][5:].decode("ascii"))
    key = rd.parse_kv(replies[5][1][5:].decode("ascii"))
    core = rd.parse_kv(replies[6][1][5:].decode("ascii"))
    assert version["board"] == "MD-UV380", version
    assert before["ui.screen"] == "home", before
    assert after["ui.screen"] != "home", after
    assert key["ui.screen"] == "home", key
    if flashrepl:
        assert core["channel_name"] == "Group", core
    assert (out / "home.png").read_bytes() != (out / "menu.png").read_bytes(), "screen did not change"
    frames = (out / "frames.txt").read_text().splitlines()
    assert frames and frames[-1].startswith("FRAME radio="), frames
    crcs = []
    for name in ("home", "menu", "key"):
        png = (out / f"{name}.png").read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack_from(">II", png, 16) == (160, 128)
        if name != "home":
            crc = f'{zlib.crc32((out / f"{name}.rgb565").read_bytes()):08x}'
            assert any(emu_check.crc_of(frame) == crc for frame in frames), (name, crc, frames)
            crcs.append(crc)
    assert crcs[0] != crcs[1], "conductor KEY did not change the FRAME crc"
    print(f'GET_VERSION: board={version["board"]}')
    print(f'KEY green: {before["ui.screen"]} -> {after["ui.screen"]}')
    print(f'conductor KEY back (01 01, 01 00): {after["ui.screen"]} -> {key["ui.screen"]}, '
          f'FRAME crc {crcs[0]} -> {crcs[1]}')
    print(f'flash: {"template" if flashrepl else "legacy"}, channel_name={core["channel_name"]}')
    print(frames[-1])
    print(f"PASS: in-process hostport, home/menu PNGs; outputs {out}")


def main():
    out = ROOT / "build/mduv380/console-check"
    check(out)
    with tempfile.TemporaryDirectory(prefix="mduv380-flash-") as work:
        image = Path(work) / "flash.bin"
        flashimg.build_image(image, "dmr-group")
        template = ROOT / "boards/mduv380/emu/flash.repl"
        repl = Path(work) / "flash.repl"
        repl.write_text(template.read_text().replace("{image}", str(image)))
        check(out / "template", repl)


if __name__ == "__main__":
    main()
