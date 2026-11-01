# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Fold a live world into a schema version 2 recording (ADR 0008 items 1, 2, 5 and 6).

Two jobs.

1. A recorder filter, before each line is written. A host port write (BYTES_IN) goes through
   recording_lint.storage_carrying(). Storage traffic that a known generator makes is stored as the
   generator reference, never as bytes. Any other storage traffic, or bytes that cannot be decoded,
   ends the recording with a named reason, for example
   RECORD_STORAGE_INPUT:debug link CODEPLUG write. Inputs that version 2 has no kind for end it too.

2. The fold, at world end. events.jsonl (the recorder's file) is the source of truth. The world folder
   then holds:
       result.json, report.md         the run summary: trace hashes, bytes to each client, notes
       recording/scenario.json        the recording: build by hash, radios, events, checkpoints, end
       recording/radio-N.trace        each radio's trace
       recording/air-*.jsonl          air deliveries and decisions
   Folders are 0700 and files 0600, tier local. recording/ holds no other .json file, because
   recording_lint.py reads every .json in a folder as a recording.

Example end record: {"t_us": 5000000, "reason": "WORLD_END_TIME", "complete": true}. A radio that
dies, a stopped recording or any other end reason gives complete: false, so it can never pass a replay.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import shutil
import tempfile

from ..history import History
from ..channel import Kind
from . import recorder

ROOT = Path(__file__).resolve().parents[3]
for _path in (ROOT / "tools/digital-twin", ROOT / "tools"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
import convert  # noqa: E402
import recording_lint  # noqa: E402
import scenario  # noqa: E402

CONDUCTOR = ROOT / "tools/conductor"
COMPLETE = "WORLD_END_TIME"
STORE_LINE = re.compile(rb"^(\d+) \d+ STORE ([0-9a-f]{64})\s*$")
GENERATOR = "codeplug-blank-block"


def private_output(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "wb")


def write_private(path, data):
    with private_output(path) as output:
        output.write(data)


def private_folder(path):
    if not path.parent.exists():
        private_folder(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def conductor_sha256():
    """The conductor and its models (airmodel.py, parts), by content and relative path, tests left out."""
    digest = hashlib.sha256()
    for path in sorted(CONDUCTOR.rglob("*.py")):
        if "tests" in path.relative_to(CONDUCTOR).parts:
            continue
        digest.update(str(path.relative_to(CONDUCTOR)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def build_identity(executable, *, sha256=None, conductor=None):
    """The radio binary by hash only. build/<board>/version holds 'version release commit_time' (Makefile)."""
    executable = Path(executable)
    version, commit_time = "unknown", 0
    try:
        fields = (executable.parent / "version").read_text().split()
        version = fields[0] if fields else version
        commit_time = int(fields[-1]) if len(fields) > 1 and fields[-1].isdigit() else 0
    except OSError:
        pass
    if sha256 is None:
        with executable.open("rb") as binary:
            sha256 = hashlib.file_digest(binary, "sha256").hexdigest()
    return {"sha256": sha256, "version": version[:64],
            "commit_time": commit_time, "conductor_sha256": conductor or conductor_sha256()}


def blank_block_reference(data):
    """The generator reference whose bytes are exactly data, or None (recording_lint.generated_bytes)."""
    try:
        seq, address = data[3], int.from_bytes(data[17:21], "little")
        reference = {"name": GENERATOR, "params": {"address": address, "seq": seq},
                     "sha256": hashlib.sha256(data).hexdigest()}
        return reference if recording_lint.generated_bytes(reference) == data else None
    except (IndexError, ValueError):
        return None


def storage_filter(line):
    """recorder filter: every input line passes here before it is written."""
    if line.get("type") != "input":
        return line
    kind, payload = line["kind"], bytes.fromhex(line.get("payload_hex", ""))
    if kind == "ETH_IN":
        try:
            scenario.validate_eth(payload.hex())
        except ValueError as error:
            raise recorder.StopRecording(f"RECORD_ETH_INPUT:{error}") from error
        return line
    if kind == "BYTES_IN":
        try:
            reason = recording_lint.storage_carrying(payload)
        except ValueError as error:
            raise recorder.StopRecording(f"RECORD_INPUT_UNDECODED:{error}")
        if reason is None:
            return line
        reference = blank_block_reference(payload)
        if reference is None:
            raise recorder.StopRecording(f"RECORD_STORAGE_INPUT:{reason}")
        line = {k: v for k, v in line.items() if k != "payload_hex"}
        return {**line, "generator": reference}
    if kind == "ENC" and not -127 <= int.from_bytes(payload, "little", signed=True) <= 127:
        raise recorder.StopRecording("RECORD_ENC_RANGE")
    if kind in ("KEY", "ENC", "AIR_IN"):
        return line  # AIR_IN is the air model's output: the air files hold it, replay makes it again
    raise recorder.StopRecording(f"RECORD_NO_V2_KIND:{kind}")


def storage_steps(text, radio_index):
    """storage_vhost.c start storage as version 2 editor steps."""
    notes = []
    if text == "blank":
        return "blank", notes
    if text == "dmr-pair":  # the template in storage_vhost.c vflash_preload
        text = (f"steps:channel=1,Simplex,433500000,433500000,dmr,1,1,1;zone=0,Pair,1;"
                f"contact=1,Peer,{3100003 - radio_index},private,1;dmr_id={3100000 + radio_index};"
                f"radio_name=N0SIM;setting=channel,1")
    if text == "dmr-group":  # the template in storage_vhost.c vflash_preload
        text = (f"steps:channel=1,Group,433500000,433500000,dmr,1,1,1;zone=0,Group,1;"
                f"contact=1,TG9,9,group,1;dmr_id={3100000 + radio_index};radio_name=N0SIM;setting=channel,1")
    steps = []
    for item in text.removeprefix("steps:").split(";"):
        op, _, rest = item.partition("=")
        f = rest.split(",")
        if op == "channel":
            steps.append({"op": "channel", "number": int(f[0]), "name": f[1], "rx_hz": int(f[2]),
                          "tx_hz": int(f[3]), "mode": f[4], "color_code": int(f[5]), "timeslot": int(f[6]),
                          "contact": int(f[7])})
        elif op == "zone":
            steps.append({"op": "zone", "index": int(f[0]), "name": f[1], "channels": [int(n) for n in f[2:]]})
        elif op == "contact":
            steps.append({"op": "contact", "index": int(f[0]), "name": f[1], "id": int(f[2]), "type": f[3],
                          "timeslot": int(f[4])})
        elif op == "dmr_id":
            steps.append({"op": "dmr_id", "id": int(f[0])})
        elif op == "radio_name":
            steps.append({"op": "radio_name", "name": f[0]})
        elif op == "setting":
            steps.append({"op": "setting", "name": f[0], "value": int(f[1]) if f[1].isdigit() else f[1]})
    return steps, notes


def printable(text, limit=128):
    return "".join(c if " " <= c <= "~" else "?" for c in str(text))[:limit] or "unknown"


class Resolver:
    def __init__(self, world):
        self.world = world
        self.recorder = recorder.get(world)
        self.recorder.add_filter(storage_filter)
        self.builds = {}
        self.conductor_hash = conductor_sha256()
        self.traces = {}
        self.stores = History()
        self.clients = {}
        self.air = {"config": None, "air_delivery": History(), "air_decision": History()}
        world.on_outbox(Kind.TRACE, self.trace)
        world.on_outbox(Kind.BYTES_OUT, self.client_bytes)
        world.on_end(self.end)
        notify = world.notify

        def resolve_notify(event):
            name = event.get("event")
            if name == "radio_started":
                radio = event["radio"]
                identity = build_identity(world.config.executable_for(radio),
                                          sha256=getattr(world.radios[radio].channel, "executable_sha256", None),
                                          conductor=self.conductor_hash)
                previous = self.builds.setdefault(radio, identity)
                if identity != previous:
                    self.recorder.stop(f"RECORD_BUILD_CHANGED:radio-{radio}:boot-{event['boot']}")
            elif name == "air_config":
                self.air["config"] = event
            elif name in ("air_delivery", "air_decision") and not self.recorder.stopped:
                self.air[name].append(event)
            elif name in ("air_policy", "world_fault"):
                self.recorder.conductor_event(name, **{k: v for k, v in event.items() if k != "event"})
            notify(event)

        world.notify = resolve_notify

    def trace(self, radio, message):
        if self.recorder.stopped:
            return
        slot = self.world.radios[radio]
        if radio not in self.traces:
            self.traces[radio] = (tempfile.TemporaryFile(), hashlib.sha256(), [0])
        stream, digest, count = self.traces[radio]
        data = message.payload + b"\n"
        stream.write(data)
        digest.update(data)
        count[0] += data.count(b"\n")
        match = STORE_LINE.match(message.payload)
        if match:
            self.stores.append({"kind": "STORE", "radio": radio, "boot": slot.boot,
                                "t_us": int(match[1]) + slot.origin, "sha256": match[2].decode()})

    def client_bytes(self, radio, message):
        if not self.recorder.stopped:
            entry = self.clients.setdefault(radio, [0, hashlib.sha256()])
            entry[0] += len(message.payload)
            entry[1].update(message.payload)

    def end(self):
        directory = self.world.api.directory
        if directory is None:
            return
        try:
            # The finish hook's STORE must arrive before the recording is folded.
            for radio, slot in sorted(self.world.radios.items()):
                for message in slot.channel.close() or ():
                    if message.kind == Kind.TRACE:
                        self.trace(radio, message)
            self.fold(Path(directory))
        except Exception as error:  # an end hook must never raise; the run summary names the failure
            write_private(Path(directory) / "result.json",
                          (dumps({"complete": False, "fold_error": printable(error, 512)}) + "\n").encode())

    def fold(self, directory):
        self.recorder.close()
        with (directory / "events.jsonl").open() as source:
            lines = History(json.loads(line) for line in source if line.strip())
        world, notes = self.world, []
        stopped = next((line for line in lines if line["type"] == "recording_stopped"), None)
        end_t = stopped["time_us"] if stopped else world.now
        reason = stopped["reason"] if stopped else (world.reason or "WORLD_STOPPED")
        duration = max(end_t, 1)
        name = lambda radio: f"radio-{radio}"

        # Radios: identity, board and start storage
        config = self.air["config"] or {}
        options = {item["radio"]: item for item in config.get("radios", [])}
        profile = convert.board_profile()
        radios = []
        for radio in sorted(world.boots):
            option = options.get(radio, {})
            identity = option.get("identity", radio)
            storage, gaps = storage_steps(option.get("storage", "blank"), option.get("radio_index", 1))
            notes.extend(f"{name(radio)} storage: {gap}" for gap in gaps)
            radios.append({"id": name(radio), "dmr_id": identity if 3100000 < identity < 0xFFFFFF else 3100000 + radio,
                           "board": dict(profile["board"]), "storage": storage,
                           "build": self.builds[radio]})

        # Fault effects are policy snapshots or restarts; replay must not also schedule the original fault.
        events, world_events, dropped = History(), History(), 0
        canceled = {line["sequence"] for line in lines
                     if line["type"] == "conductor" and line.get("event") == "input_canceled"}
        for index, line in enumerate(lines):
            if stopped is None and line["type"] == "conductor" and line.get("event") == "radio_off":
                after = lines[index + 1] if index + 1 < len(lines) else {}
                if after.get("event") == "radio_started" and after.get("radio") == line["radio"]:
                    world_events.append(line)
                else:
                    reason, end_t = "RECORD_NO_V2_KIND:power_off", line["time_us"]
                    duration = max(end_t, 1)
                    break
            if line["type"] == "conductor" and line.get("event") == "air_policy":
                world_events.append(line)
            if line["type"] != "input" or line["kind"] == "AIR_IN":
                continue
            if line["sequence"] in canceled:
                continue
            stamp = {"t_us": line["time_us"], "round": line["round"], "class": line["class"],
                     "seq": line["sequence"], "source": f"source-{line['source']}", "radio": name(line["radio"])}
            payload = bytes.fromhex(line.get("payload_hex", ""))
            if line["kind"] == "KEY":
                stamp["key"] = {"bit": payload[0], "down": bool(payload[1])}
            elif line["kind"] == "ENC":
                stamp["enc"] = {"count": int.from_bytes(payload, "little", signed=True)}
            elif line["kind"] == "ETH_IN":
                stamp["eth"] = {"hex": payload.hex()}
            elif "generator" in line:
                stamp["host"] = {"stream": "host", "generator": line["generator"]}
            else:
                stamp["host"] = {"stream": "host", "hex": payload.hex()}
            events.append(stamp)
        top = max((event["seq"] for event in events), default=0)
        for ordinal, line in enumerate(world_events, 1):
            payload = ({"air_policy": {"command": line["command"], "policy": line["policy"]}}
                       if line["event"] == "air_policy" else {"restart": {}})
            events.append({"t_us": line["time_us"], "round": 0, "class": 10, "seq": top + ordinal,
                           "source": "conductor", "radio": name(line.get("radio", min(world.boots))), **payload})
        # An input stamped exactly at the end time runs in the world's end drain, so it is kept
        kept = History(event for event in events if event["t_us"] <= duration)
        dropped = len(events) - len(kept)
        if dropped:
            notes.append(f"{dropped} inputs after the end time {end_t} us never ran and are left out")
        kept.sort(key=lambda event: (event["t_us"], event["round"], event["class"], event["seq"]))

        # Checkpoints: VIEW, FRAME, STORE, up to the end
        records = History()
        views = getattr(world, "view_checkpoints", None)
        for view in (record for history in views.records.values() for record in history) if views else ():
            records.append({**view, "radio": name(view["radio"])})
        for frame in getattr(getattr(world, "frames", None), "checkpoints", ()):
            records.append({"kind": "FRAME", "radio": name(frame.radio), "boot": frame.boot,
                            "frame": frame.frame_id, "t_us": frame.time_us, "crc32": f"{frame.crc:08x}",
                            "palette": list(frame.palette) or None, "backlight": frame.backlight})
        records.extend({**store, "radio": name(store["radio"])} for store in self.stores)
        records = History(record for record in records if record["t_us"] <= duration)
        records.sort(key=lambda r: (r["t_us"], scenario.CHECKPOINT_RANK[r["kind"]], r["radio"], r["boot"],
                                    r.get("frame", 0)))

        complete = reason == COMPLETE and stopped is None
        recording = {
            "schema_version": 2, "name": directory.name, "tier": "local", "time_model": scenario.TIME_MODEL,
            "class_table": scenario.CLASS_TABLE_VERSION, "view_version": profile["view_version"],
            "seed": config.get("seed", 0), "epoch_utc": convert.EPOCH, "duration_us": duration,
            "build": radios[0]["build"], "radios": radios, "events": kept,
            "checkpoints": records, "assertions": [],
            "end": {"t_us": min(end_t, duration), "reason": printable(reason), "complete": complete},
        }
        try:
            recording_lint.lint(recording)
            valid = None
        except ValueError as error:
            valid = printable(error, 512)

        folder = private_folder(directory / "recording")
        with private_output(folder / "scenario.json") as output:
            for chunk in json.JSONEncoder(indent=2, sort_keys=True, allow_nan=False).iterencode(recording):
                output.write(chunk.encode())
            output.write(b"\n")
        traces = {}
        for radio in sorted(world.boots):
            stream, digest, count = self.traces.get(radio, (None, hashlib.sha256(), [0]))
            with private_output(folder / f"radio-{radio}.trace") as output:
                if stream is not None:
                    stream.seek(0)
                    shutil.copyfileobj(stream, output)
                    stream.close()
            traces[name(radio)] = {"sha256": digest.hexdigest(), "lines": count[0]}
        for kind, file in (("air_delivery", "air-deliveries.jsonl"), ("air_decision", "air-decisions.jsonl")):
            with private_output(folder / file) as output:
                for event in self.air[kind]:
                    output.write((dumps(event) + "\n").encode())
        clients = {name(radio): {"bytes": entry[0], "sha256": entry[1].hexdigest()}
                   for radio, entry in sorted(self.clients.items())}
        counts = {kind: sum(r["kind"] == kind for r in records) for kind in scenario.CHECKPOINT_FIELDS}
        result = {"name": directory.name, "end": recording["end"], "lint_error": valid, "traces": traces,
                  "client_bytes": clients, "checkpoints": counts, "events": len(kept),
                  "air": {"deliveries": len(self.air["air_delivery"]), "decisions": len(self.air["air_decision"])},
                  "notes": notes}
        write_private(directory / "result.json", (json.dumps(result, indent=2, sort_keys=True) + "\n").encode())
        report = [f"# Recording {directory.name}", "",
                  f"- End: {recording['end']['reason']} at {end_t} us, complete: {str(complete).lower()}.",
                  f"- Build: sha256 {recording['build']['sha256']}, version {recording['build']['version']}.",
                  f"- Radios: {len(radios)}. Inputs: {len(kept)}. Checkpoints: "
                  + ", ".join(f"{k} {v}" for k, v in counts.items()) + ".",
                  f"- Air: {result['air']['deliveries']} deliveries, {result['air']['decisions']} decisions.",
                  f"- Lint: {'passes' if valid is None else valid}."]
        report += [f"- Note: {note}." for note in notes]
        write_private(directory / "report.md", ("\n".join(report) + "\n").encode())


def install(world):
    world.resolver = Resolver(world)
