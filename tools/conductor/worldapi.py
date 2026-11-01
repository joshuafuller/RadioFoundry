# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Versioned JSON lines on a private, same-UID Unix socket. No radio authority."""

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import selectors
import socket
import stat
import struct
import time

from .parts import audit

VERSION = 1
LIMIT = 65536
# World control commands a client may name an actor for (tools/console.py --control). The actor is only a label for
# the audit line: the world socket itself is the world capability (ADR 0004 rules 7 and 8, same UID trusted).
WORLD_CONTROL = ("run", "pause", "step", "set_ratio", "fault")
ACTOR = re.compile(r"console:s[0-9]{1,6}")
ARGUMENTS = ("ratio", "until_us", "kind", "radio", "target", "rate_ppm", "time_us")   # copied into the audit line
REFUSAL = re.compile(r"OK|[A-Z][A-Z0-9_]{2,47}")
# Pushed events. Each client gets every event kind except HIGH_RATE, unless it sends
# {"version": 1, "command": "subscribe", "events": [...]} to pick its own kinds ("*" means every kind).
# Why: udp_delivery carries each Ethernet frame a radio sends, about 845 KB/s at 1x with 4 idle radios
# (parts/udp.py). Sent to every client, it filled the queue of any client that reads slowly.
HIGH_RATE = frozenset({"udp_delivery"})
EVENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,47}")
SUBSCRIBE_MAX = 64
# When a pushed event does not fit in a client's queue it is skipped, not the client: the client later gets one
# {"event": "events_skipped", "count": N} line. Only replies can overflow a client (it sends but never reads).
SKIPPED = "events_skipped"
# Pushed events may fill only this much of the queue, so a reply always has room: a client that sat idle while
# events piled up still gets the answer to its next call (an observe.events page is at most about 33 KiB).
EVENT_LIMIT = LIMIT // 4


def peer_uid(connection):
    return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


@dataclass
class Client:
    incoming: bytearray = field(default_factory=bytearray, repr=False)  # may hold a grant secret in flight
    outgoing: bytearray = field(default_factory=bytearray, repr=False)
    events: frozenset = None    # the kinds this client asked for, "*" for all; None is the default (not HIGH_RATE)
    skipped: int = 0            # pushed events left out since the last events_skipped line


def private_directory(path):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("WORLD_UNSAFE_RUNTIME_DIRECTORY")


class WorldAPI:
    def __init__(self, world, *, uid_reader=peer_uid):
        self.world = world
        self.uid_reader = uid_reader
        self.handlers = {}
        self.clients = {}
        self.listener = None
        self.selector = None
        self.path = None
        self.directory = None
        self.read_only = set()      # commands that only read, so a busy world still answers them
        self.register("status", lambda request: self.status())
        self.register("run", lambda request: world.run(request.get("ratio", 1), until=request.get("until_us")))
        self.register("pause", lambda request: world.pause())
        self.register("step", lambda request: world.step(request.get("until_us")))
        self.register("set_ratio", lambda request: world.set_ratio(request["ratio"]))
        self.register("start", lambda request: world.start_radio(request["radio"]))
        self.register("power_off", lambda request: world.power_off(request["radio"]))
        self.register("restart", lambda request: world.restart(request["radio"]))
        self.register("reboot", lambda request: world.reboot(request["radio"]))
        self.register("end", lambda request: world.end())
        self.register("world_control.audit", self._console_audit, read_only=True)

    @property
    def live(self):
        return bool(self.clients)

    def register(self, name, fn, *, read_only=False):
        """read_only=True only for a handler that changes nothing: it is then answered while Pausing or Stepping,
        with no WORLD_BUSY refusal and so no audit line. Example: observe.status (parts/observe_api.py)."""
        if name in self.handlers:
            raise ValueError("WORLD_DUPLICATE_HANDLER")
        self.handlers[name] = fn
        if read_only:
            self.read_only.add(name)

    def _console_audit(self, request):
        """Audit one console decision that never reached a world handler: a request refused at the console's own
        gate, or the control capability granted to a page session. It changes nothing but the audit stream.
        Example: {"command": "world_control.audit", "actor": "console:s2", "action": "pause",
        "result": "refused", "reason": "CONSOLE_BAD_ORIGIN"}."""
        from .world import WorldError
        actor, action, reason = request.get("actor"), request.get("action"), request.get("reason")
        result, suppressed = request.get("result"), request.get("suppressed", 0)
        if not (isinstance(actor, str) and ACTOR.fullmatch(actor) and isinstance(reason, str)
                and REFUSAL.fullmatch(reason) and action in (*WORLD_CONTROL, "resume", "speed", "unlock", "other")
                and (result == "refused" or result == "granted" and action == "unlock" and reason == "OK")
                and type(suppressed) is int and 0 <= suppressed < (1 << 31)):
            raise WorldError("WORLD_BAD_REQUEST")
        audit.emit(self.world, "world_control", actor, None, result, reason, layer="console", action=action,
                   **({"suppressed": suppressed} if suppressed else {}))
        return {"audited": True}

    def _actor(self, request, name):
        """The audit label of a world control command, or None. A bad label is refused, never written."""
        if name not in WORLD_CONTROL or "actor" not in request:
            return None
        actor = request["actor"]
        if not isinstance(actor, str) or not ACTOR.fullmatch(actor):
            from .world import WorldError
            raise WorldError("WORLD_BAD_ACTOR")
        return actor

    def status(self):
        return {"state": self.world.state, "time_us": self.world.now, "reason": self.world.reason,
                "radios": sorted(self.world.radios)}

    def dispatch(self, request):
        from .world import WorldError
        response = {"version": VERSION}
        actor = None
        try:
            if not isinstance(request, dict):
                raise WorldError("WORLD_BAD_REQUEST")
            response["id"] = request.get("id")
            if type(request.get("version")) is not int or request["version"] != VERSION:
                raise WorldError("WORLD_BAD_VERSION")
            name = request.get("command")
            if not isinstance(name, str) or name not in self.handlers:
                raise WorldError("WORLD_UNKNOWN_COMMAND")
            actor = self._actor(request, name)
            recovery = self.world.state == "Pausing" and name in ("end", "restart", "power_off")
            if self.world.state in ("Pausing", "Stepping") and name != "status" and name not in self.read_only \
                    and not recovery and actor is None:
                principal, radio = request.get("principal"), request.get("radio")
                audit.emit(self.world, "world_command", principal if isinstance(principal, str) else None,
                           radio if type(radio) is int else None, "refused", "WORLD_BUSY",
                           command=name, state=self.world.state)
                raise WorldError("WORLD_BUSY")
            if actor is not None and self.world.state in ("Pausing", "Stepping"):
                raise WorldError("WORLD_BUSY")      # audited once below, as world_control
            response["result"] = self.handlers[name](request)
            response["ok"] = True
        except WorldError as error:
            response.update(ok=False, error=str(error))
        except (KeyError, TypeError, ValueError) as error:
            reason = str(error) if str(error).startswith("WORLD_") else "WORLD_BAD_REQUEST"
            response.update(ok=False, error=reason)
        if actor is not None:
            # One line per attempt from a named console, accepted or refused (ADR 0004 rule 12).
            audit.emit(self.world, "world_control", actor, None, "accepted" if response.get("ok") else "refused",
                       "OK" if response.get("ok") else response["error"], layer="console", command=name,
                       state=self.world.state, args={key: request[key] for key in ARGUMENTS if key in request})
        return response

    def bind(self, name, runtime=None):
        if self.listener is not None:
            raise ValueError("WORLD_ALREADY_BOUND")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("WORLD_BAD_NAME")
        runtime = runtime or os.environ.get("XDG_RUNTIME_DIR")
        if not runtime:
            raise ValueError("WORLD_NO_RUNTIME_DIRECTORY")
        root = Path(runtime)
        private_directory(root)
        parent = root / "rfoundry-vr"
        private_directory(parent)
        directory = parent / name
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError as error:
            raise ValueError("WORLD_ALREADY_EXISTS") from error
        self.directory = directory
        path = directory / "world.sock"
        try:
            fd = os.open(directory / "config.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(asdict(self.world.config), output, sort_keys=True,
                          default=lambda value: value.hex() if isinstance(value, bytes) else str(value))
                output.write("\n")
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            previous = os.umask(0o177)
            try:
                self.listener.bind(str(path))
            finally:
                os.umask(previous)
            self.path = path
            os.chmod(path, 0o600)
            self.listener.listen()
            self.listener.setblocking(False)
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.listener, selectors.EVENT_READ)
        except BaseException:
            self.close()
            raise
        return path

    def _drop(self, connection):
        self.clients.pop(connection, None)
        if self.selector is not None:
            self.selector.unregister(connection)
        connection.close()

    @staticmethod
    def _line(message):
        return json.dumps({**message, "version": VERSION}, separators=(",", ":"), allow_nan=False).encode() + b"\n"

    def _queue(self, connection, message, *, pushed=False):
        """Queue one line. A reply that does not fit drops the client; a pushed event is skipped and counted."""
        client = self.clients[connection]
        data = self._line(message)
        limit = EVENT_LIMIT if pushed else LIMIT
        if client.skipped and len(client.outgoing) + len(data) + 64 <= limit:
            client.outgoing.extend(self._line({"event": SKIPPED, "count": client.skipped}))
            client.skipped = 0
        if len(client.outgoing) + len(data) > limit:
            if pushed:
                client.skipped += 1
                return
            self._drop(connection)
            return
        client.outgoing.extend(data)
        self.selector.modify(connection, selectors.EVENT_READ | selectors.EVENT_WRITE)

    @staticmethod
    def wants(client, name):
        if client.events is None:
            return name not in HIGH_RATE
        return "*" in client.events or name in client.events

    def notify(self, event):
        name = event.get("event")
        for connection, client in list(self.clients.items()):
            if connection in self.clients and self.wants(client, name):
                self._queue(connection, event, pushed=True)

    def _subscribe(self, client, request):
        """Answer one subscribe request for this connection only. It changes no world state, so it is answered in
        any world state and writes no audit line. Example: {"version": 1, "id": 3, "command": "subscribe",
        "events": []} stops every pushed event; replies still come."""
        response = {"id": request.get("id")}
        events = request.get("events")
        if type(request.get("version")) is not int or request["version"] != VERSION:
            return {**response, "ok": False, "error": "WORLD_BAD_VERSION"}
        if not (isinstance(events, list) and len(events) <= SUBSCRIBE_MAX and all(
                isinstance(name, str) and (name == "*" or EVENT_NAME.fullmatch(name)) for name in events)):
            return {**response, "ok": False, "error": "WORLD_BAD_SUBSCRIBE"}
        client.events = frozenset(events)
        return {**response, "ok": True, "result": {"events": sorted(client.events)}}

    def poll(self, timeout=0):
        if self.selector is None:
            if timeout:
                time.sleep(timeout)
            return
        for key, mask in self.selector.select(timeout):
            connection = key.fileobj
            if connection is self.listener:
                connection, _ = self.listener.accept()
                try:
                    if self.uid_reader(connection) != os.getuid():
                        connection.close()
                        continue
                except OSError:
                    connection.close()
                    continue
                connection.setblocking(False)
                self.clients[connection] = Client()
                self.selector.register(connection, selectors.EVENT_READ)
                self._queue(connection, {"event": "state", **self.status()}, pushed=True)
                continue
            client = self.clients.get(connection)
            if client is None:
                continue
            try:
                if mask & selectors.EVENT_READ:
                    data = connection.recv(LIMIT + 1)
                    if not data or len(client.incoming) + len(data) > LIMIT:
                        self._drop(connection)
                        continue
                    client.incoming.extend(data)
                    while b"\n" in client.incoming and connection in self.clients:
                        line, _, rest = client.incoming.partition(b"\n")
                        client.incoming[:] = rest
                        try:
                            request = json.loads(line, parse_constant=lambda value: None)
                        except (ValueError, UnicodeError):
                            response = {"ok": False, "error": "WORLD_BAD_JSON"}
                        else:
                            if isinstance(request, dict) and request.get("command") == "subscribe":
                                response = {"version": VERSION, **self._subscribe(client, request)}
                            else:
                                response = self.dispatch(request)
                        if connection in self.clients:
                            self._queue(connection, response)
                if mask & selectors.EVENT_WRITE and connection in self.clients:
                    sent = connection.send(client.outgoing)
                    del client.outgoing[:sent]
                    if client.skipped and len(client.outgoing) + 64 <= EVENT_LIMIT:
                        client.outgoing.extend(self._line({"event": SKIPPED, "count": client.skipped}))
                        client.skipped = 0
                    if not client.outgoing:
                        self.selector.modify(connection, selectors.EVENT_READ)
            except BlockingIOError:
                continue
            except (ConnectionError, OSError):
                self._drop(connection)

    def close(self):
        for connection in list(self.clients):
            self._drop(connection)
        if self.selector is not None:
            self.selector.close()
            self.selector = None
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.path is not None:
            self.path.unlink(missing_ok=True)
            self.path = None
