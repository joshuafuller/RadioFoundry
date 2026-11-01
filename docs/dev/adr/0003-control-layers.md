<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# ADR 0003: Three control layers

| Field | Value |
|---|---|
| Status | Implemented |
| Date | 2026-10-31 |
| Shapes | [Architecture, "Control APIs"](../virtual-radio-architecture.md#control-apis) |

## Context

Test scripts, HIL tests, the browser, CI and the world controller all need to drive or watch a virtual radio. The
firmware already has two control protocols. The debug link is a framed binary protocol on the host port with 25
commands; the Python client and every HIL test use it. Web control is the radio's own front panel page over HTTP and
`/ws`. Both already end in one seam, the input injection API. Time, faults and the air are not radio features; today
only the research IPC controls them.

## Decision

| Layer | Clients | Owns |
|---|---|---|
| Debug link, through a host multiplexer | Test scripts, HIL tests | Everything a program does to one radio |
| Web control, served by the firmware's own HTTP and `/ws` code | The browser | The shipped front panel |
| World API, new and host only | World clients (CI, scenario runners) and the world controller | Time, lifecycle, faults, the air, recordings |

1. Both firmware protocols stay exactly as on real radios. No new firmware commands in the first slice.
2. World commands never enter the firmware and are never reachable from a radio interface.
3. Host work: a `hal_stream_t` socket adapter that accepts reconnects, a host `hal_system_t`, a host build with the
   debug link on, a `hal_netif_t` adapter for web control, a debug link multiplexer, and a versioned world API.
4. The conductor carries each radio's debug link bytes over its per-radio channel, so the multiplexer sees the same
   clock. The channel must carry a full 1032 byte reply; the research channel's 600 byte cap is not enough.

## Consequences

- The same HIL test runs on a real radio and a virtual one. Testing the virtual panel tests the shipped panel.
- The world API can change freely: no real radio depends on it.
- Multi-client rules live on the host ([0004](0004-control-ownership.md)). The firmware has one debug link parser and
  admits one `/ws`.
- The browser and scripts see the radio through different windows (`/status` JSON and GET_VIEW text); they can differ
  in small ways.
- HIL clients use wall clocks, so HIL runs need real-time pacing ([0005](0005-time-model.md)).
- Later changes noted in the spec: the security review replaced the "second browser gets 503" fallback with a gateway
  that fans out, and questioned the TCP loopback transport for HIL (an open world control point).

## Alternatives considered

- **One new protocol for everything.** One client and one place for rules, but every feature must be added twice
  (debug link for real radios, the new protocol for virtual ones), the 2168-line Python client and 52 HIL files need a
  second backend, and time commands would sit next to radio commands. Rejected.
