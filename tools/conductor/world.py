# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""One clock and one ordered input stream for all radios in a world.

Parts install callbacks before start(). Input callbacks receive a frozen Input
once on acceptance, including its final stamp. Outbox callbacks take (radio,
message), with message times translated to world time. Grant-done callbacks
return Input objects; their stamps are validated, never silently moved.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import heapq
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

from .channel import Channel, ChannelError, Completed, DONE, Kind, Message, NEVER, PAYLOAD_MAX
from .pacing import Pacing
from .parts import audit


class WorldError(RuntimeError):
    """A named refusal which the world API can return unchanged."""


def timestamp(value):
    if type(value) is not int or not 0 <= value < NEVER:
        raise WorldError("WORLD_BAD_TIME")
    return value


@dataclass(frozen=True)
class Input:
    radio: int
    kind: Kind
    payload: bytes
    time_us: int
    source: int
    round: int = 0
    cls: int = 30
    sequence: int = 0


@dataclass(frozen=True)
class Config:
    executable: str = str(Path(__file__).resolve().parents[2] / "build/vhost/rfoundry_vhost.elf")
    radios: int = 1
    radio_args: tuple = ("--host-port", "on")
    inputs: tuple = ()
    end_us: int | None = None
    replay: bool = False
    radio_executables: tuple = ()  # one entry per radio; None uses executable

    def __post_init__(self):
        if type(self.radios) is not int or not 1 <= self.radios < (1 << 32):
            raise WorldError("WORLD_BAD_RADIOS")
        if self.end_us is not None:
            timestamp(self.end_us)
        if type(self.replay) is not bool:
            raise WorldError("WORLD_BAD_REPLAY")
        object.__setattr__(self, "radio_args", tuple(self.radio_args))
        object.__setattr__(self, "inputs", tuple(self.inputs))
        if not isinstance(self.radio_executables, (tuple, list)):
            raise WorldError("WORLD_BAD_EXECUTABLES")
        executables = tuple(self.radio_executables)
        if executables and len(executables) != self.radios:
            raise WorldError("WORLD_BAD_EXECUTABLES")
        if any(path is not None and (not isinstance(path, str) or not path or "\x00" in path)
               for path in executables):
            raise WorldError("WORLD_BAD_EXECUTABLES")
        object.__setattr__(self, "radio_executables", executables)

    def executable_for(self, radio):
        return (self.radio_executables[radio - 1] if self.radio_executables else None) or self.executable


@dataclass
class Radio:
    channel: Channel
    origin: int
    boot: int
    done: Completed | None = None


_OPTIONS = {}


def _knows_option(executable, option):
    """True when this radio build lists the option in --help. A world can mix builds (version mixing), and an older
    build refuses an option it does not know, so a newer option is passed only to builds that have it."""
    try:
        key = (executable, os.stat(executable).st_mtime_ns)
    except OSError:
        return False
    if key not in _OPTIONS:
        try:
            out = subprocess.run([executable, "--help"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        _OPTIONS[key] = out
    return option in _OPTIONS[key]


class World:
    def __init__(self, config=Config(), *, channel_factory=Channel, clock=None):
        from .worldapi import WorldAPI
        self.config = config
        self.channel_factory = channel_factory
        self.now = 0
        self.state = "Paused"
        self.reason = None
        self.radios = {}
        self.boots = dict.fromkeys(range(1, config.radios + 1), 0)
        self.sources = tuple(range(1, config.radios + 1))
        self.notes = []
        self.pacing = Pacing(self.notes, **({"clock": clock} if clock else {}))
        self.api = WorldAPI(self)
        self._inputs = []
        self._sequence = 0
        self._input_hooks = []
        self._outbox_hooks = {}
        self._pausing_hooks = []
        self._grant_limits = []
        self._grant_hooks = []
        self._args_hooks = []
        self._end_hooks = []
        self._holds = []
        self._drained = False
        self._started = False
        self._stop = None
        self._flash_dir = None      # holds each radio's flash file across reboots and power cuts
        self._restore = set()       # radios whose next start loads that file
        self._rebooting = None      # the radio that is expected to exit with reason "reboot"
        self._rebooted = False
        self._pool = None

    @property
    def next_start(self):
        return self.now + int(self._drained)

    def on_input(self, fn):
        self._input_hooks.append(fn)

    def on_outbox(self, kind, fn):
        self._outbox_hooks.setdefault(Kind(kind), []).append(fn)

    def on_pausing(self, fn):
        """fn() may return a predicate: true only once its release is confirmed."""
        self._pausing_hooks.append(fn)

    def add_grant_limit(self, fn):
        self._grant_limits.append(fn)

    def on_grant_done(self, fn):
        self._grant_hooks.append(fn)

    def add_radio_args(self, fn):
        self._args_hooks.append(fn)

    def on_end(self, fn):
        self._end_hooks.append(fn)

    def notify(self, event):
        self.api.notify(event)

    def _state(self, state):
        self.state = state
        self.notify({"event": "state", "state": state, "time_us": self.now,
                     "reason": self.reason})

    def _check_radio(self, radio):
        if type(radio) is not int or radio not in self.boots:
            raise WorldError("WORLD_UNKNOWN_RADIO")

    def _command(self, *, pausing=False):
        if self.state == "Ended":
            raise WorldError("WORLD_ENDED")
        if self.state == "Stepping" or self.state == "Pausing" and not pausing:
            raise WorldError("WORLD_BUSY")

    def start(self):
        if self._started:
            raise WorldError("WORLD_ALREADY_STARTED")
        self._command()
        self._started = True
        try:
            for radio in self.boots:
                self.start_radio(radio)
            for event in self.config.inputs:
                self._queue(event)
            self._drain()
        except (OSError, ChannelError, WorldError):
            self.end("WORLD_START_FAILED")
            raise

    def start_radio(self, radio):
        # Restarting clears holds whose releases could not be confirmed while pausing.
        self._command(pausing=True)
        self._check_radio(radio)
        if radio in self.radios:
            raise WorldError("WORLD_RADIO_ON")
        args = list(self.config.radio_args)
        for fn in self._args_hooks:
            args.extend(fn(radio))
        # The conductor owns the end time, including worlds longer than 10 s.
        args.extend(("--until-ms", str((NEVER - 1) // 1000)))
        # Completed flash operations survive a power cut; firmware reboot uses the same file.
        path = self._flash_path(radio)
        executable = self.config.executable_for(radio)
        if _knows_option(executable, "--flash-file"):
            args.extend(("--flash-file", path))
        args.extend(("--restart-flash", path))
        if radio in self._restore:
            args.extend(("--restore-flash", path))
        try:
            channel = self.channel_factory(self.config.executable_for(radio), *args)
            self.boots[radio] += 1
            self._restore.discard(radio)
            self.radios[radio] = Radio(channel, self.now, self.boots[radio])
            done = self._collect(radio)
            self._completed({radio: done}, self.now)
            if self._drained and self.state != "Ended":
                channel.send(Message(Kind.DRAIN))
                self._completed({radio: self._collect(radio)}, self.now)
        except (OSError, ChannelError) as error:
            self.end(f"WORLD_RADIO_FAILURE:{radio}:{error}")
            raise WorldError(self.reason) from error
        self.notify({"event": "radio_started", "radio": radio, "boot": self.boots[radio],
                     "source": radio, "time_us": self.now})

    def power_off(self, radio):
        self._command(pausing=True)
        self._check_radio(radio)
        slot = self.radios.get(radio)
        if slot is None:
            raise WorldError("WORLD_RADIO_OFF")
        if self.state == "Pausing":
            audit.emit(self, "radio_off", None, radio, "accepted", "WORLD_PAUSING")
        del self.radios[radio]
        slot.channel.close()
        self._inputs = [entry for entry in self._inputs if entry[2].radio != radio]
        heapq.heapify(self._inputs)
        self.notify({"event": "radio_off", "radio": radio, "time_us": self.now})

    def restart(self, radio):
        self.power_off(radio)
        self._restore.add(radio)
        self.start_radio(radio)

    def _flash_path(self, radio):
        if self._flash_dir is None:
            self._flash_dir = tempfile.mkdtemp(prefix="rf-world-flash-")
        return f"{self._flash_dir}/radio{radio}.flash"

    def _chip_token(self, radio):
        """The debug link's reboot token is the first word of the chip id (--chip-uid, else the fixture value)."""
        args = list(self.config.radio_args)
        for fn in self._args_hooks:
            args.extend(fn(radio))
        token = 0x0F00D001
        for i, arg in enumerate(args[:-1]):
            if arg == "--chip-uid":
                token = int(args[i + 1].split(":")[0], 16)
        return token

    def reboot(self, radio):
        """Reboot one radio and keep its flash: ask it to reboot over the debug link, let it save its flash and exit
        (about 250 ms of virtual time), then start it again from the saved file. Grants are 1 ms while it waits, so the
        restart time does not depend on the air grant length. Example: a reboot asked at 7000001 us restarts the
        radio at about 7250000 us, with boot 2 and all its queued inputs dropped, as restart() does."""
        from .parts.dlmux import frame
        self._command()
        self._check_radio(radio)
        if radio not in self.radios:
            raise WorldError("WORLD_RADIO_OFF")
        if self.state not in ("Running", "Paused"):
            raise WorldError("WORLD_BUSY")
        request = frame(0x0D, 0, struct.pack("<BI", 0, self._chip_token(radio)))
        self._rebooting, self._rebooted = radio, False
        try:
            self._queue(Input(radio, Kind.BYTES_IN, request, self.next_start, 0x1FF))
            for _ in range(5000):
                self._exchange(Kind.GRANT, min(self._target(), self.next_start + 1000))
                if self.state == "Ended":
                    raise WorldError(self.reason)
                if self._rebooted:
                    break
            else:
                raise WorldError("WORLD_REBOOT_TIMEOUT")
        finally:
            self._rebooting = None
        self._restore.add(radio)
        self.restart(radio)

    def submit(self, radio, kind, payload, source, *, time_us=None, round=0, cls=30, internal=False):
        """Only installed parts may use internal=True for scenario/release events."""
        if self.state == "Ended":
            raise WorldError("WORLD_ENDED")
        if not internal and self.state != "Running":
            raise WorldError("WORLD_PAUSED" if self.state == "Paused" else "WORLD_BUSY")
        stamp = self.pacing.stamp(self.next_start) if time_us is None else timestamp(time_us)
        return self._queue(Input(radio, kind, payload, stamp, source, round, cls))

    def _queue(self, event):
        self._check_radio(event.radio)
        if event.radio not in self.radios:
            raise WorldError("WORLD_RADIO_OFF")
        timestamp(event.time_us)
        if event.time_us < self.next_start:
            raise WorldError("WORLD_PAST_INPUT")
        try:
            kind = Kind(event.kind)
        except ValueError as error:
            raise WorldError("WORLD_BAD_INPUT_KIND") from error
        if kind not in (Kind.BYTES_IN, Kind.KEY, Kind.ENC, Kind.ETH_IN, Kind.AIR_IN, Kind.VIEW, Kind.STORE):
            raise WorldError("WORLD_BAD_INPUT_KIND")
        supported = getattr(self.radios[event.radio].channel, "supported_inputs", None)
        if supported is not None and kind not in supported:
            raise WorldError(f"WORLD_UNSUPPORTED_INPUT:{kind.name}")
        if not isinstance(event.payload, bytes) or len(event.payload) > PAYLOAD_MAX:
            raise WorldError("WORLD_BAD_PAYLOAD")
        if kind == Kind.BYTES_IN and not event.payload:
            raise WorldError("WORLD_BAD_PAYLOAD")
        if event.cls != (20 if kind == Kind.AIR_IN else 30):
            raise WorldError("WORLD_BAD_INPUT_CLASS")
        for value in (event.source, event.round):
            if type(value) is not int or not 0 <= value < (1 << 32):
                raise WorldError("WORLD_BAD_STAMP")
        self._sequence += 1
        event = replace(event, kind=kind, sequence=self._sequence)
        heapq.heappush(self._inputs, ((event.time_us, event.round, event.cls), event.sequence, event))
        for fn in self._input_hooks:
            fn(event)
        return event

    def _cancel_inputs(self, sequences):
        self._inputs = [entry for entry in self._inputs if entry[2].sequence not in sequences]
        heapq.heapify(self._inputs)
        recorder = getattr(self, "recorder", None)
        if recorder is not None:
            for sequence in sorted(sequences):
                recorder.conductor_event("input_canceled", sequence=sequence)

    def run(self, ratio=1, *, until=None):
        self._command()
        if until is not None and timestamp(until) <= self.now:
            raise WorldError("WORLD_PAST_STOP")
        self.pacing.reset(self.now, ratio)
        self._stop = until
        self._state("Running")

    def set_ratio(self, ratio):
        self._command()
        if self.state != "Running":
            raise WorldError("WORLD_NOT_RUNNING")
        self.pacing.reset(self.now, ratio)

    def pause(self):
        self._command()
        if self.state == "Paused":
            return
        self._state("Pausing")
        self._holds = []
        for fn in self._pausing_hooks:
            hold = fn()
            if hold is not None:
                self._holds.append(hold)
        self._finish_pause()

    def _finish_pause(self):
        if not all(hold() for hold in self._holds):
            return False
        self._drain()
        if self.state != "Ended":
            self.pacing.finish(self.now)
            self._stop = None
            self._state("Paused")
        return True

    def step(self, until=None):
        if self.state != "Paused":
            raise WorldError("WORLD_NOT_PAUSED")
        if until is None:
            times = [slot.done.next_due_us for slot in self.radios.values()]
            times.extend(entry[2].time_us for entry in self._inputs)
            until = min(times, default=NEVER)
            if until == NEVER:
                raise WorldError("WORLD_NO_NEXT_EVENT")
        if timestamp(until) < self.next_start:
            raise WorldError("WORLD_PAST_STOP")
        self.pacing.reset(self.now, "max")
        self._stop = until
        self._state("Stepping")

    def _target(self):
        limits = [value for value in (self.config.end_us, self._stop) if value is not None and value > self.now]
        limits.extend(entry[2].time_us for entry in self._inputs if entry[2].time_us > self.now)
        cap = self.pacing.cap(self.api.live or self.state == "Pausing")
        if cap is not None:
            limits.append(self.now + cap)
        for fn in self._grant_limits:
            limit = fn(self.now)
            if limit is not None:
                if timestamp(limit) <= self.now:
                    raise WorldError("WORLD_BAD_GRANT_LIMIT")
                limits.append(limit)
        # With no world event or limit, step to the next firmware event.
        if not limits:
            limits.extend(slot.done.next_due_us for slot in self.radios.values()
                          if slot.done.next_due_us > self.now)
        return min(limits, default=self.now + 1000)

    def advance(self):
        """Do one grant (or wait for its deadline); callers keep servicing the API."""
        if self.state in ("Paused", "Ended"):
            return False
        if self.state == "Pausing" and self._finish_pause():
            return True
        target = self._target()
        if self.state == "Running":
            delay = self.pacing.delay(target, self.now)
            if delay:
                self.api.poll(min(delay, 0.05))
                return False
        self._exchange(Kind.GRANT, target)
        if self.state == "Ended":
            return True
        if self.config.end_us is not None and self.now >= self.config.end_us:
            self._drain()
            self.end("WORLD_END_TIME")
        elif self.state == "Pausing":
            self._finish_pause()
        elif self._stop is not None and self.now >= self._stop:
            if self.state == "Stepping":
                self._drain()
                if self.state != "Ended":
                    self._stop = None
                    self._state("Paused")
            else:
                self.pause()
        return True

    def _collect(self, radio):
        slot = self.radios[radio]
        outbox = []
        while True:
            message = slot.channel.receive()
            if slot.origin:
                message = replace(message, time_us=message.time_us + slot.origin)
            if message.kind == Kind.DONE:
                if len(message.payload) != DONE.size:
                    raise ChannelError("VCHAN_BAD_DONE")
                due, rounds = DONE.unpack(message.payload)
                return Completed(message.time_us, due + slot.origin if due != NEVER else NEVER,
                                 rounds, tuple(outbox))
            outbox.append(message)
            if message.kind == Kind.RESTART_REASON:
                return Completed(message.time_us, NEVER, 0, tuple(outbox))

    def _completed(self, results, target):
        failures = []
        for radio, done in sorted(results.items()):
            self.radios[radio].done = done
            for message in done.outbox:
                for fn in self._outbox_hooks.get(message.kind, ()):
                    fn(radio, message)
                if message.kind == Kind.RESTART_REASON and radio == self._rebooting \
                        and message.payload.startswith(b"reboot"):
                    self._rebooted = True
                elif message.kind == Kind.RESTART_REASON:
                    failures.append(f"WORLD_RADIO_FAILURE:{radio}:{message.payload.decode('utf-8', errors='replace')}")
            if done.time_us != target and not failures and not (self._rebooted and radio == self._rebooting):
                failures.append(f"WORLD_BAD_DONE_TIME:{radio}")
        if failures:
            self.end(failures[0])

    def _exchange(self, kind, target):
        try:
            while self._inputs and self._inputs[0][2].time_us <= self.now:
                event = heapq.heappop(self._inputs)[2]
                slot = self.radios[event.radio]
                slot.channel.send(Message(event.kind, event.payload, event.time_us - slot.origin,
                                          event.round, event.cls, event.source, event.sequence))
            for radio, slot in sorted(self.radios.items()):
                slot.channel.send(Message(kind, time_us=target - slot.origin))
            # Drain every socket concurrently: a full radio outbox must not block another.
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self.config.radios)
            pending = {radio: self._pool.submit(self._collect, radio) for radio in sorted(self.radios)}
            results = {radio: future.result() for radio, future in pending.items()}
            self.now = target
            self._drained = kind == Kind.DRAIN
            self._completed(results, target)
            if self.state != "Ended":
                outboxes = {radio: done.outbox for radio, done in results.items()}
                for fn in self._grant_hooks:
                    for event in fn(target, outboxes) or ():
                        self._queue(event)
        except (OSError, ChannelError) as error:
            self.end(f"WORLD_RADIO_FAILURE:{error}")

    def _drain(self):
        if not self._drained and self.state != "Ended":
            self._exchange(Kind.DRAIN, self.now)

    def end(self, reason="WORLD_STOPPED"):
        if self.state == "Ended":
            return
        if self.state == "Pausing":
            audit.emit(self, "world_end", None, None, "accepted", reason, state=self.state)
        self.reason = reason
        self.pacing.finish(self.now)
        self._state("Ended")
        try:
            # A failed collector can leave other readers active; finish them before closing channels.
            if self._pool is not None:
                self._pool.shutdown(wait=True)
                self._pool = None
            for fn in self._end_hooks:
                fn()
        finally:
            if self._flash_dir is not None:
                shutil.rmtree(self._flash_dir, ignore_errors=True)
                self._flash_dir = None
            for slot in self.radios.values():
                slot.channel.close()
            self.radios.clear()
            self._inputs.clear()

    def close(self):
        self.end()
        self.api.close()
