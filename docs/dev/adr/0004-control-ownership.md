<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# ADR 0004: Who may control a virtual radio

| Field | Value |
|---|---|
| Status | Implemented |
| Date | 2026-10-31 |
| Shapes | [Architecture, "Who may control"](../virtual-radio-architecture.md#who-may-control-0004-accepted) and check control ownership |

## Context

The firmware has no client identity. Its debug link has one parser and in network mode replies to the last sender.
Web control gives control to the link-host IP only and admits one `/ws`. Its HTTP replies send a wildcard CORS header
and its WebSocket parser does not check `Origin`, so any web page could reach a loopback radio. A debug link
screenshot holds redraws and a log read consumes lines, so a careless watcher changes the radio. None of this may
change in the firmware ([0003](0003-control-layers.md)).

## Decision

The **conductor**, one host process per world, is the only path to a virtual radio and enforces these rules. The OS
account that runs it is the trust boundary.

1. One **lease** per radio, across debug link, browser and World API keypad. Granted on purpose, never by connection order or rank. A
   generation number makes old leases fail.
2. Revocation, expiry, disconnect or a world pause first releases every tracked key and PTT hold. No successor is
   admitted until each release is confirmed or the radio is restarted with input cleared.
3. Many observers. Unknown or unauthorized commands fail closed before the firmware.
4. The multiplexer parses link frames per client, serializes one request upstream and replies to its sender.
5. Dangerous commands (REBOOT, READ_MEM, STORAGE, CRASH, WATCHDOG, CODEPLUG, STUB, RX2) need the lease and a separate
   maintenance capability. A browser never gets it.
6. `READ_MEM` sees only a bounded fake memory array, never host memory.
7. The world capability and radio control do not imply each other. The world endpoint is separate.
8. Program clients use a per-user Unix socket (folder 0700, socket 0600, peer-UID check). The browser needs a session
   from a one-time URL-fragment token exchanged at `POST /session`. An unauthenticated request may load only a
   static browser shell with no live data. Bootstrap sets a host-only `HttpOnly`, `SameSite=Strict` cookie and
   returns a separate proof kept in exact-origin session storage. Every live-data API, WebSocket, unlock and
   control request needs both credentials, plus exact numeric-loopback `Host` and exact `Origin` checks.
   HTTP uses `X-RadioFoundry-Session` (also the console's CSRF check); WebSockets offer `rf-vr-v1` and
   `rf-vr-proof.<proof>`, and receive only the fixed protocol. Cookies are host-scoped, not port-scoped: another
   loopback service can receive the cookie, which alone conveys no authority. Proofs bind the session, origin,
   radio and per-launch key. A reload retains its tab's proof; a new tab needs a fresh one-time URL.
   The gateway presents the link-host address internally, so the firmware's rule stays.
9. No control service binds to a LAN interface in world control.
10. Virtual air stays inside one world and never reaches real RF. Over-the-air SNMP between radios of one world is
    kept as realism.
11. A recording carries no authority; replay only makes a new, isolated world. Recordings are 0600.
12. Every control attempt and lease change writes an audit event that goes into the recording. Tokens are never
    logged or recorded.

## Consequences

- The firmware stays as on real radios; all policy is host code that can change freely.
- Observers in a virtual radio are answered from host caches, never from the firmware, so watching cannot change the
  run. This is stricter than the review's allowlist (five reads) and fits it; the architecture spec states the rule.
- A pause rejects client mutations instead of queuing them ([0005](0005-time-model.md) takes this rule).
- The shipped `tools/conductor/serial_handlers/protocol_unix.py` handler lets unedited HIL tests open their
  private Unix socket through pyserial; `tools/conductor/run_hil.py` configures it.
- Browser authentication, radio lease ownership and the console's separately pasted world-control capability
  remain distinct. A proof grants neither a radio lease nor world control.
- Hostile same-origin content and access to the owner's process or browser profile are outside this boundary.
  Root and the same OS user stay trusted. LAN or hosted use needs its own security design.

## Alternatives considered

- **Control policy in the firmware.** Rejected: it changes the protocols real radios ship and weakens test parity.
- **First come gets control, or a fixed rank** (HIL over scripts over browser). Rejected: control must be handed out on
  purpose.
- **Loopback alone as authentication.** Rejected: every local user and any web page can reach loopback.
