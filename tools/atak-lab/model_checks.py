# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Deterministic link impairments and real firmware scheduling checks."""

from datetime import datetime, timedelta
import random

from bridge import Pending
from lab import Node, Pipe, Ppp, decode, ethernet

DEST = ("225.0.0.201", 6969)
APP = ("239.2.3.1", 6969)


def partial(worker, event, chat):
    for gap in (499, 500, 501):
        with Node(worker, 1001) as node:
            phone = Ppp(node)
            phone.open()
            frame = phone.frame(phone.IPV4, ethernet(node.id, DEST, event)[14:])
            errors = node.status()["rx_errors"]
            assert not phone.write(frame[:40])
            assert not phone.poll(gap)
            assert not phone.write(frame[40:])
            assert bool(phone.rf) == (gap < 500), f"partial frame survived gap {gap}"
            after = node.status()["rx_errors"]
            assert (after > errors) if gap >= 500 else (after == errors)
            phone.rf.clear()
            assert phone.send(ethernet(node.id, DEST, event)[14:]), "no recovery after partial frame"


def echo(worker, event, chat):
    with Node(worker, 1001) as node:
        phone = Ppp(node)
        phone.open()
        assert not phone.poll(14999), "echo before quiet boundary"
        for delay in (1, 15000, 15000, 15000, 15000):
            before = node.status()["rx_frames"]
            assert len(phone.poll(delay)) == 1, "missing echo at quiet boundary"
            assert node.status()["rx_frames"] == before + 1, "phone echo reply was not accepted"
        assert phone.echoes == 5 and node.status()["up"], "responsive peer died"


def dead(worker, event, chat):
    with Node(worker, 1001) as node:
        phone = Ppp(node)
        phone.open()
        phone.poll(59999, answer=False)
        assert node.status()["up"], "peer died before dead boundary"
        phone.poll(1, answer=False)
        assert not node.status()["running"] and not node.status()["up"], "dead peer stayed up"


def restart(worker, event, chat):
    with Node(worker, 1001) as node:
        phone = Ppp(node)
        phone.open()
        phone.poll(60000, answer=False)
        assert not node.status()["up"]
        phone = Ppp(node)
        phone.open()
        assert node.status()["up"] and phone.send(ethernet(node.id, DEST, event)[14:]), "restart did not restore data"


def pipe_profiles(worker, event, chat):
    rng = random.Random(11)
    # Boundary profiles plus seeded partitions of payload size, escapes, chunk size, latency and pacing.
    for index in range(24):
        chunk = (20, 244)[index] if index < 2 else rng.randint(20, 244)
        latency, jitter, writes, interval = rng.randint(1, 3), rng.randint(0, 3), rng.randint(1, 4), 15
        with Node(worker, 1001) as node, Node(worker, 1002) as receiver:
            phone = Ppp(node)
            phone.uplink = Pipe(chunk, latency, jitter, writes, interval)
            phone.downlink = Pipe(chunk, latency, jitter, writes, interval)
            phone.open()
            size = (1, 556)[index] if index < 2 else rng.randint(1, 556)
            payload = bytes(rng.randrange(256) for _ in range(size))
            bursts = phone.send(ethernet(node.id, DEST, payload)[14:])
            delivered = [reply[1:] for burst in bursts for reply in receiver.command(burst) if reply[:1] == b"F"]
            assert len(delivered) == 1 and decode(delivered[0])[2] == payload, "paced pipe changed data"
            assert node.status()["rx_errors"] == 0
            for pipe in (phone.uplink, phone.downlink):
                assert pipe.trace and all(0 < length <= chunk for _, length, _ in pipe.trace)
                assert all(b[0] >= a[0] + latency for a, b in zip(pipe.trace, pipe.trace[1:]))
    # An independent exact schedule catches pacing/latency/jitter being silently ignored.
    pipe = Pipe(20, latency=2, jitter=3, writes=2, interval=30, seed=11)
    times = [due for due, _ in pipe.transfer(bytes(100), 0)]
    assert times == [5, 10, 35, 38, 63], times
    for chunk in (19, 245):
        try:
            Pipe(chunk)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid chunk size accepted")


def loss(worker, event, chat):
    for dropped in ({"drop_byte": 50}, {"drop_chunk": 1}):
        with Node(worker, 1001) as node:
            phone = Ppp(node)
            phone.open()
            packet = ethernet(node.id, DEST, b"x" * 200)[14:]
            before = node.status()["rx_errors"]
            assert not phone.write(phone.frame(phone.IPV4, packet), **dropped)
            assert not phone.rf, "damaged frame reached air"
            assert node.status()["rx_errors"] == before + 1, "FCS did not reject byte/chunk loss"
            assert phone.send(packet), "link did not recover after loss"

    with Node(worker, 1001) as node:
        phone = Ppp(node, 244)
        phone.open()
        packet = ethernet(node.id, DEST, b"lost whole frame")[14:]
        frame = phone.frame(phone.IPV4, packet)
        assert len(frame) < 244
        before = node.status()
        assert not phone.write(frame, drop_chunk=0) and not phone.rf
        assert node.status()["rx_errors"] == before["rx_errors"], "an absent frame cannot fail FCS"
        assert phone.send(packet), "no recovery after whole-frame loss"


def coalescing(worker, event, chat):
    queue = Pending()
    old_time = event.split(b'time="')[1].split(b'"')[0]
    new_time = (datetime.fromisoformat(old_time.decode().replace("Z", "+00:00")) + timedelta(seconds=1)).isoformat().replace("+00:00", "Z").encode()
    newer = event.replace(b'time="' + old_time, b'time="' + new_time).replace(b'lat="0"', b'lat="1"')
    other = event.replace(b'uid="RF-LAB-A"', b'uid="RF-LAB-B"')
    queue.put(APP, event)
    queue.put(APP, other)
    queue.put(APP, newer)
    queue.put(APP, event)  # Out-of-order and duplicate positions must not undo the newer fix.
    queue.put(APP, newer)
    queue.put(("224.10.10.1", 17012), chat)
    queue.put(("224.10.10.1", 17012), chat)
    assert len(queue.events) == 4, "position did not coalesce or chat was coalesced"
    assert [queue.pop()[2] for _ in range(4)] == [newer, other, chat, chat]


def oversize(worker, event, chat):
    queue = Pending()
    padded = event.replace(b"</detail>", b" " * (556 - len(event)) + b"</detail>")
    assert len(padded) == 556
    queue.put(APP, padded)
    try:
        queue.put(APP, padded.replace(b"</detail>", b" </detail>"))
    except ValueError as error:
        assert "557" in str(error) and "556" in str(error), "oversize lacks a reason"
    else:
        raise AssertionError("oversize CoT accepted")
    assert len(queue.events) == 1 and queue.pop()[2] == padded, "refusal changed pending event"


def capacity(worker, event, chat):
    with Node(worker, 1001) as node:
        node.command(b"M")
        node.command(b"C\0\0")
        frame = ethernet(node.id, DEST, b"test")  # 36-byte record + 12-byte UDP datagram
        maximum = node.status()["capacity"] // 48
        for _ in range(maximum):
            assert not node.command(b"H" + frame)
        stats = node.status()
        assert stats["depth"] == maximum and stats["bytes"] == maximum * 48
        node.command(b"H" + frame)
        assert node.status()["refused"] == 1 and node.status()["depth"] == maximum
        assert not node.advance(2000)
        assert node.status()["gave_up"] == 0
        assert not node.advance(1)
        assert node.status()["gave_up"] == 1, "queue did not give up at wait boundary"


def aging(worker, event, chat):
    with Node(worker, 1001) as node:
        node.command(b"M")
        node.command(b"C\0\0")
        for _ in range(20):
            node.command(b"H" + ethernet(node.id, DEST, b"test"))
        assert not node.advance(10001)
        assert node.status()["aged"] == 0, "queue aged at or before maximum age"
        assert not node.advance(1)
        stats = node.status()
        assert stats["aged"] > 0 and stats["depth"] == 0 and stats["sent"] == 0, "stale queue did not expire"


def chaining_guard(worker, event, chat):
    with Node(worker, 1001) as node, Node(worker, 1002) as receiver:
        node.command(b"M")
        node.command(b"C\0\1")
        packets = [(DEST, b"first"), (DEST, b"second"), (("225.0.0.202", 17012), b"third")]
        for destination, payload in packets:
            assert not node.command(b"H" + ethernet(node.id, destination, payload))
        assert not node.advance(100)
        assert node.status()["tx_lists"] == 0, "busy channel admitted transmission"
        node.command(b"C\1\1")
        assert not node.advance(1)
        assert node.status()["tx_lists"] == 1 and node.status()["chained"] == 1
        received = []
        bursts = 0
        while node.status()["sent"] < 2:
            assert node.now < 5000, "first chain did not complete"
            for burst in node.advance(1):
                assert burst[:1] == b"B"
                bursts += 1
                received.extend(reply[1:] for reply in receiver.command(burst) if reply[:1] == b"F")
        end = node.now
        assert end == 101 + bursts * 60 and len(received) == 2
        assert node.status()["sent"] == 2 and node.status()["tx_lists"] == 1
        assert not node.advance(249)
        assert node.status()["tx_lists"] == 1 and node.status()["tx_guarded"] == 1, "guard allowed early transmission"
        assert not node.advance(1)
        assert node.status()["tx_lists"] == 2 and node.status()["tx_last_ms"] == end + 250
        while node.status()["depth"]:
            assert node.now < 10000, "queue did not drain"
            for burst in node.advance(1):
                received.extend(reply[1:] for reply in receiver.command(burst) if reply[:1] == b"F")
        assert [decode(frame)[2] for frame in received] == [payload for _, payload in packets]


def scheduled_cot(worker, event, chat):
    for payload, expected in ((event, 42), (chat, 45)):
        with Node(worker, 1001) as node, Node(worker, 1002) as receiver:
            sender, consumer = Ppp(node), Ppp(receiver)
            for phone in (sender, consumer):
                phone.uplink = Pipe(20, latency=2, jitter=2, writes=2, interval=15)
                phone.downlink = Pipe(244, latency=2, jitter=2, writes=2, interval=15)
                phone.open()
            node.command(b"M")
            assert not sender.send(ethernet(node.id, DEST, payload)[14:]), "queued packet bypassed service"
            assert node.status()["depth"] == 1
            packets = []
            count = 0
            start = node.now
            while node.status()["depth"]:
                assert node.now - start < 5000, "scheduled CoT did not finish"
                for burst in node.advance(1):
                    assert burst[:1] == b"B"
                    count += 1
                    consumer.take(receiver.advance(max(0, node.now - receiver.now)))
                    packets.extend(consumer.receive(receiver.command(burst)))
            assert count == expected, "service preamble/burst count changed"
            assert node.now - node.status()["tx_last_ms"] == expected * 60
            assert len(packets) == 1 and packets[0][28:] == payload, "scheduled PPP CoT changed"


CASES = (partial, echo, dead, restart, pipe_profiles, loss, coalescing, oversize, capacity, aging, chaining_guard, scheduled_cot)


def check_models(worker, event, chat):
    results = []
    for case in CASES:
        case(worker, event, chat)
        results.append(case.__name__)
        print(f"PASS: link model {case.__name__}", flush=True)
    return results
