# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""A network parrot and a repeater echo on the air: opt-in elements that send a radio's own bursts back to it.

What a parrot is. On a DMR network a parrot (often talkgroup 9998) records your call and, once you let go, plays it
back. A repeater does a second, quicker thing: it sends each burst out again about 90 ms after it heard it (gotcha
14.3, docs/spec/gotchas.md). Both send our own bursts back with our own source ID untouched, but at very different
times. caller echo bug is about the first kind: the radio does not play the parrot's echo of its own call.

Neither is a radio, so neither runs firmware. This part watches the AIR_OUT records of the radios it is told to
follow and sends copies of them back as AIR_IN, record bytes unchanged (the link control, so the source ID, stays
the sender's). Only the record's start time moves.

Opt in. Nothing happens until it is configured, so every other world (replay, lookahead, recording) is unchanged. Two
ways, both before the radios start:
  - a "parrot" object inside air_config, so a scenario's `air` section carries it (tools/vradio.py --scenario);
  - the parrot_config world command with the same object, after air_config.
The object: {"replay": [radios], "gap_us": 1000000, "echo": [radios], "echo_us": 90000}.
  replay   radios whose calls the parrot plays back. A call is the voice bursts from a voice header (record kind 1)
           to the terminator (kind 2). gap_us after the terminator starts on air, the whole call is sent again to
           every powered radio of the sender's air group on the sender's channel and timeslot, the sender included.
  echo     radios whose every voice burst the repeater sends back to that radio only, echo_us later.
Each shift is rounded up to a whole TDMA frame (60 ms), so a burst keeps its slot phase.

Example. Radio 1 keys a group call: header at 1 030 000 us, voice, terminator at 2 590 000 us. With gap_us 1 000 000
the parrot's header starts at 3 610 000 us (2 590 000 + 1 000 000, rounded to the frame of the call's first burst),
and radio 1, 2 and 3 each get it at 3 639 999 us, the last microsecond of that slot, as parts/air.py hands over.

Timing and records, as parts/air.py. An AIR_IN is handed over at start + 29 999 us and a grant limit ends a grant
exactly there, so the grant length never changes what a radio gets or when (ADR 0005). This part's grant-done hook
runs after the air part's (parts install in name order), so equal stamps always queue in the same order. Each copy
is notified as an air_delivery with "element": "parrot" or "echo", so the recording's air file holds it and a
replay that configures the same parrot compares it record for record.

Limits. The copies skip the air model: no collision, loss or half-duplex rule acts on them (the modem itself still
drops what it hears while it transmits, boards/vhost/dmr_vhost.c rx_drop "tx"). A real parrot may rewrite the
source ID or the embedded LC (the reporter's repeater is Motorola hardware, caller echo bug comments); this one never
does, which is the case the issue suspects but has not confirmed.
"""

from dataclasses import replace

from .. import airmodel
from ..channel import Kind, NEVER
from ..world import Input, WorldError
from .air import Record, handover_us

FRAME_US = 2 * airmodel.SLOT_US     # one TDMA frame: a slot on each timeslot
HEADER, TERMINATOR, VOICE = 1, 2, 16    # record kinds (Record.kind, boards/vhost/dmr_air_vchan.c)


def frames_up(value):
    return -(-value // FRAME_US) * FRAME_US


def radios(value, known):
    if not isinstance(value, list) or len(set(value)) != len(value):
        raise WorldError("WORLD_BAD_PARROT")
    for radio in value:
        if type(radio) is not int or radio not in known:
            raise WorldError("WORLD_UNKNOWN_RADIO")
    return tuple(sorted(value))


def delay(value, default, reason):
    value = default if value is None else value
    if type(value) is not int or not 0 < value < NEVER // 2:
        raise WorldError(reason)
    return value


class Parrot:
    def __init__(self, world):
        self.world = world
        self.replay, self.echo = (), ()
        self.gap_us, self.echo_us = 1_000_000, 90_000
        self.air = {"groups": (), "radios": []}
        self.start = 0
        self.calls = {}     # radio -> [(world start, record)] of the call being recorded
        self.queue = []     # (handover, order, receiver, sender, element, world start, record), sorted
        self.order = 0
        self.burst = 0
        world.api.register("parrot_config", self.configure)
        self.configure_air = world.api.handlers["air_config"]
        world.api.handlers["air_config"] = self.air_config
        world.add_grant_limit(self.grant_limit)
        world.on_grant_done(self.done)

    def active(self):
        return bool(self.replay or self.echo)

    def air_config(self, request):
        result = self.configure_air(request)
        self.air = {"groups": result.get("groups", ()), "radios": result.get("radios", [])}
        if request.get("parrot") is not None:
            return {**result, "parrot": self.configure(request["parrot"])}
        return result

    def configure(self, request):
        if any(self.world.boots.values()) or self.world.state == "Ended":
            raise WorldError("WORLD_PARROT_ALREADY_STARTED")
        if not isinstance(request, dict) or set(request) - {"replay", "gap_us", "echo", "echo_us", "version",
                                                             "command", "actor"}:
            raise WorldError("WORLD_BAD_PARROT")
        replay = radios(request.get("replay", []), self.world.boots)
        echo = radios(request.get("echo", []), self.world.boots)
        gap = delay(request.get("gap_us"), 1_000_000, "WORLD_BAD_PARROT_GAP")
        echo_us = delay(request.get("echo_us"), 90_000, "WORLD_BAD_PARROT_ECHO")
        if any(self.group_of(radio) is None for radio in replay + echo):
            raise WorldError("WORLD_PARROT_NO_AIR")
        self.replay, self.echo, self.gap_us, self.echo_us = replay, echo, gap, echo_us
        config = {"replay": list(replay), "gap_us": gap, "echo": list(echo), "echo_us": echo_us}
        self.world.notify({"event": "parrot_config", "time_us": self.world.now, **config})
        return config

    def group_of(self, radio):
        return next((tuple(group) for group in self.air["groups"] if radio in group), None)

    def tuning(self, radio):
        option = next((o for o in self.air["radios"] if o["radio"] == radio), {})
        return airmodel.Tuning(option.get("channel", 1), option.get("cc"), option.get("ts", 0))

    def grant_limit(self, now):
        """End a grant at the next copy's hand-over (module text). None while nothing is queued."""
        return next((item[0] for item in self.queue if item[0] > now), None)

    def done(self, target, outboxes):
        if not self.active():
            return ()
        end = max(target, self.world.next_start)
        if end > self.start:
            for radio in sorted(outboxes):
                for message in outboxes[radio]:
                    if message.kind == Kind.AIR_OUT and radio in self.world.radios:
                        record = Record.decode(message.payload)
                        if record.kind in (HEADER, TERMINATOR, VOICE):
                            self.heard(radio, record.t_us + self.world.radios[radio].origin, record)
            self.queue.sort(key=lambda item: item[:2])
            self.start = end
        due = [item for item in self.queue if item[0] <= end]
        self.queue = self.queue[len(due):]
        return [event for event in (self.deliver(*item) for item in due) if event is not None]

    def heard(self, radio, start, record):
        if radio in self.echo:
            self.add(radio, radio, "echo", start + frames_up(self.echo_us), record)
        if radio not in self.replay:
            return
        call = self.calls.setdefault(radio, [])
        if record.kind == HEADER and call and call[-1][1].kind != HEADER:
            call.clear()    # a new call's header: whatever came before had no terminator
        call.append((start, record))
        if record.kind != TERMINATOR:
            return
        first = call[0][0]
        shift = frames_up(start + self.gap_us - first)
        receivers = [r for r in self.group_of(radio) or () if self.hears(r, record, radio)]
        for burst_start, burst in call:
            for receiver in receivers:
                self.add(receiver, radio, "parrot", burst_start + shift, burst)
        call.clear()

    def hears(self, receiver, record, sender):
        tuning = self.tuning(receiver)
        burst = airmodel.Burst(sender, 1, 0, b"", channel=self.tuning(sender).channel, ts=record.ts)
        return airmodel.match_channel(burst, tuning) and airmodel.match_timeslot(burst, tuning)

    def add(self, receiver, sender, element, start, record):
        self.order += 1
        self.queue.append((handover_us(start), self.order, receiver, sender, element, start, record))

    def deliver(self, stamp, _, receiver, sender, element, start, record):
        slot = self.world.radios.get(receiver)
        if slot is None or slot.origin > start:
            return None     # off, or restarted after the copy began
        local = start - slot.origin
        if not 0 <= local < NEVER - airmodel.SLOT_US or stamp < self.world.next_start:
            raise WorldError("WORLD_BAD_AIR_TIME")
        self.burst += 1
        wire = replace(record, t_us=local).encode()
        self.world.notify({"event": "air_delivery", "time_us": stamp, "element": element,
                           "air_start_us": start, "receiver": receiver, "sender": sender, "burst": self.burst,
                           "copy": 0, "payload": record.payload.hex(), "corrupted": False, "input_us": stamp,
                           "class": 20, "source": sender})
        return Input(receiver, Kind.AIR_IN, wire, stamp, sender, cls=20)


def install(world):
    world.parrot = Parrot(world)
