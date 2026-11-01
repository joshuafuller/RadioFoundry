# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Read-only world API messages for observers, such as the multi-radio console (tools/console.py).

The problem. A world client sees world events (state, air decisions and deliveries, air policy, faults) only from
the moment it connects, and some facts are never sent at all: accepted inputs, scheduled faults, the speed ratio,
and the air seed once air_config has passed. An observer must not read the world folder files to fill the gap
(the "no private path" rule), so this part keeps a copy here and answers two read-only messages:

    observe.status                    world state, virtual time, ratio, air setup and policy, radios,
                                      scheduled faults, assertions, and the event cursor
    observe.events  after, [limit]    the history events with a sequence number above after, oldest first

How it stays read-only. It only listens: an input hook, an audit sink and a wrapper around world.notify. It adds
no grant limit, no grant-done hook and no input, and it sends nothing to a radio. Its two handlers are registered
read_only (worldapi.py), so a Pausing or Stepping world answers them without a WORLD_BUSY audit line. It never
broadcasts: an observer pulls. A pushed line per input would fill the 64 KiB queue of a client that reads only
its own replies (run_hil.py WorldClient) and get it dropped.

What an event holds. Each one gets "seq" (1, 2, 3 ...) and keeps its world "time_us". Inputs are summaries
(radio, kind, time_us, source, sequence; key bit and up or down for KEY; detents for ENC), never payload bytes,
because a debug link input can carry a lease secret. AIR_IN inputs are left out: air_delivery already says the same.
air_delivery drops its payload hex. Audit lines are already cleaned of secrets (audit.py).

Example. PTT down on radio 1 at 3 000 000 us appears as
{"seq": 41, "event": "input", "time_us": 3000000, "radio": 1, "kind": "KEY", "key": "ptt", "down": true, ...}.

Assertions. Scenario files cannot declare them yet. A harness that owns the world registers them in Python:
observe_api.get(world).add_assertion("radio 2 heard radio 1") and later set_assertion(name, True, "at 3.06 s").
Observers only read them.
"""

from collections import deque
import json
import struct

from ..channel import Kind
from ..world import WorldError
from . import audit
from .input import KEYS

RING = 4096                 # history events kept; an observer that falls further behind sees "lost": true
PAGE_BYTES = 32 * 1024      # one observe.events reply stays far below the world API's 64 KiB client queue
PAGE_EVENTS = 512
KEY_NAMES = {bit: name for name, bit in reversed(list(KEYS.items()))}   # "ok" wins over "select" for bit 0
NOTICES = ("state", "radio_started", "radio_off", "air_config", "air_decision", "air_delivery", "air_error",
           "air_policy", "world_fault", "recording_stopped")


class Observe:
    def __init__(self, world):
        self.world = world
        self.events = deque(maxlen=RING)
        self.sequence = 0
        self.air = {"groups": [], "seed": None, "radios": []}
        self.policy = None
        self.faults = []
        self.assertions = {}
        world.on_input(self._input)
        audit.get(world).add_sink(self._audit)
        notify = world.notify

        def observed_notify(event):
            self._notice(event)
            notify(event)

        world.notify = observed_notify
        world.api.register("observe.status", lambda request: self.status(), read_only=True)
        world.api.register("observe.events", self.history, read_only=True)

    def _add(self, event):
        self.sequence += 1
        self.events.append({"seq": self.sequence, **event})

    def _input(self, event):
        kind = Kind(event.kind)
        if kind == Kind.AIR_IN:
            return
        line = {"event": "input", "time_us": event.time_us, "radio": event.radio, "kind": kind.name,
                "source": event.source, "sequence": event.sequence}
        if kind == Kind.KEY and len(event.payload) == 2:
            line.update(bit=event.payload[0], key=KEY_NAMES.get(event.payload[0], str(event.payload[0])),
                        down=bool(event.payload[1]))
        elif kind == Kind.ENC and len(event.payload) == 4:
            line["detents"] = struct.unpack("<i", event.payload)[0]
        else:
            line["bytes"] = len(event.payload)
        self._add(line)

    def _notice(self, event):
        name = event.get("event")
        if name not in NOTICES:
            return
        line = {key: value for key, value in event.items() if not (name == "air_delivery" and key == "payload")}
        line.setdefault("time_us", self.world.now)
        if name == "air_config":
            self.air = {key: event.get(key) for key in ("groups", "seed", "radios")}
        elif name == "air_policy":
            self.policy = event.get("policy")
        self._add(line)

    def _audit(self, line):
        if line.get("kind") == "world_fault" and line.get("result") == "accepted" and isinstance(line.get("fault"), dict):
            self.faults.append(line["fault"])
        self._add({"event": "audit", **{key: value for key, value in line.items() if key != "type"}})

    def add_assertion(self, name, detail=""):
        """Register one expected result; it starts as waiting (passed None)."""
        if not isinstance(name, str) or not name or name in self.assertions:
            raise ValueError("OBSERVE_BAD_ASSERTION")
        self.assertions[name] = {"name": name, "passed": None, "detail": detail, "time_us": None}
        self._add({"event": "assertion", "time_us": self.world.now, **self.assertions[name]})

    def set_assertion(self, name, passed, detail=""):
        if name not in self.assertions or type(passed) is not bool:
            raise ValueError("OBSERVE_BAD_ASSERTION")
        self.assertions[name] = {"name": name, "passed": passed, "detail": detail, "time_us": self.world.now}
        self._add({"event": "assertion", **self.assertions[name]})

    def status(self):
        world = self.world
        ratio = world.pacing.ratio
        return {"state": world.state, "time_us": world.now, "reason": world.reason,
                "ratio": "max" if ratio is None else ratio,
                "radios": [{"radio": radio, "on": radio in world.radios, "boot": world.boots[radio]}
                           for radio in sorted(world.boots)],
                "air": self.air, "policy": self.policy, "faults": list(self.faults),
                "assertions": list(self.assertions.values()),
                "cursor": self.sequence, "first": self.events[0]["seq"] if self.events else self.sequence + 1}

    def history(self, request):
        after, limit = request.get("after", 0), request.get("limit", PAGE_EVENTS)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= PAGE_EVENTS:
            raise WorldError("WORLD_BAD_CURSOR")
        first = self.events[0]["seq"] if self.events else self.sequence + 1
        out, size, cursor = [], 0, after
        start = max(0, after + 1 - first)      # events are numbered without gaps, so seq maps to an index
        for index in range(start, len(self.events)):
            event = self.events[index]
            size += len(json.dumps(event, separators=(",", ":"), default=str))
            if out and (size > PAGE_BYTES or len(out) >= limit):
                break
            out.append(event)
            cursor = event["seq"]
        return {"events": out, "cursor": cursor, "latest": self.sequence, "lost": after + 1 < first}


def get(world):
    if getattr(world, "observe", None) is None:
        world.observe = Observe(world)
    return world.observe


def install(world):
    return get(world)
