# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The air model: which radio hears which burst, when, and what goes wrong on the way.

What an air model is. Many virtual radios share one simulated air. Each grant, every radio hands the conductor the
bursts it sent (a burst is one 30 ms DMR slot of data or voice). The air model decides, for each burst and each
other radio, one thing: the radio hears it, or a named rule stops it. It is a pure function. The same inputs give
the same answer, every run, in any input order. No clock, no I/O, no random numbers. The only "chance" is a SHA-256
of fixed inputs, so a seed picks which bursts are lost and the same seed loses the same bursts.

Policy rationale: docs/dev/virtual-radio-architecture.md, "Shared air policy", and
docs/dev/adr/0005-time-model.md define grant-independent decisions. The rules below describe simulator policy,
not RF propagation or a DMR conformance claim.

Example. Radios 1 and 2 both send in the span 3 000 000 to 3 001 000 us, on the same channel (their bursts
overlap on air):

    decide((3_000_000, 3_001_000), {1: [b1], 2: [b2], 3: []}, Policy(), seed=0)

Radio 3 hears two senders at once, so the result holds one "collision" decision for each burst and no delivery
for radio 3. Radios 1 and 2 are each sending, so neither hears the other ("half_duplex"). With only radio 1
sending, radio 2 and radio 3 each get one Delivery of b1 at air_start_us 3 000 000.

Delivery time. Delivery.air_start_us is the time the burst starts on air at the receiver: the sender's slot edge
plus any delay rule. It is not the time the receiver reports it. The virtual modem adds the 30 ms slot itself:
vdmr_inbox_put(t_us, ...) takes the start on air and raises its event at t_us + VDMR_SLOT_US
(boards/vhost/dmr_vhost.h:76-80). So the caller passes air_start_us to the modem unchanged.

Rule order, for one burst:
  Send side, once per burst, before any receiver:
    1. link_down     the whole air is off.
    2. drop          this sender's burst number is dropped for everyone.
  Then for each other radio, the first rule that stops it wins:
    3. channel       the receiver is tuned to another channel.
    4. timeslot      the receiver listens on the other DMR timeslot (see match_timeslot).
    5. link_disabled this sender to this receiver link is down.
    6. half_duplex   the receiver sends a burst that overlaps this one on air, so it cannot hear.
    7. collision     more than one sender reaches the receiver with bursts that overlap this one.
    8. color_code   the receiver filters another color code (see match_color_code).
    9. loss          the seeded loss rate for this link picks this burst.
  A burst that passes is delivered, with these changes recorded as their own decisions:
   10. delay         it starts on air later.
   11. corrupt       its payload is XORed with a mask; data bursts only.
   12. duplicate     the receiver gets it twice, the damaged copy if corrupted.

Overlap on air, not the span. Two bursts overlap when their starts are less than OVERLAP_US (29 999 us) apart.
Grouping senders by a grant span would make the answer depend on the grant length, which ADR 0005 forbids.
Example: radio 1 sends at 3 000 000 and radio 2 at 3 010 000 us. The
two bursts share 20 ms of air, so radio 3 hears a collision for both, whether the conductor saw them in one 30 ms
grant or in two 1 ms grants. A burst 30 ms later shares no air and does not collide.

Why 29 999 and not 30 000. A receiver gets its AIR_IN at the last microsecond of the slot, start + 29 999 us
(parts/air.py, handover_us). A burst that starts at that same microsecond is not known yet when the conductor must
decide, and it shares only that last microsecond, so it does not count. Same rule both ways, so it is symmetric.

The caller passes the bursts to decide in outboxes (all inside span) and, in context, every other known burst
that may overlap them: earlier bursts already decided, and later ones not yet due. Context bursts count for
half_duplex and collision but get no decisions of their own.
"""

from dataclasses import dataclass, field
import hashlib
import struct

SLOT_US = 30_000
OVERLAP_US = SLOT_US - 1
PPM = 1_000_000
RULES = ("deliver", "link_down", "drop", "channel", "timeslot", "link_disabled", "half_duplex", "collision",
         "color_code", "loss", "delay", "corrupt", "duplicate")


class AirError(ValueError):
    """A bad input to the air model. Its text is a WORLD_ name the world API can return."""


@dataclass(frozen=True)
class Burst:
    """One burst a radio sent. radio and ordinal (this radio's burst count, from 1) name it.

    t_us is the sender's slot edge in world time: the burst is on air from there for one slot. kind is "data" or
    "voice". channel is the sender's channel. cc is its color code (0 to 15), or None when it sends any. ts is the
    DMR timeslot (1 or 2), or 0 when the sender's framing does not show it (as vdmr_tx_t.ts, dmr_vhost.h:61).
    """
    radio: int
    ordinal: int
    t_us: int
    payload: bytes
    kind: str = "data"
    channel: int = 1
    cc: int | None = None
    ts: int = 0


@dataclass(frozen=True)
class Tuning:
    """What one receiver listens to. cc None and ts 0 mean "any", the default."""
    channel: int = 1
    cc: int | None = None
    ts: int = 0


@dataclass(frozen=True)
class Policy:
    """Every knob of the air, as frozen values, so one snapshot holds for a whole span.

    Keys are radio numbers. loss_ppm maps (sender, receiver) to a rate in parts per million (integers, no floats).
    drops holds (sender, ordinal). delays_us maps (sender, receiver, ordinal) to microseconds. corruptions maps
    (sender, receiver, ordinal) to an XOR mask as long as the payload. duplicates holds (sender, receiver, ordinal).
    down_links holds (sender, receiver). identities maps a radio to the DMR id used in the loss hash;
    a radio not listed uses its own number.
    """
    link_up: bool = True
    down_links: frozenset = frozenset()
    drops: frozenset = frozenset()
    loss_ppm: tuple = ()
    delays_us: tuple = ()
    corruptions: tuple = ()
    duplicates: frozenset = frozenset()
    tuning: tuple = ()
    identities: tuple = ()

    def __post_init__(self):
        # Mappings are stored as sorted item tuples, so the policy is hashable and has one form.
        for name in ("loss_ppm", "delays_us", "corruptions", "tuning", "identities"):
            value = getattr(self, name)
            items = value.items() if isinstance(value, dict) else value
            object.__setattr__(self, name, tuple(sorted(items)))
        for name in ("down_links", "drops", "duplicates"):
            object.__setattr__(self, name, frozenset(getattr(self, name)))
        for _, rate in self.loss_ppm:
            if type(rate) is not int or not 0 <= rate <= PPM:
                raise AirError("WORLD_BAD_LOSS_RATE")
        for _, delay in self.delays_us:
            if type(delay) is not int or delay < 0:
                raise AirError("WORLD_BAD_DELAY")
        for _, mask in self.corruptions:
            if not isinstance(mask, bytes) or not mask:
                raise AirError("WORLD_BAD_MASK")

    def get(self, name, key, default=None):
        return dict(getattr(self, name)).get(key, default)


@dataclass(frozen=True, order=True)
class Decision:
    """One record for the recorder: which rule acted on which burst, for which receivers, and why."""
    t_us: int
    sender: int
    burst: int
    rule: str
    receivers: tuple = ()
    reason: str = ""


@dataclass(frozen=True, order=True)
class Delivery:
    """One burst one receiver hears. copy is 0, or 1 for a duplicate. air_start_us: see the module text."""
    air_start_us: int
    receiver: int
    sender: int
    burst: int
    copy: int
    payload: bytes = field(compare=True)
    corrupted: bool = False


@dataclass(frozen=True)
class Result:
    deliveries: tuple
    decisions: tuple


# The rules. Each answers one question about one burst and, past the send side, one receiver.

def link_down(policy):
    """with the link off, every burst is lost at the sender."""
    return not policy.link_up


def dropped(burst, policy):
    """a burst named by (sender, burst number) is lost for every receiver."""
    return (burst.radio, burst.ordinal) in policy.drops


def match_channel(burst, tuning):
    """a receiver hears only its own channel."""
    return burst.channel == tuning.channel


def match_timeslot(burst, tuning):
    """A receiver set to one timeslot does not hear the other. 0 on either side means "not known, hear it".
    """
    return not burst.ts or not tuning.ts or burst.ts == tuning.ts


def match_color_code(burst, tuning):
    """A receiver set to one color code does not decode another. The virtual modem also filters it
    (boards/vhost/dmr_vhost.c, dmr_rx_poll).
    None on either side means "any". It runs after collision: two bursts on one channel and one slot still
    destroy each other, whatever color code they carry, because the code is read from the burst.
    """
    return burst.cc is None or tuning.cc is None or burst.cc == tuning.cc


def link_disabled(burst, receiver, policy):
    """one direction of one link can be down."""
    return (burst.radio, receiver) in policy.down_links


def overlap(a, b):
    """True when bursts a and b share air time (module text, "Overlap on air"). A burst overlaps itself."""
    return abs(a.t_us - b.t_us) < OVERLAP_US


def half_duplex(receiver, sending):
    """a radio that sends while this burst is on air cannot hear."""
    return receiver in sending


def heard_senders(receiver, bursts, tunings, policy):
    """the senders that reach a receiver: other radios, on its channel and slot, over a link that is up.
    Sorted, so the answer never depends on input order."""
    tuning = tunings(receiver)
    return tuple(sorted({b.radio for b in bursts if b.radio != receiver and match_channel(b, tuning)
                         and match_timeslot(b, tuning) and not link_disabled(b, receiver, policy)}))


def collision(senders):
    """more than one sender on air at once, and the receiver hears none of them."""
    return len(senders) > 1


def loss_sample(seed, sender_id, receiver_id, ordinal):
    """the first 8 bytes of SHA-256 over (seed, sender id, receiver id, burst number), big endian."""
    key = struct.pack("!QIIQ", seed, sender_id, receiver_id, ordinal)
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big")


def lost(sample, rate_ppm):
    """lost when sample / 2**64 < rate / 10**6, in integers."""
    return sample * PPM < rate_ppm * (1 << 64)


def corrupt(payload, mask):
    """XOR the payload with the mask, preserving its length: a short mask is padded with zeros, and mask bytes past the payload are not used.
    Payload lengths differ by burst type (12, 18, 24 or 27 bytes, hal/hal_dmr_modem.h:59), so one mask can be set
    before the burst type is known."""
    return bytes(a ^ b for a, b in zip(payload, mask.ljust(len(payload), b"\0")))


def _check(span, outboxes, seed, context=()):
    if (not isinstance(span, tuple) or len(span) != 2 or any(type(t) is not int or t < 0 for t in span)
            or span[0] >= span[1]):
        raise AirError("WORLD_BAD_SPAN")
    if type(seed) is not int or not 0 <= seed < (1 << 64):
        raise AirError("WORLD_BAD_SEED")
    seen = set()
    for radio, bursts in outboxes.items():
        if type(radio) is not int or not 0 <= radio < (1 << 32):
            raise AirError("WORLD_UNKNOWN_RADIO")
        for burst in bursts:
            if not isinstance(burst, Burst) or burst.radio != radio:
                raise AirError("WORLD_BAD_BURST")
            if not span[0] <= burst.t_us < span[1]:
                raise AirError("WORLD_BURST_OUTSIDE_SPAN")
            if burst.kind not in ("data", "voice") or not isinstance(burst.payload, bytes):
                raise AirError("WORLD_BAD_BURST")
            if type(burst.ordinal) is not int or not 1 <= burst.ordinal < (1 << 64):
                raise AirError("WORLD_BAD_BURST")
            if (radio, burst.ordinal) in seen:
                raise AirError("WORLD_DUPLICATE_BURST")
            seen.add((radio, burst.ordinal))
    for burst in context:
        if not isinstance(burst, Burst) or type(burst.t_us) is not int:
            raise AirError("WORLD_BAD_BURST")
        if (burst.radio, burst.ordinal) in seen:
            raise AirError("WORLD_DUPLICATE_BURST")
        seen.add((burst.radio, burst.ordinal))


def decide(span, outboxes, policy, seed, context=()):
    """Decide the bursts of one span [start, end) in world us. outboxes maps every radio in the air (the receivers)
    to the bursts it sent in the span. context holds other known bursts that may overlap them (module text).
    Returns a Result whose deliveries and decisions are sorted tuples."""
    _check(span, outboxes, seed, context)
    receivers = sorted(outboxes)
    order = lambda b: (b.t_us, b.radio, b.ordinal)
    bursts = sorted((b for radio in receivers for b in outboxes[radio]), key=order)
    known = sorted([*bursts, *context], key=order)
    tuned = dict(policy.tuning)
    tunings = lambda radio: tuned.get(radio, Tuning())
    ids = dict(policy.identities)
    deliveries, decisions = [], []

    def record(burst, rule, targets, reason=""):
        if targets:
            decisions.append(Decision(burst.t_us, burst.radio, burst.ordinal, rule, tuple(targets), reason))

    for burst in bursts:
        on_air = [b for b in known if overlap(b, burst)]
        sending = {b.radio for b in on_air}
        heard = {r: heard_senders(r, on_air, tunings, policy) for r in receivers}
        others = [r for r in receivers if r != burst.radio]
        if link_down(policy):
            record(burst, "link_down", others, "the air is off")
            continue
        if dropped(burst, policy):
            record(burst, "drop", others, f"burst {burst.ordinal} of radio {burst.radio} is dropped")
            continue
        stopped = {rule: [] for rule in RULES}
        collided = set()
        for receiver in others:
            tuning = tunings(receiver)
            if not match_channel(burst, tuning):
                stopped["channel"].append(receiver)
            elif not match_timeslot(burst, tuning):
                stopped["timeslot"].append(receiver)
            elif link_disabled(burst, receiver, policy):
                stopped["link_disabled"].append(receiver)
            elif half_duplex(receiver, sending):
                stopped["half_duplex"].append(receiver)
            elif collision(heard[receiver]):
                stopped["collision"].append(receiver)
                collided.update(heard[receiver])
            elif not match_color_code(burst, tuning):
                stopped["color_code"].append(receiver)
            else:
                rate = policy.get("loss_ppm", (burst.radio, receiver), 0)
                sample = loss_sample(seed, ids.get(burst.radio, burst.radio), ids.get(receiver, receiver),
                                     burst.ordinal)
                if rate and lost(sample, rate):
                    stopped["loss"].append(receiver)
                else:
                    _deliver(burst, receiver, policy, deliveries, record)
                    stopped["deliver"].append(receiver)
        reasons = {"channel": "receiver on another channel", "timeslot": "receiver on the other timeslot",
                   "link_disabled": "link is down", "half_duplex": "receiver is sending",
                   "collision": "senders " + ",".join(map(str, sorted(collided))) + " on air at once",
                   "color_code": "receiver filters another color code", "loss": "seeded loss picked it"}
        for rule in ("channel", "timeslot", "link_disabled", "half_duplex", "collision", "color_code", "loss",
                     "deliver"):
            record(burst, rule, stopped[rule], reasons.get(rule, ""))
    return Result(tuple(sorted(deliveries)), tuple(sorted(decisions)))


def _deliver(burst, receiver, policy, deliveries, record):
    key = (burst.radio, receiver, burst.ordinal)
    delay = policy.get("delays_us", key, 0)
    if delay:
        record(burst, "delay", (receiver,), f"{delay} us late")
    payload, damaged = burst.payload, False
    mask = policy.get("corruptions", key)
    if mask is not None:
        if burst.kind != "data":
            # Corruption applies only to data payloads in this model.
            raise AirError("WORLD_CORRUPT_NEEDS_DATA")
        payload, damaged = corrupt(burst.payload, mask), True
        record(burst, "corrupt", (receiver,), f"payload XOR a {len(mask)} byte mask, {len(payload)} bytes kept")
    copies = 2 if key in policy.duplicates else 1
    if copies == 2:
        record(burst, "duplicate", (receiver,), "heard twice")
    for copy in range(copies):
        deliveries.append(Delivery(burst.t_us + delay, receiver, burst.radio, burst.ordinal, copy, payload, damaged))
