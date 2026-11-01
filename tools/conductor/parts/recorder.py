# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The live record: events.jsonl, one line per accepted input and per conductor event.

It starts with the world (the folder is made by world.api.bind) and is 0600. This is
only the live file. Schema v2 resolution, replay and compare come later (ADR 0008).

An input line holds the stamp (virtual us, round, class, sequence, source) and the
payload as hex. Debug link and web control bytes stay bytes. They are never also
written as key edges (ADR 0006), because one accepted Input makes one line.

recording hook: recorder.get(world).add_filter(fn). Each line (a dict) goes through
every filter before it is written. fn returns the line, or raises StopRecording(reason)
to end the recording; the file then ends with a recording_stopped line.
"""

import json
import os

from ..channel import Kind
from . import audit

# World notices which are conductor events. Others stay live-only.
CONDUCTOR_EVENTS = ("radio_started", "radio_off", "state")


class StopRecording(Exception):
    """A filter raises this to end the recording with a named reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Recorder:
    def __init__(self, world):
        self.world = world
        self.audit = audit.get(world)
        self.filters = []
        self.pending = []
        self.stopped = None
        self.lines = 0
        self._fd = None
        self.audit.add_sink(self.write)

    def add_filter(self, fn):
        self.filters.append(fn)

    def input(self, event):
        payload = event.payload
        if any(secret.encode("latin-1", "ignore") in payload for secret in self.audit.secrets):
            self.stop("RECORD_SECRET_IN_INPUT")
            return
        self.write({"type": "input", "time_us": event.time_us, "round": event.round, "class": event.cls,
                    "sequence": event.sequence, "source": event.source, "radio": event.radio,
                    "kind": Kind(event.kind).name, "payload_hex": payload.hex()})

    def conductor_event(self, name, **fields):
        """A conductor event: release, restart, cache read. Secrets are refused in it."""
        refused = []
        line = self.audit.clean({"event": name, **fields}, refused, "")
        if refused:
            line["refused"] = sorted(refused)
        self.write({"type": "conductor", "time_us": self.world.now, **line})

    def restart_reason(self, radio, message):
        self.conductor_event("radio_restart_reason", radio=radio, time_us=message.time_us,
                             reason=message.payload.decode("utf-8", errors="replace"))

    def write(self, line):
        if self.stopped:
            return
        try:
            for fn in self.filters:
                line = fn(line)
        except StopRecording as stop:
            self.stop(stop.reason)
            return
        self.pending.append(line)
        self.flush()

    def flush(self):
        directory = self.world.api.directory
        if directory is None or self.stopped:
            return  # Held in memory until the world folder exists.
        try:
            if self._fd is None:
                self._fd = os.open(directory / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.fchmod(self._fd, 0o600)
            while self.pending:
                line = {**self.pending[0], "line": self.lines + 1}
                os.write(self._fd, json.dumps(line, sort_keys=True, separators=(",", ":"),
                                              allow_nan=False).encode() + b"\n")
                self.pending.pop(0)
                self.lines += 1
        except OSError as error:
            self.stopped = f"RECORD_WRITE_FAILED:{error.errno}"
            self.world.notify({"event": "recording_stopped", "reason": self.stopped})

    def stop(self, reason):
        """End the recording. The last line names why; later lines are dropped."""
        if self.stopped:
            return
        self.pending.append({"type": "recording_stopped", "time_us": self.world.now, "reason": reason})
        self.flush()
        if not self.stopped:
            self.stopped = reason
        self.world.notify({"event": "recording_stopped", "reason": reason})

    def close(self):
        self.flush()
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def get(world):
    if getattr(world, "recorder", None) is None:
        world.recorder = Recorder(world)
    return world.recorder


def add_filter(world, fn):
    get(world).add_filter(fn)


def install(world):
    recorder = get(world)
    world.on_input(recorder.input)
    world.on_outbox(Kind.RESTART_REASON, recorder.restart_reason)
    world.on_end(recorder.close)
    notify = world.notify

    def recording_notify(event):
        if event.get("event") in CONDUCTOR_EVENTS:
            recorder.conductor_event(event["event"], **{k: v for k, v in event.items() if k != "event"})
        notify(event)

    world.notify = recording_notify
