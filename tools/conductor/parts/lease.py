# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Leases: who may control a radio, and the separate maintenance capability (ADR 0004).

A lease is the permission to control one radio. One holder at a time, across the
debug link and the browser. Only a world client grants it, on purpose, over the
world API (lease.grant). Connecting never grants it, and there is no rank.

Each grant gets a new generation number for that radio. A front end keeps the
number it was given and passes it to check(). Example: hil:radio0 gets radio 1
at generation 3; the lease is revoked and granted again (generation 4). A late
frame from the old session still says 3, so check() refuses it.

Maintenance is a second permission on top of the lease, for dangerous debug
commands (REBOOT, READ_MEM, STORAGE, ...). It belongs to one lease generation and
ends with it. A browser principal never gets it.

Revoke, disconnect and a world pause first release every tracked hold (holds.py).
A successor is refused until each release is confirmed or the radio restarted.
Every grant, refusal, check and change writes an audit line (audit.py).

Principals are short names "<layer>:<id>", layer one of browser, debug, hil, script.

The grant secret. A principal and a generation are not proof: anyone on the world socket can read both with
lease.status. So each grant also makes a one-time grant secret, a random string. Only the caller of
lease.grant gets it, in the reply. Every place that uses the lease must present it: debuglink.open for a
controller session, lease.grant_maintenance, and check() before each command. Example: a world client
grants radio 1 to script:holder and gets {"generation": 3, "secret": "..."}. A second client copies
script:holder and 3 from lease.status and calls debuglink.open; it has no secret, so it is refused with
LEASE_BAD_SECRET, and that refusal is audited. The secret is registered with the audit's secret filter
(audit.register_secret), so the exact value is refused in audit.jsonl and events.jsonl. lease.status never
shows it. A part inside the conductor that serves a layer (the web gateway serves browser:) is told the
secret of each grant to that layer with on_grant(), because the browser never sees the world reply.
"""

from dataclasses import dataclass, field
import hmac
import re
import secrets

from ..world import WorldError
from . import audit, holds

PRINCIPAL = re.compile(r"(browser|debug|hil|script):[A-Za-z0-9_-]{1,16}")
# Debug link commands that always need maintenance (ADR 0004 rule 5), whatever the caller says.
MAINTENANCE_COMMANDS = frozenset(("REBOOT", "READ_MEM", "STORAGE", "CRASH", "WATCHDOG", "CODEPLUG", "STUB", "RX2"))


@dataclass
class Lease:
    principal: str
    generation: int
    maintenance: bool = False
    secret: str = field(default=None, repr=False)


def is_browser(principal):
    return principal.startswith("browser:")


class Leases:
    def __init__(self, world):
        self.world = world
        self.audit = audit.get(world)
        self.holds = holds.get(world)
        self.held = {}          # radio -> Lease
        self.generations = {}   # radio -> last generation handed out
        self._revoke_hooks = []
        self._grant_hooks = {}  # layer -> fn

    def on_grant(self, layer, fn):
        """fn(radio, principal, generation, secret) for each grant to a principal of this layer.

        For the part that serves that layer inside the conductor (the web gateway for "browser"). One part
        per layer, so no other part learns the secret.
        """
        if layer in self._grant_hooks:
            raise ValueError("LEASE_DUPLICATE_GRANT_HOOK")
        self._grant_hooks[layer] = fn

    def on_revoke(self, fn):
        """fn(radio, principal, generation, reason): a front end closes or demotes that session."""
        self._revoke_hooks.append(fn)

    def _refuse(self, kind, principal, radio, reason, **fields):
        self.audit.emit(kind, principal if isinstance(principal, str) else None,
                        radio if type(radio) is int else None, "refused", reason, **fields)
        raise WorldError(reason)

    def _principal(self, kind, principal, radio):
        if not isinstance(principal, str) or not PRINCIPAL.fullmatch(principal):
            self._refuse(kind, None, radio, "LEASE_BAD_PRINCIPAL")
        return principal

    def _radio(self, kind, principal, radio):
        if type(radio) is not int or radio not in self.world.boots:
            self._refuse(kind, principal, radio, "WORLD_UNKNOWN_RADIO")

    def grant(self, radio, principal):
        kind = "lease_grant"
        principal = self._principal(kind, principal, radio)
        self._radio(kind, principal, radio)
        if self.world.state != "Running":
            self._refuse(kind, principal, radio, "LEASE_WORLD_NOT_RUNNING", state=self.world.state)
        if radio not in self.world.radios:
            self._refuse(kind, principal, radio, "WORLD_RADIO_OFF")
        if radio in self.held:
            self._refuse(kind, principal, radio, "LEASE_HELD", holder=self.held[radio].principal)
        if self.holds.unconfirmed(radio):
            self._refuse(kind, principal, radio, "LEASE_RELEASE_PENDING")
        generation = self.generations.get(radio, 0) + 1
        secret = secrets.token_urlsafe(32)
        self.audit.register_secret(secret)      # before any line that could carry it
        # Audit first: if the line cannot be written, no authority changes.
        self.audit.emit(kind, principal, radio, "accepted", "OK", lease_generation=generation,
                        layer=principal.split(":")[0], by="world")
        self.generations[radio] = generation
        self.held[radio] = Lease(principal, generation, secret=secret)
        hook = self._grant_hooks.get(principal.split(":")[0])
        if hook is not None:
            hook(radio, principal, generation, secret)
        return {"radio": radio, "principal": principal, "generation": generation, "secret": secret}

    def grant_maintenance(self, radio, principal, generation, secret=None):
        kind = "maintenance_grant"
        principal = self._principal(kind, principal, radio)
        self._radio(kind, principal, radio)
        if is_browser(principal):
            self._refuse(kind, principal, radio, "LEASE_BROWSER_NO_MAINTENANCE", lease_generation=generation)
        self.holder(kind, principal, radio, generation, secret)
        self.audit.emit(kind, principal, radio, "accepted", "OK", lease_generation=generation,
                        capability="maintenance", by="world")
        self.held[radio].maintenance = True
        return {"radio": radio, "principal": principal, "generation": generation, "maintenance": True}

    def holder(self, kind, principal, radio, generation, secret, **fields):
        """The current lease if principal, generation and grant secret all match; else refused and audited.

        The secret is compared last, so a caller that copied the public fields from lease.status is refused
        with LEASE_BAD_SECRET. The presented value is never written anywhere.
        """
        lease = self.held.get(radio)
        if lease is None or lease.principal != principal:
            self._refuse(kind, principal, radio, "LEASE_NOT_HOLDER", lease_generation=generation, **fields)
        if type(generation) is not int or generation != lease.generation:
            self._refuse(kind, principal, radio, "LEASE_STALE_GENERATION", lease_generation=generation,
                         current_generation=lease.generation, **fields)
        # compare_digest takes ASCII text only; anything else is simply wrong, not an error.
        if not (isinstance(secret, str) and secret.isascii() and hmac.compare_digest(secret, lease.secret)):
            self._refuse(kind, principal, radio, "LEASE_BAD_SECRET", lease_generation=generation, **fields)
        return lease

    def check(self, radio, principal, needs_maintenance=False, *, generation, secret, command=None):
        """For front ends, before forwarding one control command. Raises WorldError when refused."""
        kind = "control"
        if isinstance(command, str) and command.upper() in MAINTENANCE_COMMANDS:
            needs_maintenance = True
        fields = {"command": command, "needs_maintenance": bool(needs_maintenance)}
        principal = self._principal(kind, principal, radio)
        self._radio(kind, principal, radio)
        if needs_maintenance and is_browser(principal):
            self._refuse(kind, principal, radio, "LEASE_BROWSER_NO_MAINTENANCE", lease_generation=generation,
                         **fields)
        lease = self.holder(kind, principal, radio, generation, secret, **fields)
        if self.world.state != "Running":
            self._refuse(kind, principal, radio, "LEASE_WORLD_NOT_RUNNING", lease_generation=generation,
                         state=self.world.state, **fields)
        if needs_maintenance and not lease.maintenance:
            self._refuse(kind, principal, radio, "LEASE_NO_MAINTENANCE", lease_generation=generation, **fields)
        self.audit.emit(kind, principal, radio, "accepted", "OK", lease_generation=generation, **fields)
        return True

    def revoke(self, radio, reason="LEASE_REVOKED", principal=None):
        """Take the lease away. Holds are released first; successors wait for confirmation."""
        kind = "lease_revoke"
        self._radio(kind, principal, radio)
        lease = self.held.get(radio)
        if lease is None:
            self._refuse(kind, principal, radio, "LEASE_NOT_HELD", cause=reason)
        if principal is not None and principal != lease.principal:
            self._refuse(kind, principal, radio, "LEASE_NOT_HOLDER", cause=reason)
        self.audit.emit(kind, lease.principal, radio, "accepted", reason, lease_generation=lease.generation,
                        maintenance=lease.maintenance)
        del self.held[radio]
        if radio in self.world.radios:
            self.holds.release(radio, reason)
        for fn in self._revoke_hooks:
            fn(radio, lease.principal, lease.generation, reason)
        return {"radio": radio, "generation": lease.generation, "pending": self.holds.unconfirmed(radio)}

    def disconnected(self, principal):
        """A front end saw its session close: every lease it held is revoked."""
        for radio in sorted(radio for radio, lease in self.held.items() if lease.principal == principal):
            self.revoke(radio, "LEASE_DISCONNECT")

    def revoke_all(self, reason):
        for radio in sorted(self.held):
            self.revoke(radio, reason)

    def pausing(self):
        """world.on_pausing: revoke every lease, release holds, wait until each is confirmed."""
        self.revoke_all("WORLD_PAUSE")
        return lambda: not any(self.holds.unconfirmed(radio) for radio in self.world.boots)

    def status(self):
        return {str(radio): {"principal": lease.principal, "generation": lease.generation,
                             "maintenance": lease.maintenance} for radio, lease in sorted(self.held.items())}


def get(world):
    if getattr(world, "leases", None) is None:
        leases = world.leases = Leases(world)
        world.on_pausing(leases.pausing)
        api = world.api
        # .get, not [key]: a missing field is refused inside the lease code, so it is audited.
        api.register("lease.grant", lambda request: leases.grant(request.get("radio"), request.get("principal")))
        api.register("lease.revoke", lambda request: leases.revoke(request.get("radio")))
        api.register("lease.grant_maintenance", lambda request: leases.grant_maintenance(
            request.get("radio"), request.get("principal"), request.get("generation"), request.get("secret")))
        api.register("lease.status", lambda request: leases.status())
    return world.leases


def install(world):
    get(world)
