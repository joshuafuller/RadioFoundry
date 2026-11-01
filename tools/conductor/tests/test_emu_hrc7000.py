# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Emulated HR_C7000 boards as world radios, discovered from their emulator descriptions.

A call alert goes from the emulated radio to a virtual radio and its ACK comes back. Then the virtual radio's
voice call reaches the emulated radio. Two runs must have the same air decisions, deliveries and views.
Skips without XuanTie QEMU, the board ELF or a host C compiler.
"""

import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import time

import pytest

from tools import rfoundry_debug as rd, vradio
from tools.conductor.checks.interaction_check import DMR_DATA_STATS, read_view
from tools.conductor.conductor import install_parts
from tools.conductor.parts import emu
from tools.conductor.tests.test_emu import debug_replies, view_of
from tools.conductor.world import Config, World

QEMU = os.environ.get("CSKY_QEMU") or os.path.expanduser("~/tools/xuantie-qemu/src/build/qemu-system-cskyv2")
CALL_ALERT_US = 3_000_000           # the emulated radio is in DMR receive on its channel well before this
ALERT_VIEW_US = 3_600_000           # the emulated radio's own view: the data service counters
PTT_DOWN_US, PTT_UP_US = 4_000_000, 5_000_000
RX_VIEW_US = 4_800_000              # the emulated radio's own view during radio 2's call
END_US = 5_400_000


def test_in_process_host_buffer_preserves_debug_bytes():
    from tools.emu.hrc7000_emu import HostBuffer
    from tools.emu.hrc7000_lockstep import Lockstep

    host, control = HostBuffer(), Lockstep()
    control.attach(None, None, None, None, host, None, {})
    host.rx += b"request"
    host.data.set()
    assert host.take(3) == b"req" and host.data.is_set()
    assert host.take(10) == b"uest" and not host.data.is_set()
    host.send(b"reply")
    assert control.host_out == b"reply"
    assert not hasattr(host, "srv")


@pytest.mark.skipif(not os.path.exists(QEMU), reason="needs XuanTie QEMU")
def test_qemu_private_gdb_drives_paused_cpu():
    from tools.emu.hrc7000_rsp import Rsp

    with tempfile.TemporaryDirectory(prefix="rf-qemu-test-") as directory:
        endpoint = os.path.join(directory, "gdb.sock")
        kernel = Path(directory) / "paused.bin"
        kernel.write_bytes(bytes(4))     # QEMU requires an image; -S keeps these bytes from executing.
        process = subprocess.Popen(
            [QEMU, "-M", "virt", "-cpu", "ck803s", "-kernel", str(kernel), "-nographic", "-monitor", "none", "-serial", "none", "-S",
             "-gdb", f"unix:{endpoint},server=on,wait=off"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True, umask=0o077)
        rsp = None
        try:
            deadline = time.monotonic() + 10
            while rsp is None:
                try:
                    rsp = Rsp(endpoint)
                except OSError:
                    if time.monotonic() >= deadline or process.poll() is not None:
                        raise
                    time.sleep(0.05)
            assert os.stat(endpoint).st_mode & 0o777 == 0o700
            assert os.stat(directory).st_mode & 0o777 == 0o700
            emu.check_emulator_listeners(process.pid)
            assert b"PacketSize=" in rsp.cmd("qSupported")
            regs = rsp.regs()
            assert regs
            rsp.set_reg(0, 42)
            assert rsp.reg(0) == 42
        finally:
            if rsp:
                rsp.close()
            process.kill()
            process.wait(10)
            print(process.stdout.read().decode(errors="replace"))
            process.stdout.close()
        assert not emu.alive(process.pid)


def live_world(vhost_elf, tmp_path, name, elf):
    channels, events = [], []
    config = Config(executable=str(vhost_elf), radios=2, end_us=END_US,
                    radio_executables=(elf, str(vhost_elf)))
    world = World(config, channel_factory=emu.factory(channels=channels))
    views = {}

    def call_alert(channel):
        channel.debug(rd.frame(rd.DMR_DATA, 1, bytes((rd.DATA_OP_CALL_ALERT,)) + struct.pack("<I", 3100002)))

    def alert_view(channel):
        channel.debug(rd.frame(rd.GET_VIEW, 2, bytes((rd.VIEW_SECTIONS.index("dmr_data_stats"),))))

    def rx_view(channel):
        channel.debug(rd.frame(rd.GET_VIEW, 3, bytes((rd.VIEW_SECTIONS.index("dmr_call"),))))

    at = [(CALL_ALERT_US, call_alert), (ALERT_VIEW_US, alert_view), (RX_VIEW_US, rx_view)]
    try:
        install_parts(world)
        reply = world.api.dispatch({**vradio.group_air(2), "version": 1, "command": "air_config"})
        assert reply["ok"], reply
        stops = sorted(t for t, _ in at)
        world.add_grant_limit(lambda now: next((t for t in stops if now < t), None))
        world.api.bind(name, tmp_path)
        notify = world.api.notify

        def keep(event):
            if event["event"] in ("air_decision", "air_delivery"):
                events.append(event)
            notify(event)
        world.api.notify = keep
        world.start()
        emu.check_emulator_listeners(channels[0].process.pid)
        emu.check_emulator_listeners(os.getpid())
        endpoint = channels[0].ctl.r.s.getpeername()
        assert channels[0].ctl.r.s.family == socket.AF_UNIX
        assert os.stat(endpoint).st_mode & 0o777 == 0o700
        assert os.stat(os.path.dirname(endpoint)).st_mode & 0o777 == 0o700
        world.run("max")
        grants = {radio: world.leases.grant(radio, "script:keys") for radio in [2]}
        for state, time_us in (("down", PTT_DOWN_US), ("up", PTT_UP_US)):
            reply = world.api.dispatch({"version": 1, "command": "key", "radio": 2, "key": "ptt", "state": state,
                                        "time_us": time_us, **grants[2]})
            assert reply["ok"], reply
        observer = world.dlmux.connect(2, f"debug:{name}", "observer")
        pending = list(at)
        while world.state != "Ended":
            while pending and world.now >= pending[0][0]:
                assert channels[0].now == pending[0][0], (channels[0].now, pending[0][0])
                pending.pop(0)[1](channels[0])
            if world.now >= ALERT_VIEW_US and "radio2" not in views:
                views["radio2"] = read_view(world, observer, DMR_DATA_STATS)
            world.api.poll(0)
            world.advance()
        assert world.reason == "WORLD_END_TIME", world.reason
    finally:
        world.close()
    channel = channels[0]
    return views, events, channel


BOARDS = [pytest.param(c["elf"], id=b) for b, c in emu.PLATFORMS.items() if c["engine"] == "qemu"]

@pytest.mark.parametrize("elf", BOARDS)
def test_emulated_hrc7000_call_alert_and_call_with_a_virtual_radio(elf, vhost_elf, tmp_path):
    if not (os.path.exists(QEMU) and os.path.exists(elf) and shutil.which("cc")):
        pytest.skip(f"needs XuanTie QEMU, {os.path.relpath(elf, emu.ROOT)} and a host C compiler")
    board = emu.board_of(elf)
    runs = []
    for run in (1, 2):
        views, events, channel = live_world(vhost_elf, tmp_path / f"run{run}", f"emu-{board}", elf)
        assert not channel.left, "a QEMU process outlived the world"
        replies = debug_replies(channel.host_out())
        alert_reply = replies.get(1)
        own_data, own_call = view_of(replies, 2), view_of(replies, 3)
        deliveries = [e for e in events if e["event"] == "air_delivery"]
        out = [(r.t_us, r.kind, r.payload.hex()) for r in channel.air_out]
        took = [(round(t, 4), rec.kind, what) for t, rec, _, what in channel.received()]
        r2 = views.get("radio2", {})
        print(f"run {run}: {board} sent {out}")
        print(f"run {run}: air deliveries 1->2 {sum(e['sender'] == 1 and e['receiver'] == 2 for e in deliveries)}, "
              f"2->1 {sum(e['sender'] == 2 for e in deliveries)}; the {board} modem took {len(took)}: {took[:6]} ...")
        print(f"run {run}: radio 2 view: call_alerts_received={r2.get('data.call_alerts_received')} "
              f"csbk_answers_sent={r2.get('data.csbk_answers_sent')}")
        print(f"run {run}: {board} view: call_alerts_sent={own_data.get('data.call_alerts_sent')} "
              f"call_alerts_acked={own_data.get('data.call_alerts_acked')}; during the call call.src="
              f"{own_call.get('call.src')} call.dst={own_call.get('call.dst')} call.group={own_call.get('call.group')}")
        print(f"run {run}: lockstep idle points {channel.ctl.idle_points}, kicks {channel.ctl.kicks}; "
              f"dropped {channel.dropped}")
        assert alert_reply is not None and alert_reply[0] == rd.ST_OK, alert_reply
        # 1. the emulated radio's call alert reached the virtual radio, and its ACK came back
        assert any(kind == 3 for _, kind, _ in out), out
        assert r2.get("data.call_alerts_received") == "1", r2
        assert own_data.get("data.call_alerts_acked") == "1", own_data
        # 2. the virtual radio's call shows on the emulated radio
        assert own_call.get("call.src") == "3100002", own_call
        assert own_call.get("call.group") == "1", own_call
        runs.append((events, [(t, s, r.encode()) for t, s, r in channel.air_in], own_data, own_call))
    # 3. the same air decisions, inputs and views in both runs
    assert runs[0] == runs[1], f"the two runs made different air decisions or {board} views"
    print(f"same air decisions in both runs: {len(runs[0][0])} events, {len(runs[0][1])} AIR_IN")
