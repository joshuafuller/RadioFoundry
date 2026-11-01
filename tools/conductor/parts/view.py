# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Copied UI checkpoints per (radio, boot), from platform/host/vview.h.

world.view_checkpoints.records holds VIEW lists. ordered(frames) merges them
with the FRAME owner's records, with semantic state first at equal times.
"""

from collections import defaultdict
import struct

from ..history import History
from ..channel import ChannelError, Kind, PAYLOAD_MAX


HEADER = struct.Struct("<IIIQBBHH")


def decode(payload):
    if not HEADER.size + 4 <= len(payload) <= PAYLOAD_MAX:
        raise ChannelError("VIEW_BAD_LENGTH")
    radio, boot, frame, time_us, depth, locked, focus, count = HEADER.unpack_from(payload)
    strings = payload[HEADER.size:].split(b"\0")
    if locked > 1 or len(strings) != 5 or strings[-1]:
        raise ChannelError("VIEW_BAD_FIELDS")
    try:
        screen, banner, toast, query = (value.decode("utf-8") for value in strings[:-1])
    except UnicodeDecodeError as error:
        raise ChannelError("VIEW_BAD_TEXT") from error
    return dict(kind="VIEW", radio=radio, boot=boot, frame=frame, t_us=time_us,
                screen=screen, depth=depth, focus=focus, count=count, locked=bool(locked),
                banner=banner, toast=toast, query=query)


class ViewCheckpoints:
    def __init__(self, world):
        self.world = world
        self.records = defaultdict(History)
        world.add_radio_args(lambda radio: ("--radio-id", str(radio), "--boot-id", str(world.boots[radio] + 1)))
        world.on_outbox(Kind.VIEW, self.receive)

    def receive(self, radio, message):
        record = decode(message.payload)
        slot = self.world.radios[radio]
        if (record["radio"], record["boot"]) != (radio, slot.boot):
            raise ChannelError("VIEW_BAD_IDENTITY")
        if record["t_us"] + slot.origin != message.time_us:
            raise ChannelError("VIEW_BAD_TIME")
        record["t_us"] = message.time_us
        self.records[radio, slot.boot].append(record)

    def ordered(self, frames=()):
        """A snapshot for the recorder; frames are already in world time."""
        records = [record for views in self.records.values() for record in views]
        records.extend(frames)
        return sorted(records, key=lambda record: (record["t_us"], record["kind"] != "VIEW",
                                                  record["radio"], record["boot"], record["frame"]))


def install(world):
    world.view_checkpoints = ViewCheckpoints(world)
