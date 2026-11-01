# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Real firmware checks of the channel; build the virtual board before running pytest."""

import binascii
import hashlib
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from channel import Channel, ChannelError, HEADER, Kind, Message, PAYLOAD_MAX, ROOT, VERSION


ELF = ROOT / "build/vhost/rfoundry_vhost.elf"


def trace(completed):
    return b"".join(message.payload + b"\n" for message in completed.outbox if message.kind == Kind.TRACE)


def run_boot(step_us):
    with Channel(ELF, "--until-ms", 4000, "--no-stall-watch") as channel:
        data = trace(channel.collect())
        for time_us in range(step_us, 3000001, step_us):
            completed = channel.grant(time_us)
            assert completed.time_us == time_us
            data += trace(completed)
        completed = channel.drain(3000000)
        assert completed.next_due_us > 3000000
        data += trace(completed)
    assert b"CREATE radio" in data and b"CREATE host" in data
    assert b"000003000000 " in data
    return data


def test_boot_3000ms_grant_independence(capfd):
    short, long = run_boot(1000), run_boot(3000000)
    assert short == long
    with capfd.disabled():
        print(f"\nboot 3000 ms, grants 1 ms = 3000 ms: sha256 {hashlib.sha256(short).hexdigest()}")


def debug_frame(command, payload):
    body = struct.pack("<BBH", command, 7, len(payload)) + payload
    return b"\xa5\x5a" + body + struct.pack("<H", binascii.crc_hqx(body, 0xFFFF))


def run_boundary_ping(step_us):
    boundary = 2800000  # a boundary of both 1 ms and 7 ms, after the host task starts
    with Channel(ELF, "--until-ms", 4000, "--host-port", "on", "--no-stall-watch") as channel:
        data = trace(channel.collect())
        for time_us in range(step_us, boundary + 1, step_us):
            completed = channel.grant(time_us)
            assert completed.time_us == time_us
            data += trace(completed)
        assert completed.rounds == 0 and completed.next_due_us == boundary
        request = debug_frame(1, b"boundary")
        channel.send(Message(Kind.BYTES_IN, request, boundary, cls=30, source=7, sequence=1))
        completed = channel.drain(boundary)
        replies = b"".join(m.payload for m in completed.outbox if m.kind == Kind.BYTES_OUT)
        assert replies == debug_frame(0x81, b"\0boundary")
        assert completed.time_us == boundary and completed.next_due_us > boundary
        assert completed.rounds >= 2  # channel input, then the UART arrival it causes
        assert all(m.time_us == boundary for m in completed.outbox)
        data += trace(completed)
        # The suspended clock call resumes normally after a drain.
        completed = channel.grant(boundary + 1000)
        assert completed.time_us == boundary + 1000
        data += trace(completed)
    return data


def test_boundary_ping_and_drain_grant_independence(capfd):
    short, long = run_boundary_ping(1000), run_boundary_ping(7000)
    assert short == long
    with capfd.disabled():
        print(f"\nboundary PING + drain T, grants 1 ms = 7 ms: sha256 {hashlib.sha256(short).hexdigest()}")


@pytest.mark.parametrize("size", [1032, 2048])
def test_message_roundtrip(size):
    message = Message(Kind.BYTES_OUT, bytes(i % 256 for i in range(size)), 1234567, 4, 30, 9, 1 << 40)
    assert Message.decode(message.encode()) == message


@pytest.mark.parametrize("version,kind,error", [
    (VERSION + 1, Kind.GRANT, "VCHAN_BAD_VERSION"),
    (VERSION, 255, "VCHAN_UNKNOWN_KIND"),
])
def test_named_wire_error(version, kind, error):
    wire = HEADER.pack(version, kind, 0, 1000, 0, 0, 0, 0)
    with pytest.raises(ChannelError, match=error):
        Message.decode(wire)
    with Channel(ELF, "--no-stall-watch") as channel:
        channel.collect()
        channel.socket.send(wire)
        with pytest.raises(ChannelError, match=error):
            channel.collect()
        assert channel.process.wait(timeout=5) == 4


def test_past_input_ends_run():
    with Channel(ELF, "--no-stall-watch") as channel:
        channel.collect()
        channel.grant(1000)
        channel.send(Message(Kind.BYTES_IN, b"x", 999, cls=30))
        with pytest.raises(ChannelError, match="VCHAN_PAST_INPUT"):
            channel.collect()
        assert channel.process.wait(timeout=5) == 4


def test_input_after_drain_refused():
    with Channel(ELF, "--no-stall-watch") as channel:
        channel.collect()
        channel.grant(1000)
        channel.drain(1000)
        channel.send(Message(Kind.BYTES_IN, b"x", 1000, cls=30))
        with pytest.raises(ChannelError, match="VCHAN_INPUT_AFTER_DRAIN"):
            channel.collect()
        assert channel.process.wait(timeout=5) == 4


def test_receive_queued_packet_avoids_timeout_poll(monkeypatch):
    channel = Channel.__new__(Channel)
    channel.socket = Mock()
    channel.socket.fileno.return_value = 3
    message = Message(Kind.TRACE, b"trace", 1000)
    read = Mock(return_value=message.encode())
    monkeypatch.setattr(os, "read", read)
    assert channel.receive() == message
    read.assert_called_once_with(3, HEADER.size + PAYLOAD_MAX + 1)
    channel.socket.recv.assert_not_called()


def test_receive_waits_with_socket_timeout_when_queue_empty(monkeypatch):
    channel = Channel.__new__(Channel)
    channel.socket = Mock()
    message = Message(Kind.TRACE, b"trace", 1000)
    monkeypatch.setattr(os, "read", Mock(side_effect=BlockingIOError))
    channel.socket.recv.return_value = message.encode()
    assert channel.receive() == message
    channel.socket.recv.assert_called_once_with(HEADER.size + PAYLOAD_MAX + 1)
    channel.socket.settimeout.assert_not_called()
    channel.socket.recv.side_effect = TimeoutError("timed out")
    channel._stderr_tail = "radio diagnostic"
    with pytest.raises(ChannelError, match="timed out\nradio stderr:\nradio diagnostic"):
        channel.receive()


@pytest.mark.parametrize("wire,reason", [
    (b"", "VCHAN_DISCONNECTED"),
    (b"short", "VCHAN_BAD_LENGTH"),
    (HEADER.pack(VERSION + 1, Kind.TRACE, 0, 0, 0, 0, 0, 0), "VCHAN_BAD_VERSION"),
    (HEADER.pack(VERSION, 255, 0, 0, 0, 0, 0, 0), "VCHAN_UNKNOWN_KIND"),
    (HEADER.pack(VERSION, Kind.TRACE, 1, 0, 0, 0, 0, 0), "VCHAN_BAD_LENGTH"),
    (Message(Kind.TRACE, b"x" * PAYLOAD_MAX).encode() + b"x", "VCHAN_BAD_LENGTH"),
])
def test_receive_rejects_bad_packets(monkeypatch, wire, reason):
    channel = Channel.__new__(Channel)
    channel.socket = Mock()
    monkeypatch.setattr(os, "read", Mock(return_value=wire))
    with pytest.raises(ChannelError, match=reason):
        channel.receive()


def test_receive_packet_boundaries_and_oversize():
    channel = Channel.__new__(Channel)
    receiver, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with receiver, sender:
        channel.socket = receiver
        receiver.settimeout(0.01)
        message = Message(Kind.TRACE, b"x" * PAYLOAD_MAX, 1000)
        for extra in (b"x", b"x" * 8192):
            sender.sendall(message.encode())
            sender.sendall(message.encode() + extra)
            sender.sendall(Message(Kind.TRACE, b"next").encode())
            assert channel.receive() == message
            with pytest.raises(ChannelError, match="VCHAN_BAD_LENGTH"):
                channel.receive()
            assert channel.receive() == Message(Kind.TRACE, b"next")
        with pytest.raises(ChannelError, match="timed out"):
            channel.receive()
        sender.shutdown(socket.SHUT_WR)
        with pytest.raises(ChannelError, match="VCHAN_DISCONNECTED"):
            channel.receive()


def test_close_drains_before_reaping(monkeypatch):
    channel = Channel.__new__(Channel)
    channel.socket, channel.process = Mock(), Mock()
    channel.socket.fileno.return_value = 3
    store = Message(Kind.TRACE, b"000000001000 0000000001 STORE " + b"a" * 64, 1000)
    monkeypatch.setattr(os, "read", Mock(side_effect=[store.encode(), b""]))
    assert channel.close() == (store,)
    channel.socket.shutdown.assert_called_once_with(socket.SHUT_WR)
    channel.socket.close.assert_called_once()
    channel.process.communicate.assert_called_once_with(timeout=5)
    channel.process.terminate.assert_not_called()
    channel.process.kill.assert_not_called()
    channel.socket.fileno.return_value = -1
    assert channel.close() == ()
    channel.socket.shutdown.assert_called_once()


def test_close_reaps_a_stuck_child(monkeypatch):
    channel = Channel.__new__(Channel)
    channel.socket, channel.process = Mock(), Mock()
    channel.socket.fileno.return_value = 3
    monkeypatch.setattr(os, "read", Mock(side_effect=TimeoutError))
    channel.process.communicate.side_effect = [subprocess.TimeoutExpired("vhost", 5),
                                              subprocess.TimeoutExpired("vhost", 5), (b"", b"")]
    assert channel.close() == ()
    channel.socket.close.assert_called_once()
    channel.process.terminate.assert_called_once()
    channel.process.kill.assert_called_once()
    assert channel.process.communicate.call_count == 3


def test_close_keeps_the_firmware_closing_store(vhost_elf):
    with Channel(vhost_elf, "--storage", "dmr-pair", "--no-stall-watch") as channel:
        boot = trace(channel.collect())
        channel.grant(1000)
        channel.drain(1000)
        closing = channel.close()
    closing_trace = b"\n".join(m.payload for m in closing if m.kind == Kind.TRACE)
    assert b"000000000000 " in boot and b" STORE " in boot
    assert b"000000001000 " in closing_trace and b" STORE " in closing_trace
    assert b" END " in closing_trace
    assert channel.process.returncode == 0
