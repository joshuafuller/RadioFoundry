<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# Virtual radio control channel

The channel carries time grants and a radio's inputs and outputs on one connected Linux `AF_UNIX`,
`SOCK_SEQPACKET` socket. A packet socket preserves message boundaries. The parent passes its child's descriptor
with `--chan FD`. Without that option, the standalone run and its time limit are unchanged.
See the [time model](adr/0005-time-model.md) and [architecture](virtual-radio-architecture.md#the-world-controller-and-grants).
Physical board capabilities are in the [feature matrix](../feature-matrix.md).

`platform/host/vchan_proto.h` owns the kind numbers. `tools/conductor/channel.py` reads those declarations and
provides `Channel`, `Message` and `Kind`. It spawns one child and collects each outbox through `DONE`. It supplies
no pacing, world API, restart policy or multi-radio controller.

## Wire format

One socket packet is one 32-byte header followed by zero to 2048 payload bytes. Every multibyte integer is unsigned
little endian. There is no native C structure padding on the wire. Version is 1. An unknown version or kind is an
error. A packet shorter or longer than its declared length, including a truncated packet, is an error.

| Offset | Bytes | Field |
|---|---|---|
| 0 | 1 | Version |
| 1 | 1 | Kind |
| 2 | 2 | Payload length |
| 4 | 8 | Radio-relative virtual microseconds |
| 12 | 4 | Round at this time |
| 16 | 4 | Event class; inputs use 30, controls and outputs use 0 |
| 20 | 4 | Source ID; stable conductor ID on inputs, 0 on outputs |
| 24 | 8 | Sequence; supplied input order or radio output order |

For `GRANT` and `DRAIN`, only kind and time vary; payload length, round, class, source and sequence must be zero.
Output round is the last handled round, or zero before any event at that time. `DONE` disambiguates that case with
its round count. Output sequence includes `DONE` packets and is transport order, not part of the firmware trace.

| Kind | Value | Payload and direction |
|---|---|---|
| GRANT | 1 | Parent to radio: run before the header's time; no payload |
| DRAIN | 2 | Parent to radio: finish all events at the header's time; no payload |
| DONE | 3 | Radio to parent: next due time u64, then round count u64 |
| RESTART_REASON | 4 | Radio to parent: UTF-8 termination reason, no trailing NUL |
| BYTES_IN | 5 | Parent to radio: host UART bytes, nonempty |
| BYTES_OUT | 6 | Radio to parent: one host UART write |
| KEY | 7 | Parent to radio: opaque payload for the registered key owner |
| ENC | 8 | Parent to radio: opaque payload for the registered encoder owner |
| ETH_IN | 9 | Parent to radio: one Ethernet frame, interpreted by its owner |
| ETH_OUT | 10 | Radio to parent: one Ethernet frame |
| FRAME | 11 | Radio to parent: opaque screen checkpoint, produced by its owner |
| TRACE | 12 | Radio to parent: one complete trace line, without its newline |
| AIR_OUT | 13 | Reserved: radio to parent, one burst per message |
| AIR_IN | 14 | Reserved: parent to radio, one burst per message |
| VIEW | 15 | Reserved: view checkpoint; registered owner defines payload |
| STORE | 16 | Reserved: storage checkpoint; registered owner defines payload |

The air reservations use the header's radio-relative time and a payload prefix of slot u8, burst kind u8, then burst
bytes. Their owner defines and validates the slot and kind values. This channel does not implement a DMR modem.
VIEW and STORE can also have registered input owners; neither has one by default.

`DONE` next due time is `UINT64_MAX` when the queue is empty. Its round count is the last handled round plus one,
or zero if no event ran at this time. It is a round watermark, not a count of callbacks or of nonempty rounds.

## Grants, inputs and drain

The radio first answers `DONE` at time zero, before the first clock advance. Startup code that consumes no virtual
time can already have emitted trace lines. The parent reads this initial outbox before granting more time.

A grant must end strictly after the current clock and before `UINT64_MAX`. It handles events before T and answers
`DONE` at T before handling any event due at T. The parent can then send stamped inputs followed by another grant,
or by `DRAIN T`. Inputs keep the supplied time, round, sequence and source, with class 30, in `vclock_post_stamped`.
Handlers run at that stamp. An input earlier than the clock, or in an already handled round, ends the run.

For example, at 2800000 us the parent sends a BYTES_IN debug link PING with round 0, class 30, source 7 and sequence
1, followed by `DRAIN 2800000`. The input handler calls `uart_vhost_rx_push`. That existing UART hook queues the
FIFO arrival in the next causal round at the same time; its interrupt wakes the firmware's host task. The PING
reply arrives as BYTES_OUT before the drain's DONE. UART FIFO capacity and overflow behavior remain the board's
responsibility. The 2048-byte channel capacity is not a promise that the UART can receive that many bytes at once.

A drain is valid only at the current grant end, once per time. It handles every due event in every round and
answers at T, with nothing later handled. `vclock_drain_horizon` makes that boundary inclusive even inside a nested
busy wait. A wait that needs time after T stays suspended until another grant; it never finishes early. After a
drain, new inputs and the next grant must be at least T + 1 us. The stall watch is disabled while waiting for the
parent and enabled during grants and drains.

Grant boundaries and DONE are absent from the firmware trace. Reconstruct a trace by appending a newline to every
TRACE payload. For the same inputs, those bytes are independent of the grant size. Outboxes are the messages
before each DONE, in socket order. The parent must receive while the radio runs so a full socket can make progress.
Closing the socket ends the child with `VCHAN_DISCONNECTED` (exit 4). It is not a graceful stop command.
The existing `--until-ms` run limit still applies and can end a grant or drain before DONE.

## Part hooks and errors

`vchan_register(kind, handler)` attaches one input owner from its own file, normally in a constructor. The handler
receives a `const vchan_message_t *`, valid until it returns. BYTES_IN is built in and cannot be replaced. An
unregistered KEY, ENC, ETH_IN or reserved input is rejected with `VCHAN_UNHANDLED_KIND`.

`vchan_send(kind, buf, len)` sends one message with the clock's current stamp. Without a channel it does nothing.
`vchan_on_done(fn)` registers an outbox flush callback. The callbacks run in registration order before every DONE,
including the initial one. They may send output but must not change simulated time or firmware state. There are
no default callbacks. At most 16 callbacks and 64 pending input messages are supported; exhaustion fails by name.

Malformed packets end the run with exit 4 and a RESTART_REASON such as `VCHAN_BAD_VERSION`, `VCHAN_UNKNOWN_KIND`,
`VCHAN_BAD_LENGTH`, `VCHAN_BAD_CLASS`, `VCHAN_PAST_INPUT`, `VCHAN_PAST_ROUND` or `VCHAN_INPUT_AFTER_DRAIN`.
Invalid controls use `VCHAN_BAD_CONTROL`, `VCHAN_BAD_GRANT` or `VCHAN_BAD_DRAIN`. A broken socket cannot carry its
own failure reason; stderr reports it. Normal firmware termination streams its END trace and then RESTART_REASON,
including the END diagnostics. No restart is performed by this layer.

## Focused checks

```sh
make -C tests/host vclockTest vchanTest
tests/host/vclockTest
tests/host/vchanTest
make BOARD=vhost
python3 -m pytest tools/conductor/tests/test_channel.py -q
make -C tests/host vhost-skeleton vhost-repeat
python3 tools/check_doc_links.py
python3 tools/check_generic.py
```

The channel tests check 1032- and 2048-byte packets, stamped dispatch, DONE flushes and named wire failures. The
firmware tests compare 3000 ms boot traces with 1 ms and 3000 ms grants and a boundary PING with 1 ms and 7 ms
grants. The clock tests cover all-round drain and a nested busy wait. These are host protocol and scheduling
checks, not evidence of physical timing, RF interoperability or DMR standards compliance.
