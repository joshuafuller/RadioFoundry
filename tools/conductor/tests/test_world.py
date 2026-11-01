# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""World boundaries, lifecycle, and the extension contract for later parts."""

from dataclasses import FrozenInstanceError
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import subprocess
import threading
import types

import pytest

from tools.conductor.channel import ChannelError, Kind
from tools.conductor.conductor import install_parts
from tools.conductor.tests.test_lease import FakeFrontEnd, KeyRadio, audit_lines, run_until
from tools.conductor.world import Input, WorldError


def finish_step(world):
    for _ in range(100):
        if world.state in ("Paused", "Ended"):
            return
        world.advance()
    pytest.fail("step did not reach a stop")


def test_step_next_until_and_reject_paused(stub_world, capsys):
    world = stub_world(radios=2)
    world.start()
    channels = [slot.channel for slot in world.radios.values()]
    with pytest.raises(WorldError, match="WORLD_PAUSED"):
        world.submit(1, Kind.BYTES_IN, b"mutation", 9)
    assert not world._inputs
    world.step()
    finish_step(world)
    assert world.now == 1000 and world.state == "Paused"
    world.step(3500)
    finish_step(world)
    assert world.now == 3500
    assert all(slot.done.time_us == 3500 and slot.done.next_due_us > 3500 for slot in world.radios.values())
    assert all(channel.sent[-1].kind == Kind.DRAIN for channel in channels)
    with capsys.disabled():
        print("\nstep(next)=1000; step(until)=3500; both radios drained; mutation=WORLD_PAUSED")


def test_part_seams_hold_and_order(stub_world, capsys):
    world = stub_world(radios=2, replay=True)
    accepted, outboxes, notices, grants, ended = [], [], [], [], []
    released = False
    kinds = (Kind.BYTES_OUT, Kind.FRAME, Kind.ETH_OUT, Kind.TRACE, Kind.RESTART_REASON)

    def install(instance):
        instance.on_input(accepted.append)
        for kind in kinds:
            instance.on_outbox(kind, lambda radio, message: outboxes.append((radio, message.kind)))
        instance.on_pausing(lambda: lambda: released)
        instance.on_end(lambda: ended.append(instance.reason))
        instance.add_radio_args(lambda radio: ["--test-radio", str(radio)])
        instance.add_grant_limit(lambda now: now + 600)
        instance.add_grant_limit(lambda now: now + 300)

        def done(time_us, messages):
            grants.append((time_us, list(messages)))
            if time_us == 300:
                return [Input(2, Kind.BYTES_IN, b"next", time_us, 2)]
        instance.on_grant_done(done)

    install(world)
    world.api.notify = notices.append
    world.start()
    assert world.config.replay is True and world.sources == (1, 2)
    for radio, slot in world.radios.items():
        assert slot.channel.args[:2] == ("--host-port", "on")
        assert ("--test-radio", str(radio)) == slot.channel.args[2:4]
        slot.channel.outputs.extend((kind, b"event") for kind in kinds[:-1])
    world.run("max")
    stamp = world.submit(1, Kind.BYTES_IN, b"input", 7)
    assert stamp.time_us == 1 and accepted == [stamp]
    world.advance()  # Reach the input at 1 us, before its handling.
    world.advance()  # The next limit is 301, so install a boundary event below.
    world.submit(2, Kind.BYTES_IN, b"boundary", 2, time_us=world.now + 300)
    world.advance()
    assert outboxes[:8] == [(radio, kind) for radio in (1, 2) for kind in kinds[:-1]]
    assert all(radios == [1, 2] for _, radios in grants)
    with pytest.raises(WorldError, match="WORLD_PAST_INPUT"):
        world.submit(1, Kind.BYTES_IN, b"past", 1, time_us=world.now - 1)
    world.pause()
    assert world.state == "Pausing"
    channels = [slot.channel for slot in world.radios.values()]
    before = [sum(m.kind == Kind.DRAIN for m in channel.sent) for channel in channels]
    world.advance()
    assert before == [sum(m.kind == Kind.DRAIN for m in channel.sent) for channel in channels]
    released = True
    world.advance()
    assert world.state == "Paused"
    assert all(channel.sent[-1].kind == Kind.DRAIN for channel in channels)
    assert any(event.get("state") == "Pausing" for event in notices)
    assert notices[-1]["state"] == "Paused"
    world.notify({"event": "part_event"})
    assert notices[-1] == {"event": "part_event"}
    world.radios[1].channel.outputs.append((Kind.RESTART_REASON, b"stuck task input"))
    world.run("max")
    world.advance()
    assert (1, Kind.RESTART_REASON) in outboxes
    assert world.state == "Ended" and "stuck task input" in world.reason
    assert ended == [world.reason] and all(channel.closed for channel in channels)
    assert len({event.sequence for event in accepted}) == len(accepted)
    with capsys.disabled():
        print("\npart seams delivered; radio order=1,2; hold deferred drain; RESTART_REASON ended world")


def test_grant_done_returns_next_input(stub_world):
    world = stub_world(radios=2)
    accepted, received = [], []
    world.on_input(accepted.append)
    world.on_outbox(Kind.BYTES_OUT, lambda radio, message: received.append((radio, message.payload)))
    world.add_grant_limit(lambda now: now + 1000)
    world.on_grant_done(lambda time, out: [Input(2, Kind.BYTES_IN, b"air-seam", time, 1)] if time == 1000 else [])
    world.start()
    world.run("max")
    world.advance()
    assert accepted[0].time_us == 1000
    world.advance()
    assert received == [(2, b"air-seam")]


def test_lifecycle_clears_input_and_translates_restart_time(stub_world):
    world = stub_world()
    world.start()
    original = world.radios[1].channel
    path = original.args[original.args.index("--flash-file") + 1]
    assert "--restore-flash" not in original.args
    world.step(2000)
    finish_step(world)
    world.run("max")
    world.submit(1, Kind.KEY, b"down", 1, time_us=9000)
    world.restart(1)
    assert original.closed and not world._inputs and world.boots[1] == 2
    args = world.radios[1].channel.args
    assert args[args.index("--flash-file") + 1] == path
    assert args[args.index("--restore-flash") + 1] == path
    assert world.radios[1].done.time_us == 2000
    world.pause()
    world.step()
    finish_step(world)
    assert world.now == 3000
    assert world.radios[1].channel.sent[-1].time_us == 1000
    first = world.radios[1].channel
    first.outputs.append((Kind.RESTART_REASON, b"reboot (test)"))
    world.reboot(1)
    assert first.closed and world.boots[1] == 3 and world.state != "Ended"
    assert "--restore-flash" in world.radios[1].channel.args and "--restart-flash" in world.radios[1].channel.args
    assert "--restore-flash" in first.args
    world.power_off(1)
    assert not world.radios
    world.start_radio(1)
    assert world.boots[1] == 4 and "--restore-flash" not in world.radios[1].channel.args
    with pytest.raises(FrozenInstanceError):
        world.config.replay = True


@pytest.mark.parametrize("command", ["end", "restart", "power_off"])
def test_pausing_dropped_release_recovery(stub_world, tmp_path, command):
    world = stub_world(radios=2)
    world.channel_factory = KeyRadio
    world.api.directory = tmp_path
    front = FakeFrontEnd(world)
    world.start()
    world.run("max")
    channels = [slot.channel for slot in world.radios.values()]
    for radio in world.radios:
        generation = world.leases.grant(radio, "debug:c1")["generation"]
        front.press(radio, "debug:c1", generation, 4)
    run_until(world, lambda: all(channel.pins == 1 << 4 for channel in channels))
    world.holds.drop_releases(2)
    world.pause()
    world.advance()
    assert world.state == "Pausing" and not world.leases.held
    assert all(world.holds.unconfirmed(radio) for radio in world.radios)
    assert len([line for line in audit_lines(world) if line["reason"] == "HOLD_TEST_DROP"]) == 2

    response = world.api.dispatch({"version": 1, "command": command, "radio": 1})
    assert response["ok"], response
    assert channels[0].closed
    if command == "end":
        assert world.state == "Ended" and not world.radios and not world._inputs
        assert all(channel.closed for channel in channels)
        assert any(line["kind"] == "world_end" and line["reason"] == "WORLD_STOPPED"
                   and line["state"] == "Pausing" for line in audit_lines(world))
        assert world.audit._fd is None
    else:
        assert world.state == "Pausing" and not channels[1].closed
        assert any(line["kind"] == "radio_off" and line["radio"] == 1
                   and line["reason"] == "WORLD_PAUSING" for line in audit_lines(world))
        if command == "restart":
            assert world.boots[1] == 2 and world.radios[1].channel.pins == 0
            world.advance()
            assert world.state == "Pausing" and not world.holds.unconfirmed(1)
            assert world.holds.unconfirmed(2) and not world.leases.held
            world.restart(2)
            finish_step(world)
            assert world.state == "Paused" and not world.holds.pending
            assert all(slot.channel.pins == 0 for slot in world.radios.values())
            assert all(slot.channel.sent[-1].kind == Kind.DRAIN for slot in world.radios.values())
            assert {line["radio"] for line in audit_lines(world)
                    if line["reason"] == "HOLD_RADIO_RESTARTED"} == {1, 2}
            world.run("max")
            assert world.leases.grant(1, "browser:s1")["generation"] == 2
        else:
            assert 1 not in world.radios and world.holds.unconfirmed(1)
            world.advance()
            assert world.state == "Pausing"
            world.end()
            assert world.state == "Ended" and channels[1].closed


@pytest.mark.parametrize("failure", [TimeoutError("stuck"), ChannelError("VCHAN_DISCONNECTED")])
def test_radio_failure_ends_all(stub_world, failure):
    world = stub_world(radios=2)
    world.start()
    channels = [slot.channel for slot in world.radios.values()]
    channels[0].failure = failure
    world.run("max")
    world.advance()
    assert world.state == "Ended" and str(failure) in world.reason
    assert all(channel.closed for channel in channels)


def test_exchange_reuses_workers_and_keeps_radio_order(stub_world, monkeypatch):
    pools, workers = [], set()

    def pool(**options):
        pools.append(ThreadPoolExecutor(**options))
        return pools[-1]

    monkeypatch.setattr("tools.conductor.world.ThreadPoolExecutor", pool)
    world = stub_world(radios=2)
    world.start()
    collect = world._collect
    second_done = threading.Event()
    finished, delivered = [], []

    def reversed_collect(radio):
        workers.add(threading.current_thread())
        if radio == 1:
            assert second_done.wait(2)
        result = collect(radio)
        finished.append(radio)
        if radio == 2:
            second_done.set()
        return result

    monkeypatch.setattr(world, "_collect", reversed_collect)
    world.on_outbox(Kind.TRACE, lambda radio, message: delivered.append((radio, message.payload)))
    world.run("max")
    for _ in range(3):
        second_done.clear()
        for radio, slot in world.radios.items():
            slot.channel.outputs.extend(((Kind.TRACE, b"first"), (Kind.TRACE, b"second")))
        world.advance()
    assert finished == [2, 1] * 3
    assert delivered == [(radio, payload) for radio in (1, 2) for payload in (b"first", b"second")] * 3
    assert len(pools) == 1 and len(workers) == 2
    world.close()
    assert all(not worker.is_alive() for worker in workers)


def test_exchange_failure_joins_collectors_before_closing(stub_world, monkeypatch):
    world = stub_world(radios=2)
    world.start()
    collect = world._collect
    second_started, first_failed, second_done = (threading.Event() for _ in range(3))

    def fail_one(radio):
        if radio == 1:
            assert second_started.wait(2)
            first_failed.set()
            raise ChannelError("VCHAN_DISCONNECTED")
        second_started.set()
        assert first_failed.wait(2)
        result = collect(radio)
        second_done.set()
        return result

    monkeypatch.setattr(world, "_collect", fail_one)
    channels = [slot.channel for slot in world.radios.values()]
    for channel in channels:
        close = channel.close

        def checked_close(close=close):
            assert second_done.is_set()
            close()

        monkeypatch.setattr(channel, "close", checked_close)
    world.run("max")
    world.advance()
    assert world.state == "Ended" and "VCHAN_DISCONNECTED" in world.reason
    assert all(channel.closed for channel in channels)


def test_part_loader(stub_world, monkeypatch):
    from tools.conductor import conductor
    world = stub_world()
    calls = []
    monkeypatch.setattr(conductor.Path, "glob", lambda self, pattern: [conductor.Path("z.py"), conductor.Path("a.py")])
    monkeypatch.setattr(conductor.importlib, "import_module", lambda name: types.SimpleNamespace(install=lambda w: calls.append((name, w))))
    install_parts(world)
    assert calls == [("tools.conductor.parts.a", world), ("tools.conductor.parts.z", world)]


def test_socket_connected_client_receives_pause(stub_world, tmp_path):
    world = stub_world()
    world.api.bind("notice", tmp_path)
    world.start()
    held = True
    world.on_pausing(lambda: lambda: not held)
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(2)
        client.connect(str(world.api.path))
        world.api.poll(0)
        world.api.poll(0)
        client.recv(65536)
        world.run("max")
        world.pause()
        world.api.poll(0)
        events = [json.loads(line) for line in client.recv(65536).splitlines()]
        assert any(event.get("state") == "Pausing" for event in events)
        assert world.state == "Pausing"
        held = False
        world.advance()
        world.api.poll(0)
        events = [json.loads(line) for line in client.recv(65536).splitlines()]
        assert any(event.get("state") == "Paused" for event in events)


def test_firmware_step_all_radios(vhost_elf):
    from tools.conductor.world import Config, World
    world = World(Config(executable=str(vhost_elf), radios=2))
    try:
        world.start()
        world.step()
        finish_step(world)
        assert all(slot.done.time_us == world.now and slot.done.next_due_us > world.now for slot in world.radios.values())
        world.step(3500)
        finish_step(world)
        assert all(slot.done.time_us == 3500 and slot.done.next_due_us > 3500 for slot in world.radios.values())
    finally:
        world.close()


def test_power_cut_keeps_setting_and_repeats(vhost_elf):
    from tools.conductor.parts.dlmux import Parser, REPLY_MAX, frame
    from tools.conductor.world import Config, World

    def run():
        # RADIO op 5 saves the VFO frequency; op 8 only adjusts temporary squelch.
        world = World(Config(executable=str(vhost_elf),
                            radio_args=("--host-port", "on", "--storage", "steps:setting=vfo,1"),
                            inputs=(Input(1, Kind.BYTES_IN, frame(0x09, 0, b"\x00"), 1_000_000, 1),
                                    Input(1, Kind.BYTES_IN, frame(0x0A, 1, b"\x05" + (433550000).to_bytes(4, "little")),
                                          1_500_000, 1),
                                    Input(1, Kind.BYTES_IN, frame(0x09, 2, b"\x00"), 4_500_000, 1),
                                    Input(1, Kind.BYTES_IN, frame(0x09, 4, b"\x09"), 4_600_000, 1))))
        traces, replies = [], bytearray()
        world.on_outbox(Kind.TRACE, lambda radio, message: traces.append(message.payload))
        world.on_outbox(Kind.BYTES_OUT, lambda radio, message: replies.extend(message.payload))
        try:
            world.start()
            world.step(5_000_000)
            finish_step(world)
            packets = Parser(REPLY_MAX).feed(bytes(replies))
            assert ("frame", 0x8A, 1, b"\x00") in packets
            initial = next(p[3] for p in packets if p[:3] == ("frame", 0x89, 0))
            assert initial[0] == 0 and b"rx_hz=433500000\n" in initial
            view = next(p[3] for p in packets if p[:3] == ("frame", 0x89, 2))
            assert view[0] == 0 and b"rx_hz=433550000\n" in view
            storage = next(p[3] for p in packets if p[:3] == ("frame", 0x89, 4))
            assert storage[0] == 0 and b"settings.dirty=0\n" in storage
            assert b"settings.saves=1\n" in storage and b"settings.failures=0\n" in storage
            saved = Path(world._flash_path(1)).read_bytes()
            assert saved.startswith(b"RFFLASH1") and len(saved) > 8
            process = world.radios[1].channel.process
            process.kill()  # No firmware reboot or orderly shutdown can save RAM here.
            assert process.wait(timeout=5) < 0
            world.restart(1)
            assert world.boots[1] == 2 and world.state == "Paused"
            replies.clear()
            world.run("max")
            world.submit(1, Kind.BYTES_IN, frame(0x09, 3, b"\x00"), 1, time_us=6_500_000)
            world.pause()
            world.step(7_000_000)
            finish_step(world)
            packets = Parser(REPLY_MAX).feed(bytes(replies))
            view = next(p[3] for p in packets if p[:3] == ("frame", 0x89, 3))
            assert view[0] == 0 and b"rx_hz=433550000\n" in view
            assert Path(world._flash_path(1)).read_bytes() == saved
        finally:
            world.close()
        return traces

    first, second = run(), run()
    assert first and first == second


def test_flash_file_repeat(vhost_elf, tmp_path):
    traces, images = [], []
    for run in range(2):
        trace, flash = tmp_path / f"run{run}.log", tmp_path / f"run{run}.flash"
        result = subprocess.run([str(vhost_elf), "--until-ms", "6000", "--no-stall-watch",
                                 "--storage", "steps:setting=brightness,70", "--flash-file", str(flash),
                                 "--key", "1500:0:800", "--key", "2200:15:100", "--key", "2400:11:100",
                                 "--trace", str(trace)], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        traces.append(trace.read_bytes())
        images.append(flash.read_bytes())
    assert traces[0] and traces[0] == traces[1]
    assert b"BOARD flash.write" in traces[0]
    assert images[0].startswith(b"RFFLASH1") and len(images[0]) > 8 and images[0] == images[1]
