# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Host caches of the debug link's safe reads, so observers never reach the firmware.

An observer is a client that may only watch (ADR 0004 rule 3). Its reads (VERSION, GET_DIAG, GET_VIEW,
INFO_CHIP, and the log) are answered from these caches. The conductor fills them itself, at a fixed
virtual-time cadence, whether or not anyone watches. So the firmware sees the same requests with zero
observers and with three, and the run does not change (docs/dev/virtual-radio-architecture.md, "What an
observer is").

Example: with the default cadence of 250000 us, at virtual times 250 ms, 500 ms, ... the multiplexer
(dlmux.py) asks radio 1 for GET_VIEW section 0, follows each reply's "next section" to the last one, reads
GET_DIAG the same way, and drains GET_LOG. VERSION and INFO_CHIP are read once per boot. An observer's
GET_VIEW 6 then gets the last stored reply for section 6, re-framed with the observer's own sequence number.

The log is special: a firmware read removes lines (core/debuglink/dlog.c:68-96). The conductor drains it
once and keeps the lines here. Each client has its own cursor, so every client sees every line.

The cadence is a part option, fixed when the first radio starts: install(world, cadence_us=...) or
dlcache.get(world, cadence_us=...) before start. A scenario file may carry it as {"dlmux": {"cadence_us": N}};
scenario_cadence(path) reads it. It is recorded as a conductor event (events.jsonl) when it is fixed.
This part keeps the stores; dlmux.py owns the upstream requests.
"""

import json
from pathlib import Path
import struct

DEFAULT_CADENCE_US = 250_000
MIN_CADENCE_US = 10_000
LOG_LINES_MAX = 4000
REPLY_MAX = 1024        # docs/spec/debuglink.md 2: a reply payload is at most 1024 bytes
LAST = 0xFF             # GET_VIEW / GET_DIAG "next" after the last section or line

PING, VERSION, GET_DIAG, GET_LOG, GET_VIEW, INFO_CHIP = 0x01, 0x02, 0x03, 0x08, 0x09, 0x0B
ST_OK, ST_BUSY = 0, 3


def scenario_cadence(path):
    """The cadence a scenario file asks for, or the default."""
    data = json.loads(Path(path).read_text())
    value = data.get("dlmux", {}).get("cadence_us", DEFAULT_CADENCE_US)
    return check_cadence(value)


def check_cadence(value):
    if type(value) is not int or not MIN_CADENCE_US <= value <= 10_000_000:
        raise ValueError("DLCACHE_BAD_CADENCE")
    return value


class Store:
    """The cached replies of one radio boot. Each value is a reply payload (status byte first)."""

    def __init__(self):
        self.version = None
        self.chip = None
        self.views = {}
        self.diag = {}
        self.log = []           # text lines, oldest first
        self.base = 0           # absolute index of self.log[0]

    def add_log(self, text):
        self.log.extend(line for line in text.split("\n") if line)
        extra = len(self.log) - LOG_LINES_MAX
        if extra > 0:
            del self.log[:extra]
            self.base += extra


class Caches:
    def __init__(self, world, cadence_us=DEFAULT_CADENCE_US):
        self.world = world
        self.cadence_us = check_cadence(cadence_us)
        self.fixed = False
        self.stores = {}        # (radio, boot) -> Store
        world.add_radio_args(self._fix)

    def _fix(self, radio):
        if not self.fixed:
            self.fixed = True
            recorder = getattr(self.world, "recorder", None)
            if recorder is not None:
                recorder.conductor_event("dlcache_cadence", cadence_us=self.cadence_us)
        return ()

    def set_cadence(self, cadence_us):
        cadence_us = check_cadence(cadence_us)
        if cadence_us != self.cadence_us and self.fixed:
            raise ValueError("DLCACHE_CADENCE_FIXED")
        self.cadence_us = cadence_us

    def next_tick(self, now):
        return (now // self.cadence_us + 1) * self.cadence_us

    def store(self, radio):
        """The store for the radio's current boot; older boots are forgotten."""
        boot = self.world.boots.get(radio)
        for key in [key for key in self.stores if key[0] == radio and key[1] != boot]:
            del self.stores[key]
        return self.stores.setdefault((radio, boot), Store())

    def tick_requests(self, radio):
        """The (cmd, payload) requests one refresh starts with."""
        store = self.store(radio)
        requests = []
        if store.version is None:
            requests.append((VERSION, b""))
        if store.chip is None:
            requests.append((INFO_CHIP, b""))
        requests += [(GET_VIEW, b"\x00"), (GET_DIAG, b"\x00"), (GET_LOG, b"")]
        return requests

    def reply(self, radio, cmd, request, payload):
        """Store a reply to a cache request. Returns the follow-up (cmd, payload) requests."""
        if not payload or payload[0] != ST_OK:
            return []
        store = self.store(radio)
        if cmd == VERSION:
            store.version = payload
        elif cmd == INFO_CHIP:
            store.chip = payload
        elif cmd in (GET_VIEW, GET_DIAG) and len(payload) >= 6:
            first = request[0] if request else 0
            (store.views if cmd == GET_VIEW else store.diag)[first] = payload
            following = payload[5]
            if following != LAST and following > first:
                return [(cmd, bytes((following,)))]
        elif cmd == GET_LOG and len(payload) >= 6:
            store.add_log(payload[6:].decode("ascii", errors="replace"))
            if payload[5]:
                return [(GET_LOG, b"")]
        return []

    def answer(self, radio, cmd, request, cursors):
        """A reply payload from the cache, or a busy status when nothing is stored yet.

        cursors is the asking client's dict radio -> next log line index.
        """
        store = self.store(radio)
        if cmd == VERSION:
            found = store.version
        elif cmd == INFO_CHIP:
            found = store.chip
        elif cmd in (GET_VIEW, GET_DIAG):
            found = (store.views if cmd == GET_VIEW else store.diag).get(request[0] if request else 0)
        elif cmd == GET_LOG:
            return self._log(radio, store, cursors)
        else:
            found = None
        return found if found is not None else bytes((ST_BUSY,))

    def _log(self, radio, store, cursors):
        cursor = cursors.get(radio, store.base)
        lost = max(0, store.base - cursor)
        index = max(cursor, store.base) - store.base
        text = bytearray()
        while index < len(store.log):
            line = store.log[index].encode("ascii", errors="replace") + b"\n"
            if 6 + len(text) + len(line) > REPLY_MAX:
                break
            text += line
            index += 1
        cursors[radio] = store.base + index
        more = int(index < len(store.log))
        return bytes((ST_OK,)) + struct.pack("<I", lost) + bytes((more,)) + bytes(text)


def get(world, cadence_us=None):
    caches = getattr(world, "dlcache", None)
    if caches is None:
        caches = world.dlcache = Caches(world, DEFAULT_CADENCE_US if cadence_us is None else cadence_us)
    elif cadence_us is not None:
        caches.set_cadence(cadence_us)
    return caches


def install(world, cadence_us=None):
    get(world, cadence_us)
