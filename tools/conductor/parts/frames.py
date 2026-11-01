# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Panel observations, never firmware inputs (ADR 0007).

frames.sock is a read-only Unix stream. Each record is a little-endian u32
length, then the 72-byte vframe.h header (offset zero, world time), then the
complete native panel bytes. Its header contains the corresponding checkpoint.
Queues hold at most QUEUE_LIMIT records plus one partially sent record, which
must finish before another record can be sent. A new observer gets the latest
panel for each radio. finish() drains asynchronously; close() aborts delivery.
"""

from collections import deque
from dataclasses import asdict, dataclass, field
import os
import selectors
import socket
import struct
import threading
import zlib

from ..history import History
from ..channel import ChannelError, Kind, PAYLOAD_MAX
from ..worldapi import peer_uid, private_directory

HEADER = struct.Struct("<IIIQIIIHHBBBB16H")
LENGTH = struct.Struct("<I")
PANEL_MAX = 240 * 320 * 2  # boards/vhost/display_profiles_vhost.c
QUEUE_LIMIT = 32


@dataclass(frozen=True)
class Checkpoint:
    radio: int
    boot: int
    frame_id: int
    time_us: int
    crc: int
    palette: tuple
    backlight: int


@dataclass
class Observer:
    pending: deque = field(default_factory=lambda: deque(maxlen=QUEUE_LIMIT))
    sending: memoryview = field(default_factory=lambda: memoryview(b""))


class Frames:
    def __init__(self, world, *, uid_reader=peer_uid):
        self.world = world
        self.uid_reader = uid_reader
        self.checkpoints = History(encode=asdict, decode=lambda row: Checkpoint(**{**row, "palette": tuple(row["palette"])}))
        self.latest = {}
        self.clients = {}
        self.path = None
        self.listener = None
        self.thread = None
        self._partial = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._finished = threading.Event()
        world.on_outbox(Kind.FRAME, self.receive)
        world.add_radio_args(self.radio_args)
        world.on_end(self.finish)

    def radio_args(self, radio):
        self._partial.pop(radio, None)
        if self.listener is None and self.world.api.directory is not None:
            self.bind()
        return ["--radio-id", str(radio), "--boot-id", str(self.world.boots[radio] + 1)]

    def receive(self, radio, message):
        payload = message.payload
        if not HEADER.size < len(payload) <= PAYLOAD_MAX:
            raise ChannelError("FRAME_BAD_LENGTH")
        values = list(HEADER.unpack_from(payload))
        (identity, boot, frame_id, local_us, crc, size, offset, width, height,
         bpp, backlight, has_palette, reserved, *palette) = values
        slot = self.world.radios[radio]
        if (identity != radio or boot != slot.boot or
                local_us + slot.origin != message.time_us):
            raise ChannelError("FRAME_BAD_IDENTITY")
        if (bpp not in (1, 4, 16) or not width or not height or
                not 0 < size <= PANEL_MAX or size != ((width * bpp + 7) // 8) * height or
                backlight > 100 or has_palette != int(bpp == 4) or reserved or
                (not has_palette and any(palette))):
            raise ChannelError("FRAME_BAD_PANEL")
        values[3], values[6] = message.time_us, 0
        header = HEADER.pack(*values)
        if offset == 0:
            if radio in self._partial:
                raise ChannelError("FRAME_INCOMPLETE")
            self._partial[radio] = (header, bytearray())
        partial = self._partial.get(radio)
        if partial is None or partial[0] != header or offset != len(partial[1]):
            raise ChannelError("FRAME_BAD_CHUNK")
        panel = partial[1]
        if offset + len(payload) - HEADER.size > size:
            raise ChannelError("FRAME_BAD_LENGTH")
        panel.extend(payload[HEADER.size:])
        if len(panel) != size:
            return
        del self._partial[radio]
        if zlib.crc32(panel) != crc:
            raise ChannelError("FRAME_BAD_CRC")
        checkpoint = Checkpoint(radio, boot, frame_id, message.time_us, crc,
                                tuple(palette) if has_palette else (), backlight)
        record = LENGTH.pack(HEADER.size + size) + header + panel
        self.checkpoints.append(checkpoint)
        # No socket calls under this lock: observers cannot hold up a grant.
        with self._lock:
            self.latest[radio] = record
            for observer in self.clients.values():
                observer.pending.append(record)
        self._wake.set()

    def bind(self):
        directory = self.world.api.directory
        if directory is None:
            raise ValueError("FRAME_NO_WORLD_DIRECTORY")
        if self.listener is not None:
            raise ValueError("FRAME_ALREADY_BOUND")
        private_directory(directory)
        path = directory / "frames.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            previous = os.umask(0o177)
            try:
                listener.bind(str(path))
                bound = True
            finally:
                os.umask(previous)
            os.chmod(path, 0o600)
            listener.listen()
            listener.setblocking(False)
            self.path, self.listener = path, listener
            # Keep the final checkpoint deliverable after the conductor stops its radios.
            self.thread = threading.Thread(target=self._serve, name="frame-observers")
            self.thread.start()
        except BaseException:
            listener.close()
            if bound:
                path.unlink(missing_ok=True)
            self.listener = None
            raise
        return path

    def _drop(self, selector, connection):
        selector.unregister(connection)
        with self._lock:
            self.clients.pop(connection, None)
        connection.close()

    def _accept(self, selector):
        connection, _ = self.listener.accept()
        try:
            if self.uid_reader(connection) != os.getuid():
                connection.close()
                return
            connection.setblocking(False)
            observer = Observer()
            selector.register(connection, selectors.EVENT_READ)
            with self._lock:
                observer.pending.extend(self.latest.values())
                self.clients[connection] = observer
        except OSError:
            connection.close()

    def _write(self, connection, observer):
        if not observer.sending:
            with self._lock:
                if observer.pending:
                    observer.sending = memoryview(observer.pending.popleft())
        if observer.sending:
            sent = connection.send(observer.sending)
            if not sent:
                raise ConnectionError("FRAME_DISCONNECTED")
            observer.sending = observer.sending[sent:]

    def _serve(self):
        with selectors.DefaultSelector() as selector:
            selector.register(self.listener, selectors.EVENT_READ)
            try:
                while not self._stop.is_set():
                    self._wake.clear()
                    if self._finished.is_set() and self.listener.fileno() != -1:
                        selector.unregister(self.listener)
                        self.listener.close()
                    for connection, observer in list(self.clients.items()):
                        waiting = bool(observer.sending or observer.pending)
                        if self._finished.is_set() and not waiting:
                            self._drop(selector, connection)
                        else:
                            selector.modify(connection, selectors.EVENT_READ |
                                            (selectors.EVENT_WRITE if waiting else 0))
                    if self._finished.is_set() and not self.clients:
                        return
                    events = selector.select(0)
                    for key, mask in events:
                        connection = key.fileobj
                        try:
                            if connection is self.listener:
                                if not self._finished.is_set():
                                    self._accept(selector)
                                continue
                            # The stream grants no control authority, including on malformed input.
                            if mask & selectors.EVENT_READ:
                                # Drain first: closing with unread bytes sends RST, not a clean EOF.
                                try:
                                    while connection.recv(65536):
                                        pass
                                except BlockingIOError:
                                    pass
                                self._drop(selector, connection)
                            elif mask & selectors.EVENT_WRITE:
                                self._write(connection, self.clients[connection])
                        except BlockingIOError:
                            continue
                        except OSError:
                            if connection is not self.listener:
                                self._drop(selector, connection)
                    if not events:
                        self._wake.wait(0.01)
            finally:
                for connection in list(self.clients):
                    self._drop(selector, connection)
                self.listener.close()
                self.path.unlink(missing_ok=True)

    def finish(self):
        self._finished.set()
        self._wake.set()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self.thread is not None:
            self.thread.join()


def install(world):
    world.frames = Frames(world)
