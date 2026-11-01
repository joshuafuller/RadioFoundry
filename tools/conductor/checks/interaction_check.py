#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""recording acceptance: two radios in one air, a recording replayed, and no personal data kept.

Build: make -j8 BOARD=vhost. Check: python3 tools/conductor/checks/interaction_check.py [--part air|replay|privacy|all]
Exit 0 is a pass, 1 a failed check, 2 an unavailable prerequisite or an incomplete run (as observe_check.sh).

The world is tools/conductor/scenarios/two_radio_dmr.json: two radios with the dmr-pair start storage on one air
(radio 1 is DMR ID 3100001, radio 2 is 3100002). It is driven as a person would drive it:
  - PTT on radio 1 through the world API keypad ("key" command, parts/input.py), held for 1 s. The first burst of
    that call is the voice header: it carries who calls whom (the link control, LC).
  - A text message (SMS) "TEST" to 3100002 through the debug link multiplexer, as a controller with a lease: the
    DMR_DATA request, op SMS (core/debuglink/debuglink_dmr_data.c:173), ACK requested.
  - The radio view read back through observer sessions. An observer is answered from the host caches
    (parts/dlcache.py), so reading never changes what the firmware sees. Section 3 (dmr_call) holds call.src,
    section 2 (dmr) dmr.state and section 11 (dmr_data_stats) data.sms_received and data.sms_acked.

Parts:
  air      SC1: radio 2 shows call.src=3100001 during radio 1's PTT, then data.sms_received=1 on radio 2 and
           data.sms_acked=1 on radio 1.
  replay   SC2: the run's recording is replayed in new processes at max and at r = 1 (tools/conductor/replay.py).
           The trace hashes and the FRAME, VIEW and STORE checkpoint lists of all three runs are the same.
  privacy  SC3: the run's recording promotes to tier shareable and passes the shareable lint; the committed
           tests/recordings/two-radio-dmr.json passes it too. A second run gets a raw debug link CODEPLUG write from
           an unknown source. Its recording must stop with RECORD_STORAGE_INPUT:debug link CODEPLUG write, and a
           sentinel byte string from that write must be in no file of the world folder (grep -r, raw and as hex).

--write-recording makes tests/recordings/two-radio-dmr.json again from a fresh run (promote, then copy). A normal
check run never writes under tests/.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.conductor import replay  # noqa: E402
from tools.conductor.channel import ChannelError, Kind  # noqa: E402
from tools.conductor.conductor import install_parts, scenario_config  # noqa: E402
from tools.conductor.parts import dlcache, dlmux  # noqa: E402
from tools.conductor.world import Config, World, WorldError  # noqa: E402

import recording_lint  # noqa: E402  (on sys.path through parts.resolve)

SCENARIO = ROOT / "tools/conductor/scenarios/two_radio_dmr.json"
COMMITTED = ROOT / "tests/recordings/two-radio-dmr.json"
NAME = "two-radio-dmr"
CONTROLLER, OBSERVER = "debug:interaction", "debug:interaction-view"
DMR, DMR_CALL, DMR_DATA_STATS = 2, 3, 11     # GET_VIEW sections (core/debuglink/debuglink.h:22-49)
DMR_DATA, CODEPLUG, OP_SMS = 0x0E, 0x1C, 1   # core/debuglink/debuglink_proto.h
STOP_REASON = "RECORD_STORAGE_INPUT:debug link CODEPLUG write"
KINDS = ("VIEW", "FRAME", "STORE")


class CheckFailure(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise CheckFailure(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_view(world, session, section):
    """One GET_VIEW section from the observer cache, as a dict of its key=value lines."""
    session.take()
    world.dlmux.feed(session, dlmux.frame(dlcache.GET_VIEW, section, bytes((section,))))
    replies = dlmux.Parser(dlmux.REPLY_MAX).feed(session.take())
    require(len(replies) == 1 and replies[0][0] == "frame", f"no GET_VIEW {section} reply on radio {session.radio}")
    payload = replies[0][3]
    require(payload and payload[0] == dlcache.ST_OK,
            f"GET_VIEW {section} on radio {session.radio}: status {payload[:1].hex()} (cache empty?)")
    return dict(line.split("=", 1) for line in payload[6:].decode("utf-8", "replace").splitlines() if "=" in line)


def sms_frame(sms):
    payload = bytes((OP_SMS,)) + struct.pack("<IB", sms["dst"], sms["flags"]) + sms["text"].encode()
    return dlmux.frame(DMR_DATA, 0x31, payload)


def codeplug_frame(codeplug):
    """A raw CODEPLUG write (cmd 0x1C) that carries the sentinel; no known generator makes these bytes."""
    payload = b"\x04" + codeplug["sentinel"].encode()
    require(len(payload) <= dlmux.REQUEST_MAX, "sentinel too long for one debug link request")
    return dlmux.frame(CODEPLUG, 0x32, payload)


def live_run(executable, runtime, *, raw_codeplug=False, out=print):
    """Run the scenario once in a new world. Returns the world folder and the views read back."""
    data = json.loads(SCENARIO.read_text())
    world = World(scenario_config(SCENARIO, radios=data["radios"], executable=executable))
    try:
        install_parts(world)  # every part, as air.scenario_world does, with this executable
        response = world.api.dispatch({**data["air"], "version": 1, "command": "air_config"})
        if not response["ok"]:
            raise WorldError(response["error"])
    except BaseException:
        world.close()
        raise
    sms, views, codeplug = data["sms"], data["views"], data["codeplug"]
    actions = sorted({sms["time_us"], views["call_us"], views["data_us"]}
                     | ({codeplug["time_us"]} if raw_codeplug else set()))
    world.add_grant_limit(lambda now: next((t for t in actions if t > now), None))
    seen = {}
    try:
        dlcache.get(world, cadence_us=dlcache.scenario_cadence(SCENARIO))
        world.api.bind(NAME, runtime)
        world.start()
        world.run("max")
        lease = world.dlmux.leases.grant(sms["radio"], CONTROLLER)
        for key in data["keys"]:
            reply = world.api.dispatch({"version": 1, "command": "key", **lease, **key})
            require(reply["ok"], f"keypad request refused: {reply.get('error')}")
        controller = world.dlmux.connect(sms["radio"], CONTROLLER, "controller", lease["generation"], lease["secret"])
        observers = {radio: world.dlmux.connect(radio, OBSERVER, "observer") for radio in (1, 2)}
        done = set()
        while world.state != "Ended":
            now = world.now
            if now >= sms["time_us"] and "sms" not in done:
                done.add("sms")
                world.dlmux.feed(controller, sms_frame(sms))
            if raw_codeplug and now >= codeplug["time_us"] and "codeplug" not in done:
                done.add("codeplug")
                world.submit(codeplug["radio"], Kind.BYTES_IN, codeplug_frame(codeplug), codeplug["source"])
            if now >= views["call_us"] and "call" not in done:
                done.add("call")
                seen["call"] = (now, read_view(world, observers[2], DMR_CALL), read_view(world, observers[2], DMR))
            if now >= views["data_us"] and "data" not in done:
                done.add("data")
                seen["data"] = (now, read_view(world, observers[2], DMR_DATA_STATS),
                                read_view(world, observers[1], DMR_DATA_STATS))
            world.api.poll(0)
            world.advance()
        seen["sms_reply"] = dlmux.Parser(dlmux.REPLY_MAX).feed(controller.take())
        seen["stopped"] = world.recorder.stopped
        if world.reason != "WORLD_END_TIME":
            raise WorldError(world.reason)
    finally:
        world.close()
    folder = Path(runtime) / "rfoundry-vr" / NAME
    out(f"run: world {folder}, end {world.reason} at {world.now} us")
    return folder, seen


def part_air(seen, out=print):
    keys = json.loads(SCENARIO.read_text())["keys"]
    t_us, call, state = seen["call"]
    out(f"SC1 radio 1 PTT down {keys[0]['time_us']} us, up {keys[1]['time_us']} us (world API keypad)")
    out(f"SC1 at {t_us} us radio 2 view: call.src={call.get('call.src')} call.dst={call.get('call.dst')} "
        f"call.group={call.get('call.group')} dmr.state={state.get('dmr.state')}")
    require(keys[0]["time_us"] <= t_us < keys[1]["time_us"], "SC1 FAIL call view not read during PTT")
    require(call.get("call.src") == "3100001", "SC1 FAIL radio 2 does not show a call from 3100001")
    reply = seen["sms_reply"]
    out(f"SC1 SMS 'TEST' to 3100002 as controller {CONTROLLER}: reply "
        + ", ".join(f"cmd 0x{r[1]:02x} status {r[3][:1].hex()}" for r in reply))
    require(len(reply) == 1 and reply[0][:2] == ("frame", 0x80 | DMR_DATA) and reply[0][3][:1] == b"\x00",
            "SC1 FAIL the DMR_DATA SMS request was not accepted")
    t_us, received, sent = seen["data"]
    out(f"SC1 at {t_us} us radio 2 view: data.sms_received={received.get('data.sms_received')}; "
        f"radio 1 view: data.sms_sent={sent.get('data.sms_sent')} data.sms_acked={sent.get('data.sms_acked')} "
        f"data.sms_failed={sent.get('data.sms_failed')}")
    require(received.get("data.sms_received") == "1", "SC1 FAIL radio 2 data.sms_received is not 1")
    require(sent.get("data.sms_acked") == "1", "SC1 FAIL radio 1 data.sms_acked is not 1")
    out("SC1 PASS voice header and data exchange through the simulated air")


def checkpoint_lists(folder):
    value = json.loads((Path(folder) / "recording/scenario.json").read_text())
    return {kind: [c for c in value["checkpoints"] if c["kind"] == kind] for kind in KINDS}


def trace_hashes(folder):
    return {path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((Path(folder) / "recording").glob("radio-*.trace"))}


def part_replay(folder, executable, runtime, out=print):
    runs = {"recording": folder}
    pids = []
    for ratio in ("max", 1):
        lines = []
        tool = replay.Replay(folder, executable=executable, ratio=ratio, runtime=runtime, out=lines.append)
        result = tool.run()
        for line in lines:
            out(f"  {line}")
        require(result["same"], f"SC2 FAIL replay at r = {ratio}: {result['first_difference']}")
        pids.extend(pid for _, _, pid in result["pids"])
        runs[f"replay r={ratio}"] = Path(result["world"])
    recorded_pids = len(pids)
    require(recorded_pids >= 4 and len(set(pids)) == recorded_pids, "SC2 FAIL replays did not use new processes")
    reference = None
    for label, path in runs.items():
        lists, traces = checkpoint_lists(path), trace_hashes(path)
        summary = (traces, {kind: digest(items) for kind, items in lists.items()})
        out(f"SC2 {label}: " + " ".join(f"{radio} trace {sha}" for radio, sha in traces.items()))
        out(f"SC2 {label}: " + " ".join(f"{kind} {len(lists[kind])} sha256 {summary[1][kind]}" for kind in KINDS))
        if reference is None:
            reference = (summary, lists)
            require(all(lists[kind] for kind in ("VIEW", "FRAME")) and traces, "SC2 FAIL empty recording")
        else:
            require(summary == reference[0] and lists == reference[1], f"SC2 FAIL {label} differs from the recording")
    out(f"SC2 PASS three runs, same traces and checkpoint lists; replay radio pids {sorted(set(pids))}")


def lint(path, shareable=True, out=print):
    args = (["--shareable"] if shareable else []) + [str(path)]
    command = [sys.executable, str(ROOT / "tools/digital-twin/recording_lint.py"), *args]
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out(f"  $ recording_lint.py {' '.join(args)}\n  {result.stdout.strip()}")
    return result.returncode


def promote(folder):
    """The world's recording, promoted to tier shareable (recording_lint.promote, a new file beside it)."""
    return recording_lint.promote(Path(folder) / "recording/scenario.json")


def part_privacy(folder, executable, runtime, out=print):
    shareable = promote(folder)
    out(f"SC3 promoted {shareable}")
    require(lint(shareable, out=out) == 0, "SC3 FAIL promoted recording does not pass the shareable lint")
    require(COMMITTED.is_file(), f"SC3 FAIL {COMMITTED} is missing (make it with --write-recording)")
    require(lint(COMMITTED, out=out) == 0, f"SC3 FAIL {COMMITTED} does not pass the shareable lint")
    codeplug = json.loads(SCENARIO.read_text())["codeplug"]
    raw, seen = live_run(executable, runtime, raw_codeplug=True, out=out)
    lines = [json.loads(line) for line in (raw / "events.jsonl").read_text().splitlines() if line]
    end = json.loads((raw / "recording/scenario.json").read_text())["end"]
    out(f"SC3 raw CODEPLUG write from source {codeplug['source']} at {codeplug['time_us']} us: recorder stopped "
        f"{seen['stopped']!r}; events.jsonl last line {lines[-1]['type']} {lines[-1].get('reason')!r}; end {end}")
    require(seen["stopped"] == STOP_REASON and lines[-1]["type"] == "recording_stopped"
            and lines[-1]["reason"] == STOP_REASON, "SC3 FAIL the CODEPLUG write did not stop the recording")
    require(end["complete"] is False and end["reason"] == STOP_REASON, "SC3 FAIL the stopped recording is complete")
    sentinel = codeplug["sentinel"].encode()
    patterns = (sentinel.decode(), sentinel.hex(), sentinel.hex().upper())
    command = ["grep", "-r", "-a", "-F", *[arg for p in patterns for arg in ("-e", p)], str(raw)]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    files = sum(1 for path in raw.rglob("*") if path.is_file())
    out(f"  $ grep -r -a -F -e {patterns[0]} -e {patterns[1]} -e <upper hex> {raw}  ({files} files)\n"
        f"  exit {result.returncode}, output {result.stdout.strip()!r}")
    require(result.returncode == 1 and not result.stdout, "SC3 FAIL the sentinel is in the world folder")
    out("SC3 PASS shareable lint passes; the raw CODEPLUG write stopped the recording and left no bytes")


def write_recording(folder, out=print):
    shareable = promote(folder)
    value = json.loads(shareable.read_text())
    COMMITTED.write_text(json.dumps(value, indent=2) + "\n")
    out(f"wrote {COMMITTED} ({COMMITTED.stat().st_size} bytes) from {shareable}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--part", choices=("air", "replay", "privacy", "all"), default="all")
    parser.add_argument("--executable", default=Config().executable)
    parser.add_argument("--write-recording", action="store_true", help=f"make {COMMITTED.relative_to(ROOT)} again")
    args = parser.parse_args(argv)
    try:
        if not Path(args.executable).is_file():
            raise FileNotFoundError(f"{args.executable}; run make -j8 BOARD=vhost")
        with tempfile.TemporaryDirectory(prefix="-") as runtime:
            folder, seen = live_run(args.executable, runtime)
            require(seen["stopped"] is None, f"recording stopped: {seen['stopped']}")
            if args.write_recording:
                write_recording(folder)
                return 0
            if args.part in ("air", "all"):
                part_air(seen)
            if args.part in ("replay", "all"):
                part_replay(folder, args.executable, runtime)
            if args.part in ("privacy", "all"):
                with tempfile.TemporaryDirectory(prefix="-") as other:
                    part_privacy(folder, args.executable, other)
        print(f"interaction_check: PASS --part {args.part}")
        return 0
    except CheckFailure as error:
        print(str(error), flush=True)
        return 1
    except (OSError, ValueError, ChannelError, WorldError, replay.Refused) as error:
        print(f"interaction_check: INCOMPLETE: {type(error).__name__}: {error}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
