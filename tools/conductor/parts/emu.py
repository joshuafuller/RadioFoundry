# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""An emulated radio in a world: an unchanged board release ELF in Renode, stepped by the conductor's grants.

A world radio is normally a host process (build/vhost/rfoundry_vhost.elf) that talks the vchan protocol over a
socket (channel.py). Here the radio is a real board binary that runs in Renode, a chip emulator. EmuChannel stands in
for Channel: the world sends it inputs and grants and reads back an outbox and a DONE, exactly as from a host radio,
so world.py and the air part (air.py) need no change.

    from tools.conductor.parts import emu
    config = Config(executable=vhost, radios=2, radio_executables=(emu.PLATFORMS[board]["elf"], None), end_us=...)
    world = World(config, channel_factory=emu.factory())

How time works. Renode's virtual time starts at 0 when the machine is made, and the world
starts the radio at its origin, so Renode time t is world time t + origin. A GRANT to T runs the machine with
`emulation RunFor` over Renode's monitor pipes for T minus the time it is at now, to the microsecond (RunFor
"0.000999" ends at exactly 999 us; the quantum stays what boards/md380/emu/boot.resc sets, 100 us). Then the machine is
paused at T, and every input the world sends next is applied at T. Example: the air part hands an AIR_IN over at a
burst's start + 29999 us (air.py, "When the AIR_IN is handed over"); the grant before it ends there, so the record
goes into the HR_C5000 model with `sysbus.spi2.c5000 AirIn "<hex>"` at exactly that Renode time.

What goes in and out.
  KEY (bit u8, down u8) and ENC (i32): keys_key / keys_encoder from tools/emu/peripherals/keys.py.
  AIR_IN: the chip model's AirIn. Its answer ("taken" or why not) is kept in EmuChannel.air_in.
  AIR_OUT: after each grant, TakeAirOut until it is empty. Each hex string is an AIR record (air.py RECORD). A record
    is put in the outbox of the grant whose span holds its burst start: a grant to T owns [previous end, T), a drain
    owns T itself. The model emits a record at its slot start, so a slot that starts exactly at T comes out of the
    RunFor that ends at T; it is held for the next grant, because air.py refuses a burst at or after the span end.
  FRAME: the LCD model writes one FRAME line per checkpoint (peripherals/lcd_st7735.cs). New lines become TRACE
    records (where the virtual radio's own FRAME lines go, platform/host/vframe.c), and the newest one also becomes
    FRAME records for frames.sock (parts/frames.py): the 72-byte vframe.h header, then the panel bytes in chunks. The
    panel is read with SavePanel at the grant end, so it is sent only when its crc is the line's crc (a later
    checkpoint in the same grant would have changed it).
  BYTES_IN, ETH_IN, VIEW, STORE: rejected immediately with EMU_UNSUPPORTED. The separate debug() probe is
    available only to explicit emulator tests; it does not implement the browser/HIL channel contract.

DONE carries next_due NEVER: the firmware's own timers are inside Renode and the world cannot see them. While two
radios share an air, the air part caps grants at 1 ms anyway (air.py grant_limit).

Voice needs a codec. The radio runs the stock AMBE+2 code in place from flash, which this repository never holds, so
without it PTT on a DMR channel is refused ("No codec: voice off") and nothing goes on the air. tools/emu/codec_stub.py
hooks the board's codec table in the unchanged binary: present() is true and the voice bits are zeros. Everything
else in a call (LC header, embedded LC, terminator, slot timing) is the real firmware.

Each EmuChannel gets a fresh flash image (tools/emu/flashimg.py) from the --storage and --radio-index arguments the
air part adds, in its own temporary folder, because the flash model writes through to the image. Renode runs in its
own process group under `timeout -s KILL`; close() quits it and kills the group if anything is left.

PLATFORMS reads boards/<id>/emu/config.json. Each board owns its Renode script, platform, flash template and key
layout. The board comes from the ELF name build/<board>/rfoundry_<board>.elf (board_of).

The radio's own view. The world's debug link requests are dropped (above), but a test can still read the view
deterministically: EmuChannel.debug(frame) writes a debug link frame into the host port console at the paused machine
time, through the monitor, and every byte the firmware writes back is kept in EmuChannel.host_out (a hook on the
console in the CPU thread, so the same in every run). save_png() saves the panel as it is at the paused time.

The hrc7000 boards run in XuanTie QEMU (HrcChannel): tools/emu/hrc7000_emu.py's bridge with the HR_C7000 modem
model, stepped by grants through tools/emu/hrc7000_lockstep.py. Radio time moves only by what the firmware does,
so two runs repeat exactly (tools/conductor/tests/test_emu_hrc7000.py).

This module has no install(): install_parts() loads every part for every world, and only worlds that ask for an
emulated radio should get one.
"""

from collections import deque
import importlib.util
import os
from pathlib import Path
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

from .. import world as world_module
from ..channel import Channel, ChannelError, DONE, Kind, Message, NEVER, PAYLOAD_MAX
from .air import Record
from .frames import HEADER as FRAME_HEADER

ROOT = Path(__file__).resolve().parents[3]
EMU = ROOT / "tools/emu"
from tools.emu.board_config import BOARDS as PLATFORMS
# tools/emu is not a package; load its guard by path
_spec = importlib.util.spec_from_file_location("renode_guard", EMU / "renode_guard.py")
renode_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(renode_guard)
GuardedPopen = renode_guard.GuardedPopen
RENODE = os.environ.get("RENODE") or os.path.expanduser("~/tools/renode/renode")
PROMPT = re.compile(rb"\r?\n?\((monitor|[A-Za-z0-9_-]+)\) $")
PANEL = (160, 128)                      # lcd_st7735.cs Width, Height; RGB565
FRAME_LINE = re.compile(r"FRAME radio=\d+ boot=\d+ id=(\d+) us=(\d+) crc=([0-9a-f]{8}) palette=- backlight=(\d+)")


def seconds(us):
    """Renode time text for a whole number of microseconds: 999 is "0.000999". No float rounding."""
    if type(us) is not int or us < 0:
        raise ValueError("EMU_BAD_TIME")
    return f"{us // 1_000_000}.{us % 1_000_000:06d}"


def board_of(elf):
    """The board of rfoundry_<board>.elf; EMU_UNKNOWN_BOARD otherwise."""
    name = Path(str(elf)).stem
    board = name[len("rfoundry_"):] if name.startswith("rfoundry_") else Path(str(elf)).parent.name
    if board not in PLATFORMS:
        raise ChannelError(f"EMU_UNKNOWN_BOARD: {elf} (one of {', '.join(sorted(PLATFORMS))})")
    return board


HOST_OUT = '''python """
_rf_console = self.Machine["sysbus.hostport"]
_rf_host_out = open(r'{path}', 'ab')
def _rf_keep(value):
    _rf_host_out.write(chr(value))
    _rf_host_out.flush()
_rf_console.CharReceived += _rf_keep
"""
'''


def option(args, name, default):
    args = list(map(str, args))
    for i, arg in enumerate(args[:-1]):
        if arg == name:
            return args[i + 1]
    return default


def check_emulator_listeners(pid):
    """Fail closed unless this Linux process and every descendant own no listening TCP socket."""
    try:
        pending, seen = [pid], set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            proc = Path(f"/proc/{current}")
            for task in (proc / "task").iterdir():
                pending.extend(map(int, (task / "children").read_text().split()))
            sockets = set()
            for fd in (proc / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except FileNotFoundError:     # a descriptor closed during the snapshot
                    continue
                if target.startswith("socket:["):
                    sockets.add(target[8:-1])
            for family in ("tcp", "tcp6"):
                for line in (proc / "net" / family).read_text().splitlines()[1:]:
                    fields = line.split()
                    if fields[3] == "0A" and fields[9] in sockets:
                        raise ChannelError(f"EMU_UNSAFE_LISTENER: pid={current} {family} {fields[1]}")
    except (OSError, ValueError, IndexError) as error:
        raise ChannelError(f"EMU_LISTENER_CHECK_FAILED: {error}") from error


class Monitor:
    """Renode's monitor over inherited pipes: one command, one reply up to the next prompt."""

    def __init__(self, process, deadline):
        self.input, self.output = process.stdin, process.stdout
        self.buffer = b""
        try:
            self.read(deadline)
        except BaseException:
            self.close()
            raise

    def read(self, deadline=None):
        deadline = time.monotonic() + 120 if deadline is None else deadline
        while True:
            match = PROMPT.search(self.buffer)
            if match:
                text, self.buffer = self.buffer[:match.start()], self.buffer[match.end():]
                return text.decode("utf-8", "replace")
            ready, _, _ = select.select([self.output], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                raise ChannelError("EMU_MONITOR_TIMEOUT")
            data = os.read(self.output.fileno(), 65536)
            if not data:
                raise ChannelError("EMU_MONITOR_CLOSED")
            self.buffer += data

    def commands(self, *lines):
        """One reply per line, the text after the echoed command. One line at a time: with a second command already
        sent, the monitor prints no prompt after the first one, so the replies cannot be told apart."""
        replies = []
        for line in lines:
            self.input.write((line + "\n").encode())
            self.input.flush()
            text = self.read().replace("\r", "")
            first, _, rest = text.partition("\n")
            # The console inserts spaces when echoing a command across its display width.
            echoed = "".join(first.split()) == "".join(line.split())
            replies.append(rest.strip() if echoed else text.strip())
        return replies

    def command(self, line):
        return self.commands(line)[0]

    def close(self):
        try:
            self.input.close()
        except OSError:
            pass
        self.output.close()


class EmuChannel:
    """A Channel stand-in for one emulated radio (module text). Setup failures raise ChannelError.
    board: a PLATFORMS key; None picks it from the ELF's name (board_of)."""

    supported_inputs = frozenset((Kind.KEY, Kind.ENC, Kind.AIR_IN))

    def __init__(self, elf, *args, renode=RENODE, limit_s=1800, keep=None, board=None):
        self.elf = str(elf)
        self.board = board or board_of(elf)
        if self.board not in PLATFORMS:
            raise ChannelError(f"EMU_UNKNOWN_BOARD: {self.board}")
        self.platform = PLATFORMS[self.board]
        self.chip = self.platform["chip"]
        self.inbox = deque()
        self.now = 0                    # Renode time, us; the machine is paused here between grants
        self.held = []                  # AIR records whose burst starts at or after the last grant end
        self.air_out, self.air_in, self.dropped = [], [], {}
        self.frame_lines = 0
        self.process = self.monitor = self.log = None
        self.left = False
        self.kept = None                # (host_out, chip_trace) read at close(), before the work folder goes
        self.work = tempfile.mkdtemp(prefix="rf-emu-", dir=keep)
        self.log_path = os.path.join(self.work, "renode.log")
        self.frame_log = os.path.join(self.work, "frames.txt")
        self.host_path = os.path.join(self.work, "host_out.bin")
        self.trace_path = os.path.join(self.work, "chip.txt")
        self.radio_id = int(option(args, "--radio-id", 1))
        self.boot_id = int(option(args, "--boot-id", 1))
        try:
            self._boot(renode, limit_s, option(args, "--storage", "dmr-group"),
                       int(option(args, "--radio-index", 1)))
        except BaseException:
            self.close()
            raise
        self.inbox.append(Message(Kind.DONE, DONE.pack(NEVER, 0), 0))

    def _boot(self, renode, limit_s, storage, radio_index):
        sys.path.insert(0, str(EMU))
        try:
            import flashimg
        finally:
            sys.path.remove(str(EMU))
        image = os.path.join(self.work, "flash.bin")
        flashimg.build_image(image, storage, radio_index)
        flash_repl = os.path.join(self.work, "flash.repl")
        Path(flash_repl).write_text((Path(self.platform["directory"]) / self.platform["flash"]).read_text().replace("{image}", image))
        self.log = open(self.log_path, "a")
        self.process = GuardedPopen(
            ["timeout", "-s", "KILL", str(limit_s), renode, "--disable-xwt", "--plain", "--console", "-P", "-1",
             "--hide-log"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, cwd=ROOT)
        self.monitor = Monitor(self.process, time.monotonic() + 20)
        check_emulator_listeners(self.process.pid)
        platform = self.platform
        directory = Path(platform["directory"])
        host_out = os.path.join(self.work, "host_out.resc")     # a multi-line python block goes in from a file
        Path(host_out).write_text(HOST_OUT.format(path=self.host_path))
        setup = [f"logFile @{self.log_path}", f"$elf=@{self.elf}", "$tcp=false",
                 f"$repl=@{directory / platform['repl']}", f"$flashrepl=@{flash_repl}",
                 f"include @{directory / platform['resc']}", f"include @{EMU / 'codec_stub.py'}", "setup_codec_stub",
                 f"include @{EMU / 'peripherals/keys.py'}", f'keys_layout "{self.board}"', "keys_attach",
                 f"{self.chip} TracePath @{self.trace_path}", f"include @{host_out}",
                 f"lcd RadioId {self.radio_id}", f"lcd BootId {self.boot_id}", f"lcd FrameLog @{self.frame_log}"]
        for line in setup:
            reply = self.monitor.command(line)
            if re.search(r"\b(error|exception|no such)\b", reply, re.I) and "There is no" not in reply:
                raise ChannelError(f"EMU_SETUP_FAILED: {line}: {reply}")
        check_emulator_listeners(self.process.pid)
    # --- the Channel surface ---

    def send(self, message):
        if message.kind not in self.supported_inputs | {Kind.GRANT, Kind.DRAIN}:
            raise ChannelError(f"EMU_UNSUPPORTED:{message.kind.name}")
        try:
            self._send(message)
        except (OSError, ChannelError, ValueError) as error:
            self.inbox.append(Message(Kind.RESTART_REASON, f"emu: {error}".encode()[:PAYLOAD_MAX], self.now))

    def _send(self, message):
        kind = message.kind
        if kind in (Kind.GRANT, Kind.DRAIN):
            self.inbox.append(message)      # run in receive(), on the world's collector thread
            return
        if message.time_us != self.now:
            raise ChannelError(f"EMU_INPUT_TIME: {kind.name} at {message.time_us}, machine at {self.now}")
        if kind == Kind.KEY:
            if len(message.payload) != 2:
                raise ChannelError("EMU_BAD_KEY")
            self._ok(self.monitor.command(f"keys_key {message.payload[0]} {message.payload[1]}"))
        elif kind == Kind.ENC:
            detents, = struct.unpack("<i", message.payload)
            self._ok(self.monitor.command(f"keys_encoder {detents}"))
        elif kind == Kind.AIR_IN:
            reply = self.monitor.command(f'{self.chip} AirIn "{message.payload.hex()}"')
            self.air_in.append((self.now, message.source, reply))
        else:
            self.dropped[kind.name] = self.dropped.get(kind.name, 0) + 1

    @staticmethod
    def _ok(reply):
        if re.search(r"\b(error|exception)\b", reply, re.I):
            raise ChannelError(f"EMU_MONITOR: {reply}")

    def receive(self):
        if not self.inbox:
            raise ChannelError("EMU_NOTHING_TO_RECEIVE")
        message = self.inbox.popleft()
        if message.kind not in (Kind.GRANT, Kind.DRAIN):
            return message
        try:
            self.inbox.extendleft(reversed(self._run(message.time_us, message.kind == Kind.DRAIN)))
        except (OSError, ChannelError, ValueError) as error:
            self.inbox.appendleft(Message(Kind.RESTART_REASON, f"emu: {error}".encode()[:PAYLOAD_MAX], self.now))
        return self.inbox.popleft()

    def _run(self, target, drain):
        if target < self.now:
            raise ChannelError(f"EMU_PAST_GRANT: {target} < {self.now}")
        lines = [f'emulation RunFor "{seconds(target - self.now)}"'] if target > self.now else []
        replies = self.monitor.commands(*lines, f"{self.chip} TakeAirOut")
        self.now = target
        for reply in replies[:-1]:
            self._ok(reply)
        records, last = [], replies[-1]
        while last.strip('"'):
            records.append(Record.decode(bytes.fromhex(last.strip('"'))))
            last = self.monitor.command(f"{self.chip} TakeAirOut")
        self.held.extend(records)
        out = [record for record in self.held if record.t_us < target or drain and record.t_us == target]
        self.held = [record for record in self.held if record not in out]
        outbox = [Message(Kind.AIR_OUT, record.encode(), record.t_us) for record in out]
        self.air_out.extend(out)
        outbox.extend(self._frames())
        outbox.append(Message(Kind.DONE, DONE.pack(NEVER, 0), target))
        return outbox

    def _frames(self):
        try:
            with open(self.frame_log) as f:
                lines = [line.strip() for line in f if line.startswith("FRAME ")]
        except FileNotFoundError:
            return []
        new, self.frame_lines = lines[self.frame_lines:], len(lines)
        out = [Message(Kind.TRACE, line.encode(), int(FRAME_LINE.match(line)[2])) for line in new
               if FRAME_LINE.match(line)]
        if out:
            out.extend(self._panel(FRAME_LINE.match(new[-1])))
        return out

    def _panel(self, line):
        path = os.path.join(self.work, "panel.bin")
        self._ok(self.monitor.command(f"lcd SavePanel @{path}"))
        panel = Path(path).read_bytes()
        frame_id, us, crc, backlight = int(line[1]), int(line[2]), int(line[3], 16), int(line[4])
        width, height = PANEL
        if zlib.crc32(panel) != crc or len(panel) != width * height * 2:
            return []           # a later checkpoint changed the panel in this grant; its line still went out
        chunk = PAYLOAD_MAX - FRAME_HEADER.size
        return [Message(Kind.FRAME, FRAME_HEADER.pack(self.radio_id, self.boot_id, frame_id, us, crc, len(panel),
                                                      offset, width, height, 16, min(backlight, 100), 0, 0,
                                                      *([0] * 16)) + panel[offset:offset + chunk], us)
                for offset in range(0, len(panel), chunk)]

    def debug(self, frame):
        """Write one debug link frame (bytes) into the host port at the paused machine time. The firmware reads it in
        the next grant; its reply lands in host_out()."""
        self._ok(self.monitor.command(f'python "for v in {list(frame)}: _rf_console.WriteChar(v)"'))

    def host_out(self):
        """Every byte the firmware wrote to its host port so far (kept after close())."""
        if self.kept is not None:
            return self.kept[0]
        try:
            return Path(self.host_path).read_bytes()
        except FileNotFoundError:
            return b""

    def save_png(self, path, scale=2):
        """The panel now, as a PNG (the LCD model's SavePng), scale pixels per panel pixel."""
        self._ok(self.monitor.command(f"lcd SavePng @{os.path.abspath(path)} {scale}"))

    def chip_trace(self):
        """The DMR chip model's trace lines (TracePath), for checks: AIR_IN, REPORT, CFG lines (kept after close())."""
        if self.kept is not None:
            return self.kept[1]
        try:
            return Path(self.trace_path).read_text().splitlines()
        except FileNotFoundError:
            return []

    def virtual_us(self):
        """Renode's own clock, for checks: Elapsed Virtual Time in microseconds."""
        text = self.monitor.command("emulation GetTimeSourceInfo")
        h, m, s = re.search(r"Elapsed Virtual Time: (\d+):(\d+):(\d+\.\d+)", text).groups()
        whole, _, frac = s.partition(".")
        return ((int(h) * 60 + int(m)) * 60 + int(whole)) * 1_000_000 + int((frac + "000000")[:6])

    def stderr_text(self):
        try:
            return Path(self.log_path).read_text(errors="replace")[-4000:].strip()
        except OSError:
            return ""

    def error(self, reason):
        return ChannelError(reason)

    def close(self):
        """Quit Renode, kill its process group if anything is left, and remove the work folder. Returns ()."""
        if self.monitor is not None:
            try:
                self.monitor.input.write(b"quit\n")
                self.monitor.input.flush()
            except OSError:
                pass
            self.monitor.close()
            self.monitor = None
        if self.process is not None:
            try:
                self.process.wait(30)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(10)
            self.left = alive(self.process.pid)
            self.process = None
        if self.log is not None:
            self.log.close()
        if self.work and self.kept is None:
            self.kept = (self.host_out(), self.chip_trace())
        if self.work and os.environ.get("RF_EMU_KEEP") is None:
            shutil.rmtree(self.work, ignore_errors=True)
        return ()


class HrcChannel:
    """A Channel stand-in for an hrc7000 (C-SKY) board ELF in XuanTie QEMU: tools/emu/hrc7000_emu.py's bridge with the
    HR_C7000 modem model (hrc7000_modem.py) and the SPI0 codeplug flash (hrc7000_flash.py), run in a thread of this
    process and stepped by tools/emu/hrc7000_lockstep.py. The same surface as EmuChannel:

    - time: the bridge's one radio clock (hrc7000_soc.VirtualClock, radio time = world time - origin) runs to each
      grant end and pauses there. It moves only by what the firmware does (hrc7000_lockstep.py), so a run repeats;
    - AIR_OUT: the modem's records (Modem.sent), each in the outbox of the grant whose span holds its burst start, as
      EmuChannel does (a burst starting exactly at the grant end waits for the next grant, a drain owns it);
    - AIR_IN: Modem.air_in at the hand-over time: deframed at once, the receive timing from the slot end;
    - debug(frame) / host_out(): a debug link frame into the firmware's host port at the paused time, and every byte
      it wrote back;
    - KEY, ENC, BYTES_IN, ETH_IN, VIEW, STORE: rejected immediately with EMU_UNSUPPORTED.

    The flash is the --storage fixture (default dmr-group) and --radio-index member the air part passes."""

    supported_inputs = frozenset((Kind.AIR_IN,))

    def __init__(self, elf, *args, limit_s=1800):
        self.elf = str(elf)
        self.board = board_of(elf)
        if str(EMU) not in sys.path:
            sys.path.insert(0, str(EMU))
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(1, str(ROOT / "tools"))
        import hrc7000_emu
        from hrc7000_lockstep import Lockstep
        self.ctl = Lockstep()
        self.inbox = deque()
        self.now = 0
        self.held, self.air_out, self.air_in, self.dropped = [], [], [], {}
        self.taken = 0                  # Modem.sent records already looked at
        self.left = False
        self.result = None
        argv = ["--board", self.board, "--elf", self.elf, "--boot", "--modem", "--time-hooks", "--scale", "0",
                "--flash", option(args, "--storage", "dmr-group"), "--radio-index", option(args, "--radio-index", "1"),
                "--timeout", str(limit_s), "--max-stops", str(1 << 40), "--hang-s", "60"]

        def run():
            try:
                self.result = hrc7000_emu.main(argv, control=self.ctl)
            except BaseException as error:  # noqa: BLE001 - kept for the reason
                self.result = repr(error)
                self.ctl.done()
        self.thread = threading.Thread(target=run, name=f"hrc7000-{self.board}", daemon=True)
        self.thread.start()
        try:
            self.ctl.wait_paused()
            check_emulator_listeners(self.ctl.process.pid)
        except ChannelError:
            self.close()
            raise
        except RuntimeError as error:
            self.close()
            raise ChannelError(f"EMU_QEMU_FAILED: {error}: {self.result}") from error
        self.process = self.ctl.process
        self.inbox.append(Message(Kind.DONE, DONE.pack(NEVER, 0), 0))

    @property
    def modem(self):
        return self.ctl.modem

    def send(self, message):
        if message.kind not in self.supported_inputs | {Kind.GRANT, Kind.DRAIN}:
            raise ChannelError(f"EMU_UNSUPPORTED:{message.kind.name}")
        kind = message.kind
        if kind in (Kind.GRANT, Kind.DRAIN):
            self.inbox.append(message)
            return
        if message.time_us != self.now:
            self.inbox.append(Message(Kind.RESTART_REASON, f"emu: EMU_INPUT_TIME: {kind.name} at {message.time_us}, "
                                      f"machine at {self.now}".encode()[:PAYLOAD_MAX], self.now))
            return
        if kind == Kind.AIR_IN:
            record = Record.decode(message.payload)
            self.ctl.call(lambda: self.modem.air_in(record))
            self.air_in.append((self.now, message.source, record))
        else:
            self.dropped[kind.name] = self.dropped.get(kind.name, 0) + 1

    def receive(self):
        if not self.inbox:
            raise ChannelError("EMU_NOTHING_TO_RECEIVE")
        message = self.inbox.popleft()
        if message.kind not in (Kind.GRANT, Kind.DRAIN):
            return message
        try:
            self.inbox.extendleft(reversed(self._run(message.time_us, message.kind == Kind.DRAIN)))
        except (RuntimeError, ChannelError, ValueError) as error:
            self.inbox.appendleft(Message(Kind.RESTART_REASON, f"emu: {error}".encode()[:PAYLOAD_MAX], self.now))
        return self.inbox.popleft()

    def _run(self, target, drain):
        if target < self.now:
            raise ChannelError(f"EMU_PAST_GRANT: {target} < {self.now}")
        self.ctl.run_to(target / 1e6)
        self.now = target
        sent = self.modem.sent
        self.held.extend(Record.decode(wire) for _, wire, _ in sent[self.taken:])
        self.taken = len(sent)
        out = [record for record in self.held if record.t_us < target or drain and record.t_us == target]
        self.held = [record for record in self.held if record not in out]
        self.air_out.extend(out)
        return [Message(Kind.AIR_OUT, record.encode(), record.t_us) for record in out] + \
            [Message(Kind.DONE, DONE.pack(NEVER, 0), target)]

    def debug(self, frame):
        """Write one debug link frame (bytes) into the host port at the paused machine time; the firmware reads it in
        the next grant and its reply lands in host_out()."""
        host = self.ctl.host

        def put():
            with host.lock:
                host.rx += bytes(frame)
            host.data.set()
        self.ctl.call(put)

    def host_out(self):
        return bytes(self.ctl.host_out)

    def received(self):
        """What the modem did with each burst it was given: (radio s, record, RX_TYPE_INFO or None, text)."""
        return list(self.modem.received) if self.modem else []

    def stderr_text(self):
        return str(self.result or "")

    def error(self, reason):
        return ChannelError(reason)

    def close(self):
        """Stop the bridge (it kills its QEMU process group on the way out) and wait for its thread. Returns ()."""
        self.ctl.close()
        self.thread.join(60)
        process = self.ctl.process
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.left = alive(process.pid)
        return ()


def alive(pgid):
    """True while any process of this group is left."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def factory(emulated=None, *, renode=RENODE, channels=None):
    """A World channel_factory: an executable in emulated runs as an EmuChannel, any other as a real Channel.
    channels, when a list, collects every EmuChannel made (tests read air_in and air_out from it).

    The world asks each executable for --help (world._knows_option) and would run the ARM ELF through qemu-user,
    which crashes; a blank answer is what that probe returns anyway, so it is set here up front."""
    if emulated is None:
        emulated = (config["elf"] for config in PLATFORMS.values())
    emulated = {str(path) for path in emulated}
    for path in emulated:
        try:
            world_module._OPTIONS.setdefault((path, os.stat(path).st_mtime_ns), "")
        except OSError:
            pass

    def make(executable, *args):
        if str(executable) in emulated:
            if PLATFORMS[board_of(executable)].get("engine") == "qemu":
                channel = HrcChannel(executable, *args)
            else:
                channel = EmuChannel(executable, *args, renode=renode)
            if channels is not None:
                channels.append(channel)
            return channel
        return Channel(executable, *args)
    return make
