# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""One radio's packet channel. World time, pacing and lifecycle belong to the conductor."""

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
import hashlib
import os
import re
import socket
import struct
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
_PROTO = (ROOT / "platform/host/vchan_proto.h").read_text()
_KINDS = re.search(r"enum vchan_kind \{(.*?)\};", _PROTO, re.S)[1]
# The C header owns kind numbers, including reservations for later board parts.
Kind = IntEnum("Kind", {name: int(value) for name, value in
                       re.findall(r"^\s*VCHAN_([A-Z_]+) = (\d+),", _KINDS, re.M)})
VERSION = int(re.search(r"#define VCHAN_VERSION (\d+)u", _PROTO)[1])
PAYLOAD_MAX = int(re.search(r"#define VCHAN_PAYLOAD_MAX (\d+)u", _PROTO)[1])
HEADER = struct.Struct("<BBHQIIIQ")
DONE = struct.Struct("<QQ")
NEVER = (1 << 64) - 1
STDERR_LIMIT = 65536


class ChannelError(RuntimeError):
    """A named protocol error or a radio that ended before DONE."""


@dataclass(frozen=True)
class Message:
    kind: Kind
    payload: bytes = b""
    time_us: int = 0
    round: int = 0
    cls: int = 0
    source: int = 0
    sequence: int = 0

    def encode(self):
        if len(self.payload) > PAYLOAD_MAX:
            raise ChannelError("VCHAN_BAD_LENGTH")
        try:
            kind = Kind(self.kind)
        except ValueError as error:
            raise ChannelError("VCHAN_UNKNOWN_KIND") from error
        return HEADER.pack(VERSION, kind, len(self.payload), self.time_us, self.round,
                           self.cls, self.source, self.sequence) + self.payload

    @classmethod
    def decode(cls, wire):
        if len(wire) < HEADER.size or len(wire) > HEADER.size + PAYLOAD_MAX:
            raise ChannelError("VCHAN_BAD_LENGTH")
        version, kind, length, time_us, round_, event_class, source, sequence = HEADER.unpack_from(wire)
        if version != VERSION:
            raise ChannelError("VCHAN_BAD_VERSION")
        try:
            kind = Kind(kind)
        except ValueError as error:
            raise ChannelError("VCHAN_UNKNOWN_KIND") from error
        if length != len(wire) - HEADER.size:
            raise ChannelError("VCHAN_BAD_LENGTH")
        return cls(kind, wire[HEADER.size:], time_us, round_, event_class, source, sequence)


@dataclass(frozen=True)
class Completed:
    time_us: int
    next_due_us: int
    rounds: int
    outbox: tuple[Message, ...]


class Channel:
    def __init__(self, executable, *args, timeout=10):
        self.socket, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.socket.settimeout(timeout)
        try:
            # A pipe can fill before the first DONE and hide the startup error.
            self._stderr = tempfile.TemporaryFile()
            self._stderr_tail = ""
            self.process = subprocess.Popen(
                [str(executable), *map(str, args), "--chan", str(child.fileno())],
                pass_fds=(child.fileno(),), stdout=subprocess.DEVNULL, stderr=self._stderr)
            self.executable_sha256 = None
            try:
                with open(f"/proc/{self.process.pid}/exe", "rb") as binary:
                    self.executable_sha256 = hashlib.file_digest(binary, "sha256").hexdigest()
            except FileNotFoundError:
                # An already exited child has no /proc executable; collect() must report its stderr.
                if self.process.poll() is None:
                    raise
        except BaseException:
            if getattr(self, "process", None) is not None:
                self.process.kill()
                self.process.wait()
            self.socket.close()
            if hasattr(self, "_stderr"):
                self._stderr.close()
            raise
        finally:
            child.close()

    def stderr_text(self):
        stream = getattr(self, "_stderr", None)
        if stream is None or stream.closed:
            return getattr(self, "_stderr_tail", "")
        size = os.fstat(stream.fileno()).st_size
        # pread leaves the child's shared file offset unchanged.
        data = os.pread(stream.fileno(), STDERR_LIMIT, max(0, size - STDERR_LIMIT))
        return ("[stderr truncated]\n" if size > STDERR_LIMIT else "") + data.decode("utf-8", errors="replace").strip()

    def error(self, reason):
        stderr = self.stderr_text()
        return ChannelError(f"{reason}\nradio stderr:\n{stderr}" if stderr else str(reason))

    def send(self, message):
        wire = message.encode()
        try:
            if self.socket.send(wire) != len(wire):
                raise ChannelError("VCHAN_SEND_FAILED")
        except (OSError, ChannelError) as error:
            raise self.error(error) from error

    def receive(self):
        try:
            # Read queued packets without a timeout poll; one extra byte detects truncation.
            size = HEADER.size + PAYLOAD_MAX + 1
            try:
                wire = os.read(self.socket.fileno(), size)
            except BlockingIOError:
                wire = self.socket.recv(size)
            if not wire:
                raise ChannelError("VCHAN_DISCONNECTED")
            return Message.decode(wire)
        except (OSError, ChannelError) as error:
            raise self.error(error) from error

    def collect(self):
        """Read the initial DONE or a grant/drain's ordered outbox through DONE."""
        outbox = []
        while True:
            message = self.receive()
            if message.kind == Kind.RESTART_REASON:
                raise self.error(message.payload.decode("utf-8", errors="replace"))
            if message.kind == Kind.DONE:
                if len(message.payload) != DONE.size:
                    raise ChannelError("VCHAN_BAD_DONE")
                next_due, rounds = DONE.unpack(message.payload)
                return Completed(message.time_us, next_due, rounds, tuple(outbox))
            outbox.append(message)

    def grant(self, time_us):
        self.send(Message(Kind.GRANT, time_us=time_us))
        return self.collect()

    def drain(self, time_us):
        self.send(Message(Kind.DRAIN, time_us=time_us))
        return self.collect()

    def close(self):
        """Let the radio finish and return its last outbox, then reap this driver's child."""
        if self.socket.fileno() < 0:
            return ()
        outbox = []
        try:
            # EOF ends input while the read half remains open for the closing STORE.
            self.socket.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + 5
            while (remaining := deadline - time.monotonic()) > 0:
                self.socket.settimeout(remaining)
                outbox.append(self.receive())
        except (OSError, ChannelError):
            pass
        finally:
            self.socket.close()
            try:
                self.process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.communicate()
            if hasattr(self, "_stderr"):
                self._stderr_tail = self.stderr_text()
                self._stderr.close()
        return tuple(outbox)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
