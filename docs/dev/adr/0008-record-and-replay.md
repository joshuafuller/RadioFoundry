<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# ADR 0008: Record and replay

| Field | Value |
|---|---|
| Status | Implemented on this fork |
| Date | 2026-10-31 |
| Shapes | [Architecture, "Record and replay"](../virtual-radio-architecture.md#record-and-replay) |

## Context

A user should be able to attach a recording to a bug report, and a maintainer replay the same failure step for step.
Four things pull against each other. The research scenario format works and the salvage keeps it, but it ticks in
10 ms steps and records keys as gestures, which [0006](0006-input-level.md) rules out. A recording must hold the
start storage, which holds personal data, and flash dumps may never be committed. Debug link and web control input is
recorded as bytes, and some of those bytes can be a whole codeplug. Pixels change with every font tweak; semantic
state does not.

## Decision

1. **Format.** A recording is a fully resolved scenario in the research scenario format raised to schema version 2:
   times in virtual µs, every input stamped (virtual µs, round, class, sequence, source), key bits instead of
   gestures, host byte streams with no 600 byte cap, start storage as generator steps, the build by hash only, a
   `checkpoints` list, a `tier`, the time model and class table ids.
2. **Contents.** Build identity (binary hash, version, commit time, a hash of the conductor and its models), board
   profile, start storage steps, seed, fixture identities, world epoch, clock offsets, every accepted input, the
   conductor's own events (faults, reboots, releases on pause or lease change, cache reads), air deliveries and
   decisions, checkpoints, the end with a completeness mark. Run notes and the audit stream go along, not compared.
3. **Promise.** The same build, board profile and time model give the same trace byte for byte, the same checkpoints
   and outputs, under any pacing and any grant no longer than the proven lookahead; the first difference is named.
   Another build is a **compare** of VIEW records and assertions, not a replay.
4. **Checkpoints.** FRAME (pixels, [0007](0007-screen-frames-and-observation.md)); VIEW (the UI status fields, from a
   link wrap of the firmware's own `ui_app_status` call, strings copied at once, checked first); STORE (a digest of
   the virtual flash at the end and each reboot).
5. **Start.** Recording is on from world creation, and replay always starts from the recorded start state at time 0.
   No saved state to resume from. During replay the conductor's own generators are off; only recorded conductor
   events are fed. A recording without a complete end record never passes.
6. **Privacy.** No storage bytes in any recording. A storage-carrying input is a generator reference checked by hash,
   or the recording ends there with a named reason. Recordings start local (0600) and become shareable only through
   a lint and a promote step; CI lints every committed recording.
7. **Version 1** files are read only through a converter that maps or refuses each action with a reason.

## Added by the architecture spec

The implementation also requires:

- recording is on from world creation only;
- during replay the conductor's own generators (cache reads, releases, rule faults) are off; only recorded conductor
  events are fed;
- a recording without a complete end record never passes a replay;
- the stamp includes the round ([0005](0005-time-model.md)).

## Consequences

- One file kind for people, CI and live sessions; the kept compare and minimize tools work on recordings.
- The flash dump rule holds by construction: there is no field for an image.
- Live keypad text cannot be checked, so live recordings are never shareable by default.
- No change in `core/`: both checkpoints are link wraps of calls the firmware already makes.
- Old research scenarios need their direct-call actions rewritten as keys, link bytes or storage steps, and keys
  before the 1 s splash re-checked.
- Releases the conductor sends on a pause or a lease change reach the firmware, so they are recorded inputs and
  replay feeds them; it never makes new ones.

## Alternatives considered

- **Keep version 1.** Gesture keys and a 10 ms grid conflict with approved decisions. Rejected.
- **A new format beside scenarios.** Two tools for one idea would drift. Rejected.
- **Pixels only, or semantic only.** One breaks on every font change, the other misses drawing bugs. Rejected.
- **A small flash image of generated data.** It is a flash dump by name, and no check can tell it from a real one.
  Rejected.

Scenario CLI run, suite and reducer exports also use 0700 directories and 0600 files, including failure artifacts
and newly created parent directories. `compare` validates the embedded scenario schema and accepts version-2
results produced by `run`, including fractional milliseconds from its microsecond clock.
