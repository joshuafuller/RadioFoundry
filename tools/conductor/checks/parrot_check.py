#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""caller echo bug in the virtual radio: a parrot's echo of our own group call must be played.

Build: make -j8 BOARD=vhost. Check: python3 tools/conductor/checks/parrot_check.py [--part echo|replay|mixed|all]
Exit 0 is a pass, 1 a failed check, 2 an unavailable prerequisite or an incomplete run (as interaction_check.py).
The echo part defaults to --expect bug: reproducing caller echo bug passes. Use --expect fixed to check the fix.
The mixed part skips with exit 2 without an existing --fixed build.

The user's report (caller echo bug): they key a group call to the repeater's parrot, the parrot plays it back after they
let go, and their radio shows "Other TG: not played". Another radio hears the echo fine. Here the parrot is
parts/parrot.py: it records radio 1's call and, 1 s after its terminator, sends the same bursts back on the air with
radio 1's own source ID. The radios run the real firmware.

Parts:
  echo    tools/conductor/scenarios/parrot_echo.json, two radios (dmr-group start storage: radio k is DMR ID
          3100000+k, talk target TG 9). Radio 1 keys TG 9 from 1.0 to 2.5 s (PTT as KEY inputs in the
          scenario, so tools/vradio.py up --scenario runs the same world). While the parrot's copy is on the air,
          radio 1's view must show dmr.state=rx with call.src=3100001, and call.rx_calls_played and
          voice.rx_frames must grow. Radio 2 must play it too (the control: the copy is on the air and playable).
  replay  the echo run's recording is replayed in new processes (tools/conductor/replay.py, the same parrot
          configured) and must be the same byte for byte: traces, checkpoints, air deliveries (the parrot's too),
          inputs and end. A replay without the parrot must differ (negative control). This part passes whatever
          the firmware does; it proves the failing run is repeatable.
  mixed   tools/conductor/scenarios/parrot_echo_mixed.json with radio 1 on --fixed ELF, radios 2 and 3 on
          --executable (version mixing, as tools/vradio.py --build 1=ELF). Radio 1 and later radio 2 each key TG 9
          for 1.5 s and the parrot plays both back; a repeater also sends radio 1 each of its own bursts 120 ms
          late. Wanted: radio 1 (fixed) plays its parrot echo; radio 2 (unfixed) shows rx_other for its own; the
          repeater's echo tail reaches radio 1 after its TX (a new call in call.rx_calls) and is not played;
          radio 3 plays both parrot copies.

Views are read as an observer from the host caches (parts/dlcache.py, cadence 100 ms in the scenarios), as
interaction_check.py does: GET_VIEW section 2 (dmr), 3 (dmr_call) and 4 (dmr_stats). Reading never changes the run.
"""

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.conductor import replay  # noqa: E402
from tools.conductor.channel import ChannelError  # noqa: E402
from tools.conductor.conductor import install_parts, scenario_config  # noqa: E402
from tools.conductor.parts import dlcache, dlmux  # noqa: E402
from tools.conductor.world import Config, World, WorldError  # noqa: E402

SCENARIOS = ROOT / "tools/conductor/scenarios"
ECHO, MIXED = SCENARIOS / "parrot_echo.json", SCENARIOS / "parrot_echo_mixed.json"
OBSERVER = "debug:parrot-view"
DMR, DMR_CALL, DMR_STATS = 2, 3, 4     # GET_VIEW sections (core/debuglink/debuglink.h:20-49)
OWN = {1: "3100001", 2: "3100002", 3: "3100003"}


class CheckFailure(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise CheckFailure(reason)


def read_view(world, session, section):
    """One GET_VIEW section from the observer cache as a dict, or None while the cache is still empty."""
    session.take()
    world.dlmux.feed(session, dlmux.frame(dlcache.GET_VIEW, section, bytes((section,))))
    replies = dlmux.Parser(dlmux.REPLY_MAX).feed(session.take())
    if len(replies) != 1 or replies[0][0] != "frame" or not replies[0][3] or replies[0][3][0] != dlcache.ST_OK:
        return None
    return dict(line.split("=", 1) for line in replies[0][3][6:].decode("utf-8", "replace").splitlines()
                if "=" in line)


def sample(world, session):
    views = [read_view(world, session, section) for section in (DMR, DMR_CALL, DMR_STATS)]
    if any(view is None for view in views):
        return None
    dmr, call, stats = views
    return {"state": dmr.get("dmr.state"), "src": call.get("call.src"), "dst": call.get("call.dst"),
            "group": call.get("call.group"), "rx_calls": int(stats.get("call.rx_calls", 0)),
            "played": int(stats.get("call.rx_calls_played", 0)), "frames": int(stats.get("voice.rx_frames", 0))}


def live_run(path, executable, runtime, *, executables=(), name="parrot-294", out=print):
    """Run a scenario once in a new world. Returns the world folder, the views every cache tick, and the copies
    the parrot and the repeater echo sent (their air_delivery events)."""
    data = json.loads(path.read_text())
    world = World(scenario_config(path, radios=data["radios"], executable=str(executable),
                                  radio_executables=tuple(executables)))
    copies = []
    try:
        install_parts(world)
        response = world.api.dispatch({**data["air"], "version": 1, "command": "air_config"})
        if not response["ok"]:
            raise WorldError(response["error"])
        notify = world.notify

        def watch(event):
            if event.get("event") == "air_delivery" and event.get("element"):
                copies.append(event)
            notify(event)

        world.notify = watch
    except BaseException:
        world.close()
        raise
    caches = dlcache.get(world, cadence_us=dlcache.scenario_cadence(path))
    timeline = {radio: [] for radio in range(1, data["radios"] + 1)}
    try:
        world.api.bind(name, runtime)
        world.start()
        world.run("max")
        observers = {radio: world.dlmux.connect(radio, OBSERVER, "observer") for radio in timeline}
        # One read per cache tick, just after it: the reply to a tick's requests is in by then
        tick = caches.cadence_us
        world.add_grant_limit(lambda now: (now // tick + 1) * tick + tick // 2)
        last = -1
        while world.state != "Ended":
            if world.now // tick != last and world.now % tick >= tick // 2:
                last = world.now // tick
                for radio, session in observers.items():
                    view = sample(world, session)
                    if view is not None:
                        timeline[radio].append((world.now, view))
            world.api.poll(0)
            world.advance()
        if world.reason != "WORLD_END_TIME":
            raise WorldError(world.reason)
    finally:
        world.close()
    folder = Path(runtime) / "rfoundry-vr" / name
    out(f"run: {path.name}, world {folder}, end {world.reason} at {world.now} us")
    return folder, timeline, copies


def window(copies, receiver, sender, element="parrot"):
    starts = [c["air_start_us"] for c in copies
              if c["receiver"] == receiver and c["sender"] == sender and c["element"] == element]
    require(starts, f"no {element} copy of radio {sender}'s call reached radio {receiver}")
    return min(starts), max(starts) + 30_000


def during(timeline, radio, span, settle_us=200_000):
    """Views of the radio inside span (from settle_us after its start), and the last one before it."""
    start, end = span
    inside = [(t, v) for t, v in timeline[radio] if start + settle_us <= t <= end]
    before = [(t, v) for t, v in timeline[radio] if t < start]
    require(inside and before, f"radio {radio}: no view inside {start}..{end} us")
    return before[-1][1], inside


def plays(timeline, radio, sender, span, out=print, label=""):
    """True when the radio plays the copy of sender's call in span: dmr.state=rx with call.src of the sender, and
    the played-call and voice frame counters grow."""
    before, inside = during(timeline, radio, span)
    states = sorted({f"{v['state']}/{v['src']}" for _, v in inside})
    after = inside[-1][1]
    played, frames = after["played"] - before["played"], after["frames"] - before["frames"]
    out(f"  radio {radio}{label} during {span[0]}..{span[1]} us: dmr.state/call.src {', '.join(states)}; "
        f"call.rx_calls +{after['rx_calls'] - before['rx_calls']}, call.rx_calls_played +{played}, "
        f"voice.rx_frames +{frames}")
    return any(v["state"] == "rx" and v["src"] == OWN[sender] for _, v in inside) and played >= 1 and frames > 0


def part_echo(timeline, copies, out=print, *, expect="bug"):
    span = window(copies, 1, 1)
    count = sum(c["receiver"] == 1 for c in copies)
    out(f"echo: the parrot sent radio 1's call back to radio 1 as {count} bursts, on air {span[0]}..{span[1]} us, "
        f"record bytes unchanged (source ID 3100001)")
    control = plays(timeline, 2, 1, window(copies, 2, 1), out, " (bystander)")
    require(control, "echo INCOMPLETE the bystander did not play the parrot's copy; the copy is not playable")
    played = plays(timeline, 1, 1, span, out, " (the caller)")
    observed = "fixed" if played else "bug"
    require(observed == expect, f"echo FAIL expected {expect}, observed {observed} (caller echo bug)")
    out("echo PASS radio 1 plays the parrot's echo of its own call" if played else
        "echo PASS reproduced caller echo bug: radio 1 does not play the parrot's echo of its own call")


class ParrotReplay(replay.Replay):
    """A replay whose world has the recorded run's parrot: the recording has no field for it (schema v2), so the
    check configures it from the scenario, as the live run did."""

    def __init__(self, recording, parrot, **options):
        self.parrot = parrot
        super().__init__(recording, **options)

    def make_world(self):
        world = super().make_world()
        response = world.api.dispatch({**self.parrot, "version": 1, "command": "parrot_config"})
        if not response["ok"]:
            world.close()
            raise replay.Refused(f"REPLAY_WORLD_REFUSED: {response['error']}")
        return world


def part_replay(folder, executable, runtime, out=print):
    parrot = json.loads(ECHO.read_text())["air"]["parrot"]
    lines = []
    tool = ParrotReplay(folder, parrot, executable=str(executable), ratio="max", runtime=runtime,
                        out=lines.append)
    result = tool.run()
    for line in lines:
        out(f"  {line}")
    recorded = sum(1 for line in (Path(folder) / "recording/air-deliveries.jsonl").read_text().splitlines()
                   if '"element"' in line)
    require(recorded, "replay FAIL the recording holds no parrot copies")
    require(result["same"], f"replay FAIL {result['first_difference']}")
    # Negative control: the same replay without the parrot must differ, so the parrot's copies are really compared
    plain = replay.Replay(folder, executable=str(executable), ratio="max", runtime=runtime, out=lambda line: None)
    control = plain.run()
    out(f"  replay without the parrot (negative control): same={control['same']}, first difference "
        f"{control['first_difference']}")
    require(not control["same"], "replay FAIL a replay without the parrot is the same run; the copies are unchecked")
    out(f"replay PASS the recorded run (with its {recorded} parrot copies) replays byte for byte in new processes")


def part_mixed(fixed, executable, runtime, out=print):
    _, timeline, copies = live_run(MIXED, executable, runtime, executables=(str(fixed), None, None),
                                   name="parrot-294-mixed", out=out)
    out(f"mixed: radio 1 runs {fixed}; radios 2 and 3 run {executable}")
    failures = []
    # The repeater's echo of radio 1's own bursts: the part after radio 1's TX must reach it and stay unplayed
    tail = window(copies, 1, 1, "echo")
    keyed = [t for t, v in timeline[1] if v["state"] in ("tx", "tx_ending", "tx_wait")]
    tx_end = max(t for t in keyed if t < tail[1])
    before, inside = during(timeline, 1, (tx_end, tail[1] + 400_000), settle_us=0)
    after = inside[-1][1]
    out(f"  radio 1 (fixed) after its TX, {tx_end}..{tail[1] + 400_000} us (repeater echo tail): dmr.state/call.src "
        + ", ".join(sorted({f"{v['state']}/{v['src']}" for _, v in inside}))
        + f"; call.rx_calls +{after['rx_calls'] - before['rx_calls']}, call.rx_calls_played "
        f"+{after['played'] - before['played']}")
    if after["rx_calls"] == before["rx_calls"]:
        failures.append("mixed INCOMPLETE the repeater echo tail never reached radio 1's call stack")
    if after["played"] != before["played"] or any(v["state"] == "rx" for _, v in inside):
        failures.append("mixed FAIL radio 1 (fixed) played the repeater's immediate echo of its own call")
    if not plays(timeline, 1, 1, window(copies, 1, 1), out, " (fixed, own parrot echo)"):
        failures.append("mixed FAIL radio 1 (fixed) does not play the parrot's echo of its own call")
    if plays(timeline, 2, 2, window(copies, 2, 2), out, " (unfixed, own parrot echo)"):
        failures.append("mixed FAIL radio 2 (unfixed) plays its own parrot echo: the build mix did not happen")
    for sender in (1, 2):
        if not plays(timeline, 3, sender, window(copies, 3, sender), out, f" (bystander, radio {sender}'s copy)"):
            failures.append(f"mixed INCOMPLETE the bystander did not play the parrot's copy of radio {sender}")
    if failures:
        raise CheckFailure("; ".join(failures))
    out("mixed PASS fixed radio 1 plays its parrot echo and ignores the repeater's echo; unfixed radio 2 does "
        "not play its own; the bystander plays both")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--part", choices=("echo", "replay", "mixed", "all"), default="all")
    parser.add_argument("--expect", choices=("bug", "fixed"), default="bug",
                        help="expected echo behavior (default: bug)")
    parser.add_argument("--executable", default=Config().executable, help="the build under test (unfixed for mixed)")
    parser.add_argument("--fixed", help="the fixed build for radio 1 in --part mixed")
    parser.add_argument("--runs", type=int, default=1, help="run the echo part this many times")
    parser.add_argument("--keep", type=Path, help="copy the (last) echo run's world folder here")
    args = parser.parse_args(argv)
    failed = []
    try:
        if args.part in ("mixed", "all") and (not args.fixed or not Path(args.fixed).is_file()):
            print("parrot_check: SKIP mixed: no fixed build; pass --fixed PATH to an existing fixed executable")
            return 2
        if not Path(args.executable).is_file():
            raise FileNotFoundError(f"{args.executable}; build it (make -j8 BOARD=vhost)")
        with tempfile.TemporaryDirectory(prefix="rf-parrot-") as runtime:
            if args.part in ("echo", "replay", "all"):
                for run in range(1, args.runs + 1):
                    folder, timeline, copies = live_run(ECHO, args.executable, runtime, name=f"parrot-294-{run}")
                    if args.part in ("echo", "all"):
                        try:
                            part_echo(timeline, copies, expect=args.expect)
                        except CheckFailure as error:
                            print(str(error), flush=True)
                            failed.append(str(error))
                if args.keep:
                    shutil.copytree(folder, args.keep)
                    print(f"kept the run's world folder in {args.keep}")
                if args.part in ("replay", "all"):
                    part_replay(folder, args.executable, runtime)
            if args.part in ("mixed", "all"):
                part_mixed(args.fixed, args.executable, runtime)
        if failed:
            print(f"parrot_check: FAIL --part {args.part}: {len(failed)} of {args.runs} echo runs failed", flush=True)
            return 1
        print(f"parrot_check: PASS --part {args.part}")
        return 0
    except CheckFailure as error:
        print(str(error), flush=True)
        return 1
    except (OSError, ValueError, ChannelError, WorldError, replay.Refused) as error:
        print(f"parrot_check: INCOMPLETE: {type(error).__name__}: {error}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
