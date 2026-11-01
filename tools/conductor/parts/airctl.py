# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""World API messages for the Air group: loss, delay, corrupt, duplicate, link up and down.

These messages only set the air policy (tools/conductor/airmodel.py, Policy). They never touch a radio. The part
that carries bursts between radios (air.py) reads one policy snapshot per span with policy(world) and
passes it to airmodel.decide().

Who may send them. Only a world client: a client on the world socket, which is same-UID only (ADR 0004 rules 7 and
8; docs/dev/virtual-radio-architecture.md:324). WorldAPI.poll closes a peer of another UID before it can send a
line, so these handlers never see one. A world client does not get radio control from this, and a radio
controller does not get these messages: they exist only on the world endpoint.

Example. {"version": 1, "command": "air_loss", "sender": 1, "receiver": 2, "rate_ppm": 250000} makes a quarter of
radio 1's bursts lost at radio 2, the same quarter every run with the same seed. rate_ppm 0 removes the rule.

Messages (radios are world radio numbers; ordinal is the sender's burst count, from 1):
    air_policy                                          read the policy
    air_loss       sender, receiver, rate_ppm           seeded loss on one link, 0 to 1 000 000 ppm
    air_loss       sender, ordinal                      drop one burst for every receiver (dropped: false undoes)
    air_delay      sender, receiver, ordinal, delay_us  one burst starts later at one receiver, 0 removes
    air_corrupt    sender, receiver, ordinal, xor_hex   XOR mask for one data burst, 1 to 27 bytes, "" removes
    air_duplicate  sender, receiver, ordinal, enabled   one burst heard twice
    air_link       up, [sender, receiver]               the whole air, or one direction of one link
Every change is a world event ("air_policy"), so a recording holds it.
"""

from dataclasses import replace
import re
import weakref

from ..airmodel import AirError, Policy, PPM

_controls = weakref.WeakKeyDictionary()


def policy(world):
    """The policy snapshot for this world now. A frozen value: later messages make a new one."""
    return _controls[world].policy


def _radio(world, request, name):
    value = request[name]
    if type(value) is not int or value not in world.boots:
        raise AirError("WORLD_UNKNOWN_RADIO")
    return value


def _link(world, request):
    sender, receiver = _radio(world, request, "sender"), _radio(world, request, "receiver")
    if sender == receiver:
        raise AirError("WORLD_BAD_LINK")
    return sender, receiver


def _ordinal(request):
    value = request["ordinal"]
    if type(value) is not int or not 1 <= value < (1 << 64):
        raise AirError("WORLD_BAD_ORDINAL")
    return value


def _flag(request, name, default=None):
    value = request.get(name, default)
    if type(value) is not bool:
        raise AirError("WORLD_BAD_FLAG")
    return value


def _set(items, key, value, empty):
    table = dict(items)
    if value == empty:
        table.pop(key, None)
    else:
        table[key] = value
    return table


def _toggle(items, key, on):
    return frozenset(items | {key}) if on else frozenset(items - {key})


def as_json(value):
    """The policy as plain JSON: lists sorted, masks in hex."""
    return {"link_up": value.link_up,
            "down_links": sorted(map(list, value.down_links)),
            "drops": sorted(map(list, value.drops)),
            "loss_ppm": [[*key, rate] for key, rate in value.loss_ppm],
            "delays_us": [[*key, delay] for key, delay in value.delays_us],
            "corruptions": [[*key, mask.hex()] for key, mask in value.corruptions],
            "duplicates": sorted(map(list, value.duplicates))}


class AirControl:
    def __init__(self, world):
        self.world = world
        self.policy = Policy()

    def _change(self, command, new):
        if self.world.state == "Ended":
            raise AirError("WORLD_ENDED")
        self.policy = new
        self.world.notify({"event": "air_policy", "command": command, "time_us": self.world.now,
                           "policy": as_json(new)})
        return as_json(new)

    def loss(self, request):
        p = self.policy
        if "ordinal" in request:
            key = (_radio(self.world, request, "sender"), _ordinal(request))
            return self._change("air_loss", replace(p, drops=_toggle(p.drops, key, _flag(request, "dropped", True))))
        rate = request["rate_ppm"]
        if type(rate) is not int or not 0 <= rate <= PPM:
            raise AirError("WORLD_BAD_LOSS_RATE")
        return self._change("air_loss", replace(p, loss_ppm=_set(p.loss_ppm, _link(self.world, request), rate, 0)))

    def delay(self, request):
        p = self.policy
        key = (*_link(self.world, request), _ordinal(request))
        delay = request["delay_us"]
        if type(delay) is not int or not 0 <= delay < (1 << 32):
            raise AirError("WORLD_BAD_DELAY")
        return self._change("air_delay", replace(p, delays_us=_set(p.delays_us, key, delay, 0)))

    def corrupt(self, request):
        p = self.policy
        key = (*_link(self.world, request), _ordinal(request))
        text = request["xor_hex"]
        if not isinstance(text, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){0,27}", text):
            raise AirError("WORLD_BAD_MASK")
        # At most DMR_PAYLOAD_MAX bytes (hal/hal_dmr_modem.h:47); a shorter burst uses the first bytes.
        mask = bytes.fromhex(text)
        if mask and not any(mask):
            raise AirError("WORLD_BAD_MASK")  # an all-zero mask changes nothing; "" is how to remove one
        return self._change("air_corrupt", replace(p, corruptions=_set(p.corruptions, key, mask, b"")))

    def duplicate(self, request):
        p = self.policy
        key = (*_link(self.world, request), _ordinal(request))
        return self._change("air_duplicate",
                            replace(p, duplicates=_toggle(p.duplicates, key, _flag(request, "enabled"))))

    def link(self, request):
        p = self.policy
        up = _flag(request, "up")
        if "sender" not in request and "receiver" not in request:
            return self._change("air_link", replace(p, link_up=up))
        key = _link(self.world, request)
        return self._change("air_link", replace(p, down_links=_toggle(p.down_links, key, not up)))


def install(world):
    control = AirControl(world)
    _controls[world] = control
    for name, fn in (("air_policy", lambda request: as_json(control.policy)), ("air_loss", control.loss),
                     ("air_delay", control.delay), ("air_corrupt", control.corrupt),
                     ("air_duplicate", control.duplicate), ("air_link", control.link)):
        world.api.register(name, fn)
    return control
