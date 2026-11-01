#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Hypothesis test: does a radio restart make a new SMS look like a duplicate?

Build: make -j8 BOARD=vhost. Run: python3 tools/conductor/checks/sms_restart_check.py [--runs 3] [--executable PATH]
Exit 0 means the run finished and the verdict was printed (CONFIRMED or REFUTED). Exit 1 means a run differed from
the others. Exit 2 means a prerequisite was missing or the run could not finish (UNTESTABLE).

The idea. A sends text to B. The sender numbers each message (smsSeq, core/dmr/dmrDataService.c:110). That number
lives in RAM, so a restarted radio counts from the start again (the first message of any boot has seq 1,
:960-961). A receiver that sees the same sender, receiver, seq and text within 60 s (:29) calls it a retry: it
sends the ACK again but does not keep it or count it (:2321-2335).

The world is tools/conductor/scenarios/sms_restart.json, the two-radio world of interaction_check.py:
  2 s   radio 1 (A, ID 3100001) sends "OK 7" to radio 2 (B, ID 3100002), ACK requested, through the debug link
        as a controller with a lease and its grant secret.
  10 s  radio 1 restarts (world fault kind restart: the radio process ends and a new one starts, RAM is new).
  20 s  radio 1 sends the same text again (a new lease, because the restart ends the old one).
Views are read after each step from observer sessions (host cache, never seen by the firmware): section 8
(data: inbox_count, sms_state) and section 11 (data stats: sms_received, sms_duplicates, sms_sent, sms_acked).
The control run sends different text ("OK 8") at 20 s. It must be received.

Each scenario runs --runs times in new processes. The sha256 of every radio trace must be the same in all runs.
"""

import argparse
import hashlib
import json
import shutil
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.conductor.channel import ChannelError  # noqa: E402
from tools.conductor.conductor import install_parts, scenario_config  # noqa: E402
from tools.conductor.parts import dlcache  # noqa: E402
from tools.conductor.world import Config, World, WorldError  # noqa: E402
from tools.conductor.checks.interaction_check import (  # noqa: E402
    DMR_DATA_STATS, OP_SMS, CheckFailure, read_view, require, sms_frame, trace_hashes)
from tools.conductor.parts import dlmux  # noqa: E402

SCENARIO = ROOT / "tools/conductor/scenarios/sms_restart.json"
NAME = "sms-restart"
MODE = "restart"     # --mode: restart (power cut, fresh startup storage) or reboot (flash kept)
DMR_DATA_VIEW = 8      # GET_VIEW section DL_VIEW_DMR_DATA (core/debuglink/debuglink.h)
KEYS = ("data.inbox_count", "data.sms_state", "data.sms_received", "data.sms_duplicates", "data.sms_sent",
        "data.sms_acked", "data.sms_failed")


def read_radio(world, observer):
    values = {}
    for section in (DMR_DATA_VIEW, DMR_DATA_STATS):
        values.update(read_view(world, observer, section))
    return {key: values.get(key) for key in KEYS}


def live_run(executable, runtime, text2):
    """One run in a new world. Returns the world folder and what was read."""
    data = json.loads(SCENARIO.read_text())
    world = World(scenario_config(SCENARIO, radios=data["radios"], executable=executable))
    try:
        install_parts(world)
        response = world.api.dispatch({**data["air"], "version": 1, "command": "air_config"})
        if not response["ok"]:
            raise WorldError(response["error"])
    except BaseException:
        world.close()
        raise
    sends = [dict(s) for s in data["sms"]]
    sends[1]["text"] = text2
    restart, views = data["restart"], data["views"]
    actions = sorted({s["time_us"] for s in sends} | {restart["time_us"]} | {v["time_us"] for v in views})
    world.add_grant_limit(lambda now: next((t for t in actions if t > now), None))
    seen = {"views": [], "sends": []}
    try:
        dlcache.get(world, cadence_us=dlcache.scenario_cadence(SCENARIO))
        world.api.bind(NAME, runtime)
        world.start()
        world.run("max")
        if MODE == "restart":       # a power cut: the process ends, startup storage is loaded again
            reply = world.api.dispatch({"version": 1, "command": "fault", "kind": "restart", "radio": restart["radio"],
                                        "time_us": restart["time_us"]})
            require(reply["ok"], f"restart fault refused: {reply.get('error')}")
        rebooted = MODE != "reboot"
        principal = "debug:sms-restart"
        observers = {radio: world.dlmux.connect(radio, "debug:sms-restart-view", "observer") for radio in (1, 2)}
        pending, controller = list(sends), None
        todo = list(views)
        while world.state != "Ended":
            now = world.now
            if not rebooted and now >= restart["time_us"]:
                # A firmware reboot that keeps flash (World.reboot), as a real radio's reboot does
                world.reboot(restart["radio"])
                rebooted = True
                continue
            if pending and now >= pending[0]["time_us"]:
                send = pending.pop(0)
                if send["radio"] in world.dlmux.leases.held:       # the first lease is still held: hand it back
                    world.dlmux.leases.revoke(send["radio"], "LEASE_RELEASED")
                lease = world.dlmux.leases.grant(send["radio"], principal)
                controller = world.dlmux.connect(send["radio"], principal, "controller", lease["generation"],
                                                 lease["secret"])
                world.dlmux.feed(controller, sms_frame(send))
                seen["sends"].append((now, send["name"], send["text"], controller))
            if todo and now >= todo[0]["time_us"]:
                view = todo.pop(0)
                seen["views"].append((now, view["name"], read_radio(world, observers[1]),
                                      read_radio(world, observers[2])))
            world.api.poll(0)
            world.advance()
        seen["replies"] = [(t, name, text, [(r[0], r[1], r[3][:1].hex())
                                            for r in dlmux.Parser(dlmux.REPLY_MAX).feed(c.take())])
                           for t, name, text, c in seen.pop("sends")]
        if world.reason != "WORLD_END_TIME":
            raise WorldError(world.reason)
    finally:
        world.close()
    folder = Path(runtime) / "rfoundry-vr" / NAME
    return folder, seen, world.now


def one_scenario(label, executable, text2, runs, out, keep=None):
    results = []
    for number in range(1, runs + 1):
        with tempfile.TemporaryDirectory(prefix="rf-smsr-") as runtime:
            folder, seen, end = live_run(executable, runtime, text2)
            traces = trace_hashes(folder)
            if keep and number == 1:        # the recording of the first run, as a world folder
                shutil.copytree(folder / "recording", Path(keep) / label, dirs_exist_ok=True)
        summary = {"views": seen["views"], "replies": seen["replies"], "end": end}
        results.append((traces, summary))
        out(f"[{label}] run {number}: end {end} us; " + " ".join(f"{r} {s}" for r, s in traces.items()))
    same = all(r == results[0] for r in results)
    out(f"[{label}] {runs} runs identical (trace sha256 and every view read): {same}")
    return same, results[0][1]


def show(label, summary, out):
    for t, name, text, replies in summary["replies"]:
        out(f"[{label}] {name} send of {text!r} at {t} us: debug link reply "
            + ", ".join(f"cmd 0x{c:02x} status {s}" for _, c, s in replies))
    for t, name, a, b in summary["views"]:
        out(f"[{label}] read {name} at {t} us")
        out(f"[{label}]   A (radio 1): " + " ".join(f"{k[5:]}={v}" for k, v in a.items()))
        out(f"[{label}]   B (radio 2): " + " ".join(f"{k[5:]}={v}" for k, v in b.items()))


def verdict(same, hyp, ctl):
    if not same:
        return "NOT REPEATABLE"
    a, b = hyp["views"][-1][2], hyp["views"][-1][3]
    cb = ctl["views"][-1][3]
    first_b = hyp["views"][0][3]
    require(first_b["data.sms_received"] == "1", "first message was not received by B; scenario is broken")
    dropped = b["data.sms_received"] == "1" and b["data.sms_duplicates"] == "1" and b["data.inbox_count"] == "1"
    acked = a["data.sms_acked"] == "1"
    control_ok = cb["data.sms_received"] == "2" and cb["data.sms_duplicates"] in ("0", None)
    require(control_ok, f"control failed: B should receive the different text, got {cb}")
    if dropped and acked:
        return "CONFIRMED"
    return "REFUTED"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--executable", default=Config().executable)
    parser.add_argument("--keep", help="copy the first run's recording of each scenario under this folder")
    parser.add_argument("--mode", choices=("restart", "reboot"), default="restart",
                        help="restart: a power cut with fresh startup storage; reboot: a firmware reboot that keeps flash")
    args = parser.parse_args(argv)
    global MODE
    MODE = args.mode
    try:
        if not Path(args.executable).is_file():
            raise FileNotFoundError(f"{args.executable}; run make -j8 BOARD=vhost")
        data = json.loads(SCENARIO.read_text())
        same_h, hyp = one_scenario("hypothesis", args.executable, data["sms"][1]["text"], args.runs, print, args.keep)
        show("hypothesis", hyp, print)
        same_c, ctl = one_scenario("control", args.executable, data["control_text"], args.runs, print, args.keep)
        show("control", ctl, print)
        result = verdict(same_h and same_c, hyp, ctl)
        print(f"sms_restart_check: {result}")
        return 0 if result in ("CONFIRMED", "REFUTED") else 1
    except CheckFailure as error:
        print(f"sms_restart_check: UNTESTABLE: {error}", flush=True)
        return 2
    except (OSError, ValueError, ChannelError, WorldError) as error:
        print(f"sms_restart_check: UNTESTABLE: {type(error).__name__}: {error}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
