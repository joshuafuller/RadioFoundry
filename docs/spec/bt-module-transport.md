<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# Bluetooth module host transport

Design for bead `rf-cmh.2`, with the shared envelope driver implemented by `rf-cmh.9`.
This is host-tested software, not a measured Bluetooth host link. An external AT-command module
terminates Bluetooth and carries an ordered host byte stream over a 115200 UART. Its binary `DATA` envelope is
separate from the host protocol. Board hardware pages supply the wiring and module evidence; see the
[feature matrix](../feature-matrix.md) for board support.

Inputs are the approved hardware description of the module protocol, [PPP](ppp.md),
[USB networking](usb-network.md), [KISS](aprs.md#9-host-port-kiss-tnc),
[host-port CPS dispatch](cps-protocol.md#1-on-the-host-port), [debug link](debuglink.md#1-coexistence-on-the-host-port),
and the [ATAK lab](../../tools/atak-lab/README.md). No vendor implementation is required. Values called
provisional below are implementation policy for testing, not established module limits.

## 1. Stream ownership and protocols

The transport removes module envelopes on receive and adds them on transmit. Payload bytes are unchanged,
including zeroes, CR, LF, PPP flags/escapes and KISS FEND/FESC. Envelope and Bluetooth write boundaries have no
meaning to the host parser. One request can span many envelopes; one envelope can contain several requests.

One selected physical transport owns the existing host dispatcher and its replies. A cable and Bluetooth must
not feed it simultaneously. A selection change first ends the old session and clears its queued bytes; it is not
triggered by an arbitrary byte on the unselected port. Keep the cable available as the recovery choice.

| Use | Existing host behavior after removing envelopes |
|---|---|
| IP / ATAK | PPP enters on `0x7E`, negotiates LCP/IPCP, and uses the existing IP gateway; no Bluetooth-specific IP framing or CoT parsing |
| KISS | With the TNC enabled, `0xC0` starts KISS; its port 0 and existing validation/rate limits still apply |
| Debug | Existing framed debug requests in serial mode; while PPP owns the stream, use debug over UDP 3334 |
| Programming | The existing RadioFoundry CPS parser only, at its documented request boundaries |

The Bluetooth module's data channel number is unrelated to the KISS port or an IP port. Use binary channel
`00` in both directions. Do not allocate `01` for KISS or debug: the observed alternate `ADATA` channel is not
evidence of an independent host connection. Do not use hexadecimal `ADATA` for this transport.

The stock programming path and PPP/KISS do **not** run concurrently. The envelope's use for stock programming
proves byte carriage, not compatibility between a stock phone application and RadioFoundry's CPS. No stock
programming interpreter, flash passthrough or module OTA command is added. To program, close the data session,
return the host dispatcher to idle, and use a supported CPS client; do not inject programming bytes into PPP.
The existing PPP exit rules still apply, but are not a programming multiplexing mechanism.

## 2. Driver lifecycle

The shared driver owns command serialization and parsing. Board/platform code owns UART setup, interrupts,
enable polarity and electrical timing. Initialize the receive path before enabling the module; follow the
[UART startup gotcha](gotchas.md) about a busy or held-low line. Never wait indefinitely for a quiet UART.

| State | Action and transition |
|---|---|
| Disabled | Enable inactive; host disconnected; queues and accessory state empty |
| Starting | Configure 115200 (8N1 pending board verification), clear stale UART status, assert enable after the board's required delay, wait for a complete `READY` event |
| Identifying | Send `AT+VER?\r\n` once; validate a complete `+VER:` reply against the module profile and wait for `OK` |
| Idle | Module identified; no host data session; process connection and accessory events |
| Connected | A profile-matched connection event or complete channel `00` DATA envelope starts the decoded stream |
| Fault | Mark link down, discard session state, deassert enable; a new explicit enable starts again |

Use provisional finite deadlines of 5 s for READY and 2 s for identity, supplied to the parser by a monotonic
clock. Timeout or an unsupported identity enters Fault; no infinite reset loop. Unexpected READY in an active
session means module restart: tear down the old session, then identify again. An ordinary `DISC` returns to Idle.
Explicit disable from any state ends the session and deasserts enable.

`+CONN<field>,<profile>` and `+HOSTDEVICE<field>,<profile>` compare the token after the first comma
against `atmodule_profile_t.data_profile`. Audio `AG` alone never connects the host. Until the data token is
measured and set, a complete channel `00` DATA envelope starts the session. `+DISC` accepts trailing fields.
A disconnect during a pending command faults, because its delivery and acknowledgement cannot safely be reused.
`+READY` and `OK` are exact lines; `+VER:` retains a nonempty identity of at most 20 bytes. A profile may require
an exact identity. NULL permits any such identity for unqualified bench use; it does not certify compatibility.

Classic and LE discovery/control are distinct: `AT+SCAN` / `AT+CONN` / `AT+DISC` and
`AT+LESCAN` / `AT+LEDISC` must not be substituted for one another. The known command list does not establish an
LE connect command or the scan argument meanings. The driver does not scan, change names/PINs, enable audio,
or auto-connect as a side effect of starting the host stream. Select only a measured bearer/profile. Neither
classic SPP exposure nor a usable BLE GATT service is assumed from the UART commands.

## 3. Envelope and receive parser

| Direction | Contract |
|---|---|
| MCU to module | ASCII `AT+DATA=00,<n>\r\n` followed immediately by exactly `n` binary bytes; decimal byte count, no sign, no payload terminator added |
| Module to MCU | `+DATA:00,<n>\r\n<payload>`; one to three checked decimal digits, exactly `n` binary bytes, no trailer |
| Control | ASCII lines terminated by CR/LF; recognize exact event/response tokens, not substrings inside other lines |

The approved hardware description settles the SoC-side grammar. RadioFoundry uses that single grammar with
stricter validation; actual module output still needs bench verification.

Receive rules:

- Incremental header and payload states survive arbitrary UART read boundaries, including splits inside CR/LF,
  the prefix and length digits. Drain multiple complete envelopes/events from one read.
- Cap a control/header line at 128 bytes and a DATA payload at 999 bytes. Check every digit and accept at most
  three before reserving space. Zero is refused; 1 through 999 are accepted; a larger value, overflow, missing
  digits or malformed DATA header faults the session. These are local bounds, not claims
  about module capacity. Never allocate from an untrusted length.
- After a valid header, consume exactly the declared payload length. CR/LF, `READY`, `DISC`, `+DATA:` and AT text
  inside it are ordinary bytes. Never scan a payload for a delimiter or event. There is no trailer; do not trim payload whitespace.
- Between envelopes, allow empty CR/LF lines and count/discard bounded, complete unknown text lines. A partial
  line is retained. An overlong line, UART overrun, malformed envelope or loss of framing enters Fault, rather
  than searching arbitrary binary data for a plausible prefix. No garbage reaches the host dispatcher.
- A well-formed envelope on another channel is consumed to its declared end and discarded/counted. It is never
  forwarded to the selected host. Apply the same length and timeout limits to discarded envelopes.
- Control replies and events can occur between DATA envelopes, even while a transmit command is pending. Dispatch
  them separately. A module that inserts unescaped events inside a declared payload cannot support this framing;
  do not attempt to remove such text heuristically.
- Provisional receive inactivity timeout is 500 ms while a header or payload is incomplete. Expiry faults the
  session; never pass a truncated envelope to the host. Buffer one bounded envelope until complete, then deliver
  it in order. Complete DATA envelopes do not reset or finish a PPP/KISS frame.

An event is recognized only at a known framing boundary. If a disconnect indication is trapped behind an
incomplete payload, the inactivity timeout ends the session. Count malformed headers, timeouts, unknown lines,
wrong channels, overruns, queue refusals and disconnect drops for diagnostics.

## 4. Chunking, flow control and pacing

Start with **20 payload bytes per outgoing DATA envelope**, and at most 20 per phone-side bridge write.
This is a conservative test baseline, not a measured module MTU. Send shorter pending chunks without waiting to
fill 20 bytes. A measured profile may raise either limit independently, initially testing 244 bytes; the two
limits need not match. Phone-side packetization must respect its actual bearer and write limits.

At 115200 8N1 the UART ceiling is 11,520 bytes/s per direction. A 20-byte outgoing payload has a 15-byte header
(`AT+DATA=00,20\r\n`), so it occupies about 3.04 ms and has an ideal payload ceiling of about 6.58 kB/s before
Bluetooth scheduling, responses and gaps. This is a calculation, not throughput evidence.

Only one DATA command is in flight. Finish its header and payload without inserting another AT command.
Handle partial UART writes by retaining the unsent suffix. UART transmit completion only proves that the bytes
left the MCU; it does not prove Bluetooth delivery or room in the module. Do not retry a command whose delivery
is uncertain: duplicate stream bytes would corrupt PPP/KISS. Fail the session instead.

Every command, including `AT+VER?` and each DATA transmission, holds an `OK\r\n` gate. Only an OK
received after the entire command has been written releases it. The provisional command deadline is 2 s,
including UART stalls. Timeout faults without retry. A profile sets the TX limit, initially 20 and never above
508 bytes. No deliberate pacing delay is added beyond the OK gate. OK is not proof of phone delivery.

There is no verified RTS/CTS or phone-side credit contract. Never consume `0x11` or `0x13` as flow control.
The driver services RX while TX waits. Its TX queue and decoded RX queue each hold 1200 bytes, with a separate
999-byte incomplete-envelope buffer. The platform UART ring holds 2048 bytes. These finite bounds are software
policy, not proof that phone stalls or arbitrary task-service gaps are lossless.

Backpressure must reach the producer: a full queue returns a short/zero accepted count, and the caller retains
the suffix or fails the session. Never report bytes accepted and silently overwrite them later. On receive
exhaustion without a verified upstream throttle, fault the link rather than splice the stream. On the phone,
use the measured GATT write completion/credit mechanism or SPP backpressure; do not enqueue unlimited writes.

PPP MRU/MTU and DATA chunk size are different limits. Keep the existing advertised PPP MRU **584** and respect
the peer's negotiated MRU; do not lower IP MTU to 20. A maximum 584-byte PPP information field needs up to
1182 serial bytes with all 590 header/information/FCS bytes escaped and two flags, thus up to 60 chunks of 20.
No IP fragmentation or new reassembly protocol is needed. KISS escaping similarly spans chunks. PPP's existing
500 ms incomplete-frame timeout applies to gaps between delivered bytes, not total transmission time; test that
envelope buffering and pacing do not create such gaps. Existing MRU rejection/buffer limitations remain as in
[PPP section 2](ppp.md#2-lcp-rfc-1661). The PPP UDP payload limit remains 556 bytes, as in the lab.

## 5. Disconnects and accessory events

On DISC, disable, reset, framing fault or transport switch: mark the host disconnected immediately; flush UART,
module-parser and host-stream queues; end PPP/IPCP and network sessions; reset partial KISS/CPS/debug requests.
Discard unsent work belonging to that host session, including queued gateway work not yet handed to the radio
service. Do not cancel unrelated radio jobs or promise to recall a packet already on air. Reconnect creates a
new session and starts from idle with fresh PPP negotiation. Never replay old bytes or a stale programming reply
to a new peer. Application retries must submit a new complete request/datagram.

`PTTKEY` and `PTTBATT` are module control events, never host-stream bytes. Until their complete syntax and peer
association are verified, count and ignore them. Valid battery reports describe the accessory, not the radio's
battery or shutdown threshold. This data-transport implementation does not key RF from PTTKEY. If accessory PTT
is later enabled, route validated press/release state through the existing input and transmit-permission path,
require an explicitly selected accessory, and force release on disconnect/reset/timeout. Duplicate presses must
not latch PTT. Audio routing/profile setup is outside this data link.

## 6. Board and shared interfaces

Implemented declarations and integration boundary:

| Surface | Responsibility |
|---|---|
| `CAP_BT_DATA` | Board has the module/wiring for a host data transport; does not assert measured throughput, SPP, GATT or pairing security |
| Optional `board_t.bt_module` descriptor | Raw UART `hal_stream_t`, enable/flush/error hooks, startup/off timing, and the module profile; NULL when absent |
| Shared driver in `drivers/` | Lifecycle, command serialization, bounded envelope parser, queues, module profile and decoded `hal_stream_t` adapter |
| Existing `board_t.host_port` | Cable remains selected; `atmodule_stream` can replace it once the caller supplies session teardown |
| Existing `board_t.netif` | PPP netif when PPP is selected, as required by the existing PPP spec |

The selector must propagate link-down into host-session teardown; substituting a UART whose `connected()` is
always true is insufficient. Reuse `hal_stream_t` partial-write semantics. Keep pins, UART number and electrical
polarity in board/platform code, and all shared protocol behavior in one driver. No board tests in `core/`.
`CONFIG_BT_MODULE_TRANSPORT` defaults to n everywhere. When enabled, the shared engine and adapter are built,
and a board may supply the optional descriptor and `CAP_BT_DATA`. Merely compiling the feature does not power
the module or change the host port. PPP, KISS and debug retain their existing feature switches and framing.

`atmodule_stream_bind(module, clock_ms, end_session)` installs the single host-task adapter. A non-NULL teardown
callback is required. The adapter calls it on restart, disconnect, fault and rebind, before exposing a new session.
The callback must implement section 5's host protocol and gateway teardown. That firmware selection/teardown
integration is still proposed; the board keeps the cable selected. The engine itself clears all its queues and
flushes the raw UART, discarding the rest of a received batch at a session boundary. Its `generation` exposes
these transitions to other users of the engine.

Engine writes return a short/zero count on queue exhaustion. The stream adapter also faults on a short write,
because existing host producers can ignore a short return. This fails the session instead of splicing a reply.
No runtime selector, pairing UI, scan, audio setup or automatic module retry is implemented.

## 7. Open module measurements (10)

1. Exact RX DATA grammar, length encoding/separators/trailer, zero-length behavior and event ordering, including
   behavior when an event occurs during a payload.
2. Maximum accepted TX and emitted RX envelope lengths, module buffering, and whether envelopes may be split or
   coalesced across phone reads; validate the 999-byte RX and 508-byte TX software bounds.
3. Actual phone bearer: classic SPP, BLE GATT, or both; discover services/characteristics, direction and subscription
   requirements without assuming UUIDs or deriving a bearer from command names.
4. Negotiated BLE MTU and usable write/notification sizes (or SPP write limits), including 20/244-byte trials and
   both sides' fragmentation behavior.
5. Supported write mode, completion/credit/error signals, DATA replies and their meaning; whether backpressure
   survives a phone stall and whether RTS/CTS exists.
6. Sustained and burst throughput, duplex behavior, latency, jitter, loss and minimum safe pacing across phone
   stalls; include full escaped PPP frames and the 500 ms receive deadline.
7. Pairing, bonding, authentication/encryption, peer replacement and reconnect policy. Until verified, do not
   claim protected programming/debug access or silently expose them to any nearby phone.
8. READY and VER syntax/identity variants, UART format, enable/off/startup timing, restart behavior and bounded
   recovery timing; replace provisional deadlines with measured profile values.
9. CONN/HOSTDEVICE/DISC fields, channel assignment, classic versus LE versus audio association, and whether pending
   data survives disconnect inside the module. Prove old data cannot leak into a new session.
10. PTTKEY/PTTBATT formats, units, peer association, release behavior and interaction with data traffic; determine
    whether accessory/audio operation can coexist without corrupting or starving the host link.

## 8. Test plan

`tests/host/atmoduleTest.c` now covers the shared engine and stream adapter: 2,917 deterministic cases at the
default seed, including 1,000 seeded byte-preservation/partition properties. Tests cover lengths 0/1/20/244/508/
999/1000, every two-part split of valid boundary frames, all 256 payload bytes, event-looking payloads, checked
headers, wrong channels, timeout boundaries, disconnect/reset flushing, identity and OK ordering, partial TX,
queue exhaustion, accessory isolation and adapter teardown notification. A sabotage build with the TX OK gate
removed fails the exact-byte/serialization assertion. No RF implementation is linked into this test.

Remaining integration and bench plan:

1. **Host tests with a fake module (engine implemented; selector pending).** Drive the shared state machine with a virtual clock and fake UART/enable
   hooks. Cover every transition, absent/late READY and VER, unexpected reset, data versus audio connection,
   and disconnect at every header/payload position. Check exact TX bytes and partial writes. Split RX at every
   byte; coalesce envelopes/events; embed all 256 byte values and event-looking text in payloads. Test lengths
   0, 1, 20, 244, 508, 999, 1000, numeric overflow, missing digits, unknown channels, malformed/truncated headers,
   CR/LF splits, garbage, overrun and 499/500/501 ms gaps. Use seeded partitions/fuzzing with bounded memory and
   byte-for-byte output assertions. Exercise queue saturation, producer backpressure, command serialization,
   disconnect flushing, cable selection, no stale replay and PTT never causing a transmit call.
2. **ATAK lab with a module model (not implemented in this lane).** Place the actual envelope driver and fake module on the existing PPP byte
   path; keep the two gateway/air processes and no-network pipe arrangement. Exactly what remains: add the driver to the worker build, wrap both PPP serial directions in synthetic
   `+DATA:00,<n>\r\n<payload>` / `AT+DATA=00,<n>\r\n<payload>` at 20 bytes, implement READY/VER/OK in the
   fake module, then run the existing PPP cases through that path. Model the UART wire rate, finite
   buffers, READY/VER/events, credits or explicit pacing, phone stalls and independent directional limits.
   Test 20 and 244-byte chunks, the 556/557-byte UDP boundary, worst-case escaping, CoT both ways, parser faults,
   reconnect with fresh negotiation and absence of old traffic. Add a separate KISS/debug stream case through
   host dispatch; the current lab does not execute that dispatcher. A synthetic RX grammar must be named as such.
   These tests prove software against the model, not GATT negotiation, pairing or phone compatibility.
3. **On-radio pass-through, `rf-cmh.7`.** Owner-authorized bench work first records the ten measurements above
   through the module UART and a controlled phone peer. Establish exact envelopes and lossless binary carriage
   before PPP/KISS. Then run bounded bidirectional byte patterns, PPP negotiation/local ping, debug over UDP,
   KISS framing without RF, disconnect during a frame, phone stalls and reconnect without replay. Record module
   identity, bearer, lengths, pacing, byte/error counts and timing. Use only supported programming operations;
   envelope success is not stock-app compatibility. No DMR transmission until a separate run explicitly passes
   `--allow-rf`. Host and model passes alone do not complete this hardware gate.
