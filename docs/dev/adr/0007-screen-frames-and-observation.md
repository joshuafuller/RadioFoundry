<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# ADR 0007: Screen frames and observation

| Field | Value |
|---|---|
| Status | Implemented on this fork |
| Date | 2026-10-31 |
| Shapes | [Architecture, "Screen frames and observation"](../virtual-radio-architecture.md#screen-frames-and-observation) and checks observer isolation, screenshot control |

## Context

A screen frame can leave the firmware eight ways today, there are three partial frame counters and no single id.
Every way but the panel flush reads the frame buffer on request, and some of them change the run: a debug link
screenshot holds redraws for up to 1 s, web control changes its row bits and sends TCP. Drawing is not a pure
function of state either (a list sets its scroll position while it draws). One `gfx_flush` makes several driver
calls, one per band of changed rows, so a screen frame is complete only when `gfx_flush` returns.

## Decision

1. Screen frames come from the real panel flush. The virtual board's display keeps a panel copy at native depth,
   with palette and backlight, written only by `flush` and `flush_indexed`.
2. The frame id is `frames_begun`, the gfx frame count, the same number the packed screenshot already sends.
3. The checkpoint key is (radio, boot, frame id); a reboot restarts the id and the conductor counts boots.
4. A FRAME checkpoint is written once per `gfx_flush`, after it returns, by a link wrap (`-Wl,--wrap=gfx_flush`).
   Fields: radio, boot, frame id, virtual µs, CRC-32 of the panel copy, palette, backlight. A frame with no changed
   row still gets one.
5. Observers read only the panel copy and the checkpoint list, each through a bounded latest-wins queue. The radio
   never waits for an observer.
6. Any capture through the firmware (debug link SCREENSHOT, `/api/screen.bmp`, web control rows) is an input to the
   run and is recorded. The screenshot hold is in virtual time, as on a radio.

## Consequences

- The flush, the row hashing and the palette update run on the host for the first time.
- No change in `core/`. Fallback if the wrap breaks under the production flags: an optional
  `hal_display_t.frame_done` member that real boards leave NULL.
- A HIL test that takes screenshots sees the hold, as on a real radio. A watch-only client uses the frame stream.
- A one-line fix is needed when the virtual board can enter the stub: the stub banner flushes without
  `gfx_frame_begin`, so it carries the previous frame's id.

## Alternatives considered

- **Read the frame buffer.** Shows the buffer, not the panel; misses flush bugs and must know when the UI is not
  drawing. Rejected.
- **The UI draw count, or a host-only counter, as the id.** The draw count misses the splash and is not reset; a host
  counter would disagree with the screenshot's count. Rejected.
- **A checkpoint when the clock moves.** Loses frames with no changed rows and merges frames in one ms. Rejected.
- **The multiplexer answers SCREENSHOT from the panel copy.** Then the firmware's own screenshot code and hold never
  run. Not in the first slice; possible later as a switch.
