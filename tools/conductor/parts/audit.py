# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The audit stream: one line for every control attempt and lease change.

Each event says who, which radio, what and the result,
and no token ever reaches the file. A token is a secret like a session token or
the one-time browser token. This part refuses such fields by name and by value.

Use: audit.get(world).emit("lease_acquire", "uid:1000", 1, "accepted", "OK").
Parts that mint a secret call audit.get(world).register_secret(value), so that
the exact value is also refused anywhere in events.jsonl.
"""

import json
import os
import re
import time

REFUSED = "[refused]"
# Names only. "capability" is not here: a lease audit names its capability kind.
_NAME = re.compile(r"token|secret|cookie|passw(or)?d|authorization|bearer|credential|api_?key|private_?key",
                   re.I)
_RUN = re.compile(r"[A-Za-z0-9_+/=-]{24,}")
_FIELDS = ("kind", "principal", "radio", "result", "reason")


def _token_like(text):
    # A long run of key characters holding a letter and a digit, like 'k3Jx...'.
    # Names such as WORLD_UNKNOWN_COMMAND have no digit and pass.
    return any(re.search(r"[A-Za-z]", run) and re.search(r"[0-9]", run) for run in _RUN.findall(text))


class Audit:
    def __init__(self, world):
        self.world = world
        self.secrets = set()
        self.sinks = []
        self.lines = 0
        self._fd = None

    def register_secret(self, value):
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        if value:
            self.secrets.add(value)

    def add_sink(self, fn):
        """fn(line dict) gets each clean audit line; the recorder uses this."""
        self.sinks.append(fn)

    def clean(self, value, refused, path):
        """Return value with every token-like part replaced; add field paths to refused."""
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                name = str(key)
                if _NAME.search(name):
                    out[name] = REFUSED
                    refused.append(f"{path}{name}")
                else:
                    out[name] = self.clean(item, refused, f"{path}{name}.")
            return out
        if isinstance(value, (list, tuple)):
            return [self.clean(item, refused, f"{path}{index}.") for index, item in enumerate(value)]
        if isinstance(value, (bytes, bytearray)):
            refused.append(path.rstrip("."))
            return REFUSED
        if isinstance(value, str):
            if any(secret in value for secret in self.secrets) or _token_like(value):
                refused.append(path.rstrip("."))
                return REFUSED
            return value
        if value is None or isinstance(value, (bool, int, float)):
            return value
        refused.append(path.rstrip("."))
        return REFUSED

    def emit(self, kind, principal, radio, result, reason, **fields):
        """Write one audit line. Extra fields: layer, role, lease_generation, command, status."""
        refused = []
        line = self.clean({"kind": kind, "principal": principal, "radio": radio, "result": result,
                           "reason": reason, **fields}, refused, "")
        line.update(type="audit", host_ns=time.monotonic_ns(), time_us=self.world.now)
        if refused:
            line["refused"] = sorted(refused)
        directory = self.world.api.directory
        if directory is not None:
            self._write(directory, line)
        for sink in self.sinks:
            sink(line)
        self.lines += 1
        return line

    def _write(self, directory, line):
        if self._fd is None:
            self._fd = os.open(directory / "audit.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(self._fd, 0o600)
        os.write(self._fd, json.dumps(line, sort_keys=True, separators=(",", ":"),
                                      allow_nan=False).encode() + b"\n")

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def get(world):
    """The world's audit, made once. The recorder and other parts share it."""
    if getattr(world, "audit", None) is None:
        world.audit = Audit(world)
        world.on_end(world.audit.close)
    return world.audit


def emit(world, kind, principal, radio, result, reason, **fields):
    return get(world).emit(kind, principal, radio, result, reason, **fields)


def install(world):
    get(world)
