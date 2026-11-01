# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Held keys and PTT, and their release before anyone else gets control.

A hold is a key or PTT that a controller pressed and has not let go. Example: a
debug link client sends "PTT down" to radio 1. Its front end calls
holds.get(world).down(1, "ptt", "PTT", principal="debug:c7"). If the lease is
then revoked, this part sends the release ("PTT up") as a stamped conductor
input, so it is recorded like every other input (ADR 0004 rule 2).

A release counts only when it is confirmed: a TRACE line from the same radio
and the same boot, at or after the release's stamp, shows the input task saw
the key up. A restart of the radio (a new boot) also clears it, because a new
process starts with every key up. Until then, pending(radio) is true and the
lease part admits no successor.

This part does not know the KEY payload format. The front end that owns a hold
kind registers two functions for it:
  release(radio, hold) -> (Kind, payload)   the input that lets go
  confirms(radio, hold, line) -> bool       line is one TRACE line as text

Test hook: drop_releases(n) makes the next n releases not be sent. Only tests
use it, to prove a successor waits for a restart.
"""

from dataclasses import dataclass

from ..channel import Kind
from ..world import WorldError
from . import audit

# Source id stamped on conductor releases. Scenario sources are radio numbers
# (1..n) and front ends use their own ids, so 0 marks "the conductor".
CONDUCTOR_SOURCE = 0


@dataclass
class Hold:
    radio: int
    kind: str
    name: str
    principal: str
    boot: int
    order: int
    release_time_us: int | None = None
    release_sequence: int | None = None
    dropped: bool = False


class Holds:
    def __init__(self, world):
        self.world = world
        self.audit = audit.get(world)
        self.kinds = {}
        self.open = []      # pressed, not yet released, in press order
        self.pending = []   # released (or dropped), waiting for a confirm or a restart
        self._order = 0
        self._drop = 0

    def register(self, kind, release, confirms):
        if not isinstance(kind, str) or not kind or kind in self.kinds:
            raise ValueError("HOLD_BAD_KIND")
        self.kinds[kind] = (release, confirms)

    def drop_releases(self, count=1):
        """Test hook: the next count releases are not sent (and stay pending)."""
        self._drop += count

    def down(self, radio, kind, name, principal):
        """A front end accepted a press. Call only after the lease check passed."""
        if kind not in self.kinds:
            raise WorldError("HOLD_UNKNOWN_KIND")
        if radio not in self.world.radios:
            raise WorldError("WORLD_RADIO_OFF")
        self._forget_old_boots(radio)
        for hold in self.open:
            if (hold.radio, hold.kind, hold.name) == (radio, kind, name):
                return hold
        self._order += 1
        hold = Hold(radio, kind, name, principal, self.world.boots[radio], self._order)
        self.open.append(hold)
        self.audit.emit("hold_down", principal, radio, "tracked", "OK", hold_kind=kind, hold=name)
        return hold

    def _forget_old_boots(self, radio):
        """Open holds from an earlier boot were cleared by that restart; a new press is a new hold."""
        for hold in [hold for hold in self.open if hold.radio == radio]:
            if hold.boot != self.world.boots.get(radio):
                self.open.remove(hold)
                self.audit.emit("hold_release", hold.principal, radio, "cleared", "HOLD_RADIO_RESTARTED",
                                hold_kind=hold.kind, hold=hold.name)

    def up(self, radio, kind, name):
        """The controller let go itself; the hold is no longer tracked."""
        for hold in list(self.open):
            if (hold.radio, hold.kind, hold.name) == (radio, kind, name):
                self.open.remove(hold)
                self.audit.emit("hold_up", hold.principal, radio, "untracked", "OK", hold_kind=kind, hold=name)

    def release(self, radio, reason):
        """Send a release for every open hold on radio, in press order. Returns the holds."""
        if radio in self.world.radios:
            self._forget_old_boots(radio)
        holds = [hold for hold in self.open if hold.radio == radio]
        for hold in holds:
            # Pending from here on: a release that fails below still blocks a successor.
            self.open.remove(hold)
            self.pending.append(hold)
            if radio not in self.world.radios:
                hold.dropped = True
                self.audit.emit("hold_release", hold.principal, radio, "waiting", "WORLD_RADIO_OFF",
                                hold_kind=hold.kind, hold=hold.name, cause=reason)
                continue
            if self._drop:
                self._drop -= 1
                hold.dropped = True
                self.audit.emit("hold_release", hold.principal, radio, "dropped", "HOLD_TEST_DROP",
                                hold_kind=hold.kind, hold=hold.name, cause=reason)
                continue
            make, _ = self.kinds[hold.kind]
            try:
                kind, payload = make(radio, hold)
                event = self.world.submit(radio, kind, payload, CONDUCTOR_SOURCE, internal=True)
            except (WorldError, ValueError, TypeError) as error:
                # Not sent: it stays pending, so only a restart lets a successor in.
                hold.dropped = True
                self.audit.emit("hold_release", hold.principal, radio, "failed", str(error)[:64] or "HOLD_FAILED",
                                hold_kind=hold.kind, hold=hold.name, cause=reason)
                continue
            hold.release_time_us = event.time_us
            hold.release_sequence = event.sequence
            self.audit.emit("hold_release", hold.principal, radio, "sent", "OK", hold_kind=hold.kind,
                            hold=hold.name, cause=reason, sequence=event.sequence, release_us=event.time_us)
            recorder = getattr(self.world, "recorder", None)
            if recorder is not None:
                recorder.conductor_event("hold_release", radio=radio, hold_kind=hold.kind, hold=hold.name,
                                         sequence=event.sequence, cause=reason)
        return holds

    def trace(self, radio, message):
        """world.on_outbox(TRACE): confirm pending releases the line shows."""
        waiting = [hold for hold in self.pending if hold.radio == radio and not hold.dropped
                   and hold.boot == self.world.boots.get(radio) and message.time_us >= hold.release_time_us]
        if not waiting:
            return
        line = message.payload.decode("utf-8", errors="replace")
        for hold in waiting:
            _, confirms = self.kinds[hold.kind]
            if confirms(radio, hold, line):
                self.pending.remove(hold)
                self.audit.emit("hold_confirmed", hold.principal, radio, "confirmed", "OK", hold_kind=hold.kind,
                                hold=hold.name, sequence=hold.release_sequence, seen_us=message.time_us)

    def unconfirmed(self, radio):
        """True while any release on radio waits; a new boot clears its holds."""
        for hold in [hold for hold in self.pending if hold.radio == radio]:
            if radio in self.world.radios and self.world.boots.get(radio) != hold.boot:
                self.pending.remove(hold)
                self.audit.emit("hold_confirmed", hold.principal, radio, "cleared", "HOLD_RADIO_RESTARTED",
                                hold_kind=hold.kind, hold=hold.name, boot=self.world.boots[radio])
        return any(hold.radio == radio for hold in self.pending)

    def tracked(self, radio):
        return [hold for hold in self.open if hold.radio == radio]


def get(world):
    if getattr(world, "holds", None) is None:
        world.holds = Holds(world)
        world.on_outbox(Kind.TRACE, world.holds.trace)
    return world.holds


def install(world):
    get(world)
