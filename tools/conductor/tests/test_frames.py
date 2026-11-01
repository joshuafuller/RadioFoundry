# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""FRAME assembly and observer isolation, including real max-mode grant timings."""

from dataclasses import replace
import os
import socket
import stat
import struct
import threading
import time
import zlib

import pytest

from tools.conductor.channel import ChannelError, Kind, Message, PAYLOAD_MAX
from tools.conductor.parts.frames import Checkpoint, HEADER, LENGTH, Observer, QUEUE_LIMIT, install
from tools.conductor.world import Config, World


def chunks(frame_id=1, *, radio=1, boot=1, time_us=0, width=160, height=128,
           bpp=16, backlight=75):
    panel = bytes([frame_id % 256]) * (((width * bpp + 7) // 8) * height)
    palette = tuple(range(16)) if bpp == 4 else (0,) * 16
    crc = zlib.crc32(panel)
    messages = []
    for offset in range(0, len(panel), PAYLOAD_MAX - HEADER.size):
        header = struct.pack("<IIIQIIIHHBBBB16H", radio, boot, frame_id, time_us,
                             crc, len(panel), offset, width, height, bpp,
                             backlight, int(bpp == 4), 0, *palette)
        data = panel[offset:offset + PAYLOAD_MAX - HEADER.size]
        messages.append(Message(Kind.FRAME, header + data, time_us))
    return messages


def publish(frames, messages, *, origin=0):
    for message in messages:
        frames.receive(HEADER.unpack_from(message.payload)[0],
                       replace(message, time_us=message.time_us + origin))


def unpack_record(record):
    size, = LENGTH.unpack_from(record)
    assert size == len(record) - LENGTH.size
    values = HEADER.unpack_from(record, LENGTH.size)
    (radio, boot, frame_id, time_us, crc, panel_size, offset, _, _, _, backlight,
     has_palette, _, *palette) = values
    panel = record[LENGTH.size + HEADER.size:]
    assert offset == 0 and len(panel) == panel_size
    assert zlib.crc32(panel) == crc
    return Checkpoint(radio, boot, frame_id, time_us, crc,
                      tuple(palette) if has_palette else (), backlight)


@pytest.fixture
def frame_world(stub_world):
    world = stub_world(radios=2)
    install(world)
    world.start()
    try:
        yield world
    finally:
        world.frames.close()


@pytest.mark.parametrize("bpp,width,height", [(16, 240, 320), (4, 160, 120), (1, 13, 7)])
def test_complete_panel_hashes_to_checkpoint(frame_world, bpp, width, height):
    frames = frame_world.frames
    messages = chunks(bpp=bpp, width=width, height=height)
    publish(frames, messages[:-1])
    assert not frames.checkpoints and not frames.latest
    publish(frames, messages[-1:])
    assert unpack_record(frames.latest[1]) == frames.checkpoints[0]
    assert frames.checkpoints[0].palette == (tuple(range(16)) if bpp == 4 else ())
    publish(frames, chunks(2, bpp=bpp, width=width, height=height))
    assert len(frames.checkpoints) == 2


def test_radio_boot_count_and_world_time(frame_world):
    world, frames = frame_world, frame_world.frames
    for radio in (1, 2):
        assert world.radios[radio].channel.args[2:6] == ("--radio-id", str(radio), "--boot-id", "1")
        publish(frames, chunks(radio=radio))
    world.step(5000)
    world.advance()
    publish(frames, chunks(2)[:1])  # A power cut may leave an unfinished panel.
    world.restart(1)
    assert world.radios[1].channel.args[2:6] == ("--radio-id", "1", "--boot-id", "2")
    publish(frames, chunks(1, boot=2, time_us=10), origin=5000)
    assert [(c.radio, c.boot, c.frame_id, c.time_us) for c in frames.checkpoints] == [
        (1, 1, 1, 0), (2, 1, 1, 0), (1, 2, 1, 5010)]
    assert unpack_record(frames.latest[1]) == frames.checkpoints[-1]


@pytest.mark.parametrize("offset,fmt,value,error", [
    (0, "I", 2, "IDENTITY"), (4, "I", 2, "IDENTITY"), (12, "Q", 1, "IDENTITY"),
    (24, "I", 0xffffffff, "PANEL"), (32, "H", 0, "PANEL"),
    (36, "B", 8, "PANEL"), (37, "B", 101, "PANEL"),
    (38, "B", 1, "PANEL"), (39, "B", 1, "PANEL"), (40, "H", 1, "PANEL"),
    (28, "I", 1, "CHUNK"), (20, "I", 0, "CRC"),
])
def test_invalid_frame_is_not_published(frame_world, offset, fmt, value, error):
    message = chunks(width=8, height=1, bpp=1)[0]
    payload = bytearray(message.payload)
    struct.pack_into("<" + fmt, payload, offset, value)
    with pytest.raises(ChannelError, match="FRAME_BAD_" + error):
        frame_world.frames.receive(1, replace(message, payload=bytes(payload)))
    assert not frame_world.frames.checkpoints and not frame_world.frames.latest


def test_chunk_gaps_header_changes_and_truncation(frame_world):
    frames = frame_world.frames
    messages = chunks()
    publish(frames, messages[:1])
    with pytest.raises(ChannelError, match="FRAME_INCOMPLETE"):
        publish(frames, messages[:1])
    with pytest.raises(ChannelError, match="FRAME_BAD_CHUNK"):
        publish(frames, messages[2:3])
    changed = bytearray(messages[1].payload)
    struct.pack_into("<I", changed, 8, 999)
    with pytest.raises(ChannelError, match="FRAME_BAD_CHUNK"):
        publish(frames, [replace(messages[1], payload=bytes(changed))])
    with pytest.raises(ChannelError, match="FRAME_BAD_LENGTH"):
        frames.receive(1, replace(messages[1], payload=b"short"))
    with pytest.raises(ChannelError, match="FRAME_BAD_LENGTH"):
        publish(frames, [replace(messages[1], payload=messages[1].payload + b"x")])
    publish(frames, messages[1:])
    assert len(frames.checkpoints) == 1


def test_latest_wins_queue_preserves_partial_record(frame_world):
    frames = frame_world.frames
    observer = Observer()

    class Writer:
        blocked = False
        data = bytearray()

        def send(self, data):
            if self.blocked:
                raise BlockingIOError()
            self.data.extend(data[:7])
            return min(len(data), 7)

    writer = Writer()
    frames.clients[writer] = observer
    publish(frames, chunks(1, width=8, height=1, bpp=1))
    first = frames.latest[1]
    frames._write(writer, observer)
    writer.blocked = True
    for frame_id in range(2, QUEUE_LIMIT + 10):
        publish(frames, chunks(frame_id, width=8, height=1, bpp=1))
    channel = frame_world.radios[1].channel
    channel.outputs.extend((Kind.FRAME, m.payload) for m in
                           chunks(QUEUE_LIMIT + 10, time_us=1000, width=8, height=1, bpp=1))
    frame_world.step(1000)
    frame_world.advance()
    assert frame_world.state == "Paused" and frame_world.now == 1000
    assert frame_world.radios[1].done.time_us == 1000
    assert len(observer.pending) == QUEUE_LIMIT
    before = bytes(observer.sending)
    with pytest.raises(BlockingIOError):
        frames._write(writer, observer)
    assert bytes(observer.sending) == before
    writer.blocked = False
    while observer.sending or observer.pending:
        frames._write(writer, observer)
    assert writer.data[:len(first)] == first
    seen = []
    while writer.data:
        length = LENGTH.unpack_from(writer.data)[0] + LENGTH.size
        seen.append(unpack_record(writer.data[:length]))
        del writer.data[:length]
    assert seen[0] == frames.checkpoints[0]
    assert seen[1:] == frames.checkpoints[-QUEUE_LIMIT:]


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "frame worker did not respond"
        time.sleep(0.001)


def connect(frames):
    count = len(frames.clients)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(10)
    client.connect(str(frames.path))
    wait_for(lambda: len(frames.clients) == count + 1)
    return client


def read_record(client):
    def exact(size):
        data = bytearray()
        while len(data) < size:
            part = client.recv(size - len(data))
            if not part:
                assert not data, "truncated frame stream"
                return b""
            data.extend(part)
        return bytes(data)

    prefix = exact(LENGTH.size)
    if not prefix:
        return None
    data = exact(LENGTH.unpack(prefix)[0])
    assert data, "missing frame body"
    return unpack_record(prefix + data)


def test_socket_permissions_late_observer_and_read_only(frame_world, tmp_path):
    world, frames = frame_world, frame_world.frames
    world.api.bind("frames", tmp_path)
    frames.bind()
    assert stat.S_IMODE(frames.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(frames.path.parent.stat().st_mode) == 0o700
    publish(frames, chunks())
    with connect(frames) as client:
        assert read_record(client) == frames.checkpoints[-1]
        sent = list(world.radios[1].channel.sent)
        client.sendall(b'{"command":"run"}\n')
        wait_for(lambda: not frames.clients)
        assert client.recv(1) == b""
        assert world.state == "Paused" and world.radios[1].channel.sent == sent
    world.end()
    frames.thread.join(3)
    assert not frames.thread.is_alive() and not frames.path.exists()


@pytest.mark.parametrize("failure", ["different_uid", "unreadable_uid"])
def test_socket_rejects_untrusted_peer(frame_world, tmp_path, failure):
    frames = frame_world.frames
    frame_world.api.bind("refused", tmp_path)

    def uid_reader(connection):
        if failure == "unreadable_uid":
            raise OSError("credentials unavailable")
        return os.getuid() + 1

    frames.uid_reader = uid_reader
    frames.bind()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(frames.path))
        assert client.recv(1) == b"" and not frames.clients


def test_socket_install_binds_before_radio_start(stub_world, tmp_path):
    world = stub_world()
    install(world)
    world.api.bind("installed", tmp_path)
    try:
        world.start()
        assert world.frames.path == world.api.directory / "frames.sock"
        with connect(world.frames) as client:
            world.radios[1].channel.outputs.extend((Kind.FRAME, m.payload) for m in chunks(time_us=1000))
            world.step(1000)
            world.advance()
            assert read_record(client) == world.frames.checkpoints[-1]
    finally:
        world.frames.close()


def test_firmware_observers_preserve_checkpoints(tmp_path, request, capsys):
    times = {False: [], True: []}
    checkpoints = []
    fast, slow = [], []
    errors = []

    def reader(client, received):
        try:
            while (checkpoint := read_record(client)) is not None:
                received.append(checkpoint)
        except Exception as error:
            errors.append(error)

    for observed in (False, True):
        fast.clear()
        slow.clear()
        world = World(Config(end_us=60000000))
        install(world)
        frames = world.frames
        clients, readers = [], []
        try:
            # Fail at the denied socket before asking for a firmware build in restricted sandboxes.
            world.api.bind(f"{'observed' if observed else 'baseline'}{len(checkpoints)}", tmp_path)
            executable = request.getfixturevalue("vhost_elf")
            world.config = replace(world.config, executable=str(executable))
            frames.bind()
            if observed:
                for received in (fast, slow):
                    connections = set(frames.clients)
                    client = connect(frames)
                    clients.append(client)
                    if received is slow:
                        # Withhold reads until world end; bound buffering so the queue must overflow.
                        connection, = set(frames.clients) - connections
                        connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
                        continue
                    thread = threading.Thread(target=reader, args=(client, received), daemon=True)
                    readers.append(thread)
                    thread.start()
            # Identical grant boundaries in both runs; observers never change the pacing cap.
            world.add_grant_limit(lambda now: now + 20000)
            started = time.monotonic()
            world.start()
            world.run("max")
            while world.state != "Ended":
                assert time.monotonic() - started < 60, "max-mode world did not finish"
                world.advance()
            times[observed].append(time.monotonic() - started)
            assert world.reason == "WORLD_END_TIME"
            checkpoints.append(list(frames.checkpoints))
            if observed:
                reader(clients[-1], slow)
            for thread in readers:
                thread.join(30)
                assert not thread.is_alive(), "observer did not receive the final frame"
            assert not errors, errors
            if observed:
                positions_fast = [frames.checkpoints.index(checkpoint) for checkpoint in fast]
                assert positions_fast == sorted(set(positions_fast))
                assert fast[-1] == frames.checkpoints[-1]
                positions = [frames.checkpoints.index(checkpoint) for checkpoint in slow]
                assert positions == sorted(set(positions))
                assert 0 < len(slow) < len(frames.checkpoints) and slow[-1] == frames.checkpoints[-1]
        finally:
            frames.close()
            for client in clients:
                client.close()
            for thread in readers:
                thread.join(2)
            world.close()
    base, watched = min(times[False]), min(times[True])
    with capsys.disabled():
        print(f"\nmax mode (measurement only): without slow observer={base:.6f}s; "
              f"with unread observer={watched:.6f}s; "
              f"difference={abs(watched - base) / base:.2%}; "
              f"all={times}")
        print(f"checkpoints={len(checkpoints[0])}; fast={len(fast)}; slow={len(slow)}")
    assert checkpoints[0] and all(c == checkpoints[0] for c in checkpoints)
