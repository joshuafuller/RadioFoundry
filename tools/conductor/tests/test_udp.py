# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""UDP wire validation, scheduled world input, and a real firmware gateway reply."""

import hashlib

import pytest

from tools.conductor.channel import ChannelError, Kind
from tools.conductor.conductor import install_parts
from tools.conductor.parts import udp
from tools.conductor.parts.dlmux import frame as debug_frame
from tools.conductor.world import Config, World


PACKET = {"source_ip": "12.0.3.233", "destination": "225.0.0.201", "source_port": 6969,
          "port": 6969, "payload_hex": "010203"}


def request(world, **fields):
    return world.api.dispatch({"version": 1, "command": "udp", "radio": 1, **PACKET, **fields})


@pytest.mark.parametrize("payload,header", [
    (b"", "4500001c000140004011491e0c0003e9e10000c91b391b390008d7b9"),
    (b"abc", "4500001f000140004011491b0c0003e9e10000c91b391b39000b1351"),
    (b"\xff" * 558, "4500024a00014000401146f00c0003e9e10000c91b391b390236d35d"),
])
def test_wire_matches_protocol_fixture(payload, header):
    # Ethernet multicast mapping, IPv4 and UDP headers with one's-complement checksums.
    packet = {**PACKET, "payload_hex": payload.hex()}
    wire = udp.ethernet(packet)
    assert wire == bytes.fromhex("01005e0000c90256480000020800" + header) + payload
    record = udp.decode(wire)
    assert record == {"source": packet["source_ip"], "destination": packet["destination"],
                      "source_port": 6969, "port": 6969, "payload_hex": payload.hex(),
                      "payload_sha256": hashlib.sha256(payload).hexdigest(), "data": wire.hex()}


@pytest.mark.parametrize("target,mac", [("12.0.3.232", udp.RADIO_MAC), ("255.255.255.255", b"\xff" * 6)])
def test_unicast_broadcast_and_distinct_source_port(target, mac):
    wire = udp.ethernet({**PACKET, "destination": target, "source_port": 42000})
    assert wire[:6] == mac
    assert udp.decode(wire)["source_port"] == 42000


def test_scheduled_input_and_output_record(stub_world):
    world = stub_world()
    install_parts(world)
    inputs, events = [], []
    world.on_input(inputs.append)
    world.api.notify = events.append
    world.start()
    world.run("max", until=4000)
    response = request(world, time_us=1234, source=9)
    assert response["ok"], response
    assert response["result"] == {"time_us": 1234, "sequence": 1}
    event = inputs[0]
    assert (event.kind, event.time_us, event.source, event.round, event.cls) == (Kind.ETH_IN, 1234, 9, 0, 30)
    assert event.payload == udp.ethernet(PACKET)
    channel = world.radios[1].channel
    channel.outputs.append((Kind.ETH_OUT, event.payload))
    for _ in range(20):
        if world.state != "Running":
            break
        world.advance()
    assert world.state == "Paused"
    sent = [m for m in channel.sent if m.kind == Kind.ETH_IN]
    assert [(m.time_us, m.source, m.payload) for m in sent] == [(1234, 9, event.payload)]
    delivery, = [e for e in events if e["event"] == "udp_delivery"]
    assert delivery == {"event": "udp_delivery", "radio": 1, "time_us": 1234, **udp.decode(event.payload)}


@pytest.mark.parametrize("fields,error", [
    ({"source_ip": True}, "WORLD_BAD_UDP_ADDRESS"),
    ({"source_ip": "::1"}, "WORLD_BAD_UDP_ADDRESS"),
    ({"destination": "12.0.0.256"}, "WORLD_BAD_UDP_ADDRESS"),
    ({"port": True}, "WORLD_BAD_UDP_PORT"),
    ({"port": 65536}, "WORLD_BAD_UDP_PORT"),
    ({"source_port": 0}, "WORLD_BAD_UDP_PORT"),
    ({"source_port": -1}, "WORLD_BAD_UDP_PORT"),
    ({"payload_hex": "a"}, "WORLD_BAD_UDP_PAYLOAD"),
    ({"payload_hex": "ab cd"}, "WORLD_BAD_UDP_PAYLOAD"),
    ({"payload_hex": None}, "WORLD_BAD_UDP_PAYLOAD"),
    ({"payload_hex": "ab" * (udp.PAYLOAD_MAX + 1)}, "WORLD_BAD_UDP_PAYLOAD"),
    ({"time_us": -1}, "WORLD_BAD_TIME"),
    ({"time_us": 0}, "WORLD_PAST_INPUT"),
    ({"source": True}, "WORLD_BAD_STAMP"),
    ({"radio": 2}, "WORLD_UNKNOWN_RADIO"),
])
def test_input_refusals_do_not_queue(stub_world, fields, error):
    world = stub_world()
    udp.install(world)
    world.start()
    world.run("max")
    accepted = []
    world.on_input(accepted.append)
    assert request(world, **{"time_us": 1000, **fields})["error"] == error
    assert not accepted


def test_paused_and_powered_off_refusals(stub_world):
    world = stub_world()
    udp.install(world)
    world.start()
    assert request(world)["error"] == "WORLD_PAUSED"
    world.run("max")
    world.power_off(1)
    assert request(world)["error"] == "WORLD_RADIO_OFF"


@pytest.mark.parametrize("offset,value,error", [
    (14, 0x44, "UDP_BAD_IPV4"), (16, 0xff, "UDP_BAD_IPV4"),
    (20, 0x60, "UDP_BAD_IPV4"), (24, 0, "UDP_BAD_IPV4"),
    (38, 0xff, "UDP_BAD_LENGTH"), (42, 0xff, "UDP_BAD_CHECKSUM"),
])
def test_malformed_output_fails(offset, value, error):
    wire = bytearray(udp.ethernet(PACKET))
    wire[offset] = value
    with pytest.raises(ChannelError, match=error):
        udp.decode(bytes(wire))


def test_padding_zero_checksum_non_udp_and_truncated_output():
    wire = udp.ethernet(PACKET)
    padded = udp.decode(wire + b"\0" * 15)
    assert padded["payload_hex"] == PACKET["payload_hex"]
    assert udp.decode(wire[:40] + b"\0\0" + wire[42:])["payload_hex"] == PACKET["payload_hex"]
    assert udp.decode(wire[:12] + b"\x08\x06" + wire[14:]) is None
    assert udp.decode(wire[:23] + b"\x06" + wire[24:]) is None
    for malformed in (wire[:13], wire[:33], wire[:-1], wire + b"\0" * 600):
        with pytest.raises(ChannelError, match="UDP_BAD_"):
            udp.decode(malformed)


def test_firmware_gateway_reply_and_identical_traces(vhost_elf, capsys):
    stamp = 2800000
    host_ip = "12.47.77.97"  # 12.<3100001>, the first dmr-pair member (ipGatewayHostIP).
    radio_ip = "12.47.77.96"
    ping, reply = debug_frame(1, 7, b"udp"), debug_frame(0x81, 7, b"\0udp")
    network = debug_frame(0x0a, 0, b"\x19\x01")

    def run():
        world = World(Config(executable=str(vhost_elf), end_us=3000000,
                             radio_args=("--host-port", "on", "--storage", "dmr-pair",
                                         "--host", "0:" + network.hex())))
        traces, events = [], []
        udp.install(world)
        world.api.notify = events.append
        world.on_outbox(Kind.TRACE, lambda radio, message: traces.append(message.payload))
        try:
            world.start()
            world.run("max")
            result = request(world, source_ip=host_ip, destination=radio_ip, source_port=42000,
                             port=3334, payload_hex=ping.hex(), time_us=stamp)
            assert result["ok"], result
            while world.state != "Ended":
                world.advance()
            assert world.reason == "WORLD_END_TIME"
            assert any(line.startswith(f"{stamp:012d} ".encode()) and b"BOARD net.rx " in line for line in traces)
            delivery, = [e for e in events if e["event"] == "udp_delivery" and e["payload_hex"] == reply.hex()]
            assert stamp <= delivery["time_us"] <= stamp + 20000
            assert (delivery["radio"], delivery["source"], delivery["destination"],
                    delivery["source_port"], delivery["port"]) == (1, radio_ip, host_ip, 3334, 42000)
            assert delivery["payload_sha256"] == hashlib.sha256(reply).hexdigest()
            return traces, [e for e in events if e["event"] == "udp_delivery"]
        finally:
            world.close()

    first, second = run(), run()
    assert first == second
    digest = hashlib.sha256(b"\n".join(first[0])).hexdigest()
    with capsys.disabled():
        print(f"\nUDP at {stamp} us: firmware PING reply recorded; two traces identical; "
              f"sha256={digest}")
