# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""VIEW decoding, lifecycle, ordering and the real firmware channel."""

import pytest

from tools.conductor.channel import ChannelError, Kind, Message, PAYLOAD_MAX
from tools.conductor.parts.view import HEADER, decode, install
from tools.conductor.world import Config, World


def payload(*, radio=1, boot=1, frame=2, time_us=1000, screen="home"):
    return HEADER.pack(radio, boot, frame, time_us, 2, 1, 258, 513) + \
        b"\0".join(text.encode() for text in (screen, "Call", "Saved", "12")) + b"\0"


def test_decode_owned_text_and_fields():
    wire = bytearray(payload(screen="find"))
    record = decode(wire)
    wire[HEADER.size:] = b"x" * (len(wire) - HEADER.size)
    assert record == dict(kind="VIEW", radio=1, boot=1, frame=2, t_us=1000, screen="find",
                          depth=2, locked=True, focus=258, count=513, banner="Call", toast="Saved", query="12")


@pytest.mark.parametrize("wire,error", [
    (b"", "VIEW_BAD_LENGTH"),
    (b"x" * (PAYLOAD_MAX + 1), "VIEW_BAD_LENGTH"),
    (payload()[:-1], "VIEW_BAD_FIELDS"),
    (payload() + b"\0", "VIEW_BAD_FIELDS"),
    (payload()[:21] + b"\x02" + payload()[22:], "VIEW_BAD_FIELDS"),
    (payload()[:HEADER.size] + b"\xff\0\0\0\0", "VIEW_BAD_TEXT"),
])
def test_malformed(wire, error):
    with pytest.raises(ChannelError, match=error):
        decode(wire)


def test_per_radio_boot_and_view_before_frame(stub_world):
    world = stub_world(radios=2)
    install(world)
    world.start()
    for radio, slot in world.radios.items():
        assert slot.channel.args[2:6] == ("--radio-id", str(radio), "--boot-id", "1")
        slot.channel.outputs.append((Kind.VIEW, payload(radio=radio)))
    world.step(1000)
    world.advance()
    assert set(world.view_checkpoints.records) == {(1, 1), (2, 1)}
    world.restart(1)
    assert world.radios[1].channel.args[2:6] == ("--radio-id", "1", "--boot-id", "2")
    world.radios[1].channel.outputs.append((Kind.VIEW, payload(boot=2, frame=1, screen="hub")))
    world.step(2000)
    world.advance()
    assert world.view_checkpoints.records[1, 2][0]["t_us"] == 2000
    frames = [dict(kind="FRAME", radio=1, boot=2, frame=1, t_us=2000),
              dict(kind="FRAME", radio=1, boot=1, frame=1, t_us=999),
              dict(kind="FRAME", radio=1, boot=1, frame=2, t_us=1000)]
    records = world.view_checkpoints.ordered(frames)
    assert [(r["t_us"], r["kind"], r["radio"]) for r in records] == [
        (999, "FRAME", 1), (1000, "VIEW", 1), (1000, "VIEW", 2), (1000, "FRAME", 1),
        (2000, "VIEW", 1), (2000, "FRAME", 1)]


@pytest.mark.parametrize("options,stamp,error", [
    ({"radio": 2}, 1000, "VIEW_BAD_IDENTITY"),
    ({"boot": 2}, 1000, "VIEW_BAD_IDENTITY"),
    ({}, 999, "VIEW_BAD_TIME"),
])
def test_wrong_identity_or_time(stub_world, options, stamp, error):
    world = stub_world()
    install(world)
    world.start()
    with pytest.raises(ChannelError, match=error):
        world.view_checkpoints.receive(1, Message(Kind.VIEW, payload(**options), stamp))
    assert not world.view_checkpoints.records


def test_firmware_channel(vhost_elf):
    world = World(Config(executable=str(vhost_elf), radios=2))
    install(world)
    frames = []
    world.on_outbox(Kind.FRAME, lambda radio, message: frames.append((radio, message.payload[:20])))
    try:
        world.start()
        world.step(5_000_000)
        world.advance()
        assert world.state == "Paused"
        for radio in (1, 2):
            records = world.view_checkpoints.records[radio, 1]
            assert records and any(record["screen"] == "home" for record in records)
            assert [record["t_us"] for record in records] == sorted(record["t_us"] for record in records)
            for record in records:
                assert any(radio == source and HEADER.unpack_from(header + b"\0" * 6)[:3] ==
                           (radio, 1, record["frame"]) for source, header in frames)
    finally:
        world.close()
