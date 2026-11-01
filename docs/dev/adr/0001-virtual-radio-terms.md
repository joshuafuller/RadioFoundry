<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# ADR 0001: Terms for the virtual radio

| Field | Value |
|---|---|
| Status | Implemented |
| Date | 2026-10-31 |
| Shapes | [Architecture, "Terms"](../virtual-radio-architecture.md#terms) |

## Context

The project already uses many of the words a virtual radio needs, with other meanings. "Frame" means at least six
things (a debug link frame, a DMR TDMA frame, a screen picture, an Ethernet frame, and more). "View" is the radio
core's published state, read by about 2600 lines. "Session" is a CPS, MMDVM, CAT or host-link session. "Host" is
both "built for a PC" and "the computer on the USB link". Without one rule per word, specs and code drift apart.

## Decision

1. Nineteen terms with one definition each, landed in `docs/glossary.json` under the category "Virtual radio", scope
   `dev`, `preview` false: virtual radio, virtual board, host platform, world, world controller, controller,
   observer, air, air model, session, scenario, recording, replay, checkpoint, screen frame, virtual time, radio view,
   UI simulator, practice mode.
2. A bare "frame" is not a term. Say screen frame, burst, link frame or Ethernet frame.
3. "View" alone means the radio view. A browser page is a page or front panel.
4. "Session" alone means a virtual radio session, only in virtual radio docs. Other kinds keep their prefix.
5. "Host platform" is only the platform layer. The computer on a link is "the computer".
6. Not used for new work: "digital twin", "lab", "emulator", "simulator" for the virtual radio, "snapshot", "monitor"
   for watching.
7. The UI simulator and practice mode keep their names for their own tools only.

## Consequences

- Specs, code and the browser page use the same words. The glossary check guards the data.
- "Controller" and "world controller" are one prefix apart. The architecture spec keeps "world controller" for the
  part that moves time, calls the client role with world rights a "world client", and calls the host process that
  holds them all the "conductor".

## Alternatives considered

- **Keep the research words** ("digital twin", "lab"). Rejected: a twin suggests a copy kept in step with one real
  unit, and "lab" names a bundle of harnesses, not a thing you run.
- **No glossary, define words per spec.** Rejected: the clashes above are already in the tree.
