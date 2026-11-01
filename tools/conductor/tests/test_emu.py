# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Emulated radios: grant ownership against a fake monitor, then live calls with each Renode board.

HR_C5000 and HR_C6000 boards are discovered from boards/<id>/emu/config.json. Each live test runs the unchanged
ELF next to a virtual radio on one air twice, checking identical deliveries. The HR_C6000 tests also read the
emulated radio's view during and after the reply. Live tests skip without Renode or the board ELF.
"""

import json
import os
import select
import signal
import subprocess
import sys
import time
import struct

import pytest

from tools import rfoundry_debug as rd, vradio
from tools.conductor.channel import DONE, Kind, Message, NEVER
from tools.conductor.checks.interaction_check import DMR, DMR_CALL, read_view
from tools.conductor.conductor import install_parts
from tools.conductor.parts import dlcache, emu
from tools.conductor.parts.air import Record
from tools.conductor.world import Config, World

PTT_DOWN_US, PTT_UP_US, VIEW_US = 3_000_000, 4_500_000, 4_300_000
REPLY_DOWN_US, REPLY_UP_US, END_US = 5_000_000, 5_600_000, 6_000_000     # radio 2 answers: air into the model


def test_seconds_is_exact_microseconds():
    assert emu.seconds(999) == "0.000999"
    assert emu.seconds(29_999) == "0.029999"
    assert emu.seconds(3_059_999) == "3.059999"
    with pytest.raises(ValueError):
        emu.seconds(-1)


def test_monitor_removes_wrapped_command_echo():
    import io
    monitor = emu.Monitor.__new__(emu.Monitor)
    monitor.input = io.BytesIO()
    command = 'sysbus.chip AirIn "' + '00' * 80 + '"'
    monitor.read = lambda: command[:80] + ' ' + command[80:160] + ' ' + command[160:] + '\r\ntaken\r\n'
    assert monitor.command(command) == "taken"


@pytest.mark.parametrize("address", [None, "0.0.0.0", "127.0.0.1", "::", "::1"])
@pytest.mark.parametrize("descendant", [False, True])
def test_emulator_listener_guard(address, descendant):
    script = """
import socket, sys
address = sys.argv[1]
if address != 'none':
    listener = socket.socket(socket.AF_INET6 if ':' in address else socket.AF_INET)
    listener.bind((address, 0))
    listener.listen()
print('ready', flush=True)
sys.stdin.buffer.read()
"""
    args = [sys.executable, "-c", script, address or "none"]
    if descendant:
        wrapper = """
import subprocess, sys
child = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE)
try:
    sys.stdin.buffer.read()
finally:
    child.stdin.close()
    child.wait(5)
"""
        args = [sys.executable, "-c", wrapper, *args]
    process = emu.GuardedPopen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert select.select([process.stdout], [], [], 10)[0], "child did not start"
        assert process.stdout.readline() == b"ready\n"
        if address:
            with pytest.raises(emu.ChannelError, match="EMU_UNSAFE_LISTENER"):
                emu.check_emulator_listeners(process.pid)
        else:
            emu.check_emulator_listeners(process.pid)
    finally:
        process.stdin.close()
        try:
            process.wait(10)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(10)
            process.stdout.close()
    assert process.returncode == 0
    assert not emu.alive(process.pid)


def test_monitor_close_after_child_exits():
    process = emu.GuardedPopen(
        [sys.executable, "-c", "print('(monitor) ', end='', flush=True)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        monitor = emu.Monitor(process, time.monotonic() + 10)
        process.wait(10)
        monitor.input.write(b"quit\n")
        with pytest.raises(BrokenPipeError):
            monitor.input.flush()
        monitor.close()
        assert process.stdin.closed and process.stdout.closed
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(10)


def test_emulator_listener_guard_fails_closed(monkeypatch):
    read = emu.Path.read_text

    def denied(path, *args, **kwargs):
        if path.name == "tcp6":
            raise PermissionError("cannot inspect TCP listeners")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(emu.Path, "read_text", denied)
    with pytest.raises(emu.ChannelError, match="EMU_LISTENER_CHECK_FAILED"):
        emu.check_emulator_listeners(os.getpid())


@pytest.mark.parametrize("when", ["prompt", "setup"])
def test_emulator_listener_guard_constructor_cleanup(monkeypatch, when):
    script = """
import socket, sys
listener = socket.socket()
def listen():
    listener.bind(('0.0.0.0', 0))
    listener.listen()
if sys.argv[1] == 'prompt':
    listen()
sys.stdout.write('(monitor) ')
sys.stdout.flush()
for line in sys.stdin:
    if line.strip() == 'quit':
        break
    if sys.argv[1] == 'setup' and line.startswith('lcd FrameLog'):
        listen()
    sys.stdout.write('\\n(monitor) ')
    sys.stdout.flush()
"""
    made = []
    popen = emu.GuardedPopen

    def launch(args, **kwargs):
        process = popen([sys.executable, "-c", script, when], **kwargs)
        made.append(process)
        return process

    monkeypatch.setattr(emu, "GuardedPopen", launch)
    config = next(c for c in emu.PLATFORMS.values() if c["engine"] == "renode")
    with pytest.raises(emu.ChannelError, match="EMU_UNSAFE_LISTENER"):
        emu.EmuChannel(config["elf"])
    assert len(made) == 1
    assert made[0].returncode == 0
    assert not emu.alive(made[0].pid)


@pytest.mark.skipif(not os.path.exists(emu.RENODE), reason="needs Renode")
def test_emulator_monitor_pipe_has_no_tcp_listener(tmp_path):
    with (tmp_path / "stderr.log").open("wb") as log:
        process = emu.GuardedPopen(
            ["timeout", "-s", "KILL", "60", emu.RENODE, "--disable-xwt", "--plain", "--console", "-P", "-1",
             "--hide-log"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
        monitor = None
        try:
            monitor = emu.Monitor(process, time.monotonic() + 20)
            emu.check_emulator_listeners(process.pid)
            assert monitor.command('mach create "private"') == ""
            value = "a" * 180
            assert monitor.command(f'python "print(\'{value}\')"') == value
            assert "Elapsed Virtual Time:" in monitor.command("emulation GetTimeSourceInfo")
            emu.check_emulator_listeners(process.pid)
            monitor.input.write(b"quit\n")
            monitor.input.flush()
            process.wait(10)
        finally:
            if monitor:
                monitor.close()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(10)
        assert process.returncode == 0


class FakeMonitor:
    """Answers RunFor by moving its clock and TakeAirOut from a list of (slot start, record) the model would emit."""

    def __init__(self, records):
        self.now, self.records, self.lines = 0, list(records), []

    def commands(self, *lines):
        return [self.command(line) for line in lines]

    def command(self, line):
        self.lines.append(line)
        if line.startswith("emulation RunFor"):
            whole, frac = line.split('"')[1].split(".")
            self.now += int(whole) * 1_000_000 + int(frac)
            return ""
        if line.endswith("TakeAirOut"):
            for item in self.records:
                if item.t_us <= self.now:
                    self.records.remove(item)
                    return f'"{item.encode().hex().upper()}"'
            return '""'
        return ""


def fake_channel(records):
    channel = emu.EmuChannel.__new__(emu.EmuChannel)
    channel.inbox, channel.now, channel.held, channel.chip = [], 0, [], "sysbus.spi2.c5000"
    channel.air_out, channel.air_in, channel.dropped, channel.frame_lines = [], [], {}, 0
    channel.frame_log = "/nonexistent/frames.txt"
    channel.kept = None
    channel.monitor = FakeMonitor(records)
    from collections import deque
    channel.inbox = deque()
    return channel


def record(t_us):
    return Record(1, 16, t_us, 1, 0, 1, 0, bytes(9), bytes(27))


def grant(channel, kind, target):
    channel.send(Message(kind, time_us=target))
    out = []
    while True:
        message = channel.receive()
        out.append(message)
        if message.kind == Kind.DONE:
            return out


def test_a_burst_at_the_grant_end_goes_to_the_next_grant():
    channel = fake_channel([record(30_000), record(60_000)])
    first = grant(channel, Kind.GRANT, 30_000)        # the slot at 30000 starts at the end of this grant
    assert [m.kind for m in first] == [Kind.DONE]
    assert first[-1].time_us == 30_000 and DONE.unpack(first[-1].payload) == (NEVER, 0)
    second = grant(channel, Kind.GRANT, 31_000)
    assert [(m.kind, m.time_us) for m in second] == [(Kind.AIR_OUT, 30_000), (Kind.DONE, 31_000)]
    assert Record.decode(second[0].payload) == record(30_000)
    # A drain owns its own end time
    grant(channel, Kind.GRANT, 60_000)
    drained = grant(channel, Kind.DRAIN, 60_000)
    assert [(m.kind, m.time_us) for m in drained] == [(Kind.AIR_OUT, 60_000), (Kind.DONE, 60_000)]
    assert not any("RunFor" in line for line in channel.monitor.lines[-2:])


def test_inputs_apply_at_the_machine_time_only():
    channel = fake_channel([])
    grant(channel, Kind.GRANT, 1_000)
    channel.send(Message(Kind.KEY, bytes((4, 1)), 1_000))
    channel.send(Message(Kind.AIR_IN, record(0).encode(), 1_000, source=2))
    with pytest.raises(emu.ChannelError, match="EMU_UNSUPPORTED:BYTES_IN"):
        channel.send(Message(Kind.BYTES_IN, b"x", 1_000))
    assert channel.monitor.lines[-2:] == ["keys_key 4 1", f'sysbus.spi2.c5000 AirIn "{record(0).encode().hex()}"']
    assert not channel.dropped and channel.air_in[0][:2] == (1_000, 2)
    channel.send(Message(Kind.KEY, bytes((4, 0)), 999))     # not the machine time: the world gets a reason
    assert channel.receive().kind == Kind.RESTART_REASON


@pytest.mark.parametrize("board,config", emu.PLATFORMS.items())
def test_the_board_comes_from_the_elf_name(board, config):
    assert emu.board_of(config["elf"]) == board
    if config["engine"] == "renode":
        for field in ("resc", "repl", "flash"):
            assert os.path.isfile(os.path.join(config["directory"], config[field]))
        assert "keys" in config
    with pytest.raises(emu.ChannelError, match="EMU_UNKNOWN_BOARD"):
        emu.board_of("build/vhost/rfoundry_vhost.elf")


def test_install_parts_does_not_load_an_emulated_radio():
    assert not hasattr(emu, "install")


@pytest.fixture(autouse=True)
def no_renode_left():
    """Fail the test if a Renode it started (any descendant of this process) is still running after it."""
    yield
    left = emu.renode_guard.check_none_left()
    assert not left, f"Renode outlived the test (killed now): {left}"


def renode_left(pgids):
    return [pgid for pgid in pgids if emu.alive(pgid)]


C5000_TIMES = {"ptt": ((1, "down", PTT_DOWN_US), (1, "up", PTT_UP_US), (2, "down", REPLY_DOWN_US),
                       (2, "up", REPLY_UP_US)), "view_us": VIEW_US, "end_us": END_US}


def live_world(vhost_elf, tmp_path, name, elf, times=C5000_TIMES, at=()):
    """One run: returns radio 2's views during PTT, the air events, radio 1's channel and its Renode pgid.
    at: (world time, function(channel)) pairs; each is called once when the world reaches its time. The grants are
    capped there, so radio 1's machine is paused at exactly that time (the same in every run)."""
    channels, events = [], []
    config = Config(executable=str(vhost_elf), radios=2, end_us=times["end_us"],
                    radio_executables=(elf, str(vhost_elf)))
    world = World(config, channel_factory=emu.factory(channels=channels))
    views, pgid = {}, None
    try:
        install_parts(world)
        reply = world.api.dispatch({**vradio.group_air(2), "version": 1, "command": "air_config"})
        assert reply["ok"], reply
        stops = sorted({times["view_us"], *(t for t, _ in at)})
        world.add_grant_limit(lambda now: next((t for t in stops if now < t), None))
        dlcache.get(world, cadence_us=500_000)
        world.api.bind(name, tmp_path)
        notify = world.api.notify

        def keep(event):
            if event["event"] in ("air_decision", "air_delivery"):
                events.append(event)
            notify(event)
        world.api.notify = keep
        world.start()
        pgid = channels[0].process.pid
        emu.check_emulator_listeners(pgid)
        assert "Elapsed Virtual Time:" in channels[0].monitor.command("emulation GetTimeSourceInfo")
        world.run("max")
        grants = {radio: world.leases.grant(radio, "script:keys") for radio in sorted({radio for radio, _, _ in times["ptt"]})}
        for radio, state, time_us in times["ptt"]:
            reply = world.api.dispatch({"version": 1, "command": "key", "radio": radio, "key": "ptt", "state": state,
                                        "time_us": time_us, **grants[radio]})
            assert reply["ok"], reply
        observer = world.dlmux.connect(2, "debug:emu-view", "observer")
        pending = sorted(at, key=lambda item: item[0])
        while world.state != "Ended":
            while pending and world.now >= pending[0][0]:
                assert channels[0].now == pending[0][0], (channels[0].now, pending[0][0])
                pending.pop(0)[1](channels[0])
            if world.now >= times["view_us"] and not views:
                views = {"t_us": world.now, "call": read_view(world, observer, DMR_CALL),
                         "dmr": read_view(world, observer, DMR)}
            world.api.poll(0)
            world.advance()
        assert world.reason == "WORLD_END_TIME", world.reason
        folder = world.api.directory
        frames = [c for c in world.frames.checkpoints if c.radio == 1]
    finally:
        world.close()
    return views, events, channels[0], pgid, folder, frames


@pytest.mark.parametrize("board,config", [(b, c) for b, c in emu.PLATFORMS.items()
                                         if c.get("chip") == "sysbus.spi2.c5000"])
def test_emulated_group_call_reaches_a_virtual_radio(board, config, vhost_elf, tmp_path):
    if not (os.path.exists(emu.RENODE) and os.path.exists(config["elf"])):
        pytest.skip(f"needs Renode and {config['elf']}")
    runs = []
    for run in (1, 2):
        views, events, channel, pgid, folder, frames = live_world(vhost_elf, tmp_path / f"run{run}", "emu-call", config["elf"])
        assert not renode_left([pgid]), "a Renode process outlived the world"
        call = views["call"]
        taken = [reply for _, _, reply in channel.air_in]
        deliveries = [e for e in events if e["event"] == "air_delivery"]
        print(f"run {run} at {views['t_us']} us radio 2 view: call.src={call.get('call.src')} "
              f"call.dst={call.get('call.dst')} call.group={call.get('call.group')} "
              f"dmr.state={views['dmr'].get('dmr.state')}")
        print(f"run {run}: radio 1 sent {len(channel.air_out)} bursts; air deliveries 1->2: "
              f"{sum(e['sender'] == 1 and e['receiver'] == 2 for e in deliveries)}, 2->1: "
              f"{sum(e['sender'] == 2 for e in deliveries)}; AirIn answers {sorted(set(taken))}; "
              f"dropped {channel.dropped}; frames.sock checkpoints {len(frames)}")
        assert frames, "no frames.sock checkpoint from the emulated radio"
        assert PTT_DOWN_US <= views["t_us"] < PTT_UP_US
        assert call.get("call.src") == "3100001", call
        assert call.get("call.group") == "1", call
        assert any(e["sender"] == 1 and e["receiver"] == 2 for e in deliveries)
        # The recording's air log (parts/resolve.py) holds the same deliveries
        lines = [json.loads(line) for line in (folder / "recording/air-deliveries.jsonl").read_text().splitlines()
                 if line]
        assert sum(e["sender"] == 1 and e["receiver"] == 2 for e in lines) == len(channel.air_out) > 0
        # Air in: radio 2's bursts reached the HR_C5000 model at their hand-over time, and it took them
        replies = [e for e in deliveries if e["sender"] == 2 and e["receiver"] == 1]
        assert replies and len(channel.air_in) == len(replies)
        assert [t for t, _, _ in channel.air_in] == [e["input_us"] for e in replies]
        assert taken.count("taken") > len(taken) // 2, taken
        runs.append((events, channel.air_in))
    assert runs[0] == runs[1], "the two runs made different air decisions"
    print(f"same air decisions in both runs: {len(runs[0][0])} events, {len(runs[0][1])} AirIn answers")


# --- HR_C6000 boards ---

DM_PTT_DOWN_US, DM_PTT_UP_US, DM_VIEW_US = 4_000_000, 5_500_000, 5_300_000
DM_REPLY_DOWN_US, DM_REPLY_UP_US, DM_END_US = 6_000_000, 7_500_000, 9_500_000
DM_RX_VIEW_US, DM_AFTER_VIEW_US = 7_000_000, 9_000_000      # the emulated radio's own view: during radio 2's call, after it
C6000_TIMES = {"ptt": ((1, "down", DM_PTT_DOWN_US), (1, "up", DM_PTT_UP_US), (2, "down", DM_REPLY_DOWN_US),
                        (2, "up", DM_REPLY_UP_US)), "view_us": DM_VIEW_US, "end_us": DM_END_US}


def debug_replies(data):
    """The debug link replies in the bytes the firmware wrote (tools/rfoundry_debug.py framing), by sequence number:
    {seq: (status, payload)}. Request frames the console echoes (command below 0x80) are skipped."""
    replies = {}
    while len(data) >= 8:
        start = data.find(b"\xA5\x5A")
        if start < 0:
            break
        data = data[start:]
        command, sequence, length = struct.unpack_from("<BBH", data, 2)
        packet, data = data[:8 + length], data[8 + length:]
        if len(packet) == length + 8 and rd.crc16(packet[2:-2]) == struct.unpack("<H", packet[-2:])[0] \
                and command >= 0x80:
            replies[sequence] = (packet[6], packet[7:-2])
    return replies


def view_of(replies, seq):
    status, payload = replies[seq]
    assert status == rd.ST_OK, (seq, status)
    return rd.parse_kv(payload[5:].decode("ascii", "replace"))


def get_view(channel, seq, section):
    channel.debug(rd.frame(rd.GET_VIEW, seq, bytes((rd.VIEW_SECTIONS.index(section),))))


@pytest.mark.parametrize("board,config", [(b, c) for b, c in emu.PLATFORMS.items()
                                         if c.get("chip") == "sysbus.spi2.c6000"])
def test_emulated_group_call_both_ways_with_a_virtual_radio(board, config, vhost_elf, tmp_path):
    if not (os.path.exists(emu.RENODE) and os.path.exists(config["elf"])):
        pytest.skip(f"needs Renode and {config['elf']}")
    rx_png = str(tmp_path / "rx-call.png")
    runs = []
    for run in (1, 2):
        def during(channel, run=run):
            get_view(channel, 1, "dmr_call")
            get_view(channel, 2, "dmr")
            if run == 1:
                os.makedirs(os.path.dirname(rx_png), exist_ok=True)
                channel.save_png(rx_png, 2)

        def after(channel):
            get_view(channel, 3, "dmr_call")

        views, events, channel, pgid, folder, frames = live_world(
            vhost_elf, tmp_path / f"run{run}", f"emu-{board}", elf=config["elf"], times=C6000_TIMES,
            at=((DM_RX_VIEW_US, during), (DM_AFTER_VIEW_US, after)))
        assert not renode_left([pgid]), "a Renode process outlived the world"
        assert channel.board == board and channel.chip == "sysbus.spi2.c6000"
        call = views["call"]
        deliveries = [e for e in events if e["event"] == "air_delivery"]
        taken = [reply for _, _, reply in channel.air_in]
        replies = debug_replies(channel.host_out())
        own_call, own_dmr, own_after = view_of(replies, 1), view_of(replies, 2), view_of(replies, 3)
        reports = [line for line in channel.chip_trace() if " REPORT " in line]
        kinds = [r.kind for r in channel.air_out]
        print(f"run {run} at {views['t_us']} us radio 2 view: call.src={call.get('call.src')} "
              f"call.dst={call.get('call.dst')} call.group={call.get('call.group')} "
              f"dmr.state={views['dmr'].get('dmr.state')}")
        print(f"run {run}: emulated radio sent {len(channel.air_out)} bursts (headers {kinds.count(1)}, voice "
              f"{kinds.count(0x10)}, terminators {kinds.count(2)}); deliveries 1->2: "
              f"{sum(e['sender'] == 1 and e['receiver'] == 2 for e in deliveries)}, 2->1: "
              f"{sum(e['sender'] == 2 for e in deliveries)}; AirIn answers {sorted(set(taken))} "
              f"(taken {taken.count('taken')}/{len(taken)}); chip reports {len(reports)}; dropped {channel.dropped}; "
              f"frames.sock checkpoints {len(frames)}")
        print(f"run {run}: emulated radio own view at {DM_RX_VIEW_US} us: call.src={own_call.get('call.src')} "
              f"call.dst={own_call.get('call.dst')} call.group={own_call.get('call.group')} "
              f"dmr.phase={own_dmr.get('dmr.phase')}; at {DM_AFTER_VIEW_US} us last.src={own_after.get('last.src')} "
              f"last.dst={own_after.get('last.dst')} last.ms={own_after.get('last.ms')}")
        # (1) emulated radio -> virtual radio
        assert frames, "no frames.sock checkpoint from the emulated radio"
        assert DM_PTT_DOWN_US <= views["t_us"] < DM_PTT_UP_US
        assert call.get("call.src") == "3100001", call
        assert call.get("call.group") == "1", call
        assert kinds[:3] == [1, 1, 1] and 0x10 in kinds and kinds[-1] == 2, kinds
        lines = [json.loads(line) for line in (folder / "recording/air-deliveries.jsonl").read_text().splitlines()
                 if line]
        assert sum(e["sender"] == 1 and e["receiver"] == 2 for e in lines) == len(channel.air_out) > 0
        # (2) virtual radio -> emulated radio: the model took the bursts at their hand-over time, and the stack saw the call
        replies_in = [e for e in deliveries if e["sender"] == 2 and e["receiver"] == 1]
        assert replies_in and len(channel.air_in) == len(replies_in)
        assert [t for t, _, _ in channel.air_in] == [e["input_us"] for e in replies_in]
        assert taken.count("taken") > len(taken) // 2, taken
        assert len(reports) >= taken.count("taken") > 0, reports[:3]
        assert own_call.get("call.src") == "3100002" and own_call.get("call.group") == "1", own_call
        assert own_dmr.get("dmr.phase") == "rx_voice", own_dmr
        print(f"run {run}: emulated radio own view after: {own_after}")
        assert own_after.get("last.src") == "3100002", own_after
        runs.append((events, channel.air_in, own_call, own_dmr, own_after))
    assert os.path.getsize(rx_png) > 0
    assert runs[0] == runs[1], "the two runs made different air decisions or emulated radio views"
    print(f"same air decisions in both runs: {len(runs[0][0])} events, {len(runs[0][1])} AirIn answers; PNG {rx_png}")


@pytest.mark.parametrize("channel_type,kinds", [
    (emu.EmuChannel, (Kind.BYTES_IN, Kind.ETH_IN, Kind.VIEW, Kind.STORE)),
    (emu.HrcChannel, (Kind.KEY, Kind.ENC, Kind.BYTES_IN, Kind.ETH_IN, Kind.VIEW, Kind.STORE)),
])
def test_unsupported_inputs_are_rejected_before_queueing(stub_world, channel_type, kinds):
    world = stub_world()
    world.start()
    world.run("max")
    channel = channel_type.__new__(channel_type)
    channel.now, channel.dropped = 0, {}
    from collections import deque
    channel.inbox = deque()
    for kind in kinds:
        with pytest.raises(emu.ChannelError, match=f"EMU_UNSUPPORTED:{kind.name}"):
            channel.send(Message(kind, b"x"))
    world.radios[1].channel.supported_inputs = channel_type.supported_inputs
    for kind in kinds:
        with pytest.raises(Exception, match=f"WORLD_UNSUPPORTED_INPUT:{kind.name}"):
            world.submit(1, kind, b"x", 1)
    assert not world._inputs
    install_parts(world)
    with pytest.raises(Exception, match="WORLD_UNSUPPORTED_INPUT:BYTES_IN"):
        world.dlmux.connect(1, "script:test", "observer")


@pytest.mark.parametrize("channel_type", [emu.EmuChannel, emu.HrcChannel])
def test_world_starts_without_debug_link_capability(stub_world, channel_type):
    world = stub_world(end_us=10_000, radio_args=("--host-port", "on"))
    factory = world.channel_factory

    def incapable(*args):
        channel = factory(*args)
        channel.supported_inputs = channel_type.supported_inputs
        return channel

    world.channel_factory = incapable
    install_parts(world)
    world.start()
    world.run("max")
    with pytest.raises(Exception, match="WORLD_UNSUPPORTED_INPUT:BYTES_IN"):
        world.dlmux.connect(1, "script:test", "observer")
    while world.state != "Ended":
        world.advance()
    assert world.reason == "WORLD_END_TIME"
    assert not world.dlmux.links


@pytest.mark.parametrize("fail", [False, True])
def test_monitor_startup_uses_private_pipes_and_reaps_failures(tmp_path, monkeypatch, fail):
    from unittest.mock import Mock

    launches, checked, killed = [], [], []

    def launch(argv, **kwargs):
        process = Mock(pid=900000)
        launches.append((argv, kwargs, process))
        return process

    def monitor(process, deadline):
        assert process is launches[0][2]
        if fail:
            raise emu.ChannelError("EMU_MONITOR_CLOSED")
        return Mock(command=Mock(return_value=""))

    monkeypatch.setattr(emu, "GuardedPopen", launch)
    monkeypatch.setattr(emu, "Monitor", monitor)
    monkeypatch.setattr(emu, "check_emulator_listeners", checked.append)
    monkeypatch.setattr(emu.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    board = next(b for b, config in emu.PLATFORMS.items() if config["engine"] == "renode")
    if fail:
        with pytest.raises(emu.ChannelError, match="EMU_MONITOR_CLOSED"):
            emu.EmuChannel(emu.PLATFORMS[board]["elf"], keep=tmp_path)
        assert not checked
    else:
        channel = emu.EmuChannel(emu.PLATFORMS[board]["elf"], keep=tmp_path)
        try:
            setup = [call.args[0] for call in channel.monitor.command.call_args_list]
            assert setup[0].startswith("logFile @")
            assert "$tcp=false" in setup
            assert not any(line.startswith("$port=") for line in setup)
            assert checked == [channel.process.pid, channel.process.pid]
        finally:
            channel.close()
    assert len(launches) == 1
    argv, kwargs, process = launches[0]
    assert "--console" in argv and "--hide-log" in argv
    assert argv[argv.index("-P") + 1] == "-1"
    assert kwargs["stdin"] == kwargs["stdout"] == subprocess.PIPE
    assert (process.pid, signal.SIGKILL) in killed
    process.wait.assert_called()
