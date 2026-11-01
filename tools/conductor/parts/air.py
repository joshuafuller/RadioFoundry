# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Carry modem outboxes across the all-radio grant barrier (ADR 0005, item 3).

Run a configured world: python3 -m tools.conductor.parts.air --world FILE --max --output DIR.
FILE extends a version 1 conductor scenario with radios (count) and air: groups (disjoint lists of radio
numbers), seed, and radios (per-radio storage, radio_index, channel, cc, ts, identity). The air_config world
command accepts that air object before startup. Defaults are no shared air, seed 0, blank storage and channel 1.
radio_index selects the existing storage generator's member (1 or 2 for dmr-pair, 1 to 999 for dmr-group),
independently of the world radio number.

For example, air_pair.json puts radios 1 and 2 on one air and starts each with its own dmr-pair fixture.
Each grant is at most 1000 us while two powered radios share an air. All outboxes are in before decide() runs.
An AIR_IN's payload keeps the original slot start plus the model's delay (its air start). The modem reports RX at
that start + 30000 us, the end of the slot (dmr_vhost.c, vdmr_inbox_put).

When the AIR_IN is handed over. Always at air start + 30000 - 1 us, the last microsecond of the slot, never at the
grant end. So the grant length never changes what a receiver gets or when (ADR 0005, ADR 0008). Example: radio 1
sends a burst at 3 030 000 us. Radio 2 gets its AIR_IN at 3 059 999 us with 500 us, 1 ms or 30 ms grants, and
reports RX at 3 060 000 us. It cannot be the slot end itself: dmr_air_vchan.c refuses an AIR_IN at or after
start + 30000. A queued input is a grant limit in World._target, so no grant ends after the next delivery due.
A grant that ends later than that last microsecond is refused (WORLD_BAD_AIR_TIME), as before.
This is host scheduling, not an RF timing measurement.

When a burst is decided. Also at that last microsecond, start + 29999 us, not at the end of the grant it was sent
in. By then every burst that shares air with it is known (airmodel.py, "Overlap on air"), whatever the grant
length, so collisions and half duplex compare the same bursts at 500 us, 1 ms and 30 ms grants. Until then the
burst waits in a pending list, and a grant limit makes some grant end exactly at that microsecond. Example: radio 1
sends at 3 000 000 us and radio 2 at 3 010 000 us, on one channel. Radio 1's burst is decided at 3 029 999 us and
radio 2's at 3 039 999 us. Both see the other, so radio 3 gets a collision for each, never a delivery.
A receiver that is off, or that restarted after the burst began, gets nothing. Each burst keeps the air policy
(faults, tuning) of the grant it was sent in, so a fault that ends at 3 400 000 us still covers a burst sent at
3 390 000 us. Records: air_decision and air_error carry the hand-over as time_us, the burst start as t_us.
"""

from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
import argparse
import json
from pathlib import Path
import struct

from .. import airmodel
from ..channel import Channel, Kind, NEVER
from ..world import Input, World, WorldError
from . import airctl

# Matches boards/vhost/dmr_air_vchan.c. The prefix begins with the channel contract's ts, kind bytes.
RECORD = struct.Struct("<BBQbBBB9sB")


@dataclass(frozen=True)
class Record:
    ts: int
    kind: int
    t_us: int
    cc: int
    voice_index: int
    sync: int
    flags: int
    lc: bytes
    payload: bytes

    def encode(self):
        return RECORD.pack(self.ts, self.kind, self.t_us, self.cc, self.voice_index, self.sync, self.flags,
                           self.lc, len(self.payload)) + self.payload

    @classmethod
    def decode(cls, wire):
        if len(wire) < RECORD.size:
            raise WorldError("WORLD_BAD_AIR")
        ts, kind, start, cc, index, sync, flags, lc, length = RECORD.unpack_from(wire)
        if (len(wire) != RECORD.size + length or length > 27 or ts > 2 or kind not in (*range(12), 16)
                or not -1 <= cc <= 15 or index > 5 or sync > 4 or flags & ~7
                or start >= NEVER - airmodel.SLOT_US):
            raise WorldError("WORLD_BAD_AIR")
        return cls(ts, kind, start, cc, index, sync, flags, lc, wire[RECORD.size:])


def handover_us(air_start_us):
    """The world time an AIR_IN is handed to its receiver: the last microsecond of the burst's slot."""
    return air_start_us + airmodel.SLOT_US - 1


def integer(value, low, high, reason):
    if type(value) is not int or not low <= value <= high:
        raise WorldError(reason)
    return value


class Air:
    def __init__(self, world):
        self.world = world
        self.groups = ()
        self.seed = 0
        self.options = {}
        self.ordinals = dict.fromkeys(world.boots, 0)
        self.start = 0
        self.pending = []           # sent, not yet decided: (burst, (record, policy), group), by (t_us, radio, ordinal)
        self.recent = []            # decided bursts that may still overlap a pending or later one
        world.api.register("air_config", self.configure)
        world.add_radio_args(self.radio_args)
        world.add_grant_limit(self.grant_limit)
        world.add_grant_limit(self.delivery_limit)
        world.on_grant_done(self.done)

    def configure(self, request):
        if any(self.world.boots.values()) or self.world.state == "Ended":
            raise WorldError("WORLD_AIR_ALREADY_STARTED")
        groups, seen = [], set()
        for members in request.get("groups", []):
            if not isinstance(members, list) or not members:
                raise WorldError("WORLD_BAD_AIR_GROUP")
            for radio in members:
                self.world._check_radio(radio)
                if radio in seen:
                    raise WorldError("WORLD_BAD_AIR_GROUP")
                seen.add(radio)
            groups.append(tuple(sorted(members)))
        seed = integer(request.get("seed", 0), 0, NEVER, "WORLD_BAD_SEED")
        options = {}
        for item in request.get("radios", []):
            radio = item["radio"]
            self.world._check_radio(radio)
            if radio in options:
                raise WorldError("WORLD_BAD_AIR_RADIO")
            storage = item.get("storage", "blank")
            if not isinstance(storage, str) or not (storage in ("blank", "dmr-pair", "dmr-group") or storage.startswith("steps:")):
                raise WorldError("WORLD_BAD_STORAGE")
            cc = item.get("cc")
            if cc is not None:
                integer(cc, 0, 15, "WORLD_BAD_COLOR_CODE")
            options[radio] = {
                "radio": radio, "storage": storage,
                "radio_index": integer(item.get("radio_index", 1), 1, 999, "WORLD_BAD_RADIO_INDEX"),
                "channel": integer(item.get("channel", 1), 0, (1 << 32) - 1, "WORLD_BAD_CHANNEL"),
                "cc": cc, "ts": integer(item.get("ts", 0), 0, 2, "WORLD_BAD_TIMESLOT"),
                "identity": integer(item.get("identity", radio), 0, (1 << 32) - 1, "WORLD_BAD_IDENTITY"),
            }
        self.groups, self.seed, self.options = tuple(sorted(groups)), seed, options
        config = {"groups": self.groups, "seed": seed, "radios": [options[r] for r in sorted(options)]}
        self.world.notify({"event": "air_config", "time_us": self.world.now, **config})
        return config

    def radio_args(self, radio):
        if radio not in self.options:
            return ()
        option = self.options[radio]
        return ("--storage", option["storage"], "--radio-index", str(option["radio_index"]))

    def grant_limit(self, now):
        if any(sum(r in self.world.radios for r in group) > 1 for group in self.groups):
            return now + 1000
        return None

    def delivery_limit(self, now):
        """End the grant no later than the earliest AIR_IN a burst sent in it could need: the last microsecond
        of a slot that starts at the first event due in any radio on a shared air (a burst starts only at a slot
        edge, which is an event). Queued AIR_INs already limit grants in World._target. Example: a radio's slot
        edge is due at 3 030 000 us, so a 30 ms grant from there ends at 3 059 999 us, when that burst is due.
        Also no later than the hand-over of any pending burst (module text, "When a burst is decided").
        Only within the stated lookahead: lookahead_check's negative control probes longer caps on purpose."""
        if self.grant_limit(now) is None and not self.pending:
            return None
        lookahead = getattr(self.world, "air_lookahead", None)
        if lookahead is not None and lookahead.grant_us > airmodel.SLOT_US:
            return None
        limits = [handover_us(burst.t_us) for burst, _, _ in self.pending]
        if self.grant_limit(now) is not None:
            due = min((slot.done.next_due_us for radio, slot in self.world.radios.items()
                       if slot.done is not None and any(radio in group for group in self.groups)), default=NEVER)
            if due < NEVER - airmodel.SLOT_US:
                limits.append(max(due, now) + airmodel.SLOT_US - 1)
        return min(limits, default=None)

    def group_of(self, radio):
        return next((group for group in self.groups if radio in group), None)

    def done(self, target, outboxes):
        # A drain includes target itself. The following grant starts at target + 1.
        end = max(target, self.world.next_start)
        if end <= self.start:
            return ()
        policy = None
        for radio in sorted(outboxes):
            for message in outboxes[radio]:
                if message.kind != Kind.AIR_OUT:
                    continue
                record = Record.decode(message.payload)
                start = record.t_us + self.world.radios[radio].origin
                if not self.start <= start < end:
                    raise WorldError("WORLD_BURST_OUTSIDE_SPAN")
                self.ordinals[radio] += 1
                burst = airmodel.Burst(radio, self.ordinals[radio], start, record.payload,
                                       "voice" if record.kind == 16 else "data",
                                       self.options.get(radio, {}).get("channel", 1),
                                       None if record.cc < 0 else record.cc, record.ts)
                group = self.group_of(radio)
                if group is not None:   # a radio on no shared air is heard by nobody
                    policy = policy or self.policy()
                    self.pending.append((burst, (record, policy), group))
        self.pending.sort(key=lambda item: (item[0].t_us, item[0].radio, item[0].ordinal))
        self.start = end
        # Every burst that starts before end is known, so a burst is due once its hand-over is at most end.
        ready = [item for item in self.pending if handover_us(item[0].t_us) <= end]
        if not ready:
            return ()
        self.pending = self.pending[len(ready):]
        inputs = self.decide(ready)
        self.recent.extend(burst for burst, _, _ in ready)
        horizon = min([burst.t_us for burst, _, _ in self.pending] + [end])
        self.recent = [burst for burst in self.recent if burst.t_us > horizon - airmodel.OVERLAP_US]
        return inputs

    def policy(self):
        return replace(airctl.policy(self.world),
                       tuning={r: airmodel.Tuning(o["channel"], o["cc"], o["ts"]) for r, o in self.options.items()},
                       identities={r: o["identity"] for r, o in self.options.items()})

    def decide(self, ready):
        """Decide ready bursts, each under the policy that held in the grant it was sent in. Faults change only
        between grants, at their own world time, so that policy is the one in force at the burst's start."""
        records = {(burst.radio, burst.ordinal): record for burst, (record, _), _ in ready}
        batches = []
        for burst, (_, policy), group in ready:
            if not batches or batches[-1][:2] != (group, policy):
                batches.append((group, policy, []))
            batches[-1][2].append(burst)
        inputs, deliveries = [], []
        for group, policy, mine in sorted(batches, key=lambda b: (self.groups.index(b[0]), b[2][0].t_us)):
            # Receivers: the group's powered radios, plus each sender (it never hears itself).
            out = {r: [] for r in group if r in self.world.radios}
            for burst in mine:
                out.setdefault(burst.radio, []).append(burst)
            # Context: every other known burst of this air (decided, pending, or ready under another policy).
            names = {(b.radio, b.ordinal) for b in mine}
            context = [b for b in self.recent if b.radio in group]
            context += [burst for burst, _, home in self.pending + ready
                        if home == group and (burst.radio, burst.ordinal) not in names]
            span = (mine[0].t_us, mine[-1].t_us + 1)
            try:
                result = airmodel.decide(span, out, policy, self.seed, context)
            except airmodel.AirError as error:
                if str(error) != "WORLD_CORRUPT_NEEDS_DATA":
                    raise
                invalid = {key for key, _ in policy.corruptions
                           if (key[0], key[2]) in records and records[key[0], key[2]].kind == 16}
                self.world.notify({"event": "air_error", "time_us": handover_us(mine[0].t_us), "reason": str(error),
                                   "refused_corruptions": sorted(invalid)})
                valid = replace(policy, corruptions=[(k, v) for k, v in policy.corruptions if k not in invalid])
                result = airmodel.decide(span, out, valid, self.seed, context)
            # Record times come from the bursts, never from the grant end (target), so they match for any grant.
            # time_us is when the decision is made, the burst's hand-over; t_us keeps the burst start. A replay
            # compares records in time order, so a record must never carry a time before it exists.
            for decision in result.decisions:
                self.world.notify({"event": "air_decision", "time_us": handover_us(decision.t_us),
                                   **asdict(decision)})
            deliveries.extend(result.deliveries)
        for delivery in sorted(deliveries, key=lambda d: (d.air_start_us, d.sender, d.burst, d.receiver, d.copy)):
            slot = self.world.radios.get(delivery.receiver)
            if slot is None or slot.origin > delivery.air_start_us:
                continue    # off, or restarted after the burst began: that boot never heard it
            record = records[delivery.sender, delivery.burst]
            start = delivery.air_start_us - slot.origin
            record = replace(record, t_us=start, payload=delivery.payload,
                             flags=record.flags | (4 if delivery.corrupted else 0))
            stamp = handover_us(delivery.air_start_us)
            if not 0 <= start < NEVER - airmodel.SLOT_US or stamp < self.world.next_start:
                raise WorldError("WORLD_BAD_AIR_TIME")
            event = Input(delivery.receiver, Kind.AIR_IN, record.encode(), stamp, delivery.sender, cls=20)
            inputs.append(event)
            self.world.notify({"event": "air_delivery", "time_us": stamp, **asdict(delivery),
                               "payload": delivery.payload.hex(), "input_us": stamp, "class": 20,
                               "source": delivery.sender})
        return inputs


def install(world):
    return Air(world)


def scenario_world(path, *, channel_factory=Channel):
    from ..conductor import install_parts, scenario_config
    data = json.loads(Path(path).read_text())
    world = World(scenario_config(path, radios=data["radios"]), channel_factory=channel_factory)
    try:
        install_parts(world)
        response = world.api.dispatch({**data["air"], "version": 1, "command": "air_config"})
        if not response["ok"]:
            raise WorldError(response["error"])
        return world
    except BaseException:
        world.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", type=Path, required=True)
    parser.add_argument("--max", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("build/air-pair"))
    args = parser.parse_args()
    world = scenario_world(args.world)
    try:
        args.output.mkdir(parents=True, exist_ok=True)
        with ExitStack() as files:
            traces = {r: files.enter_context((args.output / f"radio-{r}.trace").open("wb")) for r in world.boots}
            deliveries = files.enter_context((args.output / "air-deliveries.jsonl").open("w"))
            decisions = files.enter_context((args.output / "air-decisions.jsonl").open("w"))
            world.on_outbox(Kind.TRACE, lambda r, m: traces[r].write(m.payload + b"\n"))
            notify = world.api.notify

            def record(event):
                if event["event"].startswith("air_"):
                    output = deliveries if event["event"] == "air_delivery" else decisions
                    output.write(json.dumps(event, sort_keys=True) + "\n")
                notify(event)

            world.api.notify = record
            world.start()
            world.run("max" if args.max else 1)
            while world.state != "Ended":
                world.advance()
            print(f"{world.reason} time_us={world.now}")
            return 0 if world.reason == "WORLD_END_TIME" else 1
    finally:
        world.close()


if __name__ == "__main__":
    raise SystemExit(main())
