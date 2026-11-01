# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Replay and compare a version 2 recording in a new world of fresh radio processes (ADR 0008 items 3 and 5).

    python3 tools/conductor/replay.py replay RECORDING [--ratio max|R] [--grant-us N] [--runtime DIR]
    python3 tools/conductor/replay.py compare RECORDING [--executable ELF] [--ratio max|R] [--runtime DIR]
    python3 tools/conductor/replay.py replay|compare RECORDING --stop-at-ms T [--no-browser]

RECORDING is a world folder, its recording/ folder, or recording/scenario.json (parts/resolve.py writes them).
Use --build RADIO=PATH for each radio that needs another executable, for example --build 2=old.elf.
RADIO is its position in the recording, starting at 1. Replay checks each binary against its recorded sha256;
radios without their own build identity use the recording's top-level build (older recordings).

What a replay is. A new, isolated world is made with Config(replay=True), so the debug link multiplexer sends no
cache refreshes of its own (parts/dlmux.py _host_port). New radio processes start at time 0 from the recorded
start storage; no saved RAM or flash is read. The recorded inputs are played at their stamps (virtual us, round,
class, sequence, source), whatever the radios answer: this is "open loop". Recorded restarts and air policy snapshots
run at their times. The air model runs again and makes the air deliveries again; each one takes the sequence
number that the recording left free for it, so every stamp is the recorded one.

Same run means: the same trace byte for byte, the same checkpoints (VIEW, FRAME, STORE), the same outputs (air
deliveries and decisions, bytes to clients), the same inputs and the same end. The check runs as it goes: after
each grant every record before the world time is settled and is compared. The first difference wins; at one time
VIEW is checked first, then FRAME, STORE, the air, then the trace. The replay stops there and names it, for
example "at 3000.000 ms radio-1 TRACE line 9518 was '... BOARD key.scan', now '... BOARD key.scan down=0x10'".

Refusals, each with a named reason, before any radio starts:
- the time model or class table differs (scenario.validate_v2);
- the recording has no complete end: it can never pass;
- the board profile, VIEW version or world epoch differs;
- the radio binary or the conductor differs: "not a replay: build differs, use compare";
- an input that this tool cannot play yet (a flash-preserving reboot request): REPLAY_INPUT_UNAVAILABLE.

compare runs the same inputs on another build and checks only VIEW records and the recording's VIEW assertions,
for a bisect. FRAME, STORE, the trace and the air are not compared, because another build may draw or log
differently and still be right.

Time travel (--stop-at-ms T, or the library call Replay.travel(t_us)). The replay runs to virtual time T and checks
the run so far: every record before T must be the recorded one (in replay mode the trace lines, VIEW, FRAME, STORE
and the air; in compare mode VIEW only), and no recorded trace line before T may be missing. It prints a sha256 of
that prefix on both sides, for example "replay: before 1500.000 ms radio-2 trace lines 3933 sha256 dffb..
recorded dffb..". If anything differs it stops there, names it and opens nothing. If all is the same, the world
leaves replay: it now takes inputs of its own (from a browser or a world client), it has no end time, and the web
gateway starts. First each radio returns to the serial personality, so a recorded network session ends and debug
link replies reach the multiplexer again. A short debug link session then asks each radio for its view (a replay
world's caches are empty), for example "radio-2 view at 1522.000 ms: dmr.state=rx call.src=3100001". The gateway needs about
230 ms of virtual time to bring each radio's page up (the radio's USB network, DHCP, the page and its websocket), so
the world pauses at T plus that time, never at T itself; all three times are printed. Then it prints one browser
address per radio, only once that radio's page is ready, as tools/vradio.py does. The world keeps serving, paused,
until Ctrl-C or `python3 tools/vradio.py down --name NAME` (NAME is the printed world name).

A pause takes every lease away (parts/lease.py pausing), so while the world runs each radio's lease goes back to its
browser. A world client can resume or step the world. The recording's later key and encoder inputs then go on at
their times, and the browser may add its own. Its later debug link and network bytes do not: this world's own
multiplexer and gateway now use those links, and the recorded frames' sequence numbers would clash with theirs
(LIVE_DROPS). Example: tests/recordings/two-radio-dmr.json holds radio 1's PTT from 1.0 to 2.0 s, and
`compare tests/recordings/two-radio-dmr.json --stop-at-ms 1500` shows radio 2 in the call from 3100001 and pauses at
about 1.73 s, still inside the PTT. A committed shareable recording keeps no trace or air files, and it was made by
an older build (the binary's hash holds its commit), so it works only with compare. A world folder recorded on this
build works with replay too.

Air tuning is not a field of schema version 2. Like scenario.py run, the replay puts every radio on one air, on
channel 1, and takes each radio's color code and timeslot from its start storage's current channel. A recording
made with other tuning gives a named AIR difference, never a silent pass.
"""

import argparse
from dataclasses import dataclass, replace
import hashlib
import heapq
import json
import os
from pathlib import Path
import signal
import struct
import sys
import webbrowser

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.conductor.channel import Channel, Kind  # noqa: E402
from tools.conductor.conductor import install_parts  # noqa: E402
from tools.conductor.parts import airctl, dlcache, dlmux, lookahead, resolve, webgw  # noqa: E402
from tools.conductor.world import Config, Input, World, WorldError  # noqa: E402

import convert  # noqa: E402  (on sys.path through parts.resolve)
import recording_lint  # noqa: E402
import scenario  # noqa: E402

INF = float("inf")
REBOOT_SOURCE = 0x1FF       # world.reboot's request source
# Past T the world's own multiplexer and gateway talk on the debug link and the network, so the recorded bytes on
# those links stop: their frame sequence numbers would clash with the live ones. Keys and the encoder go on.
LIVE_DROPS = (Kind.BYTES_IN, Kind.ETH_IN)
QUEUE_AHEAD_US = 50_000     # recorded inputs enter the world queue this far ahead of their time
OPEN_LIMIT_US = 3_000_000   # the gateway must have every radio's page up this long after T
DMR, DMR_CALL = 2, 3        # GET_VIEW sections (core/debuglink/debuglink.h): dmr.state, call.src
VIEWER = "debug:time-travel"
# At one time: VIEW first (ADR 0008 item 4), then FRAME, STORE, the air, the trace, then the end-of-run outputs
RANK = {"VIEW": 0, "FRAME": 1, "STORE": 2, "AIR_DELIVERY": 3, "AIR_DECISION": 4, "TRACE": 5, "INPUT": 6,
        "CLIENT_BYTES": 7, "ASSERTION": 8, "END": 9}


class Refused(Exception):
    """The recording cannot be replayed (or compared) here; the message names why."""


@dataclass(frozen=True)
class Difference:
    t_us: float
    kind: str
    radio: str
    detail: str

    def key(self):
        return (self.t_us, RANK[self.kind], self.radio)

    def __str__(self):
        when = "at the end" if self.t_us == INF else f"at {self.t_us / 1000:.3f} ms"
        return f"{when} {self.radio} {self.kind} {self.detail}"


def short(value, limit=160):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return repr(text if len(text) <= limit else text[:limit] + "...")


def describe(kind, expected, actual):
    """The difference between two records, field by field when both are records."""
    if expected is None:
        return f"was nothing, now {short(actual)}"
    if actual is None:
        return f"was {short(expected)}, now nothing"
    if isinstance(expected, dict) and isinstance(actual, dict):
        fields = sorted(set(expected) | set(actual))
        parts = [f"{name} was {short(expected.get(name), 64)}, now {short(actual.get(name), 64)}"
                 for name in fields if expected.get(name) != actual.get(name)]
        return "; ".join(parts)
    return f"was {short(expected)}, now {short(actual)}"


class Stream:
    """One ordered list of records, recorded against replayed. Items are (t_us, value)."""

    def __init__(self, kind, radio, expected, label="record"):
        self.kind, self.radio, self.label = kind, radio, label
        self.expected = list(expected)
        self.actual = []
        self.cursor = 0

    def first(self, settled):
        """The first difference among records before settled, or None. Equal records move the cursor on."""
        while True:
            i = self.cursor
            e = self.expected[i] if i < len(self.expected) else None
            a = self.actual[i] if i < len(self.actual) else None
            te = e[0] if e is not None else INF
            ta = a[0] if a is not None else INF
            if min(te, ta) >= settled:
                return None
            if e is not None and a is not None and e[1] == a[1] and te == ta:
                self.cursor += 1
                continue
            # Only the settled side counts: the other one has no record yet at that time
            detail = describe(self.kind, e[1] if te < settled and te <= ta else None,
                              a[1] if ta < settled and ta <= te else None)
            return Difference(min(te, ta), self.kind, self.radio, f"{self.label} {i + 1} {detail}")


class TraceStream(Stream):
    """Trace lines carry radio-local times, so a line is placed by the replayed copy's world time."""

    def __init__(self, radio, data):
        super().__init__("TRACE", radio, (), "line")
        self.lines = data.split(b"\n")[:-1] if data else []

    def first(self, settled):
        while self.cursor < len(self.actual):
            t_us, line = self.actual[self.cursor]
            if t_us >= settled:
                return None
            expected = self.lines[self.cursor] if self.cursor < len(self.lines) else None
            if expected != line:
                was = "nothing" if expected is None else short(expected.decode("utf-8", "replace"))
                return Difference(t_us, "TRACE", self.radio,
                                  f"line {self.cursor + 1} was {was}, now {short(line.decode('utf-8', 'replace'))}")
            self.cursor += 1
        if settled == INF and self.cursor < len(self.lines):
            return Difference(INF, "TRACE", self.radio, f"line {self.cursor + 1} was "
                              f"{short(self.lines[self.cursor].decode('utf-8', 'replace'))}, now nothing")
        return None


def checkpoint_records(world):
    """VIEW, FRAME and STORE records as parts/resolve.py folds them, in world time."""
    name = lambda radio: f"radio-{radio}"
    records = []
    views = getattr(world, "view_checkpoints", None)
    for view in (views.ordered() if views else ()):
        records.append({**view, "radio": name(view["radio"])})
    for frame in getattr(getattr(world, "frames", None), "checkpoints", ()):
        records.append({"kind": "FRAME", "radio": name(frame.radio), "boot": frame.boot,
                        "frame": frame.frame_id, "t_us": frame.time_us, "crc32": f"{frame.crc:08x}",
                        "palette": list(frame.palette) or None, "backlight": frame.backlight})
    records.extend({**store, "radio": name(store["radio"])} for store in world.resolver.stores)
    return records


def by_stream(records):
    """Group checkpoint records into (kind, radio) streams of (t_us, record), in recorded order."""
    groups = {}
    for record in sorted(records, key=lambda r: (r["t_us"], scenario.CHECKPOINT_RANK[r["kind"]], r["radio"],
                                                 r["boot"], r.get("frame", 0))):
        groups.setdefault((record["kind"], record["radio"]), []).append((record["t_us"], record))
    return groups


def find_recording(path):
    path = Path(path)
    if path.is_dir() and (path / "recording/scenario.json").is_file():
        path = path / "recording/scenario.json"
    elif path.is_dir():
        path = path / "scenario.json"
    if not path.is_file():
        raise Refused(f"REPLAY_NO_RECORDING: {path} is not a recording")
    return path


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def load(path, mode="replay"):
    """The recording and its files. Only recording/* and the fold's result.json next to it are read.

    compare never reads the traces or the air files, so a lone shareable scenario.json (no files beside it) loads
    for compare; replay refuses it with REPLAY_MISSING_FILE."""
    path = find_recording(path)
    folder = path.parent
    try:
        value = json.loads(path.read_text())
    except ValueError as error:
        raise Refused(f"REPLAY_BAD_JSON: {error}")
    try:
        value = scenario.validate_v2(value)
    except ValueError as error:
        raise Refused(f"not a replay: {error}")
    if "build" not in value:
        raise Refused("not a replay: a scenario without 'build' and 'end' is not a recording; use scenario.py run")
    files = {"traces": {}, "air_delivery": [], "air_decision": [], "summary": None}
    for radio in value["radios"]:
        trace = folder / f"{radio['id']}.trace"
        if not trace.is_file():
            if mode == "compare":
                files["traces"][radio["id"]] = None
                continue
            raise Refused(f"REPLAY_MISSING_FILE: {trace.name}")
        files["traces"][radio["id"]] = trace.read_bytes()
    for kind, name in (("air_delivery", "air-deliveries.jsonl"), ("air_decision", "air-decisions.jsonl")):
        if not (folder / name).is_file():
            if mode == "compare":
                continue
            raise Refused(f"REPLAY_MISSING_FILE: {name}")
        files[kind] = read_jsonl(folder / name)
    summary = folder.parent / "result.json"   # the fold's run summary: bytes to each client
    if summary.is_file():
        files["summary"] = json.loads(summary.read_text())
    return value, files


def check_world(value, executables, mode):
    """Every refusal before a radio starts."""
    end = value["end"]
    if not end["complete"]:
        raise Refused(f"not a replay: the recording has no complete end (end {end['reason']!r} at "
                      f"{end['t_us']} us, complete: false); it can never pass")
    profile = convert.board_profile()
    if value["view_version"] != profile["view_version"]:
        raise Refused(f"not a replay: VIEW version {value['view_version']} differs from this build's "
                      f"{profile['view_version']}")
    if value["epoch_utc"] != convert.EPOCH:
        raise Refused(f"not a replay: world epoch {value['epoch_utc']} differs from {convert.EPOCH}")
    for radio in value["radios"]:
        for field in ("id", "display", "flash_size", "keymap_sha256"):
            if radio["board"][field] != profile["board"][field]:
                raise Refused(f"not a replay: board profile differs: {radio['id']} board {field} "
                              f"{radio['board'][field]!r}, this build {profile['board'][field]!r}")
        if any(radio.get("clock", {}).values()):
            raise Refused(f"REPLAY_INPUT_UNAVAILABLE: {radio['id']} clock offset or drift")
    builds = {}
    for index, (radio, executable) in enumerate(zip(value["radios"], executables), 1):
        expected = radio.get("build", value["build"])
        remedy = f"provide --build {index}=PATH with sha256 {expected['sha256']}"
        if executable not in builds:
            try:
                builds[executable] = resolve.build_identity(executable)
            except OSError as error:
                raise Refused(f"REPLAY_NO_EXECUTABLE: {radio['id']} {executable}: {error.strerror}; {remedy}") from error
        if mode == "replay":
            for field, label in (("sha256", "radio binary"), ("conductor_sha256", "conductor")):
                if builds[executable][field] != expected[field]:
                    raise Refused(f"not a replay: build differs, use compare ({radio['id']} {label} sha256 "
                                  f"{builds[executable][field]}, recorded {expected[field]})"
                                  + (f"; to replay, {remedy}" if field == "sha256" else ""))
    build = builds[executables[0]]
    if mode == "replay":
        for field, label in (("sha256", "radio binary"), ("conductor_sha256", "conductor")):
            if build[field] != value["build"][field]:
                raise Refused(f"not a replay: build differs, use compare ({label} sha256 {build[field]}, "
                              f"recorded {value['build'][field]})")
    return build


def plan(value):
    """Recorded inputs and world events, with every refusal for what cannot be played."""
    numbers = {radio["id"]: index for index, radio in enumerate(value["radios"], 1)}
    inputs, world_events = [], []
    for index, event in enumerate(value["events"]):
        radio = numbers[event["radio"]]
        if "restart" in event or "air_policy" in event:
            world_events.append({**event, "radio": radio})
            continue
        source = event["source"]
        if not source.startswith("source-") or not source[7:].isdigit():
            raise Refused(f"REPLAY_INPUT_UNAVAILABLE: event {index} source {source!r}")
        source = int(source[7:])
        if source == REBOOT_SOURCE:
            raise Refused(f"REPLAY_INPUT_UNAVAILABLE: event {index} is a reboot request; replaying a reboot "
                          "that keeps flash is not supported yet")
        if "key" in event:
            kind, payload = Kind.KEY, bytes((event["key"]["bit"], int(event["key"]["down"])))
        elif "enc" in event:
            kind, payload = Kind.ENC, struct.pack("<i", event["enc"]["count"])
        elif "host" in event and "generator" in event["host"]:
            reference = event["host"]["generator"]
            payload = recording_lint.generated_bytes(reference)
            if payload is None or hashlib.sha256(payload).hexdigest() != reference["sha256"]:
                raise Refused(f"REPLAY_GENERATOR_MISMATCH: event {index} {reference['name']}")
            kind = Kind.BYTES_IN
        elif "host" in event:
            kind, payload = Kind.BYTES_IN, bytes.fromhex(event["host"]["hex"])
        elif "eth" in event:
            kind, payload = Kind.ETH_IN, bytes.fromhex(event["eth"]["hex"])
        else:
            kind = next((k for k in scenario.EVENT_KINDS if k in event), "unknown")
            raise Refused(f"REPLAY_INPUT_UNAVAILABLE: event {index} kind {kind}")
        if event["class"] != 30:
            raise Refused(f"REPLAY_INPUT_UNAVAILABLE: event {index} class {event['class']}")
        inputs.append(Input(radio, kind, payload, event["t_us"], source, event["round"], 30, event["seq"]))
    return inputs, world_events


def storage_tuning(storage):
    """The color code and timeslot of the start storage's current channel, or the air's 'any'."""
    if not isinstance(storage, list):
        return None, 0
    current = next((s["value"] for s in storage if s["op"] == "setting" and s["name"] == "channel"), 1)
    channel = next((s for s in storage if s["op"] == "channel" and s["number"] == current), None)
    if channel is None or channel["mode"] != "dmr":
        return None, 0
    return channel.get("color_code", 1), channel.get("timeslot", 1)


class ReplayWorld(World):
    """A world whose input stamps are the recorded ones.

    A recorded input keeps its recorded sequence. An air delivery made again by the air model takes the next
    sequence the recording left free: the recorder leaves AIR_IN out (the air files hold it), so those numbers are
    exactly the gaps. Any other input would be the conductor making one of its own, which a replay must never do.

    After go_live() (time travel, past T) the world takes inputs of its own too: each one gets a sequence above
    every recorded one, so two inputs never share a sequence (the queue orders equal times by sequence).
    """

    def __init__(self, config, recorded, **options):
        super().__init__(config, **options)
        self._recorded = set(recorded)
        self._free = 0
        self.live = False

    def go_live(self):
        """Take inputs of our own from now on, and drop the queued recorded LIVE_DROPS inputs.

        The debug link multiplexer tracks every debug link input it sees as a request in flight (parts/dlmux.py
        _input), so the dropped ones leave its flight lists too; else each would wait out the reply timeout."""
        self.live = True
        self._live = max(self._recorded | {self._free, self._sequence})
        dropped = {entry[2].sequence for entry in self._inputs
                   if entry[2].sequence in self._recorded and Kind(entry[2].kind) in LIVE_DROPS}
        self._inputs = [entry for entry in self._inputs if entry[2].sequence not in dropped]
        heapq.heapify(self._inputs)
        mux = getattr(self, "dlmux", None)
        for link in (mux.links.values() if mux is not None else ()):
            link.flight = [entry for entry in link.flight if entry.input_sequence not in dropped]

    def _queue(self, event):
        if self.live and not event.sequence:
            self._sequence = self._live
            queued = super()._queue(event)
            self._live = queued.sequence
            return queued
        if event.sequence:
            self._sequence = event.sequence - 1
        elif Kind(event.kind) == Kind.AIR_IN:
            self._free += 1
            while self._free in self._recorded:
                self._free += 1
            self._sequence = self._free - 1
        else:
            raise WorldError(f"REPLAY_OWN_INPUT:{Kind(event.kind).name}:radio-{event.radio}")
        return super()._queue(event)


class Replay:
    def __init__(self, recording, *, mode="replay", executable=None, ratio="max", grant_us=None, runtime=None,
                 channel_factory=Channel, out=print, builds=None):
        if mode not in ("replay", "compare"):
            raise ValueError("mode is replay or compare")
        self.mode, self.ratio, self.grant_us, self.out = mode, ratio, grant_us, out
        self.executable = str(executable or Config().executable)
        self.value, self.files = load(recording, mode)
        builds = builds or {}
        if any(type(radio) is not int or not 1 <= radio <= len(self.value["radios"])
               or not isinstance(path, (str, Path)) or not str(path) or "\x00" in str(path)
               for radio, path in builds.items()):
            raise Refused("REPLAY_BAD_BUILD: --build requires RADIO=PATH with a radio number from the recording")
        self.executables = tuple(str(builds.get(radio, self.executable))
                                 for radio in range(1, len(self.value["radios"]) + 1))
        self.build = check_world(self.value, self.executables, mode)
        self.inputs, self.world_events = plan(self.value)
        self.pending = [event for event in self.inputs if event.time_us > 0]
        self.input_index = self.world_index = 0
        if grant_us is not None and (type(grant_us) is not int or not 1 <= grant_us <= lookahead.VDMR_LOOKAHEAD_US):
            raise Refused(f"REPLAY_BAD_GRANT: {grant_us} us; a grant is 1 to the proven lookahead "
                          f"{lookahead.VDMR_LOOKAHEAD_US} us")
        self.runtime = runtime
        self.channel_factory = channel_factory
        self.pids = []
        self.difference = None
        self.directory = None

    # --- world ---

    def make_world(self):
        value = self.value
        encoder = "on" if any("enc" in event for event in value["events"]) else "off"
        profile = convert.board_profile()
        config = Config(executable=self.executable, radios=len(value["radios"]), replay=True,
                        radio_executables=self.executables,
                        radio_args=("--host-port", "on", "--encoder", encoder),
                        inputs=tuple(event for event in self.inputs if event.time_us == 0),
                        end_us=value["duration_us"])
        world = ReplayWorld(config, [event.sequence for event in self.inputs], channel_factory=self.channel_factory)
        try:
            install_parts(world)
            displays = {index: radio["board"]["display"] for index, radio in enumerate(value["radios"], 1)}
            if any(display != profile["board"]["display"] for display in displays.values()):
                world.add_radio_args(lambda radio: ("--display-profile", displays[radio]))
            options = []
            for index, radio in enumerate(value["radios"], 1):
                storage, member = scenario.start_storage(radio)
                cc, ts = storage_tuning(radio["storage"])
                options.append({"radio": index, "storage": storage, "radio_index": member,
                                "identity": radio["dmr_id"], "channel": 1, "cc": cc, "ts": ts})
            request = {"version": 1, "command": "air_config", "groups": [list(range(1, len(options) + 1))],
                       "seed": value["seed"], "radios": options,
                       "lookahead_proven": self.grant_us is not None and self.grant_us > 1000}
            response = world.api.dispatch(request)
            if not response["ok"]:
                raise Refused(f"REPLAY_WORLD_REFUSED: {response['error']}")
            if self.grant_us is not None:
                world.add_grant_limit(lambda now: now + self.grant_us)
            world.add_grant_limit(self.next_limit)
            # Apply class-10 effects after the preceding span, including before the final end drain.
            world.on_grant_done(lambda target, outboxes: self.feed(world))
            return world
        except BaseException:
            world.close()
            raise

    def bind(self, world):
        base = f"{self.mode}-{os.getpid()}"  # short: a Unix socket path holds 107 bytes
        for attempt in range(1000):
            try:
                world.api.bind(base if attempt == 0 else f"{base}-{attempt}", self.runtime)
                break
            except ValueError as error:
                if str(error) != "WORLD_ALREADY_EXISTS":
                    raise
        else:
            raise Refused("REPLAY_NO_WORLD_NAME")
        self.directory = world.api.directory

    def next_limit(self, now):
        times = [event["t_us"] for event in self.world_events[self.world_index:] if event["t_us"] > now]
        if self.input_index < len(self.pending):
            times.append(self.pending[self.input_index].time_us)
        times = [t for t in times if t > now]
        return min(times, default=None)

    def feed(self, world):
        """World events due now, then every input up to QUEUE_AHEAD_US ahead, at its recorded stamp."""
        while self.world_index < len(self.world_events) and self.world_events[self.world_index]["t_us"] <= world.now:
            event = self.world_events[self.world_index]
            t_us, radio = event["t_us"], event["radio"]
            if t_us < world.now:
                raise WorldError(f"REPLAY_LATE_WORLD_EVENT:radio-{radio}:{t_us}")
            self.world_index += 1
            if "restart" in event:
                state = world.state
                if state == "Stepping":
                    world.state = "Paused"
                try:
                    world.restart(radio)
                finally:
                    if world.state != "Ended":
                        world.state = state
            else:
                change = event["air_policy"]
                policy = change["policy"]
                value = airctl.Policy(link_up=policy["link_up"], down_links=map(tuple, policy["down_links"]),
                                      drops=map(tuple, policy["drops"]), duplicates=map(tuple, policy["duplicates"]),
                                      loss_ppm={tuple(row[:2]): row[2] for row in policy["loss_ppm"]},
                                      delays_us={tuple(row[:3]): row[3] for row in policy["delays_us"]},
                                      corruptions={tuple(row[:3]): bytes.fromhex(row[3])
                                                   for row in policy["corruptions"]})
                # One recorded snapshot makes one notification, even when a rule was removed.
                airctl._controls[world]._change(change["command"], value)
        while self.input_index < len(self.pending):
            event = self.pending[self.input_index]
            if getattr(world, "live", False) and Kind(event.kind) in LIVE_DROPS:
                self.input_index += 1
                continue
            if event.time_us > world.now + QUEUE_AHEAD_US:
                break
            # An input after a pending restart of its radio waits for that restart (a restart drops the queue)
            if any("restart" in pending and pending["t_us"] <= event.time_us and pending["radio"] == event.radio
                   for pending in self.world_events[self.world_index:]):
                break
            if event.time_us < world.next_start:
                raise WorldError(f"REPLAY_LATE_INPUT:radio-{event.radio}:{event.time_us}")
            world._queue(event)
            self.input_index += 1

    # --- streams ---

    def make_streams(self):
        value, files = self.value, self.files
        compare = self.mode == "compare"
        streams = {}
        expected = by_stream(value["checkpoints"])
        for radio in value["radios"]:
            for kind in (("VIEW",) if compare else ("VIEW", "FRAME", "STORE")):
                streams[kind, radio["id"]] = Stream(kind, radio["id"], expected.get((kind, radio["id"]), []))
            if not compare:
                streams["TRACE", radio["id"]] = TraceStream(radio["id"], files["traces"][radio["id"]])
        if not compare:
            for kind, name in (("air_delivery", "AIR_DELIVERY"), ("air_decision", "AIR_DECISION")):
                streams[name, "air"] = Stream(name, "air", [(e["time_us"], e) for e in files[kind]], "event")
        return streams

    def collect(self, world, streams):
        """The replayed checkpoints so far into their streams (the trace and air streams fill as they come)."""
        actual = by_stream(checkpoint_records(world))
        for (kind, radio), stream in streams.items():
            if kind in ("VIEW", "FRAME", "STORE"):
                stream.actual = actual.get((kind, radio), [])

    def first(self, streams, settled):
        found = [d for d in (stream.first(settled) for stream in streams.values()) if d is not None]
        return min(found, key=Difference.key, default=None)

    # --- run ---

    def prepare(self):
        """The streams and a new, bound world with every hook on it; nothing started yet."""
        self.pending = [event for event in self.inputs if event.time_us > 0]
        self.input_index = self.world_index = 0
        streams = self.make_streams()
        world = self.make_world()
        try:
            def trace(radio, message):
                stream = streams.get(("TRACE", f"radio-{radio}"))
                if stream is not None:
                    stream.actual.append((message.time_us, message.payload))
            world.on_outbox(Kind.TRACE, trace)
            notify = world.notify

            def watch(event):
                name = event.get("event")
                if name in ("air_delivery", "air_decision"):
                    stream = streams.get((name.upper(), "air"))
                    if stream is not None and not world.live:
                        stream.actual.append((event["time_us"], json.loads(json.dumps(event, sort_keys=True))))
                elif name == "radio_started":
                    pid = world.radios[event["radio"]].channel.process.pid if event["radio"] in world.radios \
                        and hasattr(world.radios[event["radio"]].channel, "process") else None
                    self.pids.append((event["radio"], event["boot"], pid))
                    self.out(f"{self.mode}: radio-{event['radio']} boot {event['boot']} is a new process, "
                             f"pid {pid}, started at {event['time_us']} us")
                notify(event)
            world.notify = watch
            self.bind(world)
        except BaseException:
            world.close()
            raise
        return streams, world

    def begin(self, world):
        self.out(f"{self.mode}: {self.value['name']} in a new world {self.directory}, "
                 f"{len(self.inputs)} inputs, {len(self.world_events)} world events, ratio {self.ratio}, "
                 f"grant {'default' if self.grant_us is None else f'{self.grant_us} us'}")
        world.start()
        world.run(self.ratio)

    def turn(self, world, streams, differences):
        """One grant and its check. True when the run stops here (the end, or a replay's first difference)."""
        self.feed(world)
        if not world.advance():
            return False
        if world.state == "Ended":
            return True
        self.collect(world, streams)
        difference = self.first(streams, world.now)
        if difference is None:
            return False
        differences.append(difference)
        if self.mode == "replay":
            world.end("REPLAY_DIFFERENCE")
            return True
        for stream in streams.values():  # compare: note it and go on to the next
            found = stream.first(world.now)
            if found == difference:
                stream.cursor += 1
        return False

    def run(self):
        streams, world = self.prepare()
        differences = []
        try:
            self.begin(world)
            while world.state != "Ended":
                if self.turn(world, streams, differences):
                    break
        except WorldError as error:
            differences.append(Difference(world.now, "END", "world", f"stopped: {error}"))
        finally:
            world.close()
        if not differences or self.mode == "compare":
            differences.extend(self.finish(world, streams))
        differences.sort(key=Difference.key)
        self.difference = differences[0] if differences else None
        return self.report(differences)

    # --- time travel ---

    def travel(self, t_us, *, open_radios=True):
        """Replay to virtual time t_us, check the run so far, then leave the world paused and serving.

        Returns a Travel. When result["same"] is false the world is already closed and nothing was opened. Example:
        Replay("tests/recordings/two-radio-dmr.json", mode="compare").travel(1_500_000) pauses at about 1.73 s
        with both radios' pages up; travel.addresses maps each radio to its one-time browser address."""
        if type(t_us) is not int or not 0 < t_us < self.value["duration_us"]:
            raise Refused(f"REPLAY_BAD_STOP: {t_us} us; stop inside the recording, 1 to "
                          f"{self.value['duration_us'] - 1} us")
        streams, world = self.prepare()
        world.add_grant_limit(lambda now: t_us if now < t_us else None)
        differences = []
        try:
            self.begin(world)
            while world.state != "Ended" and world.now < t_us:
                if self.turn(world, streams, differences):
                    break
            if world.state == "Ended" and not differences:
                differences.append(Difference(world.now, "END", "world", f"ended before T: {world.reason}"))
            if not differences:
                self.collect(world, streams)
                differences.extend(self.missing_trace(world, streams, t_us))
        except WorldError as error:
            differences.append(Difference(world.now, "END", "world", f"stopped: {error}"))
        except BaseException:
            world.close()
            raise
        prefix = self.prefix_hashes(streams, t_us)
        when = f"before {t_us / 1000:.3f} ms"
        for name, (count, recorded, replayed) in prefix.items():
            self.out(f"{self.mode}: {when} {name} {count} sha256 {replayed} recorded {recorded}")
        result = {"mode": self.mode, "same": not differences, "stop_at_us": t_us, "prefix": prefix,
                  "first_difference": str(differences[0]) if differences else None,
                  "world": str(self.directory), "name": self.directory.name if self.directory else None}
        travel = Travel(self, world, result)
        if differences:
            self.difference = differences[0]
            world.close()
            self.out(f"{self.mode}: DIFFERS {when}, first difference {differences[0]}; nothing opened")
            return travel
        self.out(f"{self.mode}: SAME run {when}: " + ", ".join(
            f"{name} ({count})" for name, (count, _, _) in prefix.items()) + " match the recording")
        if open_radios:
            try:
                travel.open()
            except BaseException:
                world.close()
                raise
        return travel

    def missing_trace(self, world, streams, t_us):
        """A recorded trace line stamped before T that the replay has not made yet (its stamp is radio-local)."""
        found = []
        for (kind, radio), stream in streams.items():
            if kind != "TRACE" or stream.cursor >= len(stream.lines):
                continue
            slot = world.radios.get(int(radio.split("-")[1]))
            origin = slot.origin if slot is not None else 0
            line = stream.lines[stream.cursor]
            stamp = line.split(b" ", 1)[0]
            if stamp.isdigit() and int(stamp) + origin < t_us:
                found.append(Difference(int(stamp) + origin, "TRACE", radio, f"line {stream.cursor + 1} was "
                                        f"{short(line.decode('utf-8', 'replace'))}, now nothing"))
        return found

    def prefix_hashes(self, streams, t_us):
        """For each checked stream: (records before T, sha256 of the recorded ones, sha256 of the replayed ones)."""
        digest = lambda items: hashlib.sha256(b"".join(items)).hexdigest()
        encode = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        found = {}
        for (kind, radio), stream in sorted(streams.items()):
            if isinstance(stream, TraceStream):
                lines = [line for t, line in stream.actual if t < t_us]
                recorded = stream.lines[:len(lines)]
                found[f"{radio} trace lines"] = (len(lines), digest(line + b"\n" for line in recorded),
                                                 digest(line + b"\n" for line in lines))
            else:
                recorded = [encode(v) for t, v in stream.expected if t < t_us]
                replayed = [encode(v) for t, v in stream.actual if t < t_us]
                found[f"{radio} {kind}"] = (len(replayed), digest(recorded), digest(replayed))
        return found

    def finish(self, world, streams):
        """After the end: the closing STOREs, the replay's own fold, and every record left."""
        found = []
        folder = None if self.directory is None else self.directory / "recording"
        replayed = None
        if folder is not None and (folder / "scenario.json").is_file():
            replayed = json.loads((folder / "scenario.json").read_text())
        if replayed is None:
            return [Difference(INF, "END", "world", f"the replay folded no recording ({world.reason})")]
        actual = by_stream(replayed["checkpoints"])
        for (kind, radio), stream in streams.items():
            if kind in ("VIEW", "FRAME", "STORE"):
                stream.actual = actual.get((kind, radio), [])
            elif kind == "TRACE":
                data = (folder / f"{radio}.trace").read_bytes()
                lines = data.split(b"\n")[:-1]
                times = [t for t, _ in stream.actual]
                stream.actual = [(times[i] if i < len(times) else world.now, line) for i, line in enumerate(lines)]
        compare = self.mode == "compare"
        while True:
            difference = self.first(streams, INF)
            if difference is None:
                break
            found.append(difference)
            if not compare:
                break
            for stream in streams.values():
                if stream.first(INF) == difference:
                    stream.cursor += 1
                    if isinstance(stream, TraceStream):
                        stream.cursor = len(stream.actual)
        found.extend(self.assertions(replayed))
        if compare:
            return found
        events = Stream("INPUT", "world", [(e["t_us"], e) for e in self.value["events"]], "event")
        events.actual = [(e["t_us"], e) for e in replayed["events"]]
        difference = events.first(INF)
        if difference is not None:
            found.append(difference)
        summary = self.files["summary"]
        result_path = self.directory / "result.json"
        if summary is not None and result_path.is_file():
            result = json.loads(result_path.read_text())
            for radio in sorted(set(summary.get("client_bytes", {})) | set(result.get("client_bytes", {}))):
                was, now = summary["client_bytes"].get(radio), result["client_bytes"].get(radio)
                if was != now:
                    found.append(Difference(INF, "CLIENT_BYTES", radio, describe("CLIENT_BYTES", was, now)))
        if replayed["end"] != self.value["end"]:
            found.append(Difference(INF, "END", "world", describe("END", self.value["end"], replayed["end"])))
        return found

    def assertions(self, replayed):
        """The recording's VIEW assertions, checked on the replayed VIEW records."""
        found = []
        views = [r for r in replayed["checkpoints"] if r["kind"] == "VIEW"]
        for index, assertion in enumerate(self.value["assertions"]):
            condition = assertion["condition"]
            if condition not in ("screen_id", "state_value"):
                if self.mode == "compare":
                    found.append(Difference(INF, "ASSERTION", assertion["radio"],
                                            f"{index} {condition} cannot be checked from VIEW records"))
                continue
            at = assertion.get("at_us", self.value["duration_us"])
            last = [r for r in views if r["radio"] == assertion["radio"] and r["t_us"] <= at]
            field = "screen" if condition == "screen_id" else assertion["field"]
            want = assertion["screen"] if condition == "screen_id" else assertion["value"]
            got = last[-1].get(field) if last else None
            if type(got) is not type(want) or got != want:
                found.append(Difference(at, "ASSERTION", assertion["radio"],
                                        f"{index} {condition} {field} wanted {short(want)}, now {short(got)}"))
        return found

    def report(self, differences):
        traces = {}
        folder = None if self.directory is None else self.directory / "recording"
        for radio in self.value["radios"]:
            path = None if folder is None else folder / f"{radio['id']}.trace"
            data = self.files["traces"][radio["id"]]
            traces[radio["id"]] = {
                "recorded": hashlib.sha256(data).hexdigest() if data is not None else None,
                "replayed": hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None}
        counts = {kind: sum(r["kind"] == kind for r in self.value["checkpoints"]) for kind in scenario.CHECKPOINT_FIELDS}
        result = {"mode": self.mode, "same": not differences, "first_difference": str(differences[0]) if differences
                  else None, "differences": [str(d) for d in differences[:50]], "difference_count": len(differences),
                  "pids": [list(p) for p in self.pids], "traces": traces, "checkpoints": counts,
                  "world": str(self.directory), "build": self.build["sha256"],
                  "recorded_build": self.value["build"]["sha256"]}
        if self.mode == "replay":
            for radio, hashes in traces.items():
                self.out(f"replay: {radio} trace sha256 recorded {hashes['recorded']} replayed {hashes['replayed']}")
            if differences:
                self.out(f"replay: DIFFERS, first difference {differences[0]}")
            else:
                self.out("replay: SAME run: traces byte for byte, checkpoints "
                         + ", ".join(f"{k} {v}" for k, v in counts.items())
                         + f", air, inputs, client bytes and end {self.value['end']}")
        else:
            self.out(f"compare: build {self.build['sha256']} against recorded {self.value['build']['sha256']}; "
                     "VIEW records and assertions only")
            for difference in differences[:50]:
                self.out(f"compare: {difference}")
            self.out(f"compare: {len(differences)} VIEW or assertion differences" if differences
                     else "compare: VIEW records and assertions are the same")
        return result


class Travel:
    """A replayed world held at a moment: after Replay.travel(t_us), paused, its web gateway up.

    world is the World; addresses maps each radio to its one-time browser address (opening it grants that browser
    the radio's lease, as tools/vradio.py does). serve() keeps it answering the world API and the browsers until
    the world ends (Ctrl-C, a world client's end, or `vradio.py down`); close() ends it and stops every radio.
    """

    def __init__(self, replay, world, result):
        self.replay, self.world, self.result = replay, world, result
        self.addresses, self.principals = {}, {}
        self.paused_us = self.view_us = None
        self.views = {}

    def open(self):
        """Leave replay at T, read each radio's view, start the gateway, run until every page is up, then pause."""
        replay, world, out = self.replay, self.world, self.replay.out
        t_us = world.now
        world.go_live()
        # No end time any more, and the debug link multiplexer refreshes its caches again (parts/dlmux.py _host_port)
        world.config = replace(world.config, replay=False, end_us=None)
        out(f"{replay.mode}: at {t_us / 1000:.3f} ms the world leaves replay and opens the radios' web pages")
        self.views, self.view_us = self.read_views(t_us), world.now
        self.result.update(views={str(radio): view for radio, view in self.views.items()}, view_us=world.now)
        for radio, view in self.views.items():
            out(f"{replay.mode}: radio-{radio} view at {world.now / 1000:.3f} ms: "
                + " ".join(f"{k}={v}" for k, v in view.items()))
        gateway = webgw.get(world)
        gateway.fixed = False   # the setting was fixed (off) when the radios started; it is set once more, here
        gateway.set_enabled(True)
        for radio in sorted(world.radios):
            slot = world.radios[radio]
            origin = slot.origin
            # The gateway stamps its USB network setup at origin + SETTINGS_US; past T that is in the past, so for
            # this one call the setup is placed at the next free time. Nothing else reads origin in between.
            slot.origin = max(origin, world.next_start - webgw.SETTINGS_US)
            try:
                gateway.radio_started({"event": "radio_started", "radio": radio, "boot": world.boots[radio],
                                       "source": radio, "time_us": world.now})
            finally:
                slot.origin = origin
            session = gateway.token({"radio": radio})
            world.leases.grant(radio, session["principal"])
            self.principals[radio] = session["principal"]
            self.addresses[radio] = session["url"]
        while world.state not in ("Paused", "Ended"):
            if world.state == "Running" and all(gateway.status().get(str(radio), {}).get("state") == "up"
                                                for radio in world.radios):
                world.pause()
                continue
            if world.now > t_us + OPEN_LIMIT_US:
                world.end("REPLAY_PAGES_NOT_UP")
                break
            world.api.poll(0)
            replay.feed(world)
            world.advance()
        if world.state == "Ended":
            raise WorldError(f"REPLAY_OPEN_FAILED:{world.reason}")
        self.paused_us = world.now
        self.result["paused_us"] = world.now
        out(f"{replay.mode}: paused at {world.now / 1000:.3f} ms (T {t_us / 1000:.3f} ms plus "
            f"{(world.now - t_us) / 1000:.3f} ms to bring the pages up); world: {self.result['name']}")

    def read_views(self, t_us):
        """Each radio's dmr.state, call.src and call.dst, asked of the radio itself just after T.

        A recorded gateway may already have switched USB to network mode, routing debug replies away from the UART.
        The virtual UART still accepts inputs, so switch back and wait for the firmware's confirmation first.
        A replay world's caches are empty (its multiplexer sent nothing). Before the gateway starts, a
        short controller session per radio sends GET_VIEW for sections 2 and 3 (a few ms of virtual time), then
        gives its lease back. Its requests go first; the multiplexer's own cache refresh queues behind them."""
        world, mux = self.world, self.world.dlmux
        serial = set()

        def switched(radio, message):
            if b" BOARD net.select 0" in message.payload:
                serial.add(radio)

        world.on_outbox(Kind.TRACE, switched)
        try:
            for radio in sorted(world.radios):
                world.submit(radio, Kind.BYTES_IN,
                             dlmux.frame(webgw.RADIO, 0xF0, bytes((webgw.OP_USB_NETWORK, 0))),
                             webgw.SOURCE, internal=True)
            while serial != set(world.radios):
                if world.state == "Ended" or world.now > t_us + OPEN_LIMIT_US:
                    raise WorldError(f"REPLAY_NO_SERIAL:{world.reason}")
                world.api.poll(0)
                self.replay.feed(world)
                world.advance()
        finally:
            world._outbox_hooks[Kind.TRACE].remove(switched)
        sessions = {}
        try:
            for radio in sorted(world.radios):
                lease = world.leases.grant(radio, VIEWER)
                session = mux.connect(radio, VIEWER, "controller", lease["generation"], lease["secret"])
                sessions[radio] = (session, dlmux.Parser(dlmux.REPLY_MAX), {})
                mux.feed(session, dlmux.frame(dlcache.GET_VIEW, DMR, bytes((DMR,)))
                         + dlmux.frame(dlcache.GET_VIEW, DMR_CALL, bytes((DMR_CALL,))))
            while True:
                for session, parser, replies in sessions.values():
                    for item in parser.feed(session.take()):
                        if item[0] == "frame":
                            replies[item[2]] = item[3]
                if all(len(replies) == 2 for _, _, replies in sessions.values()):
                    break
                if world.state == "Ended" or world.now > t_us + OPEN_LIMIT_US:
                    raise WorldError(f"REPLAY_NO_VIEW:{world.reason}")
                world.api.poll(0)
                self.replay.feed(world)
                world.advance()
        finally:
            for radio, (session, _, _) in sessions.items():
                mux.disconnect(session)
                if world.state != "Ended" and radio in world.leases.held:
                    world.leases.revoke(radio)
        views = {}
        for radio, (_, _, replies) in sessions.items():
            fields = {}
            for section in (DMR, DMR_CALL):
                payload = replies[section]
                if payload[:1] == bytes((dlcache.ST_OK,)):
                    fields.update(line.split("=", 1) for line in payload[6:].decode("utf-8", "replace").splitlines()
                                  if "=" in line)
            views[radio] = {k: v for k, v in fields.items() if k in ("dmr.state", "call.src", "call.dst")}
        return views

    def serve(self):
        """Answer the world API and the browsers until the world ends. Resumed, the recorded keys go on."""
        world = self.world
        while world.state != "Ended":
            world.api.poll(0.05 if world.state == "Paused" else 0)
            if world.state == "Running":
                self.lend()
            if world.state != "Ended":
                self.replay.feed(world)
                world.advance()
        world.api.poll(0)   # flush the reply to a world client's end command before closing its socket

    def lend(self):
        """While the world runs, each radio's lease goes back to its printed browser holder (a pause revokes every
        lease, parts/lease.py pausing). A radio whose lease someone else holds, or whose release waits, is left."""
        leases = self.world.leases
        for radio, principal in self.principals.items():
            if radio in self.world.radios and radio not in leases.held and not leases.holds.unconfirmed(radio):
                try:
                    leases.grant(radio, principal)
                except WorldError:
                    pass

    def close(self):
        self.world.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("replay", "compare"))
    parser.add_argument("recording", type=Path)
    parser.add_argument("--executable", default=Config().executable)
    parser.add_argument("--build", action="append", default=[], metavar="RADIO=PATH",
                        help="use this executable for a radio (1-based recording order; repeat for more radios)")
    parser.add_argument("--ratio", default="max", help="max, or virtual time per wall time (1 is real time)")
    parser.add_argument("--grant-us", type=int, help=f"longest grant, up to {lookahead.VDMR_LOOKAHEAD_US} us")
    parser.add_argument("--runtime", help="the folder that holds rfoundry-vr/ (default XDG_RUNTIME_DIR)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON too")
    parser.add_argument("--stop-at-ms", type=float, help="time travel: replay to this virtual time, check it, "
                        "pause and open each radio in the browser; serves until Ctrl-C or vradio.py down")
    parser.add_argument("--no-browser", action="store_true", help="with --stop-at-ms: print the addresses only")
    args = parser.parse_args(argv)
    builds = {}
    for selection in args.build:
        radio, separator, path = selection.partition("=")
        if not separator or not radio.isdecimal() or int(radio) < 1 or not path or "\x00" in path:
            parser.error("--build requires RADIO=PATH with a positive radio number")
        if int(radio) in builds:
            parser.error("--build names the same radio more than once")
        builds[int(radio)] = path
    ratio = "max" if args.ratio == "max" else float(args.ratio)
    try:
        replay = Replay(args.recording, mode=args.mode, executable=args.executable, ratio=ratio,
                        grant_us=args.grant_us, runtime=args.runtime, builds=builds)
        if args.stop_at_ms is not None:
            return time_travel(replay, round(args.stop_at_ms * 1000), args)
        result = replay.run()
    except Refused as error:
        print(f"{args.mode}: refused: {error}")
        return 2
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["same"] else 1


def time_travel(replay, t_us, args):
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: signal.default_int_handler(signum, frame))
    travel = None
    try:
        travel = replay.travel(t_us)
        if args.json:
            print(json.dumps(travel.result, indent=2, sort_keys=True))
        if not travel.result["same"]:
            return 1
        for radio, url in sorted(travel.addresses.items()):
            print(f"radio {radio}: {url} lease holder: {travel.principals[radio]}", flush=True)
            if not args.no_browser and not webbrowser.open(url):
                print(f"browser unavailable; open the printed URL for radio {radio}.", file=sys.stderr)
        print(f"serving until Ctrl-C or: python3 tools/vradio.py down --name {travel.result['name']}", flush=True)
        travel.serve()
    except KeyboardInterrupt:
        if travel is not None:
            travel.world.end()
    except WorldError as error:
        print(f"{args.mode}: stopped: {error}")
        return 1
    finally:
        try:
            if travel is not None:
                travel.close()
        finally:
            signal.signal(signal.SIGTERM, previous)
    print(f"ended: {travel.world.reason}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
