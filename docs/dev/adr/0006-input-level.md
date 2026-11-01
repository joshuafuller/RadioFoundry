<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# ADR 0006: Input enters at a virtual keypad

| Field | Value |
|---|---|
| Status | Implemented |
| Date | 2026-10-31 |
| Shapes | [Architecture, "Input"](../virtual-radio-architecture.md#input) and check input boundary |

## Context

On a radio a key press passes five steps: the board scan every 10 ms, debounce, the gesture engine, the key map, and
emit (PTT and MONITOR edges to the radio core, everything else to the UI queue). Each step skipped is a step not
tested. Today 17 paths inject input. The research worker calls `ui_app_key` directly and skips all five steps. The
firmware's own `input_inject_*` path keeps the steps but cannot say all a finger can: one hold at a time, at least
30 ms and at most 10 s, no key held at power-on, and a host thread calling it would break the fixed interrupt points
of [0002](0002-execution-model.md).

## Decision

1. Every key, encoder turn and PTT the world sends enters through a **virtual keypad behind `hal_input_t`**:
   `scan()` returns the bits held now, `encoder()` the detents since the last scan (at most +/-127, the rest kept for
   the next scan). World events set them at virtual
   times and raise an interrupt.
2. The debug link and web control keep their `input_inject_*` path unchanged. It is shipped firmware code.
3. Direct `ui_app_key` is banned on the virtual radio path. The UI simulator keeps it as a UI unit test.
4. The record unit is a key bit down or up, or a signed encoder detent count, at virtual µs. Never a gesture. The bit,
   not the action. No clamp.
5. One boundary: keys sent over the debug link or web control are recorded as those bytes, never also as key edges.
6. PTT is one more key bit; a keypad PTT has no 10 s cap. The radio's own TX time-out stops it.

World API `key` and `enc` commands require the controller's `principal`, lease `generation`, and grant `secret`.
The world socket alone carries no radio-control authority. Delivered key downs enter the shared hold tracker;
key ups remove them. Revocation or pause cancels that lease's queued edges and encoder turns, records the
cancellations, and releases delivered holds. A successor waits for the firmware's key scan to confirm release.
Canceled inputs remain in the audit history but are excluded from replay. Offline `Config.inputs` describe
an isolated scenario's physical inputs; they are not a live World API control session.

## Consequences

- Chords, long holds, boot keys and too-short taps can all be said, and the firmware answers as a radio would.
- Each virtual board needs a key map. Tests name keys by action or by a name table in `boards/vhost`; the record
  stores the bit.
- Pin-level detail (key matrix, quadrature phases) is not modeled; board driver tests cover those drivers.
- Seen, not fixed: an injected action no key gives posts straight to the UI queue and skips the radio edge; web
  control looks keys up in the static key map, not the live one.

## Alternatives considered

- **The world uses `input_inject_*` only.** Not thread safe from the world, cannot express overlapping or long
  holds, and marks input as from the host. Rejected.
- **Direct `ui_app_key`.** Skips the whole input layer and races the UI task. Banned.
- **Pin level.** Each board wires keys differently; a virtual board would copy one real driver. Not now; possible
  later behind the same seam.
