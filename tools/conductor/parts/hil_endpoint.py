# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The HIL endpoint: where an unmodified hardware-in-the-loop test connects (, ADR 0004).

A HIL test takes one string, --radio-port. This part makes that string point at a Unix socket the
conductor owns, and wires the socket to the debug link multiplexer (dlmux.py).

Words. An endpoint is the socket file made for one run. A peer check asks the kernel which user is
on the other end of a connection (SO_PEERCRED). A lease is the permission to steer one radio.

One run, in order (world API commands, sent by run_hil.py):
  hil.create {radio}       makes the socket, answers {endpoint, url, principal}. No authority yet.
  hil.grant {endpoint}     gives the radio's controller lease to the endpoint (lease.grant, audited).
                           Add "maintenance": true only when the run needs it. It is audited too.
                           The lease's grant secret stays inside this part: the endpoint presents it
                           when its connection opens a controller session, and the reply leaves it out.
  ... the test connects and runs; pytest starts only after the grant answered ...
  hil.release {endpoint}   revokes the lease (holds are released first), closes and removes the socket.
Connecting never grants or changes a lease. A connection before the grant is refused and audited.

The socket is $XDG_RUNTIME_DIR/rfvr/<6 hex>.sock: folder 0700 and owned by us, socket 0600 from the
start (umask 0177), a pathname socket of at most 100 bytes. The peer's UID is checked before one byte is
read, and its PID, UID and GID go in the audit. One connection at a time: a second is refused and audited.

Each connection is one multiplexer session under the lease generation (its own frame parser). When it
closes, its half frame and queued requests are dropped, its keys and PTT are released, and the next
connection waits until the releases are confirmed. The lease stays with the endpoint, so a test may close
and reopen the port. The lease ends on hil.release, world end, a pause, a revoke by anyone, or when no
world client is left.

The firmware has one reply path per mode. This endpoint works in serial mode (the default world, with
no web gateway): in network mode the debug link replies go as UDP, not on the UART.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets
import selectors
import socket
import struct

from ..world import WorldError
from ..worldapi import private_directory
from . import audit, dlmux, holds, lease

PATH_MAX = 100          # sockaddr_un holds 108 bytes
READ_MAX = 4096


def peer_credentials(connection):
    """(pid, uid, gid) of the process on the other end, from the kernel."""
    return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))


@dataclass
class Endpoint:
    id: str
    radio: int
    principal: str
    path: Path
    listener: object
    generation: int | None = None
    secret: str | None = field(default=None, repr=False)
    connection: object = None
    session: object = None
    connections: int = 0
    pending: bytearray = None

    def __post_init__(self):
        self.pending = bytearray()


class Endpoints:
    def __init__(self, world):
        self.world = world
        self.audit = audit.get(world)
        self.holds = holds.get(world)
        self.leases = lease.get(world)
        self.mux = dlmux.get(world)
        self.endpoints = {}
        self.creator_seen = False
        self.leases.on_revoke(self._revoked)
        world.on_end(self.close)

    # --- world API ---

    def _emit(self, ep, result, reason, kind="hil_endpoint", **fields):
        self.audit.emit(kind, ep.principal, ep.radio, result, reason, endpoint=ep.id,
                        lease_generation=ep.generation, **fields)

    def _find(self, request):
        ep = self.endpoints.get(request.get("endpoint"))
        if ep is None:
            raise WorldError("HIL_UNKNOWN_ENDPOINT")
        return ep

    def create(self, request):
        radio = request.get("radio")
        if type(radio) is not int or radio not in self.world.boots:
            raise WorldError("WORLD_UNKNOWN_RADIO")
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime or not os.path.isabs(runtime):
            raise WorldError("HIL_NO_RUNTIME_DIRECTORY")
        folder = Path(runtime) / "rfvr"
        try:
            private_directory(Path(runtime))
            private_directory(folder)
        except (OSError, ValueError) as error:
            raise WorldError("HIL_UNSAFE_RUNTIME_DIRECTORY") from error
        short = secrets.token_hex(3)
        path = folder / f"{short}.sock"
        if len(os.fsencode(path)) > PATH_MAX:
            raise WorldError("HIL_PATH_TOO_LONG")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)    # not inheritable (PEP 446)
        previous = os.umask(0o177)
        try:
            listener.bind(str(path))
        except OSError as error:
            listener.close()
            raise WorldError("HIL_BIND_FAILED") from error
        finally:
            os.umask(previous)
        os.chmod(path, 0o600)
        listener.listen(2)
        listener.setblocking(False)
        ep = Endpoint(short, radio, f"hil:{short}", path, listener)
        self.endpoints[short] = ep
        self._wake(listener)
        self._emit(ep, "created", "OK", uid=os.getuid())
        return {"endpoint": short, "principal": ep.principal, "radio": radio, "url": f"unix://{path}"}

    def grant(self, request):
        ep = self._find(request)
        if ep.generation is not None:
            raise WorldError("HIL_ALREADY_GRANTED")
        granted = self.leases.grant(ep.radio, ep.principal)        # audited, or refused with the reason
        ep.generation, ep.secret = granted["generation"], granted["secret"]
        if request.get("maintenance") is True:
            self.leases.grant_maintenance(ep.radio, ep.principal, ep.generation, ep.secret)
        self._emit(ep, "granted", "OK", maintenance=request.get("maintenance") is True)
        public = {key: value for key, value in granted.items() if key != "secret"}
        return {**public, "endpoint": ep.id}

    def release(self, request):
        ep = self._find(request)
        pending = False
        held = self.leases.held.get(ep.radio)
        if held is not None and held.principal == ep.principal:
            pending = self.leases.revoke(ep.radio, "HIL_RUN_END", principal=ep.principal)["pending"]
        self._teardown(ep, "HIL_RUN_END")
        return {"endpoint": ep.id, "pending": pending}

    # --- teardown ---

    def _revoked(self, radio, principal, generation, reason):
        for ep in list(self.endpoints.values()):
            if (ep.radio, ep.principal, ep.generation) == (radio, principal, generation):
                self._teardown(ep, reason)

    def _wake(self, sock, add=True):
        """Let the world API's own wait end when this socket has data."""
        selector = self.world.api.selector
        if selector is None:
            return
        try:
            if add:
                selector.register(sock, selectors.EVENT_READ, "hil")
            else:
                selector.unregister(sock)
        except (KeyError, ValueError, OSError):
            pass

    def _detach(self, ep, reason):
        """The connection ended. The lease stays; the session, its queue and its holds go."""
        session, connection = ep.session, ep.connection
        ep.session = ep.connection = None
        ep.pending.clear()
        if connection is not None:
            self._wake(connection, add=False)
            connection.close()
        if session is not None and not session.closed:
            session.closed = True
            self.mux.sessions.pop(session.id, None)
            link = self.mux.links.get(ep.radio)
            if link is not None:
                link.queue = type(link.queue)(request for request in link.queue if request.owner is not session)
            self.audit.emit("debuglink_close", ep.principal, ep.radio, "closed", reason, role="controller",
                            session=session.id)
            if ep.radio in self.world.radios:
                self.holds.release(ep.radio, reason)
        self._emit(ep, "disconnected", reason, kind="hil_disconnect", connection=ep.connections)

    def _teardown(self, ep, reason):
        if self.endpoints.pop(ep.id, None) is None:
            return
        if ep.connection is not None:
            self._detach(ep, reason)
        self._wake(ep.listener, add=False)
        ep.listener.close()
        ep.path.unlink(missing_ok=True)
        self._emit(ep, "closed", reason)

    def close(self):
        """World end: the lease ends with the world, then every socket goes."""
        for ep in list(self.endpoints.values()):
            held = self.leases.held.get(ep.radio)
            if held is not None and held.principal == ep.principal:
                try:
                    self.leases.revoke(ep.radio, "WORLD_END", principal=ep.principal)
                except WorldError:
                    pass
            self._teardown(ep, "WORLD_END")

    # --- connections ---

    def _accept(self, ep):
        while True:
            try:
                connection, _ = ep.listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            try:
                pid, uid, gid = peer_credentials(connection)
            except OSError:
                connection.close()
                self._emit(ep, "refused", "HIL_PEER_UNKNOWN", kind="hil_connect")
                continue
            who = {"peer_pid": pid, "peer_uid": uid, "peer_gid": gid}
            if uid != os.getuid():
                connection.close()
                self._emit(ep, "refused", "HIL_OTHER_USER", kind="hil_connect", **who)
            elif ep.connection is not None:
                connection.close()
                self._emit(ep, "refused", "HIL_SECOND_CONNECTION", kind="hil_connect", **who)
            elif ep.generation is None:
                connection.close()
                self._emit(ep, "refused", "HIL_NO_LEASE", kind="hil_connect", **who)
            elif self.holds.unconfirmed(ep.radio):
                connection.close()
                self._emit(ep, "refused", "LEASE_RELEASE_PENDING", kind="hil_connect", **who)
            else:
                try:
                    session = self.mux.connect(ep.radio, ep.principal, "controller", ep.generation, ep.secret)
                except WorldError as error:
                    connection.close()
                    self._emit(ep, "refused", str(error), kind="hil_connect", **who)
                    continue
                connection.setblocking(False)
                ep.connection, ep.session = connection, session
                ep.connections += 1
                self._wake(connection)
                self._emit(ep, "accepted", "OK", kind="hil_connect", connection=ep.connections,
                           session=session.id, **who)

    def _serve(self, ep):
        connection, session = ep.connection, ep.session
        if session.closed:                  # the multiplexer ended it (slow client, revoke)
            self._detach(ep, "DLMUX_SESSION_CLOSED")
            return
        try:
            data = connection.recv(READ_MAX)
        except (BlockingIOError, InterruptedError):
            data = None
        except OSError:
            self._detach(ep, "HIL_SOCKET_ERROR")
            return
        if data == b"":
            self._detach(ep, "HIL_CLIENT_CLOSED")
            return
        if data:
            self.mux.feed(session, data)
        if session.closed:
            self._detach(ep, "DLMUX_SESSION_CLOSED")
            return
        if session.outgoing:
            try:
                sent = connection.send(session.outgoing)
                del session.outgoing[:sent]
            except (BlockingIOError, InterruptedError):
                pass
            except OSError:
                self._detach(ep, "HIL_SOCKET_ERROR")

    def service(self):
        """Called from the world API's poll."""
        for ep in list(self.endpoints.values()):
            self._accept(ep)
            if ep.connection is not None:
                self._serve(ep)
        # Every world client is gone: the creator is too. Nobody is left to end the run.
        if self.endpoints and not self.world.api.clients:
            if self.creator_seen:
                for ep in list(self.endpoints.values()):
                    held = self.leases.held.get(ep.radio)
                    if held is not None and held.principal == ep.principal:
                        self.leases.revoke(ep.radio, "HIL_CLIENT_GONE", principal=ep.principal)
                    self._teardown(ep, "HIL_CLIENT_GONE")
        elif self.world.api.clients:
            self.creator_seen = True


def get(world):
    if getattr(world, "hil_endpoints", None) is None:
        endpoints = world.hil_endpoints = Endpoints(world)
        api = world.api
        api.register("hil.create", endpoints.create)
        api.register("hil.grant", endpoints.grant)
        api.register("hil.release", endpoints.release)
        poll = api.poll

        def chained(timeout=0):
            poll(timeout)
            endpoints.service()

        api.poll = chained
    return world.hil_endpoints


def install(world):
    get(world)
