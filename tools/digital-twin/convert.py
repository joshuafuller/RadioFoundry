# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Convert a version 1 research scenario to a schema version 2 scenario, or refuse it with named reasons.

ADR 0008 (decision 7): version 1 files are read only through this converter. The mapping is the one in the record
and replay brief (virtual-radio/assets/10-brief.md, section 3.3):

  - at_ms becomes t_us = at_ms x 1000. Every input gets a full stamp: round 0, its class, a sequence number in key
    order, source "scenario". Inside one µs the order is the class table, then the version 1 list order;
  - phase before_tick maps to nothing (inputs land before the tick); phase after_tick adds 1 µs (flagged);
  - key press becomes the key's bit down at t and up at t + 80 ms; key long becomes down at t and up at
    t + 600 + 120 ms (INPUT_INJECT_PRESS_MS, INPUT_LONG_MS, INPUT_INJECT_LONG_EXTRA_MS, core/input/input.h:11,47-48).
    The bit comes from the virtual board's key map (boards/vhost/keypad_vhost.c). A key with no bit is refused;
  - the splash trap: the UI drops every key pressed during its 1 s splash (core/ui/ui.c:36,106-108). The old worker
    had no splash, so every key or PTT edge within 1 s of a boot (time 0 or a restart) is flagged, and its expected results must be set again;
  - ptt becomes edges of the PTT key bit; serial_input becomes host port bytes; udp stays a network input;
  - screen events go (FRAME checkpoints replace them; flagged);
  - world faults (restart, host_link, link, loss, corrupt, duplicate, delay, flash_cut, flash_corrupt) keep their
    meaning as class 10 events;
  - every other action called a firmware function or a research model control directly, with no input-level
    equivalent: it is refused with its reason, and a person rewrites it as keys, debug link bytes or storage steps.

The output is a version 2 *scenario*: no build and no end yet, because no run has pinned a binary. The runner pins
them when it records. Flags (changes a person should read) go to stderr, never into the file.

Usage: convert.py NAME|FILE [-o OUT]   one scenario (a scenarios.py name or a version 1 JSON file)
       convert.py --all                one line per scenario in scenarios.py
Exit status: 0 converted, 2 refused (or any refused with --all), 1 bad input.
"""
import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

import scenario as schema

ROOT = Path(__file__).resolve().parents[2]
KEYPAD = ROOT / "boards/vhost/keypad_vhost.c"
DISPLAY = ROOT / "boards/vhost/display_profiles_vhost.c"
FLASH = ROOT / "boards/vhost/flash_vhost.c"
VIEW_HEADER = ROOT / "core/debuglink/debuglink.h"

PRESS_US = 80 * 1000			# INPUT_INJECT_PRESS_MS (core/input/input.h:47)
LONG_US = (600 + 120) * 1000		# INPUT_LONG_MS + INPUT_INJECT_LONG_EXTRA_MS (core/input/input.h:11,48)
SPLASH_US = 1000 * 1000			# SPLASH_MS (core/ui/ui.c:36)
EPOCH = "2026-01-01T00:00:00Z"		# a fixed world epoch: version 1 had none
# Version 1 logical keys are hal_input.h actions 1..8 (scenario.py KEY_ACTIONS, hal/hal_input.h:13-14)
KEY_ACTION_NAMES = {"up": "ACT_NAV_UP", "down": "ACT_NAV_DOWN", "left": "ACT_NAV_LEFT", "right": "ACT_NAV_RIGHT",
                    "select": "ACT_SELECT", "back": "ACT_BACK", "quick": "ACT_QUICK", "context": "ACT_CONTEXT"}
WORLD_FAULTS = ("restart", "host_link", "link", "loss", "corrupt", "duplicate", "delay", "flash_cut", "flash_corrupt")
# Actions with no input-level equivalent, and why (scenario.py executor: each sends a worker command that calls the
# firmware or a research model directly)
REFUSED_ACTIONS = {
    "tune": "calls the radio core directly; rewrite as keys or a debug link request",
    "brightness": "sets the backlight by a direct call; rewrite as keys",
    "settings_save": "saves settings by a direct call; rewrite as keys that change a setting",
    "storage_reload": "reloads storage by a direct call; rewrite as a restart",
    "channel_rename": "edits the codeplug by a direct call; rewrite as keys or start storage steps",
    "channel_queue": "queues a codeplug edit by a direct call; rewrite as keys or start storage steps",
    "channel_read": "reads the codeplug by a direct call; use an assertion or a debug link request",
    "queue_limits": "sets firmware queue limits by a direct call; rewrite as start storage steps or a build option",
    "ppp_start": "starts PPP by a direct call; rewrite as host port bytes",
    "watchdog_starve": "starves the watchdog by a direct call; no world fault does this yet",
    "sync_loss": "a research modem model control; the virtual board has no such fault yet",
    "miss_tx_done": "a research modem model control; the virtual board has no such fault yet",
    "refuse_tx": "a research modem model control; the virtual board has no such fault yet",
    "stall": "a research worker control; the virtual radio's stall watch ends a spinning run instead",
    "repeater_available": "a research repeater model control; version 2 has no repeater model yet",
    "repeater_power": "a research repeater model control; version 2 has no repeater model yet",
    "repeater_data": "a research repeater model input; version 2 has no repeater model yet",
    "repeater_voice": "a research repeater model input; version 2 has no repeater model yet",
    "site": "a research roaming site model input; version 2 has no site model yet",
    "site_data": "a research roaming site model input; version 2 has no site model yet",
    "site_voice": "a research roaming site model input; version 2 has no site model yet",
}
REFUSED_RADIO_FIELDS = {
    "host_network": "the research worker's host network has no version 2 world equivalent yet",
    "hotspot_schedule": "a fixed research schedule; the firmware's own kernel picks task order (ADR 0002)",
    "task_schedule": "a fixed research schedule; the firmware's own kernel picks task order (ADR 0002)",
    "color_code": "codeplug configuration; rewrite as start storage steps",
    "talkgroup": "codeplug configuration; rewrite as start storage steps",
    "timeslot": "codeplug configuration; rewrite as start storage steps",
    "repeater_mode": "codeplug configuration; rewrite as start storage steps",
    "confirmed_ip": "codeplug configuration; rewrite as start storage steps",
    "worker": "pins a research worker by path; version 2 names the build by hash only",
    "profile": "pins a research board profile; version 2 runs the virtual board",
    "display": "a research display; pick a virtual board display profile instead",
    "flash_map": "a research flash map; the virtual board's flash map applies",
}


class Refused(Exception):
    """The scenario cannot be converted. args[0] is the list of named reasons."""


def board_profile():
    """The virtual board as version 2 names it: id, default display, flash size, key map identity and key bits."""
    source = KEYPAD.read_text()
    names = re.search(r"enum\s*\{([^}]*K_SELECT[^}]*)\}", source).group(1)
    bits = {name.strip(): index for index, name in enumerate(names.split(",")) if name.strip()}
    table = re.search(r"vboard_keymap\[\]\s*=\s*\{(.*?)\n\};", source, re.S).group(1)
    rows = re.findall(r"\{\s*(K_\w+),\s*(ACT_\w+),\s*(ACT_\w+)[^}]*\}", table)
    actions = {}
    for key, short, _long in rows:
        actions.setdefault(short, bits[key])
    keymap = "\n".join(" ".join(row.split()) for row in re.findall(r"\{[^}]*\}", table))
    display = re.search(r'#define\s+VDISPLAY_DEFAULT_PROFILE\s+"([^"]+)"', DISPLAY.read_text()).group(1)
    flash = int(re.search(r"#define\s+FLASH_SIZE\s+(0x[0-9a-fA-F]+)u?", FLASH.read_text()).group(1), 16)
    view = int(re.search(r"#define\s+DL_VIEW_VERSION\s+(\d+)", VIEW_HEADER.read_text()).group(1))
    return {"board": {"id": "vhost", "display": display, "flash_size": flash,
                      "keymap_sha256": hashlib.sha256(keymap.encode()).hexdigest()},
            "actions": actions, "view_version": view}


def _us(name):
    return name[:-3] + "_us" if name.endswith("_ms") else name


def convert(v1):
    """Return (version 2 scenario, flags). Raise Refused with every reason found."""
    schema.validate(v1)
    v1 = copy.deepcopy(v1)
    profile = board_profile()
    reasons, flags = [], []
    for field in ("repeater",):
        if field in v1:
            reasons.append(f"field {field}: a shared research repeater; version 2 has no repeater model yet")
    if "integrated_core" in v1:
        flags.append(f"integrated_core {v1['integrated_core']} dropped: the virtual radio always runs the whole firmware")
    storage = v1.get("codeplug_fixture", "blank")
    flags.append(f"storage: every radio starts from fixture {storage!r}")
    flags.append(f"epoch_utc: version 1 had none; set to {EPOCH}")
    radios = []
    for radio in v1["radios"]:
        for field, why in REFUSED_RADIO_FIELDS.items():
            if field in radio:
                reasons.append(f"radio {radio['id']} field {field}: {why}")
        if "flash_size" in radio:
            if radio["flash_size"] > profile["board"]["flash_size"]:
                reasons.append(f"radio {radio['id']} flash_size {radio['flash_size']:#x}: larger than the virtual board")
            else:
                flags.append(f"radio {radio['id']} flash_size {radio['flash_size']:#x} dropped: the virtual board has "
                             f"{profile['board']['flash_size']:#x}")
        out = {"id": radio["id"], "dmr_id": radio["dmr_id"], "board": dict(profile["board"]), "storage": storage}
        if "clock" in radio:
            out["clock"] = dict(radio["clock"])
        radios.append(out)
    ptt_bit = profile["actions"].get("ACT_PTT")
    pending = []	# (t_us, class, list index, edge order, event)
    held = {}		# (radio, bit) -> (down time, up time, label) of the last press
    boots = {}		# radio -> virtual µs of its last boot: 0, then each restart (the splash runs at every boot)
    for index, event in enumerate(v1["events"]):
        action, at = event["action"], event["at_ms"] * 1000
        where = f"event {index} ({action} at {event['at_ms']} ms)"
        if event.get("phase") == "after_tick":
            at += 1
            flags.append(f"{where}: phase after_tick becomes +1 µs, so it lands just after the tick")
        stamp = {"radio": event["radio"]}
        extra = {key: event[key] for key in ("id", "after") if key in event}
        if action in REFUSED_ACTIONS:
            reasons.append(f"{where}: {REFUSED_ACTIONS[action]}")
            continue
        if action == "screen":
            flags.append(f"{where}: dropped; FRAME checkpoints are written anyway")
            continue
        if action in ("key", "ptt"):
            if action == "key":
                bit = profile["actions"].get(KEY_ACTION_NAMES[event["key"]])
                if bit is None:
                    reasons.append(f"{where}: key {event['key']!r} ({KEY_ACTION_NAMES[event['key']]}) has no bit in "
                                   "the virtual board's key map")
                    continue
                hold = PRESS_US if event["gesture"] == "press" else LONG_US
                edges = [(at, True), (at + hold, False)]
                label = f"key {event['key']}"
                flags.append(f"{where}: {event['key']} {event['gesture']} becomes bit {bit} down at {at} µs, up at "
                             f"{at + hold} µs (the old path skipped the input task)")
            else:
                bit, label = ptt_bit, "ptt"
                edges = [(at, event["down"])]
            boot = boots.get(event["radio"], 0)
            if at < boot + SPLASH_US:
                flags.append(f"{where}: SPLASH TRAP: the UI drops keys pressed during its 1 s splash after the boot "
                             f"at {boot} µs (core/ui/ui.c:106-108); set the expected results again" +
                             ("; a PTT edge may still reach the radio core (core/input/input.h:16)"
                              if action == "ptt" else ""))
            edge_key = (event["radio"], bit)
            last = held.get(edge_key)
            if action == "key" and last is not None and last[1] is not None and at <= last[1]:
                reasons.append(f"{where}: bit {bit} is still held by {last[2]} until {last[1]} µs")
                continue
            if action == "ptt":
                is_down = last is not None and last[1] is None
                if is_down == event["down"]:
                    reasons.append(f"{where}: ptt is already {'down' if event['down'] else 'up'}")
                    continue
                held[edge_key] = (at, None if event["down"] else at, label)
            else:
                held[edge_key] = (at, at + hold, label)
            for order, (t, down) in enumerate(edges):
                ids = extra if order == 0 else ({"id": extra["id"] + "-up", "after": [extra["id"]]}
                                                if "id" in extra else {})
                pending.append((t, 30, index, order, {**stamp, "key": {"bit": bit, "down": down}, **ids}))
            continue
        if action == "serial_input":
            pending.append((at, 30, index, 0, {**stamp, "host": {"stream": "host-port", "hex": event["hex"]}, **extra}))
            continue
        if action == "udp":
            payload = {key: event[key] for key in ("destination", "port", "payload_hex")}
            pending.append((at, 30, index, 0, {**stamp, "udp": payload, **extra}))
            continue
        if action == "restart":
            boots[event["radio"]] = at
        if action in WORLD_FAULTS:
            skip = {"at_ms", "radio", "action", "phase", "id", "after"}
            payload = {_us(key): value for key, value in event.items() if key not in skip}
            if "delay_us" in payload:
                payload["delay_us"] *= 1000
            pending.append((at, 10, index, 0, {**stamp, action: payload, **extra}))
            continue
        reasons.append(f"{where}: no version 2 mapping")
    duration = v1["duration_ms"] * 1000
    for t, *_rest in pending:
        if t >= duration:
            reasons.append(f"an expanded key release at {t} µs falls after the end at {duration} µs")
            break
    if reasons:
        raise Refused(reasons)
    events = []
    for seq, (t, cls, _index, _order, body) in enumerate(sorted(pending, key=lambda item: item[:4])):
        kind = next(key for key in body if key in schema.EVENT_KINDS)
        event = {"t_us": t, "round": 0, "class": cls, "seq": seq, "source": "scenario", "radio": body["radio"],
                 kind: body[kind]}
        event.update({key: body[key] for key in ("id", "after") if key in body})
        events.append(event)
    assertions = [{_us(key): value * 1000 if key.endswith("_ms") else value for key, value in assertion.items()}
                  for assertion in v1["assertions"]]
    result = {"schema_version": 2, "name": v1["name"], "tier": "local", "time_model": schema.TIME_MODEL,
              "class_table": schema.CLASS_TABLE_VERSION, "view_version": profile["view_version"],
              "seed": v1["seed"], "epoch_utc": EPOCH, "duration_us": duration, "radios": radios,
              "events": events, "checkpoints": [], "assertions": assertions}
    return schema.validate_v2(result), flags


def load(source):
    import scenarios
    if source in scenarios.SCENARIOS:
        return copy.deepcopy(scenarios.SCENARIOS[source])
    return json.loads(Path(source).read_text())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Convert a version 1 scenario to schema version 2")
    parser.add_argument("source", nargs="?", help="a scenarios.py name or a version 1 JSON file")
    parser.add_argument("-o", "--output", help="write the version 2 file here (default: stdout)")
    parser.add_argument("--all", action="store_true", help="convert every scenario in scenarios.py, one line each")
    args = parser.parse_args(argv)
    if args.all == bool(args.source):
        parser.error("give one scenario or --all")
    if args.all:
        import scenarios
        refused = 0
        for name, v1 in scenarios.SCENARIOS.items():
            try:
                result, flags = convert(v1)
                print(f"converted {name}: {len(result['events'])} events, {len(flags)} flags, "
                      "validate_v2 accepts")
            except Refused as error:
                refused += 1
                print(f"refused {name}: {len(error.args[0])} reason(s); first: {error.args[0][0]}")
        return 2 if refused else 0
    try:
        v1 = load(args.source)
        result, flags = convert(v1)
    except Refused as error:
        for reason in error.args[0]:
            print(f"refused: {reason}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for flag in flags:
        print(f"flag: {flag}", file=sys.stderr)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
