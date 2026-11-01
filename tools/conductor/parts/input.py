# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""ADR 0006 input commands: key {radio, key, state, time_us}, enc {radio, detents, time_us}.

An integer key is a bit index; a string names a key in the virtual keypad map.
KEY payload is bit u8, down u8. ENC payload is signed i32 little endian.
Config.radio_args selects the profile with --encoder on (off by default).
Accepted Input objects contain only bits/counts, ready for the recording hook.
"""

import struct
import re

from ..channel import Kind
from ..world import WorldError
from . import holds, lease

# boards/vhost/keypad_vhost.c: select, back, navigation, PTT, monitor, digits.
KEYS = {"ok": 0, "select": 0, "back": 1, "up": 2, "down": 3, "ptt": 4, "monitor": 5,
        **{str(digit): digit + 6 for digit in range(10)}}


def install(world):
    leases = lease.get(world)
    held = holds.get(world)
    pending = {}
    hold_kind = "world.key"

    def confirms(radio, hold, line):
        scan = re.search(r" BOARD key.scan pins=([0-9a-f]{8})$", line)
        return scan is not None and not int(scan[1], 16) & (1 << int(hold.name))

    held.register(hold_kind, lambda radio, hold: (Kind.KEY, bytes((int(hold.name), 0))), confirms)

    def delivered(target, outboxes):
        for sequence, (event, principal, boot) in list(pending.items()):
            if event.time_us >= world.next_start:
                continue
            del pending[sequence]
            if event.kind != Kind.KEY or world.boots[event.radio] != boot:
                continue
            bit, down = event.payload
            if down:
                held.down(event.radio, hold_kind, str(bit), principal)
            else:
                held.up(event.radio, hold_kind, str(bit))

    def revoked(radio, principal, generation, reason):
        sequences = {seq for seq, (event, _, _) in pending.items() if event.radio == radio}
        world._cancel_inputs(sequences)
        for seq in sequences:
            del pending[seq]

    world.on_grant_done(delivered)
    leases.on_revoke(revoked)

    def submit(request, kind, payload):
        principal = request.get("principal")
        leases.check(request["radio"], principal, generation=request.get("generation"),
                     secret=request.get("secret"), command=kind.name.lower())
        event = world.submit(request["radio"], kind, payload, request.get("source", 0),
                             time_us=request.get("time_us"))
        pending[event.sequence] = (event, principal, world.boots[event.radio])
        return {"time_us": event.time_us, "sequence": event.sequence}

    def key(request):
        value = request["key"]
        if isinstance(value, str):
            if value not in KEYS:
                raise WorldError("WORLD_UNKNOWN_KEY")
            bit = KEYS[value]
        elif type(value) is int and 0 <= value <= 31:
            bit = value
        else:
            raise WorldError("WORLD_BAD_KEY")
        state = request["state"]
        if state not in ("down", "up"):
            raise WorldError("WORLD_BAD_KEY_STATE")
        return submit(request, Kind.KEY, bytes((bit, int(state == "down"))))

    def enc(request):
        count = request["detents"]
        if type(count) is not int or not -(1 << 31) <= count < (1 << 31):
            raise WorldError("WORLD_BAD_DETENTS")
        args = world.config.radio_args
        enabled = False
        for index, arg in enumerate(args[:-1]):
            if arg == "--encoder":
                enabled = args[index + 1] == "on"
        if not enabled:
            raise WorldError("WORLD_NO_ENCODER")
        return submit(request, Kind.ENC, struct.pack("<i", count))

    world.api.register("key", key)
    world.api.register("enc", enc)
