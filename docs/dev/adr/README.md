<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# Architecture decisions

These records explain the virtual radio implementation and its alternatives.
They describe this branch; they do not imply upstream acceptance.

## Index

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-virtual-radio-terms.md) | Terms for the virtual radio | Implemented |
| [0002](0002-execution-model.md) | The firmware's own tasks on a deterministic host FreeRTOS port, one process per radio | Implemented |
| [0003](0003-control-layers.md) | Three control layers: debug link, web control, a host-only world API | Implemented |
| [0004](0004-control-ownership.md) | Who may control a virtual radio: the conductor, leases, observers | Implemented |
| [0005](0005-time-model.md) | One virtual clock per radio, grants to sync points, pacing as a gate | Implemented |
| [0006](0006-input-level.md) | A virtual keypad behind `hal_input_t` | Implemented |
| [0007](0007-screen-frames-and-observation.md) | Screen frames from the panel flush; observers read host copies | Implemented |
| [0008](0008-record-and-replay.md) | A recording is a resolved scenario, schema version 2 | Implemented |

The architecture these decisions add up to is [The Virtual Radio: Architecture](../virtual-radio-architecture.md).
