<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# ADR 0005: The time model

| Field | Value |
|---|---|
| Status | Implemented on this fork |
| Date | 2026-10-31 |
| Shapes | [Architecture, "Time model"](../virtual-radio-architecture.md#time-model) |

## Context

The firmware reads time four ways: `hal_millis` and `hal_micros`, the RTOS tick and every RTOS timeout, the DMR
modem's `now_us`, and the modem's slot and PCM interrupts. On a PC these must all come from a clock the simulation
owns, or runs do not repeat. Many radios must share one world time, a person must be able to pause and step, CI wants
runs faster than real time, and HIL tests sleep on the wall clock. Inputs from live clients arrive in wall time.

One example. In the startup spike, a key and the tick were both due at 2000 ms. The spike ran the tick first, so the
2000 ms scan missed the key and the 2010 ms scan saw it. A rule must be chosen and written down.

## Decision

1. **One clock per radio.** `hal_millis`, `hal_micros`, the tick and every timeout, modem `now_us`, slot edges and
   PCM blocks all come from it. It stands still while code runs. Reading it never moves it.
2. **Waits.** Sleeps sleep; time jumps when every task waits. Busy waits move the clock, run what falls due, and stop
   at the grant end. A loop that polls the clock without sleeping ends the run as stuck.
3. **Grants.** The world controller grants every radio time up to the next sync point: the earliest of the next
   world event, the proven air lookahead (1 ms until the virtual modem states a longer one and a check proves it), and
   the pacing cap (1 ms by default at a ratio or with a live client; none in max mode with no live client). Outboxes
   are read in radio-number order; collisions are decided after every outbox is in.
4. **Pacing.** Paused, Running (ratio or max, with a Behind sub-state), Pausing (releases sent and confirmed),
   Stepping (`until T`, `next`), Ended. Pacing
   changes only when grants go out, never what the firmware sees. Behind is recorded, never skipped.
5. **Lag.** The world waits for its slowest radio. Only the whole world pauses. A radio in a debugger changes nothing;
   the stall watch counts only while a radio holds a grant or a drain and is off for a radio started with the debugger option.
6. **Order.** Each event's key is (virtual µs, round, class, sequence). Class table: 10 faults and power, 20 air, 30
   clients and scenario inputs, 40 the radio's own hardware, 50 tick, 99 end. The tick comes last among events queued
   before T; the end runs after every round at its time. A grant to T handles events due before T; events due at T
   run in the next grant. Where the world stops, a drain at T handles every event due at T in all rounds, so a
   stop point has T fully handled. An event due in the past ends the run.
7. **Live inputs** land no earlier than the next grant; lateness is recorded; their order repeats because it is
   recorded.

Two rules keep control and replay deterministic:

- **Pause.** While paused, client commands are refused and held keys
  and PTT are released. Those releases are recorded inputs from the conductor. Leases are
  revoked; the same holds while Stepping.
- **Causal tie-break.** Events made at the same virtual time by different sources never run before their cause.
  An event created while time T is handled and due at T joins the next round at T, whatever its class.

## Added by the architecture spec

The implementation also requires:

- the round as part 2 of the order key (the mechanism of the approved causal tie-break);
- a grant to T handles events before T; events at T run in the next grant;
- a "drain T" at every stop point, the next grant starting at T + 1 µs;
- the Pausing state: releases confirmed before the world stops, so a stop with open holds lands after the requested
  point and its time is reported;
- the stall watch counts during grants and drains, and is off for a radio started with the debugger option.

## Consequences

- The same scenario gives the same trace at any pacing and any grant no longer than the proven lookahead. A check
  proves it.
- Pause and step are free: the world stops only at sync points, every radio at one time, no air in flight.
- At first, many-radio DMR worlds run with 1 ms grants and are slower, until the virtual modem proves its lookahead.
- Live inputs can wait up to one grant.
- It changes two earlier texts: the execution model's "grant to the next slot edge" (slot edges move with a slot
  clock restart) and the spike's tick-first order.
- Recordings name their time model and class table, so a later change to the order rule refuses old recordings
  instead of replaying them under new rules ([0008](0008-record-and-replay.md)).

## Alternatives considered

- **Fixed 1 ms lockstep, every tick a world barrier.** Simple, but about 500 000 barriers per wall second at idle
  speed. Kept as the safe setting of this option.
- **Wall-clock time** (the stock ports). Runs do not repeat; no pause, step or faster than real time. Rejected.
- **Reading the clock costs a little** (C2). Every new `hal_millis` call would change traces. Rejected.
- **Assist after many still reads** (C3). Kept as a later opt-in switch for bench tools.
