<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# ADR 0002: The firmware's own tasks on a deterministic host port

| Field | Value |
|---|---|
| Status | Implemented |
| Date | 2026-10-31 |
| Shapes | [Architecture, "Layers"](../virtual-radio-architecture.md#layers) |

## Context

A virtual radio could run the firmware in two ways. It could run the firmware's own startup and FreeRTOS tasks on a
host port. Or a harness could call the tasks' step functions (`radio_sm_poll` and the like) in a loop of its own, as
the research worker and the UI simulator do.

The research worker's loop already differs from the firmware's tasks. Example: a key press that changes channel goes
into a queue on the radio, and the radio task drains the whole queue before its tick and poll. The worker applies the
request the moment it arrives. So a test can pass on an order no radio ever runs. The worker also never runs
`app_start` and cannot run the 17 blocking `hal_delay_ms` sites in core.

Prior art: only designs in which one thread runs at a time, the tool owns the clock and interrupts land at
chosen points repeat exactly. The stock FreeRTOS POSIX and Windows ports do not.

The startup spike built a host port (a fork of the MIT FreeRTOS POSIX port, one thread runs at a time, one virtual
clock) and a virtual board. The real startup booted every task the board starts, with no change in `core/`. Two
boots gave the same trace byte for byte, also under load and with eight runs at once. An idle radio ran about 85
times real time, about 500 times with all its threads on one CPU, in about 19 MB.

## Decision

1. A virtual radio runs the firmware's own startup and tasks on a deterministic host FreeRTOS port: new
   `platform/host/` and `boards/vhost/`, built by the repository's `Makefile` with `BOARD=vhost`.
2. One process per radio.
3. Interrupts run only at fixed points: when the clock moves, at the end of a critical section, when a thread starts.
4. Code takes no virtual time. A bare spin stops the clock; the run ends as stuck and names the task.
5. Step-function calls stay in focused unit tests only. They are never a way to run a virtual radio.
6. A step-function harness for very large worlds is not built unless a measured cost per radio misses a scale target
   the owner sets.

## Consequences

- The task order comes from the real kernel, not from a copy that drifts. New firmware tasks come for free.
- The guarantee has stated limits: no interrupt inside plain C or a critical section, no time slicing between equal
  priorities, no slow code, no spinning loop. The bench and HIL cover them.
- We own a FreeRTOS port and must re-check it when the kernel moves.
- Board parts are the large cost: a virtual DMR modem, the crash platform, later USB. Any route needs them.
- The context switch sits behind one function, so a WebAssembly build can use fibers later.
- Fallback inside this decision: if the port proves flaky, rebuild it on the native simulator's thread engine (an
  Apache-2.0 base, an owner policy call).

## Alternatives considered

- **Harness loops call step functions.** Cheap now and it repeats, but it repeats an order a person picked by hand.
  It already drifts from the firmware and every upstream change to a task loop must be copied. Rejected for the
  virtual radio; kept for unit tests.
- **Firmware tasks for a few radios, a harness for many.** Two ways to run a radio bring back the drift. Kept only as
  a trigger on a measured need.
