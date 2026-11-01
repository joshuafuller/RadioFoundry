#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Check a virtual radio's GNU ld --cref map and boot trace (ADR 0002).

The map records object references, not a function call graph. Pair the firmware's startup references with the
trace's CREATE and IN records: the start task must create the other tasks while it is running.
"""
import argparse
from pathlib import Path
import re
import sys

STEPS = ("radio_sm_poll", "radio_sm_tick", "ui_app_tick", "ui_app_key", "settings_poll", "storage_job_poll")
ALLOWED = ("core/", "hal/", "drivers/", "third_party/")
DRIVER_TASKS = ("boards/vhost/audio_vhost.o", "boards/vhost/dmr_vhost.o")  # board parts' deferred handler tasks, as the radios' driver glue
STARTERS = {
    "radio_core_start": "core/radio/radio_task.o",
    "input_start": "core/input/input_task.o",
    "ui_start": "core/ui/ui.o",
    "hostlink_start": "core/hostlink/hostlink.o",
}
TASKS = ["start", "radio", "input", "ui", "host"]


def cross_references(text):
    _, marker, table = text.partition("Cross Reference Table\n")
    if not marker:
        raise ValueError("map has no Cross Reference Table (link with --cref)")
    refs, symbol = {}, None
    for line in table.splitlines():
        if not line.strip() or line.startswith("#") or line.split() == ["Symbol", "File"]:
            continue
        if line[0].isspace():
            if symbol is None:
                raise ValueError("cross reference without a symbol")
            refs[symbol].append(line.strip())
        else:
            fields = line.split(None, 1)
            if len(fields) != 2 or fields[0] in refs:
                raise ValueError("invalid cross reference: " + line)
            symbol, obj = fields
            refs[symbol] = [obj]
    return refs


def check_map(text):
    refs = cross_references(text)
    app = "core/app/app.o"
    definition = refs.get("app_start", [""])[0]
    if not definition.endswith(app):
        raise ValueError("app_start is not defined by " + app)
    prefix = definition[:-len(app)]
    if prefix and not prefix.endswith("/"):
        raise ValueError("invalid build prefix: " + prefix)

    def objects(symbol):
        if symbol not in refs or len(refs[symbol]) < 2:
            raise ValueError(symbol + ": missing definition or caller")
        out = []
        for obj in refs[symbol]:
            if not obj.startswith(prefix):
                raise ValueError(symbol + ": object outside the build: " + obj)
            path = obj[len(prefix):]
            if ".." in Path(path).parts:
                raise ValueError(symbol + ": invalid object path: " + obj)
            out.append(path)
        return out

    for symbol in STEPS:
        for obj in objects(symbol):
            if not obj.startswith(ALLOWED):
                raise ValueError(symbol + ": object outside firmware directories: " + obj)
    if objects("app_start") != [app, "platform/host/main.o"]:
        raise ValueError("app_start must be called only by platform/host/main.o")
    for symbol, definition in STARTERS.items():
        if objects(symbol) != [definition, app]:
            raise ValueError(symbol + ": startup must come only from " + app)
    creators = objects("xTaskCreate")
    for obj in [app, *STARTERS.values()]:
        if obj not in creators[1:]:
            raise ValueError("xTaskCreate: missing firmware creator " + obj)
    for obj in creators:
        if not obj.startswith(ALLOWED) and obj not in DRIVER_TASKS:
            raise ValueError("xTaskCreate: object outside firmware directories: " + obj)


def check_trace(text):
    created, ran = [], set()
    boot = scheduler = ended = False
    running = None
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("#"):
            continue
        m = re.fullmatch(r"\d{12} \d{10} (.+)", line)
        if not m or ended:
            raise ValueError("trace: invalid line %d" % n)
        event = m[1]
        if event == "BOOT main":
            if boot or created or scheduler:
                raise ValueError("trace: duplicate or late BOOT main")
            boot = True
        elif not boot:
            raise ValueError("trace: event before BOOT main")
        elif event == "SCHEDULER start":
            if scheduler or created != ["start"]:
                raise ValueError("trace: scheduler must follow CREATE start")
            scheduler = True
        elif event.startswith("IN "):
            if not scheduler:
                raise ValueError("trace: task ran before the scheduler")
            running = event[3:]
            if running in TASKS:
                if running not in created:
                    raise ValueError("trace: task ran before creation: " + running)
                ran.add(running)
        elif event.startswith("CREATE "):
            m = re.fullmatch(r"CREATE (.+) prio=\d+", event)
            if not m:
                raise ValueError("trace: invalid CREATE record")
            task = m[1]
            if task not in TASKS:
                continue
            if task == "start":
                if scheduler or running is not None or created:
                    raise ValueError("trace: start must be created before the scheduler")
            elif not scheduler or running != "start":
                raise ValueError("trace: " + task + " was not created by the running start task")
            created.append(task)
            if created != TASKS[:len(created)]:
                raise ValueError("trace: task creation order differs from app_start's chain")
        elif event.startswith("END "):
            if not event.startswith("END virtual time limit ("):
                raise ValueError("trace: boot did not end at the virtual time limit")
            ended = True
    if not ended or created != TASKS or ran != set(TASKS):
        raise ValueError("trace: incomplete boot (need start, radio, input, ui and host created and run)")


def self_test():
    fixtures = Path(__file__).parent / "testdata" / "check_task_loops"
    good = (fixtures / "firmware.map.txt").read_text(encoding="utf-8")
    boot = (fixtures / "boot.trace").read_text(encoding="utf-8")
    check_map(good)
    check_map(good.replace("build/vhost/", "/tmp/ci/virtual-default/"))
    check_trace(boot)
    cases = [
        ("outside caller fixture", check_map, (fixtures / "outside.map.txt").read_text(encoding="utf-8"),
         "radio_sm_poll: object outside firmware directories: tools/harness.o"),
        ("missing cross reference table", check_map, "", "no Cross Reference Table"),
        ("missing caller", check_map, good.replace("radio_sm_tick ", "other_tick "), "radio_sm_tick: missing"),
        ("fake core path", check_map, good.replace("core/radio/radio_task.o", "tools/core/radio/radio_task.o"),
         "outside firmware directories"),
        ("parent traversal", check_map, good.replace("core/radio/radio_task.o", "core/../tools/harness.o"),
         "invalid object path"),
        ("wrong startup caller", check_map, good.replace("platform/host/main.o", "tools/harness.o"),
         "app_start must be called only"),
        ("missing firmware creator", check_map, good.replace("xTaskCreate ", "other_create "), "xTaskCreate: missing"),
        ("outside task creator", check_map, good + "                                                  tools/harness.o\n",
         "object outside the build"),
        ("missing task", check_trace, "\n".join(x for x in boot.splitlines() if "host" not in x), "incomplete boot"),
        ("wrong parent task", check_trace, boot.replace("IN start", "IN IDLE"), "not created by the running start"),
        ("wrong creation order", check_trace, boot.replace("CREATE input", "CREATE host"), "creation order differs"),
        ("duplicate task", check_trace, boot.replace("CREATE radio", "CREATE start"), "before the scheduler"),
        ("no scheduler", check_trace, boot.replace("SCHEDULER start", "BOARD init"), "before the scheduler"),
        ("truncated trace", check_trace, "\n".join(boot.splitlines()[:-1]), "incomplete boot"),
        ("failed boot", check_trace, boot.replace("END virtual time limit", "END watchdog"), "did not end"),
        ("malformed trace", check_trace, "garbage", "invalid line"),
    ]
    for name, check, data, expected in cases:
        try:
            check(data)
        except ValueError as e:
            if expected not in str(e):
                raise AssertionError("%s: wrong failure: %s" % (name, e)) from e
            print("ok %s: rejected (%s)" % (name, e))
        else:
            raise AssertionError(name + ": accepted invalid input")
    print("check_task_loops self-test PASSED (valid map/boot and %d rejection cases)" % len(cases))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--map", type=Path, help="GNU ld map built with --cref")
    ap.add_argument("--trace", type=Path, help="completed virtual radio boot trace")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.map is None or args.trace is None:
        ap.error("--map and --trace are required")
    try:
        check_map(args.map.read_text(encoding="utf-8"))
        check_trace(args.trace.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as e:
        print("check_task_loops: FAIL: " + str(e), file=sys.stderr)
        return 1
    print("check_task_loops: PASSED (6 step functions; start, radio, input, ui, host from app_start's chain)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
