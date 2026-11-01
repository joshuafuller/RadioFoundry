# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Discoverable host scenarios; saved run inputs are their replay authority."""
import copy
from hotspot_fixture import DATA_BURSTS

CALL = {"condition": "voice_event", "event": "begin", "source": 1001,
        "destination": 201, "group": True}

SCENARIOS = {
    "hotspot-active-peers": {
        "schema_version": 1, "name": "hotspot-active-peers", "seed": 1, "duration_ms": 5000,
        "integrated_core": True, "codeplug_fixture": "dmr-data",
        "radios": [{"id": name, "dmr_id": identity, "flash_size": 0x200000, "host_network": name != "a"}
                   for name, identity in (("a", 1001), ("b", 1002), ("c", 1003))],
        "events": [
            {"at_ms": 100, "radio": "a", "action": "serial_input",
             "hex": "e01a0280020a0280800100800080000080800000000000000000"},
            {"at_ms": 200, "radio": "b", "action": "udp", "destination": "225.0.0.201", "port": 6969,
             "payload_hex": b"peer traffic while hotspot owns A".hex()},
            {"at_ms": 2700, "radio": "a", "action": "host_link", "enabled": False},
            {"at_ms": 2800, "radio": "b", "action": "udp", "destination": "225.0.0.201", "port": 6969,
             "payload_hex": b"peer traffic after hotspot leaves".hex()},
        ],
        "assertions": [
            {"condition": "mmdvm_data_match", "radio": "a", "source_radio": "b",
             "from_ms": 0, "through_ms": 2600, "count": 7},
            {"condition": "state_value", "radio": "a", "at_ms": 2600, "field": "hotspot", "value": True},
            {"condition": "state_value", "radio": "a", "at_ms": 2740, "field": "hotspot", "value": False},
            {"condition": "state_value", "radio": "a", "at_ms": 2740, "field": "radio_rx_hz", "value": 433500000},
        ] + [{"condition": "udp_delivery", "radio": "c", "count": 1, "source": "12.0.3.234",
              "destination": "225.0.0.201", "source_port": 6969, "port": 6969, "payload_hex": payload.hex()}
             for payload in (b"peer traffic while hotspot owns A", b"peer traffic after hotspot leaves")]
        + [{"condition": "no_tx", "radio": name, "from_ms": 0, "through_ms": 5000} for name in ("a", "c")],
    },
    "hotspot-queued-disconnect": {
        "schema_version": 1, "name": "hotspot-queued-disconnect", "seed": 1, "duration_ms": 4000,
        "integrated_core": True, "codeplug_fixture": "dmr-data",
        "radios": [{"id": name, "dmr_id": identity, "flash_size": 0x200000, "host_network": True}
                   for name, identity in (("a", 1001), ("b", 1002))],
        "events": [
            {"at_ms": 10, "radio": "a", "action": "serial_input",
             "hex": "e01a0280020a0280800100800080000080800000000000000000"},
            {"at_ms": 20, "radio": "a", "action": "serial_input",
             "hex": "e014040060bdd61960bdd6198000000060bdd619"},
        ] + [{"at_ms": 100 + index * 10, "radio": "a", "action": "serial_input", "hex": "e0251a00" + frame}
             for index, frame in enumerate(DATA_BURSTS)] + [
            {"at_ms": 420, "radio": "a", "action": "host_link", "enabled": False},
            {"at_ms": 1000, "radio": "a", "action": "host_link", "enabled": True},
            {"at_ms": 1020, "radio": "a", "action": "serial_input", "hex": "e00300"},
            {"at_ms": 1220, "radio": "a", "action": "host_link", "enabled": False},
            {"at_ms": 1300, "radio": "a", "action": "host_link", "enabled": True},
            {"at_ms": 1400, "radio": "b", "action": "udp", "destination": "225.0.0.201", "port": 6969,
             "payload_hex": b"normal after disconnect".hex()},
        ],
        "assertions": [
            {"condition": "state_value", "radio": "a", "at_ms": at, "field": field, "value": value}
            for at, field, value in (
                (400, "hotspot", True), (400, "tx_active", True), (400, "modem_queued", 8),
                (400, "hotspot_net_frames", 15), (400, "hotspot_net_dropped", 0),
                (440, "hotspot", False), (440, "tx_active", False), (440, "modem_queued", 0),
                (1040, "hotspot", True), (1040, "hotspot_starts", 2),
                (1240, "hotspot", False), (1240, "hotspot_stops", 2))
        ] + [
            {"condition": "data_bptc", "radio": "a", "count": 1},
            {"condition": "no_tx", "radio": "a", "from_ms": 440, "through_ms": 4000},
            {"condition": "udp_delivery", "radio": "a", "count": 1, "source": "12.0.3.234",
             "destination": "225.0.0.201", "source_port": 6969, "port": 6969,
             "payload_hex": b"normal after disconnect".hex()},
        ],
    },
    "hotspot-pending-entry-disconnect": {
        "schema_version": 1, "name": "hotspot-pending-entry-disconnect", "seed": 1,
        "duration_ms": 4000, "integrated_core": True, "codeplug_fixture": "dmr-data",
        "radios": [{"id": name, "dmr_id": identity, "flash_size": 0x200000, "host_network": True}
                   for name, identity in (("a", 1001), ("b", 1002))],
        "events": [
            {"at_ms": 100, "radio": "a", "action": "udp", "destination": "225.0.0.201", "port": 6969,
             "payload_hex": (b"normal TX retains ownership " + bytes(range(120))).hex()},
            {"at_ms": 200, "radio": "a", "action": "serial_input", "hex": "e00300"},
            {"at_ms": 220, "radio": "a", "action": "host_link", "enabled": False},
            {"at_ms": 3800, "radio": "a", "action": "host_link", "enabled": True},
            {"at_ms": 3900, "radio": "a", "action": "serial_input", "hex": "e00300"},
        ],
        "assertions": [
            {"condition": "state_value", "radio": "a", "at_ms": at, "field": field, "value": value}
            for at, field, value in (
                (200, "tx_active", True), (200, "hotspot", False),
                (200, "hotspot_enter_attempts", 1), (200, "hotspot_starts", 0),
                (3700, "tx_active", False), (3700, "hotspot", False),
                (3700, "hotspot_enter_attempts", 1), (3700, "hotspot_starts", 0),
                (3820, "hotspot", False), (3820, "hotspot_enter_attempts", 1),
                (3920, "hotspot", True), (3920, "hotspot_enter_attempts", 2),
                (3920, "hotspot_starts", 1))
        ] + [
            {"condition": "udp_delivery", "radio": "b", "count": 1, "source": "12.0.3.233",
             "destination": "225.0.0.201", "source_port": 6969, "port": 6969,
             "payload_hex": (b"normal TX retains ownership " + bytes(range(120))).hex()},
            {"condition": "no_tx", "radio": "b", "from_ms": 0, "through_ms": 4000},
            {"condition": "no_tx", "radio": "a", "from_ms": 3700, "through_ms": 4000},
            {"condition": "state_value", "radio": "b", "field": "hotspot", "value": False},
        ],
    },
    "ui-settings-recovery": {
        "schema_version": 1, "name": "ui-settings-recovery", "seed": 1, "duration_ms": 4000,
        "radios": [{"id": "a", "dmr_id": 1001}, {"id": "b", "dmr_id": 1002}],
        "events": [
            {"at_ms": index * 100, "radio": "a", "action": "key", "key": key, "gesture": "press"}
            for index, key in enumerate(("select", "up", "select", "down", "select", "select", "down", "select"))
        ] + [
            {"at_ms": 800, "radio": "a", "action": "screen"},
            {"at_ms": 3000, "radio": "a", "action": "restart"},
        ] + [
            {"at_ms": 3100 + index * 100, "radio": "a", "action": "key", "key": key, "gesture": "press"}
            for index, key in enumerate(("select", "up", "select", "down", "select"))
        ] + [
            {"at_ms": 3600, "radio": "a", "action": "screen"},
            {"at_ms": 3700, "radio": "b", "action": "screen"},
        ],
        "assertions": [
            {"condition": "screen_text", "radio": "a", "at_ms": at, "text": "90%", "visible": True}
            for at in (800, 3600)
        ] + [
            {"condition": "state_value", "radio": "a", "field": "brightness", "value": 90},
            {"condition": "state_value", "radio": "b", "field": "brightness", "value": 100},
            {"condition": "screen_id", "radio": "a", "at_ms": 3600, "screen": "group"},
            {"condition": "screen_id", "radio": "b", "at_ms": 3700, "screen": "home"},
        ] + [
            {"condition": "no_tx", "radio": radio, "from_ms": 0, "through_ms": 4000}
            for radio in ("a", "b")
        ],
    },
    "ui-during-traffic": {
        "schema_version": 1, "name": "ui-during-traffic", "seed": 1, "duration_ms": 4000,
        "radios": [{"id": "a", "dmr_id": 1001}, {"id": "b", "dmr_id": 1002}],
        "events": [
            {"at_ms": 0, "radio": "a", "action": "ptt", "down": True},
            {"at_ms": 100, "radio": "a", "action": "udp", "destination": "225.0.0.201",
             "port": 6969, "payload_hex": "75692d74726166666963"},
            {"at_ms": 200, "radio": "a", "action": "key", "key": "select", "gesture": "press"},
            {"at_ms": 200, "radio": "b", "action": "key", "key": "select", "gesture": "press"},
            {"at_ms": 400, "radio": "a", "action": "screen"},
            {"at_ms": 400, "radio": "b", "action": "screen"},
            {"at_ms": 600, "radio": "a", "action": "key", "key": "back", "gesture": "press"},
            {"at_ms": 600, "radio": "b", "action": "key", "key": "back", "gesture": "press"},
            {"at_ms": 800, "radio": "a", "action": "screen"},
            {"at_ms": 800, "radio": "b", "action": "screen"},
            {"at_ms": 1200, "radio": "a", "action": "ptt", "down": False},
            {"at_ms": 3400, "radio": "a", "action": "key", "key": "select", "gesture": "press"},
            {"at_ms": 3400, "radio": "b", "action": "key", "key": "select", "gesture": "press"},
            {"at_ms": 3500, "radio": "a", "action": "screen"},
            {"at_ms": 3500, "radio": "b", "action": "screen"},
        ],
        "assertions": [
            {"condition": "screen_id", "radio": radio, "at_ms": at, "screen": screen}
            for at, screen in ((400, "hub"), (800, "home"), (3500, "hub")) for radio in ("a", "b")
        ] + [
            {"condition": "screen_text", "radio": radio, "at_ms": 800, "text": text, "visible": True}
            for radio, text in (("a", "TX"), ("b", "RX"))
        ] + [
            {**CALL, "radio": "b", "late_entry": False, "count": 1},
            {"condition": "voice_event", "radio": "b", "event": "end", "count": 1},
            {"condition": "udp_delivery", "radio": "b", "count": 1,
             "source": "12.0.3.233", "destination": "225.0.0.201", "source_port": 6969,
             "port": 6969, "payload_hex": "75692d74726166666963"},
            {"condition": "delivery_count", "radio": "a", "count": 0},
            {"condition": "delivery_count", "radio": "b", "count": 1},
            {"condition": "no_tx", "radio": "b", "from_ms": 0, "through_ms": 4000},
            {"condition": "no_tx", "radio": "a", "from_ms": 3400, "through_ms": 4000},
        ],
    },
    "radio-fault-recovery": {
        "schema_version": 1, "name": "radio-fault-recovery", "seed": 1, "duration_ms": 8000,
        "radios": [{"id": "a", "dmr_id": 1001}, {"id": "b", "dmr_id": 1002}],
        "events": [
            {"at_ms": 0, "radio": "a", "action": "brightness", "value": 30},
            {"at_ms": 0, "radio": "a", "action": "settings_save"},
            {"at_ms": 0, "radio": "b", "action": "brightness", "value": 70},
            {"at_ms": 0, "radio": "b", "action": "settings_save"},
            {"at_ms": 0, "radio": "a", "action": "watchdog_starve"},
            {"at_ms": 100, "radio": "a", "action": "flash_cut", "units": 10},
            {"at_ms": 100, "radio": "a", "action": "brightness", "value": 90},
            {"at_ms": 110, "radio": "a", "action": "settings_save"},
            {"at_ms": 200, "radio": "a", "action": "ptt", "down": True},
            {"at_ms": 400, "radio": "a", "action": "screen"},
            {"at_ms": 400, "radio": "b", "action": "screen"},
            {"at_ms": 600, "radio": "b", "action": "host_link", "enabled": False},
            {"at_ms": 700, "radio": "b", "action": "sync_loss"},
            {"at_ms": 710, "radio": "b", "action": "tune", "channel": 2},
            {"at_ms": 1000, "radio": "b", "action": "tune", "channel": 1},
            {"at_ms": 1500, "radio": "b", "action": "host_link", "enabled": True},
            {"at_ms": 2000, "radio": "b", "action": "screen"},
            {"at_ms": 5100, "radio": "a", "action": "screen"},
            {"at_ms": 6000, "radio": "a", "action": "udp", "destination": "225.0.0.201",
             "port": 6969, "payload_hex": "7265636f7665726564"},
        ],
        "assertions": [
            {"condition": "screen_text", "radio": radio, "at_ms": at, "text": text, "visible": visible}
            for radio, at, text, visible in (("a", 400, "TX", True), ("b", 400, "RX", True),
                                             ("b", 2000, "RX", True), ("a", 5100, "TX", False))
        ] + [
            {"condition": "state_value", "radio": radio, "field": field, "value": value}
            for radio, field, value in (("a", "brightness", 30), ("b", "brightness", 70),
                                        ("a", "now_ms", 3000), ("b", "now_ms", 8000),
                                        ("a", "watchdog_starving", False), ("b", "host_connected", True),
                                        ("b", "channel", 1))
        ] + [
            {"condition": "udp_delivery", "radio": "b", "count": 1,
             "source": "12.0.3.233", "destination": "225.0.0.201", "source_port": 6969,
             "port": 6969, "payload_hex": "7265636f7665726564"},
            {"condition": "delivery_count", "radio": "a", "count": 0},
            {"condition": "delivery_count", "radio": "b", "count": 1},
            {"condition": "no_tx", "radio": "a", "from_ms": 5000, "through_ms": 5990},
            {"condition": "no_tx", "radio": "b", "from_ms": 0, "through_ms": 8000},
        ],
    },
    "three-radio-voice-recovery": {
        "schema_version": 1, "name": "three-radio-voice-recovery", "seed": 1, "duration_ms": 4000,
        "radios": [{"id": "a", "dmr_id": 1001}, {"id": "b", "dmr_id": 1002}, {"id": "c", "dmr_id": 1003}],
        "events": [
            {"at_ms": 0, "radio": "a", "action": "link", "target": "c", "enabled": False},
            {"at_ms": 0, "radio": "a", "action": "ptt", "down": True},
            {"at_ms": 300, "radio": "a", "action": "link", "target": "c", "enabled": True},
            {"at_ms": 600, "radio": "a", "action": "loss", "target": "c", "rate_ppm": 250000},
            {"at_ms": 800, "radio": "a", "action": "loss", "target": "c", "rate_ppm": 500000},
            {"at_ms": 1000, "radio": "a", "action": "loss", "target": "c", "rate_ppm": 1000000},
            {"at_ms": 1200, "radio": "b", "action": "sync_loss"},
            {"at_ms": 2000, "radio": "a", "action": "loss", "target": "c", "rate_ppm": 0},
            {"at_ms": 3000, "radio": "a", "action": "ptt", "down": False},
        ],
        "assertions": [
            {"condition": "no_tx", "radio": "b", "from_ms": 0, "through_ms": 4000},
            {"condition": "no_tx", "radio": "c", "from_ms": 0, "through_ms": 4000},
            {"condition": "no_tx", "radio": "a", "from_ms": 3600, "through_ms": 4000},
            {**CALL, "radio": "b", "late_entry": False, "count": 1},
            {**CALL, "radio": "b", "late_entry": True, "count": 1},
            {**CALL, "radio": "c", "late_entry": False, "count": 0},
            {**CALL, "radio": "c", "late_entry": True, "count": 1, "from_ms": 2000, "through_ms": 3600},
            {"radio": "b", "condition": "voice_event", "event": "end", "count": 2},
            {"radio": "c", "condition": "voice_event", "event": "end", "count": 1, "from_ms": 2000, "through_ms": 3600},
        ],
    },
}


def _queued_hotspot_cycles():
    base = SCENARIOS["hotspot-queued-disconnect"]
    result = copy.deepcopy(base)
    result.update(name="hotspot-queued-disconnect-32", duration_ms=32 * 4200, events=[], assertions=[])
    # Preserve slot phase between cycles; export expands all inputs, with no runtime generation.
    for cycle in range(32):
        offset = cycle * 4200
        payload = (b"normal after disconnect " + bytes([cycle])).hex()
        for event in base["events"]:
            event = copy.deepcopy(event)
            event["at_ms"] += offset
            if event["action"] == "udp":
                event["payload_hex"] = payload
            result["events"].append(event)
        for assertion in base["assertions"]:
            assertion = copy.deepcopy(assertion)
            if assertion["condition"] == "data_bptc":
                continue
            if "at_ms" in assertion:
                assertion["at_ms"] += offset
            if assertion["condition"] == "state_value":
                if assertion["field"] in ("hotspot_starts", "hotspot_stops"):
                    assertion["value"] += cycle * 2
                elif assertion["field"] == "hotspot_net_frames":
                    assertion["value"] += cycle * len(DATA_BURSTS)
            elif assertion["condition"] == "no_tx":
                assertion["from_ms"] += offset
                assertion["through_ms"] = offset + 4200
            elif assertion["condition"] == "udp_delivery":
                assertion["payload_hex"] = payload
            result["assertions"].append(assertion)
    result["assertions"].append({"condition": "data_bptc", "radio": "a", "count": 32})
    return result


SCENARIOS["hotspot-queued-disconnect-32"] = _queued_hotspot_cycles()

# Retain queued-TX teardown and data recovery before exercising both normal voice directions.
SCENARIOS["hotspot-voice-recovery"] = copy.deepcopy(SCENARIOS["hotspot-queued-disconnect"])
SCENARIOS["hotspot-voice-recovery"].update(name="hotspot-voice-recovery", duration_ms=10000)
SCENARIOS["hotspot-voice-recovery"]["events"].extend([
    {"at_ms": at, "radio": radio, "action": "ptt", "down": down}
    for at, radio, down in ((4200, "b", True), (6200, "b", False), (7200, "a", True), (9200, "a", False))
])
# Integrated radio_sm installs the production AMBE sink, not the standalone voice_event observer.
# integrated_lab_test verifies identified PCM at the actual receiving audio boundary.

SCENARIOS["voice-queue-expiry-boundary"] = {
    "schema_version": 1, "name": "voice-queue-expiry-boundary", "seed": 1, "duration_ms": 14000,
    "integrated_core": True, "codeplug_fixture": "dmr-data",
    "radios": [{"id": name, "dmr_id": identity, "flash_size": 0x200000, "task_schedule": "slot-first"}
               for name, identity in (("a", 1001), ("b", 1002))],
    "events": [
        {"at_ms": 120, "radio": "a", "action": "ptt", "down": True},
        {"at_ms": 330, "radio": "a", "action": "udp", "destination": "225.0.0.201", "port": 6969,
         "payload_hex": b"service waiting behind voice".hex()},
        {"at_ms": 340, "radio": "a", "action": "udp", "destination": "225.0.0.202", "port": 6969,
         "payload_hex": b"expired behind voice".hex()},
        {"at_ms": 10350, "radio": "a", "action": "ptt", "down": False},
        {"at_ms": 11000, "radio": "a", "action": "udp", "destination": "225.0.0.201", "port": 6969,
         "payload_hex": b"fresh after expiry".hex()},
    ],
    "assertions": [
        {"condition": "state_value", "radio": "a", "field": "depth", "value": depth, "at_ms": at}
        for at, depth in ((350, 2), (10340, 2), (10350, 0), (14000, 0))
    ] + [
        {"condition": "udp_delivery", "radio": "b", "count": count, "source": "12.0.3.233",
         "destination": destination, "source_port": 6969, "port": 6969, "payload_hex": payload.hex()}
        for payload, destination, count in ((b"service waiting behind voice", "225.0.0.201", 0),
                                            (b"expired behind voice", "225.0.0.202", 0),
                                            (b"fresh after expiry", "225.0.0.201", 1))
    ] + [{"condition": "no_tx", "radio": "b", "from_ms": 0, "through_ms": 14000}],
}

# Keep the research inputs available to convert.py; never silently drop their unsupported checks.
REFUSALS = {
    "hotspot-active-peers": "needs world host-network input and hotspot state observations",
    "hotspot-queued-disconnect": "needs scheduled host bytes, host-link faults and hotspot state observations",
    "hotspot-pending-entry-disconnect": "needs world host-network input and host-link faults",
    "ui-settings-recovery": "needs flash-preserving restart and rendered-text observations",
    "ui-during-traffic": "needs world host-network input and voice/text observations",
    "radio-fault-recovery": "needs world watchdog, flash and sync faults; direct firmware calls are refused",
    "three-radio-voice-recovery": "needs scheduled sync faults and voice sink observations",
    "hotspot-queued-disconnect-32": "needs scheduled host bytes, host-link faults and hotspot state observations",
    "hotspot-voice-recovery": "needs scheduled host bytes, host-link faults and hotspot state observations",
    "voice-queue-expiry-boundary": "needs world host-network input and queue observations; fixed task order is refused",
}


def _air_pair():
    from convert import board_profile, EPOCH
    from scenario import TIME_MODEL, CLASS_TABLE_VERSION
    profile = board_profile()
    return {
        "schema_version": 2, "name": "air_pair", "tier": "local", "time_model": TIME_MODEL,
        "class_table": CLASS_TABLE_VERSION, "view_version": profile["view_version"], "seed": 0,
        "epoch_utc": EPOCH, "duration_us": 5000000,
        "radios": [{"id": name, "dmr_id": identity, "board": dict(profile["board"]), "storage": "dmr-data"}
                   for name, identity in (("a", 3100001), ("b", 3100002))],
        "events": [{"t_us": at, "round": 0, "class": 30, "seq": seq, "source": "scenario", "radio": "a",
                    "key": {"bit": profile["actions"]["ACT_PTT"], "down": down}}
                   for seq, (at, down) in enumerate(((3000000, True), (4000000, False)))],
        "checkpoints": [],
        "assertions": [{"condition": "no_tx", "radio": "b", "from_us": 0, "through_us": 5000000}],
    }


WORLD_SCENARIOS = {"air_pair": _air_pair()}
