#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Compare real firmware at 1 ms and the stated lookahead, at ratio 1 and max.

Build first: make -j8 BOARD=vhost
Run: python3 tools/conductor/checks/lookahead_check.py
Negative control: append --negative-control (120 ms; must exit 1, never 0).
Exit 2 means a missing prerequisite or incomplete coverage, not a negative-control pass.
The scenario remains unproven until all four complete traces match. No trace lines
are filtered. Ratio 1 keeps ADR 0005's independent 1 ms pacing cap; each run prints
its largest actual grant. Reboot must preserve flash; restart is not a substitute.
"""

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.conductor.channel import Channel, Kind
from tools.conductor.conductor import install_parts, scenario_config
from tools.conductor.parts.air import Record
from tools.conductor.parts.lookahead import VDMR_LOOKAHEAD_US
from tools.conductor.world import Config, World, WorldError
from tools.rfoundry_debug import DMR_DATA, frame

SCENARIO = ROOT / "tools/conductor/scenarios/lookahead.json"


def compare(reference, candidate, label):
    for radio in sorted(reference):
        left, right = reference[radio], candidate[radio]
        if not left or not right:
            raise WorldError(f"LOOKAHEAD_EMPTY_TRACE radio={radio}")
        if left == right:
            continue
        offset = next((i for i, (a, b) in enumerate(zip(left, right)) if a != b), min(len(left), len(right)))
        line = left[:offset].count(b"\n") + 1
        raise WorldError(f"LOOKAHEAD_TRACE_DIFFERENCE {label} radio={radio} byte={offset} line={line} "
                         f"reference={left[offset:offset + 80]!r} candidate={right[offset:offset + 80]!r}")


def coverage(traces, replies, phases, last_us, boundary, rebooted, data):
    rx = traces[2]
    required = (b"kind=voice_lc_header", b"kind=voice_A", b"kind=terminator_lc",
                b"kind=data_header", b"kind=rate12_data")
    for kind in required:
        if not any(b"BOARD dmr.rx " in line and kind in line for line in rx.splitlines()):
            raise WorldError(f"LOOKAHEAD_MISSING_RX {kind.decode()}")
    # A successful send reply and the receiver's actual inbox rule out an empty data test.
    inbox = b"\0" + struct.pack("<IIBB", 3100001, 3100002, 0, 3) + data["sms_text"].encode("ascii")
    if frame(DMR_DATA | 0x80, 1, b"\0") not in replies[1] or frame(DMR_DATA | 0x80, 2, inbox) not in replies[2]:
        raise WorldError("LOOKAHEAD_MISSING_DATA_EXCHANGE")
    radio = data["reboot"]["radio"]
    if not rebooted or (radio, 1) not in phases or (radio, 2) not in phases:
        raise WorldError("LOOKAHEAD_MISSING_REBOOT")
    if phases[radio, 1] == phases[radio, 2]:
        raise WorldError("LOOKAHEAD_REBOOT_DID_NOT_MOVE_PHASE")
    if not last_us:
        raise WorldError("LOOKAHEAD_MISSING_LAST_US_BURST")
    if not boundary:
        raise WorldError("LOOKAHEAD_MISSING_BOUNDARY_INPUT")


def run(grant_us, ratio, *, scenario=SCENARIO, executable=Config().executable, channel_factory=Channel):
    data = json.loads(Path(scenario).read_text())
    config = scenario_config(scenario, radios=data["radios"], executable=executable)
    # Submit at each scheduled boundary so a reboot cannot discard future inputs.
    events = sorted([(e.time_us, e) for e in config.inputs] + [(data["reboot"]["time_us"], None)],
                    key=lambda item: item[0])
    world = World(replace(config, inputs=()), channel_factory=channel_factory)
    traces = {r: bytearray() for r in world.boots}
    replies = {r: bytearray() for r in world.boots}
    phases = {}
    last_us = boundary = rebooted = False
    previous = largest = 0
    began = time.monotonic()
    label = f"grant_us={grant_us} ratio={ratio}"
    try:
        install_parts(world)
        reply = world.api.dispatch({**data["air"], "version": 1, "command": "air_config",
                                    "lookahead_proven": grant_us == VDMR_LOOKAHEAD_US})
        if not reply["ok"]:
            raise WorldError(reply["error"])
        # Only this check may probe an unsafe cap. Production config has no arbitrary override.
        world.air_lookahead.grant_us = grant_us
        world.add_radio_args(lambda r: ("--dmr-phase-us", str(data["phase_us"][str(r)])))
        world.add_grant_limit(lambda now: events[0][0] if events and events[0][0] > now else None)

        def trace(radio, message):
            traces[radio].extend(message.payload + b"\n")
            if b"BOARD dmr.slot " in message.payload:
                phases.setdefault((radio, world.boots[radio]), message.time_us % 30_000)

        def outbox(radio, message):
            nonlocal last_us
            start = Record.decode(message.payload).t_us + world.radios[radio].origin
            last_us |= start == world.now - 1

        def barrier(target, outboxes):
            nonlocal previous, largest
            largest = max(largest, target - previous)
            previous = target

        world.on_outbox(Kind.TRACE, trace)
        world.on_outbox(Kind.BYTES_OUT, lambda r, m: replies[r].extend(m.payload))
        world.on_outbox(Kind.AIR_OUT, outbox)
        world.on_grant_done(barrier)
        world.start()
        world.run(ratio)
        while world.state != "Ended":
            while events and events[0][0] == world.now:
                _, event = events.pop(0)
                if event is None:
                    radio = data["reboot"]["radio"]
                    world.reboot(radio)
                    rebooted = world.boots[radio] == 2 and world.radios[radio].origin == world.now
                else:
                    world.submit(event.radio, event.kind, event.payload, event.source,
                                 time_us=event.time_us, internal=True)
                    boundary |= event.time_us == data["boundary_input_us"]
            world.advance()
        if world.reason != "WORLD_END_TIME":
            raise WorldError(world.reason)
        coverage(traces, replies, phases, last_us, boundary, rebooted, data)
        return {r: bytes(trace) for r, trace in traces.items()}
    finally:
        world.close()
        print(f"{label} wall_s={time.monotonic() - began:.6f} largest_grant_us={largest}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", default=Config().executable)
    parser.add_argument("--negative-control", action="store_true")
    args = parser.parse_args(argv)
    if not Path(args.executable).is_file():
        print(f"LOOKAHEAD_MISSING_EXECUTABLE {args.executable}; run make -j8 BOARD=vhost", flush=True)
        return 2
    try:
        if args.negative_control:
            candidate = run(120_000, "max", executable=args.executable)
            reference = run(1000, "max", executable=args.executable)
            compare(reference, candidate, "grant_us=120000")
            print("LOOKAHEAD_NEGATIVE_CONTROL_NOT_DETECTED")
            return 0
        results = []
        errors = []
        for grant, ratio in ((1000, 1), (VDMR_LOOKAHEAD_US, 1), (1000, "max"), (VDMR_LOOKAHEAD_US, "max")):
            try:
                traces = run(grant, ratio, executable=args.executable)
                if results:
                    compare(results[0], traces, f"grant_us={grant} ratio={ratio}")
                results.append(traces)
            except WorldError as error:
                errors.append(str(error))
                print(f"grant_us={grant} ratio={ratio} {error}", flush=True)
        if errors:
            print("LOOKAHEAD_UNPROVEN: keep lookahead_proven=false (1000 us)")
            return 1
        for radio in sorted(results[0]):
            print(f"radio={radio} sha256 " + " ".join(hashlib.sha256(result[radio]).hexdigest() for result in results))
        return 0
    except (OSError, WorldError) as error:
        print(str(error), flush=True)
        # Missing sockets, firmware, reboot or workload coverage cannot validate a control.
        return 1 if re.search(r"WORLD_BAD_AIR_TIME|VCHAN_BAD_AIR_TIME|LOOKAHEAD_TRACE_DIFFERENCE", str(error)) else 2


if __name__ == "__main__":
    raise SystemExit(main())
