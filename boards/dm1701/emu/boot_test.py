#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Boot the release ELF through dm1701.resc and the unchanged hostport.py hooks.

python3 boards/dm1701/emu/boot_test.py --out /tmp/dm1701-check
python3 boards/dm1701/emu/boot_test.py --tcp --out /tmp/dm1701-tcp

The first check drives the virtual console directly, checks the I2C model, asks
GET_VERSION and GET_VIEW, and presses Select through the debug link. The second
checks GET_VERSION through the TCP terminal used by external clients. Neither
opens hardware. TCP support is required for the second check; failure is an error.
"""
import argparse
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
import rfoundry_debug as rd  # noqa: E402
sys.path.insert(0, str(ROOT / "tools" / "emu"))
import flashimg  # noqa: E402


REGISTERS = '''python """
from System import Array, Byte
rf = self.Machine["sysbus.i2c3.rf"]
def write(*data):
    rf.Write(Array[Byte](data))
def read(address):
    write(address)
    rf.FinishTransmission()
    return list(rf.Read(2))
assert read(0x1B) == [0, 255]
assert read(0x1C) == [0, 0]
write(0x04, 0x12, 0x34)
write(0x7F, 0, 1)
write(0x04, 0xAB, 0xCD)
assert read(0x04) == [0xAB, 0xCD]
write(0x7F, 0, 0)
assert read(0x04) == [0x12, 0x34]
write(0x7F, 0, 2)
assert read(0x7F) == [0, 0]
write(0x04, 0xFF)
write(0x80, 0xFF, 0xFF)
assert read(0x04) == [0x12, 0x34]
write(0x30, 0, 1)
assert read(0x04) == [0, 0]
assert read(0x1B) == [0, 255]
rf.Reset()
print("AT1846S registers: PASS")
console = self.Machine["sysbus.hostport"]
def receive(value, output=replies_file):
    output.write(chr(value))
    output.flush()
console.CharReceived += receive
"""
'''


def flash_repl(out):
    """A fresh dmr-group image in out and dm1701.flash.repl filled in for it (as tools/emu/emu_check.py does)"""
    image = out / "flash.bin"
    image.unlink(missing_ok=True)
    Path(str(image) + ".security").unlink(missing_ok=True)
    flashimg.build_image(image, "dmr-group", 1)
    repl = out / "flash.repl"
    repl.write_text((ROOT / "boards/dm1701/emu/flash.repl").read_text().replace("{image}", str(image)))
    return repl


def console_check(out, renode, env):
    script = (f"$tcp=false\n$flashrepl=@{flash_repl(out)}\ninclude @boards/dm1701/emu/boot.resc\n"
              f'python "replies_file = open(r\'{out}/replies.bin\', \'wb\')"\n' + REGISTERS)
    script += f'lcd FrameLog @{out}/frames.txt\nemulation RunFor "3"\n'
    section = bytes((rd.VIEW_SECTIONS.index("link"),))
    requests = [(rd.VERSION, b""), (rd.GET_VIEW, section),
                (rd.KEY, bytes((0, rd.ACTIONS["select"], rd.PRESS, 0))), (rd.GET_VIEW, section)]
    for sequence, (command, payload) in enumerate(requests, 1):
        packet = list(rd.frame(command, sequence, payload))
        script += f'python "for value in {packet}: console.WriteChar(value)"\nemulation RunFor "0.5"\n'
        if sequence in (2, 4):
            name = "home" if sequence == 2 else "selected"
            script += f'lcd SavePng @{out}/{name}.png\nlcd SavePanel @{out}/{name}.rgb565\n'
    script += 'python "replies_file.close()"\nquit\n'
    resc = out / "check.resc"
    resc.write_text(script)
    result = subprocess.run([renode, "--disable-xwt", "--console", "--plain", str(resc)],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    log = result.stdout + result.stderr
    (out / "renode.log").write_text(log)
    assert result.returncode == 0 and "AT1846S registers: PASS" in log, log[-5000:]
    assert (out / "replies.bin").exists(), log[-5000:]
    data = (out / "replies.bin").read_bytes()
    replies = {}
    while data:
        assert data[:2] == b"\xA5\x5A", data
        command, sequence, length = struct.unpack_from("<BBH", data, 2)
        packet, data = data[:8 + length], data[8 + length:]
        assert len(packet) == length + 8 and rd.crc16(packet[2:-2]) == struct.unpack("<H", packet[-2:])[0]
        if command < 0x80:  # VirtualConsole echoes the input as well as the firmware's replies.
            assert packet == rd.frame(requests[sequence - 1][0], sequence, requests[sequence - 1][1])
            continue
        assert packet[6] == rd.ST_OK and command == requests[sequence - 1][0] | 0x80, packet
        replies[sequence] = packet[7:-2]
    assert set(replies) == {1, 2, 3, 4}, f"missing replies: {replies}; see {out}/renode.log"
    version = rd.parse_kv(replies[1].decode("ascii"))
    before = rd.parse_kv(replies[2][5:].decode("ascii"))
    after = rd.parse_kv(replies[4][5:].decode("ascii"))
    assert version["board"] == "DM-1701", version
    assert before["ui.screen"] == "home", before
    assert after["ui.screen"] != before["ui.screen"], after
    assert (out / "home.rgb565").read_bytes() != (out / "selected.rgb565").read_bytes()
    for name in ("home", "selected"):
        png = (out / f"{name}.png").read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack_from(">II", png, 16) == (160, 128)
    lines = (out / "frames.txt").read_text().splitlines()
    assert lines and all(line.startswith("FRAME radio=1 boot=1 ") for line in lines), lines
    print("AT1846S registers: PASS")
    print(f"GET_VERSION board={version['board']} uptime_ms={version['uptime_ms']}")
    print(f"GET_VIEW ui.screen={before['ui.screen']} -> {after['ui.screen']} after debug-link Select")
    print(lines[-1])
    print(f"PNG home={out}/home.png selected={out}/selected.png (160x128 panel, first 8 rows hidden on radio)")


def tcp_check(out, renode, env):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with (out / "renode.log").open("w") as log:
        proc = subprocess.Popen([renode, "--disable-xwt", "--console", "--plain", "-e",
                                 f"$tcp=true; $port={port}; $flashrepl=@{flash_repl(out)}; include @boards/dm1701/emu/boot.resc; start"],
                                cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=log,
                                stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                assert proc.poll() is None, f"Renode exited; see {out}/renode.log"
                try:
                    with rd.DebugLink(f"socket://127.0.0.1:{port}", timeout=2) as link:
                        version = link.version()
                        assert version.get("board") == "DM-1701", version
                        print(f"TCP GET_VERSION board={version['board']} uptime_ms={version['uptime_ms']}")
                        return
                except (OSError, rd.DebugLinkError):
                    time.sleep(0.1)
            raise AssertionError(f"no TCP GET_VERSION reply; see {out}/renode.log")
        finally:
            if proc.poll() is None:
                proc.stdin.write("quit\n")
                proc.stdin.flush()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            proc.stdin.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tcp", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "build/emu-dm1701")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "frames.txt").unlink(missing_ok=True)
    env = dict(os.environ, XDG_CONFIG_HOME=str(out / "config"))
    renode = os.environ.get("RENODE", str(Path.home() / "tools/renode/renode"))
    (tcp_check if args.tcp else console_check)(out, renode, env)
    print("dm1701_test: PASS")


if __name__ == "__main__":
    main()
