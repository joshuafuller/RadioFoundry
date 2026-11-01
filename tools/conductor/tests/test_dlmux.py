# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The debug link multiplexer and observer caches against the real vhost firmware."""

import hashlib
import json
import os
from pathlib import Path
import random
import socket
import stat
import time

import pytest

from tools.conductor.channel import Kind
from tools.conductor.parts import audit, dlcache, dlmux, lease, recorder
from tools.conductor.world import Config, Input, World

SCENARIO = Path(__file__).resolve().parents[1] / "scenarios/dlmux.json"


def test_crc16_matches_bitwise_reference():
    def reference(data, crc=0xFFFF):
        for byte in data:
            crc ^= byte << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        return crc

    assert dlmux.crc16(b"123456789") == 0x29B1
    rng = random.Random(0x1021)
    sizes = [0, 1, 4, 68, 1028, 2048] + [rng.randrange(4097) for _ in range(100)]
    for size in sizes:
        data = rng.randbytes(size)
        initial = rng.randrange(1 << 16)
        for seed in (0, 0xFFFF, initial):
            expected = reference(data, seed)
            assert dlmux.crc16(data, seed) == expected
            assert dlmux.crc16(bytearray(data), seed) == expected
            assert dlmux.crc16(memoryview(data), seed) == expected
            split = size // 2
            assert dlmux.crc16(data[split:], dlmux.crc16(data[:split], seed)) == expected


def make_world(tmp_path, elf, name="dlmux", **options):
    world = World(Config(executable=str(elf), **options))
    recorder.install(world)
    mux = dlmux.get(world)
    world.api.bind(name, tmp_path)
    return world, mux


@pytest.fixture
def live(tmp_path, vhost_elf):
    world, mux = make_world(tmp_path, vhost_elf, end_us=10_000_000)
    world.audit_lines = []
    audit.get(world).add_sink(world.audit_lines.append)
    world.bytes_in = []
    world.on_input(lambda event: event.kind == Kind.BYTES_IN and world.bytes_in.append(event))
    try:
        world.start()
        world.run("max")
        run_until(world, 1_100_000)     # past the first cache refreshes
        yield world, mux
    finally:
        world.close()


def run_until(world, time_us):
    while world.state not in ("Ended", "Paused") and world.now < time_us:
        world.advance()


def replies(data):
    """(cmd, seq, payload) for each frame in data."""
    return [item[1:] for item in dlmux.Parser(dlmux.REPLY_MAX).feed(data) if item[0] == "frame"]


def controller(world, mux, principal="script:ctl"):
    granted = lease.get(world).grant(1, principal)
    return mux.connect(1, principal, "controller", granted["generation"], granted["secret"])


def test_two_clients_interleave_and_each_gets_only_its_own_replies(live):
    world, mux = live
    first = controller(world, mux)
    second = mux.connect(1, "script:ctl", "controller", first.generation, first.secret)   # the holder again
    observer = mux.connect(1, "script:obs", "observer")
    before = len(world.bytes_in)
    for seq in (1, 2, 3):
        mux.feed(first, dlmux.frame(0x01, seq, b"first-%d" % seq))
        mux.feed(second, dlmux.frame(0x01, seq, b"second-%d" % seq))
        mux.feed(observer, dlmux.frame(0x01, seq, b"observer-%d" % seq))
    run_until(world, world.now + 100_000)
    got = {name: replies(session.take()) for name, session in
           (("first", first), ("second", second), ("observer", observer))}
    for name, frames in got.items():
        assert frames == [(0x81, seq, b"\0" + b"%s-%d" % (name.encode(), seq)) for seq in (1, 2, 3)], name
    # The two controllers' six PINGs went upstream, one frame per input; the observer's never did.
    pings = [event for event in world.bytes_in[before:] if event.payload[2] == 0x01]
    assert len(pings) == 6 and all(len(replies_of(event.payload)) == 1 for event in pings)
    assert {event.source for event in pings} == {first.source, second.source}


def replies_of(payload):
    return [item for item in dlmux.Parser(dlmux.REQUEST_MAX).feed(payload) if item[0] == "frame"]


def test_observer_get_view_is_answered_from_the_cache(live):
    world, mux = live
    observer = mux.connect(1, "script:obs", "observer")
    cached = mux.caches.store(1).views[0]
    count = len(world.bytes_in)
    mux.feed(observer, dlmux.frame(0x09, 42, b"\x00"))
    assert len(world.bytes_in) == count        # the radio's BYTES_IN count does not change
    assert replies(observer.take()) == [(0x89, 42, cached)]
    assert cached[0] == 0 and b"section=core" in cached


def test_observer_key_is_refused_and_audited(live):
    world, mux = live
    observer = mux.connect(1, "script:obs", "observer")
    count = len(world.bytes_in)
    mux.feed(observer, dlmux.frame(0x06, 5, bytes((0, 5, 0, 0))))
    assert replies(observer.take()) == [(0x86, 5, bytes((dlmux.ST_REFUSED,)))]
    assert len(world.bytes_in) == count
    line = world.audit_lines[-1]
    assert (line["kind"], line["principal"], line["result"], line["reason"], line["command"], line["role"]) == (
        "control", "script:obs", "refused", "DLMUX_OBSERVER_READ_ONLY", "KEY", "observer")
    assert not world.holds.tracked(1)


def test_unknown_command_is_refused_before_the_firmware(live):
    world, mux = live
    session = controller(world, mux)
    count = len(world.bytes_in)
    mux.feed(session, dlmux.frame(0x42, 9, b"x"))
    run_until(world, world.now + 20_000)
    assert replies(session.take()) == [(0xC2, 9, bytes((dlmux.ST_UNKNOWN,)))]
    assert not [event for event in world.bytes_in[count:] if event.source == session.source]
    assert world.audit_lines[-1]["reason"] == "DLMUX_UNKNOWN_COMMAND"
    # A frame with a bad CRC gets an error frame from the multiplexer, also without the firmware.
    bad = bytearray(dlmux.frame(0x01, 3, b"ab"))
    bad[-1] ^= 0xFF
    mux.feed(session, bytes(bad))
    assert replies(session.take()) == [(0xFF, 3, bytes((dlmux.ST_BAD_CRC, 0x01)))]


def test_read_mem_without_maintenance_is_refused(live):
    world, mux = live
    session = controller(world, mux)
    count = len(world.bytes_in)
    mux.feed(session, dlmux.frame(0x0C, 4, (0x20000000).to_bytes(4, "little") + (16).to_bytes(2, "little")))
    run_until(world, world.now + 20_000)
    assert replies(session.take()) == [(0x8C, 4, bytes((dlmux.ST_REFUSED,)))]
    assert not [event for event in world.bytes_in[count:] if event.source == session.source]
    line = world.audit_lines[-1]
    assert (line["kind"], line["result"], line["reason"], line["command"]) == (
        "control", "refused", "LEASE_NO_MAINTENANCE", "READ_MEM")


def test_radio_refusal_of_a_forwarded_command_is_audited(live):
    # The audit names the completion status, not only the lease decision.
    world, mux = live
    session = controller(world, mux)
    lease.get(world).grant_maintenance(1, session.principal, session.generation, session.secret)
    mux.feed(session, dlmux.frame(0x0C, 4, (0x20004000).to_bytes(4, "little") + (1).to_bytes(2, "little")))
    run_until(world, world.now + 200_000)
    assert replies(session.take()) == [(0x8C, 4, bytes((dlmux.ST_REFUSED,)))]
    lines = [line for line in world.audit_lines if line.get("command") == "READ_MEM"]
    assert [(line["kind"], line["result"], line["reason"]) for line in lines] == [
        ("control", "accepted", "OK"), ("control_reply", "refused", "DLMUX_RADIO_REFUSED")]
    assert (lines[-1]["principal"], lines[-1]["radio"], lines[-1]["status"], lines[-1]["session"],
            lines[-1]["seq"], lines[-1]["lease_generation"]) == (
        session.principal, 1, dlmux.ST_REFUSED, session.id, 4, session.generation)
    # A reply with status OK gets its completion line too.
    count = len(world.audit_lines)
    mux.feed(session, dlmux.frame(0x01, 5, b"ok"))
    run_until(world, world.now + 200_000)
    assert replies(session.take()) == [(0x81, 5, b"\0ok")]
    done = [line for line in world.audit_lines[count:] if line["kind"] == "control_reply"]
    assert [(line["result"], line["reason"], line["command"], line["status"], line["seq"]) for line in done] == [
        ("completed", "OK", "PING", dlmux.ST_OK, 5)]


def test_log_is_drained_once_and_copied_to_every_client(live):
    world, mux = live
    one, two = mux.connect(1, "script:one", "observer"), mux.connect(1, "script:two", "observer")
    for session in (one, two):
        mux.feed(session, dlmux.frame(0x08, 1))
    texts = [replies(session.take())[0][2] for session in (one, two)]
    assert texts[0] == texts[1] and b"debuglink: up" in texts[0]
    mux.feed(one, dlmux.frame(0x08, 2))
    assert b"debuglink: up" not in replies(one.take())[0][2]    # its own cursor moved on


def test_socket_endpoint_is_private_and_binds_the_role(live, monkeypatch):
    world, mux = live
    result = world.api.dispatch({"version": 1, "command": "debuglink.open", "radio": 1,
                                 "principal": "script:sock", "role": "observer"})
    assert result["ok"], result
    path = Path(result["result"]["path"])
    assert path.parent == world.api.directory and stat.S_IMODE(path.stat().st_mode) == 0o600
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(path))
    client.settimeout(0.02)
    world.api.poll(0)                 # accept: the socket file is gone, the role stays observer
    assert not path.exists()
    client.sendall(dlmux.frame(0x01, 7, b"hi") + dlmux.frame(0x07, 8, b"\x01"))
    deadline = time.monotonic() + 5
    data = b""
    while len(replies(data)) < 2 and time.monotonic() < deadline:
        world.api.poll(0.01)
        try:
            data += client.recv(4096)
        except socket.timeout:
            pass
    assert replies(data) == [(0x81, 7, b"\0hi"), (0x87, 8, bytes((dlmux.ST_REFUSED,)))]
    client.close()
    # A controller session needs the current lease; another UID is turned away.
    refused = world.api.dispatch({"version": 1, "command": "debuglink.open", "radio": 1,
                                  "principal": "script:sock", "role": "controller", "generation": 1})
    assert refused["error"] == "LEASE_NOT_HOLDER"
    other = world.api.dispatch({"version": 1, "command": "debuglink.open", "radio": 1,
                                "principal": "script:x", "role": "observer"})["result"]
    monkeypatch.setattr(world.api, "uid_reader", lambda connection: os.getuid() + 1)
    intruder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    intruder.connect(other["path"])
    world.api.poll(0)
    intruder.settimeout(5)
    assert intruder.recv(16) == b""
    assert world.audit_lines[-1]["reason"] == "DLMUX_OTHER_USER"


def test_cadence_is_a_part_option_fixed_at_start(tmp_path, vhost_elf):
    assert dlcache.scenario_cadence(SCENARIO) == 250_000
    world = World(Config(executable=str(vhost_elf), end_us=600_000))
    try:
        recorder.install(world)
        dlcache.install(world, cadence_us=100_000)
        mux = dlmux.get(world)
        world.api.bind("cadence", tmp_path)
        world.start()
        with pytest.raises(ValueError, match="DLCACHE_CADENCE_FIXED"):
            dlcache.get(world, cadence_us=250_000)
        world.run("max")
        run_until(world, 600_000)
        assert mux.status()["cadence_us"] == 100_000
        lines = [json.loads(line) for line in (world.api.directory / "events.jsonl").read_text().splitlines()]
        assert {"cadence_us": 100_000} .items() <= next(
            line for line in lines if line.get("event") == "dlcache_cadence").items()
    finally:
        world.close()


def run_scenario(tmp_path, elf, name, observers):
    data = json.loads(SCENARIO.read_text())
    inputs = tuple(Input(event["radio"], Kind[event["kind"]], bytes.fromhex(event["payload_hex"]),
                         event["time_us"], event["source"]) for event in data["inputs"])
    world = World(Config(executable=str(elf), inputs=inputs, end_us=data["end_us"]))
    dlcache.install(world, cadence_us=dlcache.scenario_cadence(SCENARIO))
    mux = dlmux.get(world)
    world.api.bind(name, tmp_path)
    digest = hashlib.sha256()
    lines = []
    world.on_outbox(Kind.TRACE, lambda radio, message: (digest.update(message.payload + b"\n"),
                                                         lines.append(message.payload)))
    sessions = [mux.connect(1, f"script:obs{n}", "observer") for n in range(observers)]
    answered = []

    def poll_views(target, outboxes):
        for session in sessions:
            mux.feed(session, dlmux.frame(0x09, target // 1000 % 256, bytes((target // 1000 % 7,))))
            answered.extend(replies(session.take()))
        return ()

    world.on_grant_done(poll_views)
    try:
        world.start()
        world.run("max")
        while world.state != "Ended":
            world.advance()
        assert world.reason == "WORLD_END_TIME"
    finally:
        world.close()
    return digest.hexdigest(), len(lines), answered


def test_trace_hash_is_the_same_with_zero_and_three_observers(tmp_path, vhost_elf):
    (tmp_path / "a").mkdir(mode=0o700)
    (tmp_path / "b").mkdir(mode=0o700)
    alone, count_alone, _ = run_scenario(tmp_path / "a", vhost_elf, "alone", 0)
    watched, count_watched, answered = run_scenario(tmp_path / "b", vhost_elf, "watched", 3)
    print(f"\nzero observers:  {alone}  ({count_alone} TRACE lines)")
    print(f"three observers: {watched}  ({count_watched} TRACE lines, {len(answered)} cached GET_VIEW replies)")
    assert len(answered) > 100 and sum(payload[0] == 0 for _, _, payload in answered) > 100
    assert alone == watched


def test_hold_kinds_follow_the_firmware(live):
    hold = dlmux.Multiplexer._hold_of
    assert hold(0x07, b"\x01") == ("debuglink.ptt", "PTT", "down")
    assert hold(0x07, b"\x00\x10\x27") == ("debuglink.ptt", "PTT", "up")
    assert hold(0x06, bytes((0, 19, 3, 0))) == ("debuglink.ptt", "PTT", "down")
    assert hold(0x06, bytes((3, 19, 100, 0))) == ("debuglink.ptt", "PTT", "down")
    assert hold(0x06, bytes((0, 20, 3, 0))) == ("debuglink.raw", "action:20", "down")
    assert hold(0x06, bytes((0, 20, 4, 0))) == ("debuglink.raw", "action:20", "up")
    assert hold(0x06, bytes((0, 5, 0, 0))) == ("debuglink.timed", "timed", "down")
    assert hold(0x06, bytes((1, 1, 0, 0, 0, 100, 0))) == ("debuglink.timed", "timed", "down")
    assert hold(0x06, bytes((2,))) is None
    assert hold(0x06, bytes((0, 23, 3, 1))) is None


def test_world_client_copying_the_holder_from_lease_status_is_refused(live):
    # Principal and generation are public (lease.status), so they are
    # not proof. Only the one-time grant secret, returned to the caller of lease.grant, opens a controller.
    world, mux = live
    granted = world.api.dispatch({"version": 1, "command": "lease.grant", "radio": 1, "principal": "script:holder"})
    assert granted["ok"], granted
    status = world.api.dispatch({"version": 1, "command": "lease.status"})["result"]["1"]
    count, keys = len(world.audit_lines), len(world.bytes_in)
    copied = world.api.dispatch({"version": 1, "command": "debuglink.open", "radio": 1, "role": "controller",
                                 "principal": status["principal"], "generation": status["generation"]})
    assert copied == {"version": 1, "id": None, "ok": False, "error": "LEASE_BAD_SECRET"}
    line = world.audit_lines[-1]
    assert len(world.audit_lines) == count + 1 and (line["kind"], line["principal"], line["result"],
                                                   line["reason"]) == (
        "debuglink_open", "script:holder", "refused", "LEASE_BAD_SECRET")
    assert not mux.sessions or all(s.role == "observer" for s in mux.sessions.values())
    # A wrong or odd secret is refused the same way, never by a crash.
    for wrong in ("x" * 43, "é" * 43, 7, None):
        reply = world.api.dispatch({"version": 1, "command": "debuglink.open", "radio": 1, "role": "controller",
                                    "principal": "script:holder", "generation": status["generation"],
                                    "secret": wrong})
        assert reply["error"] == "LEASE_BAD_SECRET", (wrong, reply)
    # Raising the holder to maintenance needs the secret too.
    raised = world.api.dispatch({"version": 1, "command": "lease.grant_maintenance", "radio": 1,
                                 "principal": "script:holder", "generation": status["generation"]})
    assert raised["error"] == "LEASE_BAD_SECRET" and not lease.get(world).held[1].maintenance
    # The requester's own secret works, and the radio sees nothing from the refused attempts.
    secret = granted["result"]["secret"]
    opened = world.api.dispatch({"version": 1, "command": "debuglink.open", "radio": 1, "role": "controller",
                                 "principal": "script:holder", "generation": status["generation"],
                                 "secret": secret})
    assert opened["ok"], opened
    assert not [event for event in world.bytes_in[keys:] if event.source >= dlmux.SESSION_SOURCE]


def test_grant_secret_never_reaches_status_audit_or_recording(tmp_path, vhost_elf):
    world, mux = make_world(tmp_path, vhost_elf, name="secret", end_us=3_000_000)
    try:
        world.start()
        world.run("max")
        run_until(world, 1_100_000)
        granted = world.api.dispatch({"version": 1, "command": "lease.grant", "radio": 1,
                                      "principal": "script:holder"})["result"]
        secret = granted["secret"]
        assert len(secret) >= 40 and secret in audit.get(world).secrets
        status = [world.api.dispatch({"version": 1, "command": name})["result"]
                  for name in ("lease.status", "status")] + [mux.status()]
        assert secret not in json.dumps(status)
        session = mux.connect(1, "script:holder", "controller", granted["generation"], secret)
        assert secret not in repr(session) and secret not in repr(lease.get(world).held[1])
        lease.get(world).grant_maintenance(1, "script:holder", granted["generation"], secret)
        mux.feed(session, dlmux.frame(0x01, 1, b"hi"))
        run_until(world, world.now + 200_000)
        assert replies(session.take()) == [(0x81, 1, b"\0hi")]
        lease.get(world).revoke(1)
        run_until(world, world.now + 200_000)
        directory = world.api.directory
    finally:
        world.close()
    files = [directory / "audit.jsonl", directory / "events.jsonl"]
    texts = [path.read_text() for path in files]
    assert all(texts) and all('"recording_stopped"' not in text for text in texts)
    counts = [text.count(secret) for text in texts]
    print(f"\nsecret count: audit.jsonl {counts[0]}, events.jsonl {counts[1]} "
          f"({texts[1].count(chr(10))} event lines, recording not stopped)")
    assert counts == [0, 0]
    assert '"lease_grant"' in texts[1] and '"control_reply"' in texts[1]
