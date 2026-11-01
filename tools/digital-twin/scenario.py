# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Versioned private host-scenario input contract."""
import copy
from collections import deque
import ipaddress
import re
import hashlib
import json
from pathlib import Path
import sys
import time
import argparse
import struct
import os
import signal
import subprocess

HOST_TIMEOUT_S = 60
CLI_TIMEOUT_S = 90


def component_status():
    root = Path(__file__).resolve().parents[2]
    models = {
        "firmware": ("platform/host/main.c", "Firmware tasks on the host FreeRTOS port; no hardware timing proof."),
        "channel": ("tools/conductor/parts/air.py", "Shared burst air; no calibrated RF propagation."),
        "storage": ("boards/vhost/storage_vhost.c", "Named fixtures and editor steps on virtual NOR."),
        "ui": ("tools/conductor/parts/view.py", "Production UI state and framebuffer checkpoints."),
        "physical_input": ("tools/conductor/parts/input.py", "Key bits and encoder detents through the input task."),
        "codec": ("boards/vhost/audio_vhost.c", "No decoded speech or hardware audio validation."),
    }
    result = {name: {"model_coverage": "partial", "hardware_validation": "none",
                     "provenance": "documentation-derived", "source": source,
                     "source_sha256": hashlib.sha256((root / source).read_bytes()).hexdigest(),
                     "limitations": limits} for name, (source, limits) in models.items()}
    result["codec"]["check_status"] = "not_run"
    return result


def fields(value, expected, where):
    if not isinstance(value, dict) or set(value) != set(expected.split()):
        raise ValueError(f"{where}: expected exactly {expected}")


def integer(value, low, high, where):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{where}: expected integer in [{low}, {high}]")


def board_display(name):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import boards
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError("display board: invalid registry identifier")
    try:
        board = boards.load(only=[name])[name]
    except boards.RegistryError as error:
        raise ValueError(f"display board: {error}") from error
    display_class = board.data["hardware"]["gallery"]
    # Use the complete gallery row: its insets and font switches are not implied by dimensions.
    source = (Path(boards.ROOT) / boards.UI_SIM).read_text()
    table = re.search(r"sim_geoms\[\]\s*=\s*\{(.*?)\n\};", source, re.S)
    rows = re.findall(r'\{\s*"([^"]+)",\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(true|false),\s*(true|false)\s*\}',
                      table.group(1) if table else "")
    for label, width, height, bpp, inset, inset_tb, aa, big in rows:
        if label == display_class:
            return {"width": int(width), "height": int(height), "bpp": int(bpp),
                    "inset_left": int(inset), "inset_right": int(inset),
                    "inset_top": int(inset_tb), "inset_bottom": int(inset_tb),
                    "font_aa": aa == "true", "font_big": big == "true"}
    raise ValueError(f"display model missing for registry class: {display_class}")


def validate(value):
    fields(value, "schema_version name seed duration_ms radios events assertions" +
           (" repeater" if isinstance(value, dict) and "repeater" in value else "") +
           (" codeplug_fixture" if isinstance(value, dict) and "codeplug_fixture" in value else "") +
           (" integrated_core" if isinstance(value, dict) and "integrated_core" in value else ""), "scenario")
    value = copy.deepcopy(value)
    if "integrated_core" in value and type(value["integrated_core"]) is not bool:
        raise ValueError("integrated_core must be boolean")
    if "codeplug_fixture" in value and (value["codeplug_fixture"] not in ("dmr-data", "dmr-roaming") or not value.get("integrated_core", False)):
        raise ValueError("codeplug_fixture must be dmr-data or dmr-roaming and requires integrated_core")
    if "repeater" in value:
        fields(value["repeater"], "hang_ms", "repeater")
        integer(value["repeater"]["hang_ms"], 60, 60000, "repeater hang_ms")
        if value["repeater"]["hang_ms"] % 10:
            raise ValueError("repeater hang must be tick aligned")
    integer(value["schema_version"], 1, 1, "schema_version")
    if not isinstance(value["name"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value["name"]):
        raise ValueError("name: expected a short alphanumeric identifier")
    integer(value["seed"], 0, 2**64 - 1, "seed")
    duration = value["duration_ms"]
    integer(duration, 10, 3600000, "duration_ms")
    if duration % 10:
        raise ValueError("duration_ms: must align with the 10 ms clock")
    radios = value["radios"]
    if not isinstance(radios, list) or len(radios) not in (2, 3):
        raise ValueError("radios: this runner currently supports two or three independent radios")
    names, identities = set(), set()
    for radio in radios:
        fields(radio, "id dmr_id" + (" profile" if "profile" in radio else "") + (" worker" if "worker" in radio else "") + (" clock" if "clock" in radio else "") +
               (" host_network" if "host_network" in radio else "") +
               (" hotspot_schedule" if "hotspot_schedule" in radio else "") +
               (" task_schedule" if "task_schedule" in radio else "") +
               (" flash_map" if "flash_map" in radio else "") +
               (" display" if "display" in radio else "") + (" flash_size" if "flash_size" in radio else "") +
               (" color_code" if "color_code" in radio else "") + (" talkgroup" if "talkgroup" in radio else "") +
               (" confirmed_ip" if "confirmed_ip" in radio else "") +
               (" timeslot" if "timeslot" in radio else "") +
               (" repeater_mode" if "repeater_mode" in radio else ""), "radio")
        if "task_schedule" in radio:
            if radio["task_schedule"] not in ("slot-first", "radio-first", "host-first") or not value.get("integrated_core", False):
                raise ValueError("task_schedule requires slot-first/radio-first/host-first and integrated_core")
        if "hotspot_schedule" in radio:
            if radio["hotspot_schedule"] not in ("immediate", "deferred") or not value.get("integrated_core", False):
                raise ValueError("hotspot_schedule requires immediate/deferred and integrated_core")
        if "host_network" in radio:
            if type(radio["host_network"]) is not bool or not value.get("integrated_core", False):
                raise ValueError("host_network requires a boolean and integrated_core")
        if "profile" in radio or "worker" in radio:
            raise ValueError("retired profile/worker selection: use a version 2 board and build hash")
        if "timeslot" in radio:
            integer(radio["timeslot"], 1, 2, "timeslot")
        if "repeater_mode" in radio and type(radio["repeater_mode"]) is not bool:
            raise ValueError("repeater_mode must be a boolean")
        if "confirmed_ip" in radio and type(radio["confirmed_ip"]) is not bool:
            raise ValueError("confirmed_ip must be a boolean")
        if "talkgroup" in radio:
            integer(radio["talkgroup"], 1, 0xFFFFFF, "talkgroup")
        if "color_code" in radio:
            integer(radio["color_code"], 0, 15, "color_code")
        if isinstance(radio.get("flash_map"), str):
            name = radio["flash_map"]
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                raise ValueError("storage board: invalid registry identifier")
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import boards
            try:
                board = boards.load(only=[name])[name]
            except boards.RegistryError as error:
                raise ValueError(f"storage board: {error}") from error
            size = board.data.get("flash", {}).get("backup", {}).get("spi_size")
            if not size:
                raise ValueError("storage board: NOR capacity is unknown; model missing")
            if "flash_size" in radio and radio["flash_size"] != size:
                raise ValueError("flash_size conflicts with registry capacity")
            radio["flash_size"] = size
            radio["flash_map"] = {key: board.flash_map.get(key) for key in
                                  ("openfw_eeprom", "openfw_flash", "openfw_end")}
        if "flash_size" in radio:
            integer(radio["flash_size"], 0x40000, 0x1000000, "flash_size")
            if radio["flash_size"] % 4096:
                raise ValueError("flash_size must be sector aligned")
        if "flash_map" in radio:
            layout = radio["flash_map"]
            fields(layout, "openfw_eeprom openfw_flash openfw_end", "flash_map")
            size = radio.get("flash_size", 0x40000)
            for key, address in layout.items():
                integer(address, 0, size, key)
                if address % 4096:
                    raise ValueError("flash_map addresses must be sector aligned")
            if (layout["openfw_flash"] != layout["openfw_eeprom"] + 0x20000 or
                    layout["openfw_end"] < layout["openfw_flash"] + 0x100000):
                raise ValueError("flash_map has unsupported image spacing or extent")
        if "display" in radio:
            if isinstance(radio["display"], str):
                radio["display"] = board_display(radio["display"])
            display = radio["display"]
            fields(display, "width height bpp inset_left inset_right inset_top inset_bottom" +
                   (" font_aa" if "font_aa" in display else "") +
                   (" font_big" if "font_big" in display else ""), "radio display")
            for option in ("font_aa", "font_big"):
                if option in display and type(display[option]) is not bool:
                    raise ValueError("display font options must be booleans")
            integer(display["width"], 32, 65535, "display width")
            integer(display["height"], 32, 320, "display height")
            integer(display["bpp"], 1, 16, "display bpp")
            if display["bpp"] not in (1, 4, 16):
                raise ValueError("display bpp: expected 1, 4 or 16")
            for side in ("left", "right", "top", "bottom"):
                integer(display["inset_" + side], 0, 255, "display inset")
            stride = (display["width"] * display["bpp"] + 7) // 8
            if (stride * display["height"] > 40960
                    or display["inset_left"] + display["inset_right"] + 32 > display["width"]
                    or display["inset_top"] + display["inset_bottom"] + 32 > display["height"]):
                raise ValueError("display exceeds worker framebuffer or has insufficient usable area")
        if "clock" in radio:
            fields(radio["clock"], "offset_us drift_ppm", "radio clock")
            integer(radio["clock"]["offset_us"], -(2**32 * 1000), 2**32 * 1000, "clock offset_us")
            integer(radio["clock"]["drift_ppm"], -1000, 1000, "clock drift_ppm")
        name, identity = radio["id"], radio["dmr_id"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
            raise ValueError("radio id: expected a short alphanumeric identifier")
        integer(identity, 1, 0xFFFFFE, "dmr_id")
        if name in names or identity in identities:
            raise ValueError("duplicate radio identity")
        names.add(name)
        identities.add(identity)
    if "repeater" in value:
        if any(not radio.get("repeater_mode", False) for radio in radios):
            raise ValueError("shared repeater requires repeater-mode members")
        if len({radio.get("color_code", 1) for radio in radios}) != 1:
            raise ValueError("shared repeater currently requires one color code")
    events = value["events"]
    if not isinstance(events, list):
        raise ValueError("events: expected array")
    last_time = -1
    for event in events:
        if not isinstance(event, dict) or event.get("action") not in ("site_data", "site_voice", "site", "udp", "ptt", "refuse_tx", "stall", "queue_limits", "repeater_available", "repeater_power", "link", "loss", "corrupt", "sync_loss", "duplicate", "delay", "miss_tx_done", "host_link", "tune", "brightness", "settings_save", "flash_cut", "storage_reload", "restart", "flash_corrupt", "watchdog_starve", "screen", "channel_rename", "channel_queue", "channel_read", "key", "ppp_start", "serial_input", "repeater_data", "repeater_voice"):
            raise ValueError("unsupported action; currently supported: site_data, site_voice, site, udp, ptt, refuse_tx, stall, queue_limits, repeater_available, repeater_power, link, loss, corrupt, sync_loss, duplicate, delay, miss_tx_done, host_link, tune, brightness, settings_save, flash_cut, storage_reload, restart, flash_corrupt, watchdog_starve, screen, channel_rename, channel_queue, channel_read, key, ppp_start, serial_input, repeater_data, repeater_voice")
        parameters = {"site_data": "site timeslot hex", "site_voice": "site timeslot hex", "site": "site available rssi_dbm color_code", "udp": "destination port payload_hex", "ptt": "down", "refuse_tx": "enabled", "stall": "enabled", "repeater_available": "enabled", "repeater_power": "enabled", "queue_limits": "wait_ms max_age_ms", "link": "target enabled", "loss": "target rate_ppm", "corrupt": "target ordinal xor_hex", "sync_loss": "", "duplicate": "target ordinal", "delay": "target ordinal delay_ms", "miss_tx_done": "count", "host_link": "enabled", "tune": "channel", "brightness": "value", "settings_save": "", "flash_cut": "units", "storage_reload": "", "restart": "", "flash_corrupt": "offset xor_hex", "watchdog_starve": "", "screen": "", "channel_rename": "channel name", "channel_queue": "channel name", "channel_read": "channel", "key": "key gesture", "ppp_start": "active", "serial_input": "hex", "repeater_data": "timeslot hex", "repeater_voice": "timeslot hex"}
        optional = " rx_only" if event["action"] == "channel_rename" and "rx_only" in event else ""
        if "phase" in event:
            if (event["action"] not in ("restart", "key") or not value.get("integrated_core", False) or
                    event["phase"] not in ("before_tick", "after_tick") or event.get("at_ms") == 0):
                raise ValueError("phase requires an integrated restart/key after time zero, before_tick or after_tick")
            optional += " phase"
        optional += "".join(" " + key for key in ("id", "after") if key in event)
        fields(event, "at_ms radio action " + parameters[event["action"]] + optional, "event")
        if "repeater" in value and event["action"] == "repeater_available":
            raise ValueError("local activation responses cannot be combined with a shared repeater")
        if event["action"] == "repeater_power" and "repeater" not in value:
            raise ValueError("repeater_power requires a shared repeater")
        if "rx_only" in event and type(event["rx_only"]) is not bool:
            raise ValueError("channel rx_only must be boolean")
        if event["action"] == "ppp_start" and type(event["active"]) is not bool:
            raise ValueError("PPP active must be boolean")
        if event["action"] == "serial_input":
            if not isinstance(event["hex"], str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,600}", event["hex"]):
                raise ValueError("serial input requires 1 to 600 bytes of hexadecimal data")
        if event["action"] in ("repeater_data", "site_data"):
            integer(event["timeslot"], 1, 2, "repeater timeslot")
            if not isinstance(event["hex"], str) or not re.fullmatch(r"[0-9a-fA-F]{68}", event["hex"]):
                raise ValueError("repeater data requires a type byte and 33 coded bytes")
        if event["action"] in ("repeater_voice", "site_voice"):
            integer(event["timeslot"], 1, 2, "repeater timeslot")
            if not isinstance(event["hex"], str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){33,34}", event["hex"]):
                raise ValueError("repeater voice requires 33 or 34 bytes of hexadecimal data")
            voice = bytes.fromhex(event["hex"])
            if voice[0] >= 6 or voice[28] > 3 or (len(voice) == 34 and voice[33] > 15):
                raise ValueError("invalid repeater voice index, LCSS or color code")
        if event["action"] == "key":
            if not isinstance(event["key"], str) or event["key"] not in ("up", "down", "left", "right", "select", "back", "quick", "context"):
                raise ValueError("unknown logical UI key")
            if event["gesture"] not in ("press", "long"):
                raise ValueError("UI key gesture must be press or long")
        if event["action"] in ("channel_rename", "channel_queue", "channel_read"):
            integer(event["channel"], 1, 1024, "codeplug channel")
            if event["action"] in ("channel_rename", "channel_queue"):
                if not isinstance(event["name"], str) or not re.fullmatch(r"[ -~]{1,16}", event["name"]):
                    raise ValueError("channel name must be 1 to 16 printable ASCII characters")
        if event["action"] == "flash_corrupt":
            flash_size = next((radio.get("flash_size", 0x40000) for radio in radios
                               if radio["id"] == event["radio"]), 0x40000)
            integer(event["offset"], 0, flash_size - 1, "flash_corrupt offset")
            mask = event["xor_hex"]
            if not isinstance(mask, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,512}", mask):
                raise ValueError("flash_corrupt xor_hex: expected 1 to 512 bytes")
            if event["offset"] + len(mask) // 2 > flash_size:
                raise ValueError("flash_corrupt exceeds simulated image")
        if event["action"] == "brightness":
            integer(event["value"], 10, 100, "brightness value")
        if event["action"] == "flash_cut":
            integer(event["units"], 0, 1000000, "flash_cut units")
        if event["action"] == "tune":
            integer(event["channel"], 1, 0xFFFFFFFF, "tune channel")
        if event["action"] in ("site", "site_data", "site_voice"):
            if value.get("codeplug_fixture") != "dmr-roaming":
                raise ValueError("site observations require the integrated roaming fixture")
            integer(event["site"], 0, 2, "site")
        if event["action"] == "site":
            if value.get("codeplug_fixture") != "dmr-roaming":
                raise ValueError("site observations require the integrated roaming fixture")
            integer(event["site"], 0, 2, "site")
            integer(event["rssi_dbm"], -127, -1, "site RSSI")
            integer(event["color_code"], 0, 15, "site color code")
            if type(event["available"]) is not bool:
                raise ValueError("site available must be boolean")
        if event["action"] == "miss_tx_done":
            integer(event["count"], 0, 65535, "miss_tx_done count")
        at = event["at_ms"]
        integer(at, 0, duration - 10, "event at_ms")
        if at % 10 or at < last_time:
            raise ValueError("events must be time-ordered on the 10 ms clock")
        last_time = at
        if not isinstance(event["radio"], str) or event["radio"] not in names:
            raise ValueError("event references an unknown radio")
        if event["action"] in ("channel_rename", "channel_queue", "channel_read"):
            radio = next(radio for radio in radios if radio["id"] == event["radio"])
            if radio.get("flash_size", 0x40000) < 0x200000:
                raise ValueError("codeplug actions require at least a 2 MiB flash fixture")
        if event["action"] in ("site_data", "site_voice", "site", "sync_loss", "miss_tx_done", "tune", "brightness", "settings_save", "flash_cut", "storage_reload", "restart", "flash_corrupt", "watchdog_starve", "screen", "channel_rename", "channel_queue", "channel_read", "key", "ppp_start", "serial_input", "repeater_data", "repeater_voice"):
            continue
        if event["action"] in ("link", "loss", "corrupt", "duplicate", "delay"):
            if not isinstance(event["target"], str) or event["target"] not in names or event["target"] == event["radio"]:
                raise ValueError("link target must name a different radio")
        if event["action"] == "delay":
            integer(event["ordinal"], 1, 0xFFFFFFFF, "ordinal")
            integer(event["delay_ms"], 10, 3600000, "delay_ms")
            if event["delay_ms"] % 10:
                raise ValueError("delay_ms must align with the 10 ms clock")
            continue
        if event["action"] == "duplicate":
            integer(event["ordinal"], 1, 0xFFFFFFFF, "ordinal")
            continue
        if event["action"] == "corrupt":
            integer(event["ordinal"], 1, 0xFFFFFFFF, "ordinal")
            if not isinstance(event["xor_hex"], str) or not re.fullmatch(r"[0-9a-fA-F]{66}", event["xor_hex"]):
                raise ValueError("xor_hex: expected exactly 33 bytes of hexadecimal data")
            continue
        if event["action"] == "loss":
            integer(event["rate_ppm"], 0, 1000000, "rate_ppm")
            continue
        if event["action"] == "queue_limits":
            for field in ("wait_ms", "max_age_ms"):
                integer(event[field], 0, 65535, field)
            continue
        if event["action"] != "udp":
            field = "down" if event["action"] == "ptt" else "enabled"
            if type(event[field]) is not bool:
                raise ValueError(f"{event['action']} {field}: expected boolean")
            continue
        if not isinstance(event["destination"], str):
            raise ValueError("destination: expected IPv4 address string")
        ipaddress.IPv4Address(event["destination"])
        integer(event["port"], 1, 65535, "UDP port")
        payload = event["payload_hex"]
        if not isinstance(payload, str) or len(payload) > 2048 or not re.fullmatch(r"(?:[0-9a-fA-F]{2})*", payload):
            raise ValueError("payload_hex: expected at most 1024 bytes of hexadecimal data")
    predecessors = {}
    for index, event in enumerate(events):
        identity = event.get("id")
        if identity is not None or "id" in event:
            if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identity) or identity in predecessors:
                raise ValueError("event id must be a unique short identifier")
        dependencies = event.get("after", [])
        if (not isinstance(dependencies, list) or len(dependencies) > 256 or
                any(not isinstance(dep, str) for dep in dependencies) or
                len(set(dependencies)) != len(dependencies) or (dependencies and identity is None)):
            raise ValueError("event after requires an id and distinct predecessor identifiers")
        order = (event["at_ms"], event.get("phase") == "after_tick", index)
        if any(dep not in predecessors or predecessors[dep] >= order for dep in dependencies):
            raise ValueError("event predecessor must already exist and execute before its dependent")
        if identity is not None:
            predecessors[identity] = order
    assertions = value["assertions"]
    if not isinstance(assertions, list) or not assertions:
        raise ValueError("assertions: expected a nonempty array")
    for assertion in assertions:
        if not isinstance(assertion, dict) or assertion.get("condition") not in ("delivery_count", "udp_delivery", "voice_event", "screen_text", "state_value", "no_tx", "codeplug_channel", "screen_id", "serial_udp_delivery", "data_bptc", "mmdvm_data_match"):
            raise ValueError("unsupported assertion; currently supported: delivery_count, udp_delivery, voice_event, screen_text, state_value, no_tx, codeplug_channel, screen_id, serial_udp_delivery, data_bptc, mmdvm_data_match")
        extra = " source destination source_port port payload_hex" if assertion["condition"] in ("udp_delivery", "serial_udp_delivery") else ""
        if assertion["condition"] == "voice_event":
            event = assertion.get("event")
            if not isinstance(event, str) or event not in ("begin", "frame", "lost", "end"):
                raise ValueError("voice event must be begin, frame, lost, or end")
            extra = " event" + {"begin": " source destination group late_entry", "frame": " hex", "lost": "", "end": ""}[event]
        if assertion["condition"] == "voice_event" and ("from_ms" in assertion or "through_ms" in assertion):
            extra += " from_ms through_ms"
        parameters = {"mmdvm_data_match": " source_radio from_ms through_ms count", "screen_text": " at_ms text visible", "screen_id": " at_ms screen", "state_value": " field value", "no_tx": " from_ms through_ms", "codeplug_channel": " at_ms channel name rx_hz tx_hz"}
        optional = " rx_only" if assertion["condition"] == "codeplug_channel" and "rx_only" in assertion else ""
        if assertion["condition"] == "state_value" and "at_ms" in assertion:
            optional = " at_ms"
        fields(assertion, "radio condition" + parameters.get(assertion["condition"], " count" + extra) + optional, "assertion")
        if "rx_only" in assertion and type(assertion["rx_only"]) is not bool:
            raise ValueError("codeplug assertion rx_only must be boolean")
        if not isinstance(assertion["radio"], str) or assertion["radio"] not in names:
            raise ValueError("assertion references an unknown radio")
        if assertion["condition"] == "mmdvm_data_match":
            if not isinstance(assertion["source_radio"], str) or assertion["source_radio"] not in names or assertion["source_radio"] == assertion["radio"]:
                raise ValueError("MMDVM match requires a distinct source radio")
            integer(assertion["from_ms"], 0, duration, "MMDVM match start")
            integer(assertion["through_ms"], assertion["from_ms"], duration, "MMDVM match end")
            integer(assertion["count"], 1, 100000, "MMDVM match count")
            if assertion["from_ms"] % 10 or assertion["through_ms"] % 10:
                raise ValueError("MMDVM match interval must align with the 10 ms clock")
            continue
        if assertion["condition"] == "screen_id":
            integer(assertion["at_ms"], 0, duration - 10, "screen assertion time")
            if assertion["at_ms"] % 10:
                raise ValueError("screen assertion time must be tick aligned")
            if not isinstance(assertion["screen"], str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,63}", assertion["screen"]):
                raise ValueError("screen assertion requires a screen identifier")
            continue
        if assertion["condition"] == "codeplug_channel":
            integer(assertion["channel"], 1, 1024, "codeplug assertion channel")
            integer(assertion["at_ms"], 0, duration - 10, "codeplug assertion time")
            if assertion["at_ms"] % 10:
                raise ValueError("codeplug assertion time must be tick aligned")
            if not isinstance(assertion["name"], str) or not re.fullmatch(r"[ -~]{1,16}", assertion["name"]):
                raise ValueError("codeplug assertion name must be printable ASCII")
            for field in ("rx_hz", "tx_hz"):
                integer(assertion[field], 1, 999999999, field)
            continue
        if assertion["condition"] == "no_tx":
            integer(assertion["from_ms"], 0, duration, "no_tx from_ms")
            integer(assertion["through_ms"], assertion["from_ms"], duration, "no_tx through_ms")
            if assertion["from_ms"] % 10 or assertion["through_ms"] % 10:
                raise ValueError("no_tx interval must align with the 10 ms clock")
            continue
        if assertion["condition"] == "state_value":
            if "at_ms" in assertion:
                integer(assertion["at_ms"], 10, duration, "state assertion time")
                if assertion["at_ms"] % 10:
                    raise ValueError("state assertion time must align with the 10 ms clock")
            if not isinstance(assertion["field"], str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,63}", assertion["field"]):
                raise ValueError("state assertion field must be a status field name")
            if type(assertion["value"]) not in (bool, int, str):
                raise ValueError("state assertion value must be boolean, integer or string")
            if isinstance(assertion["value"], str) and len(assertion["value"]) > 128:
                raise ValueError("state assertion string exceeds 128 characters")
            continue
        if assertion["condition"] == "screen_text":
            integer(assertion["at_ms"], 0, duration - 10, "screen assertion at_ms")
            if assertion["at_ms"] % 10:
                raise ValueError("screen assertion must align with the 10 ms clock")
            if not isinstance(assertion["text"], str) or not 1 <= len(assertion["text"]) <= 128:
                raise ValueError("screen assertion text must contain 1 to 128 characters")
            if type(assertion["visible"]) is not bool:
                raise ValueError("screen assertion visible must be boolean")
            continue
        if assertion["condition"] == "data_bptc":
            integer(assertion["count"], 0, 0xFFFFFFFF, "data BPTC burst count")
            continue
        if assertion["condition"] == "voice_event":
            integer(assertion["count"], 0, 0xFFFFFFFF, "voice event count")
            if "from_ms" in assertion:
                integer(assertion["from_ms"], 0, duration, "voice from_ms")
                integer(assertion["through_ms"], assertion["from_ms"], duration, "voice through_ms")
            if assertion["event"] == "begin":
                for field in ("source", "destination"):
                    integer(assertion[field], 1, 0xFFFFFF, field)
                for field in ("group", "late_entry"):
                    if type(assertion[field]) is not bool:
                        raise ValueError(f"{field}: expected boolean")
            if assertion["event"] == "frame":
                if not isinstance(assertion["hex"], str) or not re.fullmatch(r"[0-9a-fA-F]{18}", assertion["hex"]):
                    raise ValueError("hex: expected exactly 9 encoded frame bytes")
            continue
        if assertion["condition"] in ("udp_delivery", "serial_udp_delivery"):
            for field in ("source", "destination"):
                if not isinstance(assertion[field], str):
                    raise ValueError(f"{field}: expected IPv4 address string")
                ipaddress.IPv4Address(assertion[field])
            for field in ("source_port", "port"):
                integer(assertion[field], 1, 65535, field)
            payload = assertion["payload_hex"]
            if not isinstance(payload, str) or len(payload) > 2048 or not re.fullmatch(r"(?:[0-9a-fA-F]{2})*", payload):
                raise ValueError("payload_hex: expected at most 1024 bytes of hexadecimal data")
        integer(assertion["count"], 0, len(events), "delivery count")
    if value.get("integrated_core", False):
        for radio in radios:
            if radio.get("flash_size", 0x40000) < 0x200000:
                raise ValueError("integrated core requires at least a 2 MiB codeplug fixture")
            if set(radio) - {"id", "dmr_id", "flash_size", "flash_map", "display", "worker", "profile", "host_network", "hotspot_schedule", "task_schedule", "clock", "repeater_mode", "timeslot"}:
                raise ValueError("integrated core uses codeplug configuration; modem overrides are not supported yet")
        actions = {"key", "screen", "restart", "channel_queue", "channel_read", "flash_cut", "storage_reload"}
        if value.get("codeplug_fixture") == "dmr-data":
            actions.update(("udp", "ptt", "refuse_tx", "miss_tx_done", "sync_loss", "ppp_start", "serial_input", "host_link", "link"))
        if value.get("codeplug_fixture") == "dmr-roaming":
            actions.update(("site", "site_data", "site_voice"))
        if any(event["action"] not in actions for event in value["events"]):
            raise ValueError("unsupported integrated-core action or missing DMR codeplug fixture")
    return copy.deepcopy(value)


# --- Schema version 2: a recording (docs/dev/adr/0008-record-and-replay.md) ---
# A recording is a fully resolved scenario: what the world was made of, every input with its full stamp, and the
# checkpoints the run wrote. validate() above stays the version 1 contract that the research executor (run, execute)
# reads; version 1 files reach version 2 only through convert.py (ADR 0008, decision 7).
#
# Two forms pass validate_v2:
#   - a scenario: no "build" and no "end", and an empty "checkpoints" list. Every input is resolved (key bits, µs,
#     stamps), but no run has pinned a binary yet. convert.py writes this form;
#   - a recording: "build" and "end" both present, and the checkpoints the run wrote.
# Example event (key bit 3 goes down at 1.2 s, the fifth input queued):
#   {"t_us": 1200000, "round": 0, "class": 30, "seq": 4, "source": "scenario", "radio": "a",
#    "key": {"bit": 3, "down": true}}

# The time model this tool replays under (docs/dev/adr/0005-time-model.md). A recording made under another time model
# or another class table is refused, never replayed under the new rules (ADR 0005, consequences)
TIME_MODEL = "adr-0005"
# The class table: at one time and round a lower class runs first (ADR 0005 item 6; platform/host/vclock.h:24-31)
CLASS_TABLE_VERSION = 1
CLASS_TABLE = {10: "world", 20: "air", 30: "input", 40: "own", 50: "tick", 99: "end"}
# Start storage by name: blank flash (the firmware's defaults and built-in channels) or a research fixture
STORAGE_FIXTURES = ("blank", "dmr-data", "dmr-roaming")
# Production editor API steps (core/codeplug/codeplug.h:91,97,109,179,180) plus a settings override. There is no step
# and no field anywhere in version 2 that takes storage content, so a recording cannot hold a flash image
STORAGE_STEPS = {
    "dmr_id": ("id", ""),
    "radio_name": ("name", ""),
    "channel": ("number name rx_hz tx_hz mode", "color_code timeslot contact rx_only"),
    "zone": ("index name channels", ""),
    "contact": ("index name id type", "timeslot"),
    "setting": ("name value", ""),
}
# Recorded inputs by kind: (class, payload fields). Keys, encoder detents, host port bytes and network packets are
# inputs (class 30); scripted faults and restarts are world events (class 10). Classes 40, 50 and 99 are the radio's
# own and are never recorded as inputs; the end of the run is the "end" record
EVENT_KINDS = {
    "key": (30, "bit down"),
    "enc": (30, "count"),
    "host": (30, "stream hex"),
    "eth": (30, "hex"),
    "udp": (30, "destination port payload_hex"),
    "restart": (10, ""),
    "air_policy": (10, "command policy"),
    "host_link": (10, "enabled"),
    "link": (10, "target enabled"),
    "loss": (10, "target rate_ppm"),
    "corrupt": (10, "target ordinal xor_hex"),
    "duplicate": (10, "target ordinal"),
    "delay": (10, "target ordinal delay_us"),
    "flash_cut": (10, "units"),
    "flash_corrupt": (10, "offset xor_hex"),
}
EVENT_STAMP = ("t_us", "round", "class", "seq", "source", "radio")
HOST_MAX_BYTES = 65536		# one host port write; version 1 capped this at 600 (a debug link reply can be 1032)
CHECKPOINT_FIELDS = {
    "VIEW": "kind radio boot frame t_us screen depth focus count locked banner toast query",
    "FRAME": "kind radio boot frame t_us crc32 palette backlight",
    "STORE": "kind radio boot t_us sha256",
}
CHECKPOINT_RANK = {"VIEW": 0, "FRAME": 1, "STORE": 2}	# at one time VIEW is checked first (spec, VIEW checkpoint)
ASSERTION_FIELDS = {
    "delivery_count": ("count", ""),
    "udp_delivery": ("count source destination source_port port payload_hex", ""),
    "serial_udp_delivery": ("count source destination source_port port payload_hex", ""),
    "voice_event": ("event count", "from_us through_us"),
    "screen_text": ("at_us text visible", ""),
    "screen_id": ("at_us screen", ""),
    "state_value": ("field value", "at_us"),
    "no_tx": ("from_us through_us", ""),
    "codeplug_channel": ("at_us channel name rx_hz tx_hz", "rx_only"),
    "data_bptc": ("count", ""),
    "mmdvm_data_match": ("source_radio from_us through_us count", ""),
}
_IDENT = r"[a-zA-Z0-9_-]{1,64}"
_HEX64 = r"[0-9a-f]{64}"


def exact(value, required, where, optional=""):
    """Like fields(), but the message names each unknown and each missing field."""
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected an object")
    required, optional = set(required.split()), set(optional.split())
    unknown = sorted(set(value) - required - optional)
    missing = sorted(required - set(value))
    if unknown:
        raise ValueError(f"{where}: unknown field {', '.join(map(repr, unknown))}")
    if missing:
        raise ValueError(f"{where}: missing field {', '.join(map(repr, missing))}")


def text(value, pattern, where):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"{where}: expected a string matching {pattern}")


def boolean(value, where):
    if type(value) is not bool:
        raise ValueError(f"{where}: expected a boolean")


def hex_bytes(value, low, high, where):
    if not isinstance(value, str) or len(value) % 2 or not low <= len(value) // 2 <= high or \
            not re.fullmatch(r"[0-9a-fA-F]*", value):
        raise ValueError(f"{where}: expected {low} to {high} bytes of hexadecimal data")


def validate_eth(value):
    """Record complete Ethernet inputs, never opaque payloads, storage bytes or browser credentials.

    ARP and DHCP carry link setup. UDP uses the host/TFTP storage lint. TCP admits only the gateway's
    fixed HTTP requests and masked web control commands, including empty pongs and close codes.
    This check also runs through validate_v2, so recording lint cannot bypass it with an eth field.
    """
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    from tools.conductor.parts import tcppeer, webgw
    import recording_lint

    hex_bytes(value, 14, tcppeer.FRAME_MAX, "eth hex")
    frame = bytes.fromhex(value)

    def refuse(reason):
        raise ValueError("eth: " + reason)

    def padding(end):
        if not end <= len(frame) <= max(end, 60) or any(frame[end:]):
            refuse("invalid length or nonzero padding")

    if frame[12:14] == b"\x08\x06":
        padding(42)
        if frame[14:20] != b"\x00\x01\x08\x00\x06\x04" or frame[20:22] not in (b"\0\1", b"\0\2"):
            refuse("unsupported ARP")
        return
    ip = frame[14:34]
    if frame[12:14] != b"\x08\x00" or len(ip) != 20 or ip[0] != 0x45:
        refuse("unsupported Ethernet or IPv4 header")
    total = int.from_bytes(ip[2:4], "big")
    if total < 20 or int.from_bytes(ip[6:8], "big") & 0xbfff or tcppeer.checksum(ip):
        refuse("invalid or fragmented IPv4")
    padding(14 + total)
    segment = frame[34:14 + total]
    pseudo = ip[12:20] + struct.pack("!BBH", 0, ip[9], len(segment))
    if ip[9] == tcppeer.PROTO_UDP:
        if len(segment) < 8 or int.from_bytes(segment[4:6], "big") != len(segment):
            refuse("invalid UDP length")
        if segment[6:8] != b"\0\0" and tcppeer.checksum(pseudo + segment):
            refuse("invalid UDP checksum")
        data = segment[8:]
        if segment[:4] == struct.pack("!HH", tcppeer.DHCP_CLIENT, tcppeer.DHCP_SERVER):
            # Compare with the peer's synthetic DHCP forms; no hostname or arbitrary options are recorded.
            peer = tcppeer.Peer(b"")
            peer.xid = int.from_bytes(data[4:8], "big")
            request = data[243:255]
            if (data == peer._bootp(1) or data == peer._bootp(3, ciaddr=ip[12:16]) or
                    (request[:2] == b"\x32\x04" and request[6:8] == b"\x36\x04" and
                     data == peer._bootp(3, request))):
                return
            refuse("unsupported DHCP input")
        reason = recording_lint.storage_carrying(data)
        if reason:
            refuse(reason + " stored as bytes")
        return
    if ip[9] != tcppeer.PROTO_TCP or len(segment) < 20 or tcppeer.checksum(pseudo + segment):
        refuse("unsupported or invalid transport")
    offset = (segment[12] >> 4) * 4
    if (segment[2:4] != b"\0\x50" or not 20 <= offset <= len(segment) or
            segment[20:offset] not in (b"", struct.pack("!BBH", 2, 4, tcppeer.MSS))):
        refuse("unsupported TCP port or options")
    data = segment[offset:]
    if not data:
        return
    host = re.escape(tcppeer.dotted(ip[16:20]).encode())
    if re.fullmatch(rb"GET (/|/keys\.json|/radio\.svg|/mic\.svg) HTTP/1.1\r\nHost: " + host +
                    rb"\r\nAccept-Encoding: gzip\r\nConnection: close\r\n\r\n", data):
        return
    if re.fullmatch(rb"GET /ws HTTP/1.1\r\nHost: " + host +
                    rb"\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    rb"Sec-WebSocket-Key: [A-Za-z0-9+/]{22}==\r\nSec-WebSocket-Version: 13\r\n\r\n", data):
        return
    # shortcut: partial TCP application messages stop recording; add stream decoding if the gateway needs them.
    buffer = bytearray(data)
    frames = webgw.parse_frames(buffer, True, 125)
    if frames is None or buffer or not frames:
        refuse("undecoded TCP input")
    for opcode, payload in frames:
        if opcode == webgw.WS_TEXT and re.fullmatch(rb"[dueas]-?[0-9]{1,4}", payload):
            continue
        if opcode == webgw.WS_PONG and not payload or opcode == webgw.WS_CLOSE and len(payload) in (0, 2):
            continue
        refuse("unsupported web control input")


def validate_storage(storage, where):
    """Start storage: a fixture name, or a list of editor steps. Never bytes."""
    if isinstance(storage, str):
        if storage not in STORAGE_FIXTURES:
            raise ValueError(f"{where}: unknown fixture {storage!r}; expected one of {', '.join(STORAGE_FIXTURES)} "
                             "or a list of editor steps")
        return
    if not isinstance(storage, list) or not storage or len(storage) > 4096:
        raise ValueError(f"{where}: expected a fixture name ({', '.join(STORAGE_FIXTURES)}) or a list of 1 to "
                         "4096 editor steps")
    names = r"[ -~]{1,16}"		# CODEPLUG_NAME_LEN (core/codeplug/codeplug.h:17), printable ASCII
    for index, step in enumerate(storage):
        at = f"{where} step {index}"
        if not isinstance(step, dict) or step.get("op") not in STORAGE_STEPS:
            raise ValueError(f"{at}: op must be one of {', '.join(STORAGE_STEPS)}")
        required, optional = STORAGE_STEPS[step["op"]]
        exact(step, "op " + required, at, optional)
        op = step["op"]
        if op == "dmr_id":
            integer(step["id"], 1, 16776415, at + " id")	# CODEPLUG_DMR_ID_MAX (codeplug.h:19)
        if op in ("radio_name", "channel", "zone", "contact"):
            text(step["name"], names, at + " name")
        if op == "channel":
            integer(step["number"], 1, 1024, at + " number")
            for field in ("rx_hz", "tx_hz"):
                integer(step[field], 1, 999999999, f"{at} {field}")
            if step["mode"] not in ("fm", "dmr"):
                raise ValueError(f"{at} mode: expected fm or dmr")
            if "color_code" in step:
                integer(step["color_code"], 0, 15, at + " color_code")
            if "timeslot" in step:
                integer(step["timeslot"], 1, 2, at + " timeslot")
            if "contact" in step:
                integer(step["contact"], 0, 1024, at + " contact")
            if "rx_only" in step:
                boolean(step["rx_only"], at + " rx_only")
        if op == "zone":
            integer(step["index"], 0, 255, at + " index")
            channels = step["channels"]
            if not isinstance(channels, list) or len(channels) > 1024:
                raise ValueError(f"{at} channels: expected a list of channel numbers")
            for number in channels:
                integer(number, 1, 1024, at + " channel")
        if op == "contact":
            integer(step["index"], 1, 1024, at + " index")
            integer(step["id"], 1, 0xFFFFFF, at + " id")
            if step["type"] not in ("group", "private", "all_call"):
                raise ValueError(f"{at} type: expected group, private or all_call")
            if "timeslot" in step:
                integer(step["timeslot"], 0, 2, at + " timeslot")
        if op == "setting":
            text(step["name"], r"[a-z_][a-z0-9_]{0,63}", at + " name")
            if type(step["value"]) not in (bool, int, str) or \
                    (isinstance(step["value"], str) and len(step["value"]) > 64):
                raise ValueError(f"{at} value: expected a boolean, an integer or a string of at most 64 characters")


def validate_assertions_v2(assertions, names, duration):
    if not isinstance(assertions, list):
        raise ValueError("assertions: expected an array")
    for index, assertion in enumerate(assertions):
        at = f"assertion {index}"
        if not isinstance(assertion, dict) or assertion.get("condition") not in ASSERTION_FIELDS:
            raise ValueError(f"{at}: condition must be one of {', '.join(ASSERTION_FIELDS)}")
        condition = assertion["condition"]
        required, optional = ASSERTION_FIELDS[condition]
        if condition == "voice_event":
            event = assertion.get("event")
            if event not in ("begin", "frame", "lost", "end"):
                raise ValueError(f"{at}: voice event must be begin, frame, lost, or end")
            required += {"begin": " source destination group late_entry", "frame": " hex"}.get(event, "")
            if "from_us" in assertion or "through_us" in assertion:
                required += " from_us through_us"
        exact(assertion, "radio condition " + required, at, optional)
        if assertion["radio"] not in names:
            raise ValueError(f"{at}: unknown radio")
        if "at_us" in assertion:
            integer(assertion["at_us"], 0, duration, at + " at_us")
        if "from_us" in assertion:
            integer(assertion["from_us"], 0, duration, at + " from_us")
            integer(assertion["through_us"], assertion["from_us"], duration, at + " through_us")
        if "count" in assertion:
            integer(assertion["count"], 0 if condition != "mmdvm_data_match" else 1, 0xFFFFFFFF, at + " count")
        if condition == "mmdvm_data_match" and (assertion["source_radio"] not in names or
                                                assertion["source_radio"] == assertion["radio"]):
            raise ValueError(f"{at}: MMDVM match requires a distinct source radio")
        if condition == "screen_id":
            text(assertion["screen"], r"[a-z_][a-z0-9_-]{0,63}", at + " screen")
        if condition == "screen_text":
            text(assertion["text"], r"[\s\S]{1,128}", at + " text")
            boolean(assertion["visible"], at + " visible")
        if condition == "state_value":
            text(assertion["field"], r"[a-z_][a-z0-9_]{0,63}", at + " field")
            if type(assertion["value"]) not in (bool, int, str) or \
                    (isinstance(assertion["value"], str) and len(assertion["value"]) > 128):
                raise ValueError(f"{at} value: expected a boolean, an integer or a short string")
        if condition == "codeplug_channel":
            integer(assertion["channel"], 1, 1024, at + " channel")
            text(assertion["name"], r"[ -~]{1,16}", at + " name")
            for field in ("rx_hz", "tx_hz"):
                integer(assertion[field], 1, 999999999, f"{at} {field}")
            if "rx_only" in assertion:
                boolean(assertion["rx_only"], at + " rx_only")
        if condition == "voice_event" and assertion["event"] == "begin":
            for field in ("source", "destination"):
                integer(assertion[field], 1, 0xFFFFFF, f"{at} {field}")
            for field in ("group", "late_entry"):
                boolean(assertion[field], f"{at} {field}")
        if condition == "voice_event" and assertion["event"] == "frame":
            text(assertion["hex"], r"[0-9a-fA-F]{18}", at + " hex")
        if condition in ("udp_delivery", "serial_udp_delivery"):
            for field in ("source", "destination"):
                text(assertion[field], r"[0-9.]{7,15}", f"{at} {field}")
                ipaddress.IPv4Address(assertion[field])
            for field in ("source_port", "port"):
                integer(assertion[field], 1, 65535, f"{at} {field}")
            hex_bytes(assertion["payload_hex"], 0, 1024, at + " payload_hex")


def validate_event_v2(event, index, names, duration, held):
    at = f"event {index}"
    if not isinstance(event, dict):
        raise ValueError(f"{at}: expected an object")
    kinds = [key for key in event if key in EVENT_KINDS]
    if len(kinds) != 1:
        unknown = sorted(set(event) - set(EVENT_STAMP) - {"id", "after"})
        raise ValueError(f"{at}: expected exactly one input kind ({', '.join(EVENT_KINDS)})" +
                         (f"; unknown field {', '.join(map(repr, unknown))}" if unknown else ""))
    kind = kinds[0]
    cls, payload_fields = EVENT_KINDS[kind]
    exact(event, " ".join(EVENT_STAMP) + " " + kind, at, "id after")
    integer(event["t_us"], 0, duration, at + " t_us")	# the end drain runs inputs due at the end time
    integer(event["round"], 0, 65535, at + " round")
    integer(event["class"], 0, 99, at + " class")
    if event["class"] not in CLASS_TABLE:
        raise ValueError(f"{at}: class {event['class']} is not in class table {CLASS_TABLE_VERSION}")
    if event["class"] != cls:
        raise ValueError(f"{at}: a {kind} input is class {cls} ({CLASS_TABLE[cls]}), not {event['class']}")
    integer(event["seq"], 0, 2**63 - 1, at + " seq")
    text(event["source"], r"[a-z][a-z0-9_-]{0,31}", at + " source")	# a fixed id, never a process id
    if event["radio"] not in names:
        raise ValueError(f"{at}: unknown radio")
    payload, where = event[kind], f"{at} {kind}"
    exact(payload, payload_fields, where)
    if kind == "key":
        integer(payload["bit"], 0, 31, where + " bit")
        boolean(payload["down"], where + " down")
        edge = (event["radio"], payload["bit"])
        if held.get(edge, False) == payload["down"]:
            raise ValueError(f"{where}: key bit {payload['bit']} is already {'down' if payload['down'] else 'up'}")
        held[edge] = payload["down"]
    if kind == "enc":
        integer(payload["count"], -127, 127, where + " count")
        if payload["count"] == 0:
            raise ValueError(f"{where} count: an encoder input moves at least one detent")
    if kind == "host":
        text(payload["stream"], _IDENT, where + " stream")
        hex_bytes(payload["hex"], 1, HOST_MAX_BYTES, where + " hex")
    if kind == "eth":
        try:
            validate_eth(payload["hex"])
        except ValueError as error:
            raise ValueError(f"{where}: {error}") from error
    if kind == "udp":
        text(payload["destination"], r"[0-9.]{7,15}", where + " destination")
        ipaddress.IPv4Address(payload["destination"])
        integer(payload["port"], 1, 65535, where + " port")
        hex_bytes(payload["payload_hex"], 0, 1024, where + " payload_hex")
    if kind == "air_policy":
        # Snapshots use world radio numbers: positions in radios, starting at 1 (airctl.as_json).
        integer(event["round"], 0, 0, where + " round")
        if payload["command"] not in ("air_loss", "air_delay", "air_corrupt", "air_duplicate", "air_link"):
            raise ValueError(f"{where} command: unknown air policy change")
        policy = payload["policy"]
        exact(policy, "link_up down_links drops loss_ppm delays_us corruptions duplicates", where + " policy")
        boolean(policy["link_up"], where + " link_up")
        for field, width, radio_count in (("down_links", 2, 2), ("drops", 2, 1), ("loss_ppm", 3, 2),
                                         ("delays_us", 4, 2), ("corruptions", 4, 2), ("duplicates", 3, 2)):
            rows, seen = policy[field], set()
            if not isinstance(rows, list):
                raise ValueError(f"{where} {field}: expected an array")
            for row in rows:
                if not isinstance(row, list) or len(row) != width:
                    raise ValueError(f"{where} {field}: expected {width} fields")
                for radio in row[:radio_count]:
                    integer(radio, 1, len(names), where + " " + field + " radio")
                if radio_count == 2 and row[0] == row[1]:
                    raise ValueError(f"{where} {field}: must name different radios")
                if field in ("drops", "delays_us", "corruptions", "duplicates"):
                    integer(row[radio_count], 1, 2**64 - 1, where + " " + field + " ordinal")
                if field == "loss_ppm":
                    integer(row[-1], 1, 1000000, where + " rate_ppm")
                if field == "delays_us":
                    integer(row[-1], 1, 2**32 - 1, where + " delay_us")
                if field == "corruptions":
                    hex_bytes(row[-1], 1, 27, where + " xor_hex")
                    if not any(bytes.fromhex(row[-1])):
                        raise ValueError(f"{where} xor_hex: mask must change a bit")
                key = tuple(row[:-1] if field in ("loss_ppm", "delays_us", "corruptions") else row)
                if key in seen:
                    raise ValueError(f"{where} {field}: duplicate rule")
                seen.add(key)
    if kind in ("host_link", "link"):
        boolean(payload["enabled"], where + " enabled")
    if "target" in payload and (payload["target"] not in names or payload["target"] == event["radio"]):
        raise ValueError(f"{where} target: must name a different radio")
    if "ordinal" in payload:
        integer(payload["ordinal"], 1, 0xFFFFFFFF, where + " ordinal")
    if kind == "loss":
        integer(payload["rate_ppm"], 0, 1000000, where + " rate_ppm")
    if kind == "corrupt":
        text(payload["xor_hex"], r"[0-9a-fA-F]{66}", where + " xor_hex")
    if kind == "delay":
        integer(payload["delay_us"], 1, 3600 * 10**6, where + " delay_us")
    if kind == "flash_cut":
        integer(payload["units"], 0, 1000000, where + " units")
    if kind == "flash_corrupt":
        integer(payload["offset"], 0, 0x10000000 - 1, where + " offset")
        hex_bytes(payload["xor_hex"], 1, 512, where + " xor_hex")	# an XOR mask, not storage content
    return (event["t_us"], event["round"], event["class"], event["seq"])


def validate_checkpoints(checkpoints, names, duration):
    if not isinstance(checkpoints, list):
        raise ValueError("checkpoints: expected an array")
    last = None
    for index, record in enumerate(checkpoints):
        at = f"checkpoint {index}"
        if not isinstance(record, dict) or record.get("kind") not in CHECKPOINT_FIELDS:
            raise ValueError(f"{at}: kind must be VIEW, FRAME or STORE")
        exact(record, CHECKPOINT_FIELDS[record["kind"]], at)
        if record["radio"] not in names:
            raise ValueError(f"{at}: unknown radio")
        integer(record["boot"], 0, 65535, at + " boot")
        integer(record["t_us"], 0, duration, at + " t_us")
        if "frame" in record:
            integer(record["frame"], 0, 2**32 - 1, at + " frame")
        if record["kind"] == "VIEW":
            text(record["screen"], r"[a-z_][a-z0-9_-]{0,63}", at + " screen")
            for field, high in (("depth", 255), ("focus", 65535), ("count", 65535)):
                integer(record[field], 0, high, f"{at} {field}")
            boolean(record["locked"], at + " locked")
            for field in ("banner", "toast", "query"):
                text(record[field], r"[ -~]{0,128}", f"{at} {field}")
        if record["kind"] == "FRAME":
            text(record["crc32"], r"[0-9a-f]{8}", at + " crc32")
            palette = record["palette"]
            if palette is not None and (not isinstance(palette, list) or len(palette) != 16):
                raise ValueError(f"{at} palette: expected null or 16 RGB565 entries")
            for entry in palette or ():
                integer(entry, 0, 0xFFFF, at + " palette entry")
            integer(record["backlight"], 0, 100, at + " backlight")
        if record["kind"] == "STORE":
            text(record["sha256"], _HEX64, at + " sha256")	# a digest of the virtual flash, never its content
        order = (record["t_us"], CHECKPOINT_RANK[record["kind"]])
        if last is not None and order < last:
            raise ValueError(f"{at}: checkpoints out of order (by time, then VIEW, FRAME, STORE)")
        last = order


def validate_recording_build(build, at):
    exact(build, "sha256 version commit_time conductor_sha256", at)  # by hash, never a path
    text(build["sha256"], _HEX64, at + " sha256")
    text(build["conductor_sha256"], _HEX64, at + " conductor_sha256")
    text(build["version"], r"[ -~]{1,64}", at + " version")
    integer(build["commit_time"], 0, 2**63 - 1, at + " commit_time")


def validate_v2(value):
    """Validate a schema version 2 scenario or recording; return a copy. Refuse every unknown field."""
    if not isinstance(value, dict):
        raise ValueError("scenario: expected an object")
    exact(value, "schema_version name tier time_model class_table view_version seed epoch_utc duration_us "
          "radios events checkpoints assertions", "scenario", "build end")
    value = copy.deepcopy(value)
    integer(value["schema_version"], 2, 2, "schema_version")
    # Checked first after the version: a recording made under other order rules gives another run from the same stamps
    if value["time_model"] != TIME_MODEL:
        raise ValueError(f"replay refuses: time model {value['time_model']!r} is not the current {TIME_MODEL!r}")
    if value["class_table"] != CLASS_TABLE_VERSION or type(value["class_table"]) is not int:
        raise ValueError(f"replay refuses: class table {value['class_table']!r} is not the current "
                         f"{CLASS_TABLE_VERSION} ({', '.join(f'{k} {v}' for k, v in CLASS_TABLE.items())})")
    text(value["name"], r"[a-zA-Z0-9_-]{1,128}", "name")
    if value["tier"] not in ("local", "shareable"):
        raise ValueError("tier: expected local or shareable")
    integer(value["view_version"], 1, 65535, "view_version")
    integer(value["seed"], 0, 2**64 - 1, "seed")
    text(value["epoch_utc"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", "epoch_utc")
    try:
        time.strptime(value["epoch_utc"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise ValueError("epoch_utc: not a valid UTC time") from error
    duration = value["duration_us"]
    integer(duration, 1, 3600 * 10**6, "duration_us")
    recording = "build" in value or "end" in value
    if recording:
        if not {"build", "end"} <= set(value):
            raise ValueError("scenario: a recording needs both 'build' and 'end'")
        validate_recording_build(value["build"], "build")
        end = value["end"]
        exact(end, "t_us reason complete", "end")
        integer(end["t_us"], 0, duration, "end t_us")
        text(end["reason"], r"[ -~]{1,128}", "end reason")
        boolean(end["complete"], "end complete")
    elif value["checkpoints"]:
        raise ValueError("checkpoints: only a recording (with 'build' and 'end') holds checkpoints")
    radios = value["radios"]
    if not isinstance(radios, list) or not 1 <= len(radios) <= 16:
        raise ValueError("radios: expected 1 to 16 radios")
    names, identities = set(), set()
    for index, radio in enumerate(radios):
        at = f"radio {index}"
        exact(radio, "id dmr_id board storage", at, "clock build")
        if "build" in radio:
            if not recording:
                raise ValueError(f"{at} build: only a recording holds build identities")
            validate_recording_build(radio["build"], at + " build")
        text(radio["id"], _IDENT, at + " id")
        integer(radio["dmr_id"], 1, 0xFFFFFE, at + " dmr_id")
        if radio["id"] in names or radio["dmr_id"] in identities:
            raise ValueError("duplicate radio identity")
        names.add(radio["id"])
        identities.add(radio["dmr_id"])
        board = radio["board"]
        exact(board, "id display flash_size keymap_sha256", at + " board")
        text(board["id"], _IDENT, at + " board id")
        text(board["display"], r"(rgb565|indexed|mono)-\d{2,4}x\d{2,4}", at + " board display")
        integer(board["flash_size"], 0x40000, 0x10000000, at + " board flash_size")
        if board["flash_size"] % 4096:
            raise ValueError(f"{at} board flash_size: must be sector aligned")
        text(board["keymap_sha256"], _HEX64, at + " board keymap_sha256")
        validate_storage(radio["storage"], at + " storage")
        if "clock" in radio:
            exact(radio["clock"], "offset_us drift_ppm", at + " clock")
            integer(radio["clock"]["offset_us"], -(2**32 * 1000), 2**32 * 1000, at + " clock offset_us")
            integer(radio["clock"]["drift_ppm"], -1000, 1000, at + " clock drift_ppm")
    events = value["events"]
    if not isinstance(events, list):
        raise ValueError("events: expected an array")
    last, seqs, held, predecessors = None, set(), {}, {}
    # Live recording histories keep validation memory independent of session length.
    if type(events) is not list:
        seqs = type(events)()
    for index, event in enumerate(events):
        key = validate_event_v2(event, index, names, duration, held)
        # The stamp order rule: records are sorted by the order key (virtual µs, round, class, sequence), ADR 0005 item 6
        if last is not None and key <= last:
            raise ValueError(f"event {index}: stamp out of order: {key} is not after {last} "
                             "(order key: t_us, round, class, seq)")
        if event["seq"] in seqs:
            raise ValueError(f"event {index}: sequence {event['seq']} is used twice")
        last = key
        seqs.add(event["seq"])
        identity, dependencies = event.get("id"), event.get("after", [])
        if "id" in event:
            text(identity, _IDENT, f"event {index} id")
            if identity in predecessors:
                raise ValueError(f"event {index} id: {identity!r} is used twice")
        if (not isinstance(dependencies, list) or len(dependencies) > 256 or
                len(set(map(str, dependencies))) != len(dependencies) or (dependencies and identity is None)):
            raise ValueError(f"event {index} after: requires an id and distinct predecessor identifiers")
        if any(dep not in predecessors for dep in dependencies):
            raise ValueError(f"event {index} after: a predecessor must come earlier")
        if identity is not None:
            predecessors[identity] = index
    validate_checkpoints(value["checkpoints"], names, duration)
    validate_assertions_v2(value["assertions"], names, duration)
    return copy.deepcopy(value)


def serial_ipv4_frames(stream):
    """Check RFC 1662 output framing/FCS independently of the firmware parser."""
    if not stream:
        return []
    if stream[0] != 0x7e or stream[-1] != 0x7e:
        raise ValueError("incomplete PPP output")
    frames = []
    for encoded in stream.split(b"\x7e"):
        if not encoded:
            continue
        raw, escaped = bytearray(), False
        for byte in encoded:
            if escaped:
                raw.append(byte ^ 0x20)
                escaped = False
            elif byte == 0x7d:
                escaped = True
            else:
                raw.append(byte)
        if escaped or len(raw) < 6 or raw[:2] != b"\xff\x03":
            raise ValueError("invalid PPP output framing")
        crc = 0xffff
        for byte in raw:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ (0x8408 if crc & 1 else 0)
        if crc != 0xf0b8:
            raise ValueError("invalid PPP output FCS")
        if raw[2:4] == b"\x00\x21":
            frames.append(b"\0" * 12 + b"\x08\x00" + raw[4:-2])
    return frames


def udp_matches(frame, expected):
    from tools.conductor.parts.tcppeer import checksum

    if len(frame) < 42 or frame[12:15] != b"\x08\x00\x45" or frame[23] != 17:
        return False
    ip, udp = frame[14:34], frame[34:]
    source_port, port, length, crc = struct.unpack("!HHHH", udp[:8])
    if (int.from_bytes(ip[2:4], "big") != len(frame) - 14 or length != len(udp)
            or int.from_bytes(ip[6:8], "big") & 0xBFFF or checksum(ip) != 0):
        return False
    if crc and checksum(ip[12:20] + struct.pack("!BBH", 0, 17, length) + udp) != 0:
        return False
    return (ip[12:16] == ipaddress.IPv4Address(expected["source"]).packed
            and ip[16:20] == ipaddress.IPv4Address(expected["destination"]).packed
            and source_port == expected["source_port"] and port == expected["port"]
            and udp[8:] == bytes.fromhex(expected["payload_hex"]))


def validate_build(manifest, worker_digest):
    fields(manifest, "schema_version command cwd git_revision git_dirty compiler_version inputs worker_sha256 limits",
           "build manifest")
    integer(manifest["schema_version"], 1, 1, "build manifest schema_version")
    for key in ("cwd", "git_revision", "compiler_version", "limits"):
        if not isinstance(manifest[key], str) or not manifest[key]:
            raise ValueError(f"build manifest: invalid {key}")
    if type(manifest["git_dirty"]) is not bool:
        raise ValueError("build manifest: invalid git_dirty")
    if (not isinstance(manifest["command"], list) or not manifest["command"]
            or any(not isinstance(arg, str) for arg in manifest["command"])):
        raise ValueError("build manifest: invalid command")
    inputs = manifest["inputs"]
    if (not isinstance(inputs, dict) or not inputs
            or any(not path or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                   for path, digest in inputs.items())):
        raise ValueError("build manifest: invalid input hashes")
    if manifest["worker_sha256"] != worker_digest:
        raise ValueError("build manifest worker hash mismatch")


def start_storage(radio):
    """Translate the schema's storage description into the vhost editor syntax."""
    storage = radio["storage"]
    if storage == "blank":
        return "blank", 1
    if storage == "dmr-data":
        # The vhost fixture uses two synthetic identities, unlike the research fixture.
        if radio["dmr_id"] not in (3100001, 3100002):
            raise ValueError("WORLD_STORAGE_IDENTITY_UNAVAILABLE: dmr-data needs 3100001 or 3100002")
        return "dmr-pair", radio["dmr_id"] - 3100000
    if isinstance(storage, str):
        raise ValueError(f"WORLD_STORAGE_UNAVAILABLE: {storage}")
    steps = []
    for step in storage:
        op = step["op"]
        if op == "channel":
            if step.get("rx_only", False):
                raise ValueError("WORLD_STORAGE_RX_ONLY_UNAVAILABLE")
            values = [step[k] for k in ("number", "name", "rx_hz", "tx_hz", "mode")]
            values += [step.get("color_code", 1), step.get("timeslot", 1), step.get("contact", 0)]
        elif op == "zone":
            values = [step["index"], step["name"], *step["channels"]]
        elif op == "contact":
            values = [step[k] for k in ("index", "name", "id", "type")] + [step.get("timeslot", 0)]
        elif op == "setting":
            values = [step["name"], int(step["value"]) if isinstance(step["value"], bool) else step["value"]]
        else:
            values = [step["id"] if op == "dmr_id" else step["name"]]
        if any(any(c in str(value) for c in ",;") for value in values):
            raise ValueError("WORLD_STORAGE_DELIMITER_UNAVAILABLE: editor names cannot contain ',' or ';'")
        steps.append(op + "=" + ",".join(map(str, values)))
    return "steps:" + ";".join(steps), 1


def run(value, worker, expected_worker=None, expected_simulator=None):
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        validate(value)
        raise ValueError("WORLD_SCHEMA_V2_REQUIRED: use convert.py and review its flags")
    scenario = validate_v2(value)
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from tools.conductor.parts.faults import RETIRED_INPUTS, SUPPORTED_INPUTS
    if "build" in scenario:
        raise ValueError("WORLD_RECORDING_REPLAY_UNAVAILABLE: use tools/conductor/replay.py")
    from convert import board_profile, EPOCH
    profile = board_profile()
    if scenario["view_version"] != profile["view_version"]:
        raise ValueError("WORLD_VIEW_VERSION_UNAVAILABLE")
    if scenario["epoch_utc"] != EPOCH:
        raise ValueError("WORLD_EPOCH_UNAVAILABLE: no configurable world epoch yet")
    options = []
    for index, radio in enumerate(scenario["radios"], 1):
        if any(radio["board"][key] != profile["board"][key] for key in ("id", "flash_size", "keymap_sha256")):
            raise ValueError(f"WORLD_BOARD_UNAVAILABLE: {radio['id']}")
        if any(radio.get("clock", {}).values()):
            raise ValueError(f"WORLD_CLOCK_UNAVAILABLE: {radio['id']}")
        storage, member = start_storage(radio)
        options.append({"radio": index, "storage": storage, "radio_index": member,
                        "identity": radio["dmr_id"], "channel": 1})
    for event in scenario["events"]:
        kind = next(key for key in EVENT_KINDS if key in event)
        if kind not in ("key", "enc", "udp", *SUPPORTED_INPUTS):
            reason = ("retired: " + RETIRED_INPUTS[kind] if kind in RETIRED_INPUTS
                      else "needs a scheduled world API command")
            raise ValueError(f"WORLD_INPUT_UNAVAILABLE: {kind}; {reason}")
        if event["round"] != 0:
            raise ValueError("WORLD_INPUT_ROUND_UNAVAILABLE: world commands use round 0")
        if kind == "udp" and len(event["udp"]["payload_hex"]) // 2 > 558:
            raise ValueError("WORLD_BAD_UDP_PAYLOAD: virtual network frames hold at most 558 payload bytes")
    view_fields = {"screen", "depth", "focus", "count", "locked", "banner", "toast", "query", "boot", "frame"}
    for assertion in scenario["assertions"]:
        condition = assertion["condition"]
        if condition not in ("no_tx", "screen_id", "state_value", "udp_delivery"):
            raise ValueError(f"WORLD_ASSERTION_UNAVAILABLE: {condition}")
        if condition == "state_value" and assertion["field"] not in view_fields:
            raise ValueError(f"WORLD_STATE_UNAVAILABLE: {assertion['field']}")
    worker_digest = hashlib.sha256(Path(worker).read_bytes()).hexdigest()
    sources = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted((root / "tools/conductor").rglob("*.py")) if "tests" not in path.parts}
    conductor_digest = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    simulator_digest = hashlib.sha256(json.dumps({"conductor": conductor_digest, "vhost": worker_digest,
        "executor": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}, sort_keys=True).encode()).hexdigest()
    if expected_worker is not None and expected_worker != worker_digest:
        raise ValueError("vhost binary hash differs from expected worker SHA-256")
    if expected_simulator is not None and expected_simulator != simulator_digest:
        raise ValueError("simulator hash differs from expected simulator SHA-256")
    from tools.conductor.conductor import install_parts
    from tools.conductor.channel import Kind
    from tools.conductor.parts.dlmux import frame as debug_frame
    from tools.conductor.world import Config, World
    world = World(Config(executable=str(Path(worker).resolve()), radios=len(options),
                         radio_args=("--host-port", "on", "--encoder",
                                     "on" if any("enc" in e for e in scenario["events"]) else "off"),
                         end_us=scenario["duration_us"]))
    names = {index: radio["id"] for index, radio in enumerate(scenario["radios"], 1)}
    numbers = {name: index for index, name in names.items()}
    sources = {name: index for index, name in enumerate(sorted({e["source"] for e in scenario["events"]}), 1)}
    network_radios = {e["radio"] for e in scenario["events"] if "udp" in e}
    network_radios.update(a["radio"] for a in scenario["assertions"] if a["condition"] == "udp_delivery")
    trace, state, assertions, execution_error = [], {}, [], None
    frames_seen = set()
    pending_inputs = deque((index, event) for index, event in enumerate(scenario["events"])
                           if not any(kind in event for kind in SUPPORTED_INPUTS))
    start = time.monotonic()

    def command(name, **fields):
        response = world.api.dispatch({"version": 1, "command": name, **fields})
        if not response["ok"]:
            raise ValueError(response["error"])
        return response["result"]

    def output(radio, message):
        kind = {Kind.TRACE: "firmware", Kind.AIR_OUT: "burst", Kind.BYTES_OUT: "serial_output"}[message.kind]
        data = message.payload.decode("utf-8", errors="replace") if message.kind == Kind.TRACE else message.payload.hex()
        trace.append({"kind": kind, "radio": names[radio], "at_ms": message.time_us / 1000,
                      "t_us": message.time_us, "data": data})

    def view(radio, message):
        record = world.view_checkpoints.records[radio, world.radios[radio].boot][-1]
        state[names[radio]] = {**record, "radio": names[radio]}
        trace.append({"kind": "view", "radio": names[radio], "at_ms": message.time_us / 1000,
                      "t_us": message.time_us, "data": state[names[radio]]})

    def frame(radio, message):
        checkpoint = world.frames.checkpoints[-1] if world.frames.checkpoints else None
        if (checkpoint is not None and (checkpoint.radio, checkpoint.time_us) == (radio, message.time_us)
                and checkpoint not in frames_seen):
            frames_seen.add(checkpoint)
            trace.append({"kind": "frame", "radio": names[radio], "at_ms": message.time_us / 1000,
                          "t_us": message.time_us, "crc32": f"{checkpoint.crc:08x}",
                          "boot": checkpoint.boot, "frame": checkpoint.frame_id,
                          "palette": list(checkpoint.palette), "backlight": checkpoint.backlight})

    def notify(event):
        if event["event"] == "udp_delivery":
            trace.append({**event, "kind": "delivery", "radio": names[event["radio"]],
                          "t_us": event["time_us"], "at_ms": event["time_us"] / 1000})
            return
        # Pacing notes are wall-time measurements, not reproducible scenario output.
        if event["event"] != "state":
            trace.append({**json.loads(json.dumps(event)), "kind": event["event"],
                          "at_ms": event.get("time_us", world.now) / 1000})

    grants = {}

    def inputs_at(target, outboxes):
        # Submit after class-10 faults: restart discards the old radio's queued inputs.
        while pending_inputs and pending_inputs[0][1]["t_us"] <= target:
            index, event = pending_inputs.popleft()
            fields = {"radio": numbers[event["radio"]], "time_us": event["t_us"], "source": sources[event["source"]]}
            if "key" in event:
                command("key", **grants[fields["radio"]], **fields, key=event["key"]["bit"], state="down" if event["key"]["down"] else "up")
            elif "enc" in event:
                command("enc", **grants[fields["radio"]], **fields, detents=event["enc"]["count"])
            else:
                source_ip = str(ipaddress.IPv4Address(0x0c000000 | scenario["radios"][fields["radio"] - 1]["dmr_id"]))
                command("udp", **fields, **event["udp"], source_ip=source_ip, source_port=event["udp"]["port"])
            trace.append({**event, "kind": "input", "at_ms": event["t_us"] / 1000, "event_index": index})

    try:
        install_parts(world)
        world.api.notify = notify
        world.add_radio_args(lambda radio: ("--display-profile", scenario["radios"][radio - 1]["board"]["display"]))
        # Legacy UDP scenarios expect a network link. Select it through the firmware's own command.
        world.add_radio_args(lambda radio: ("--host", "0:" + debug_frame(0x0a, 0, b"\x19\x01").hex())
                             if names[radio] in network_radios else ())
        for kind in (Kind.TRACE, Kind.AIR_OUT, Kind.BYTES_OUT):
            world.on_outbox(kind, output)
        world.on_outbox(Kind.VIEW, view)
        world.on_outbox(Kind.FRAME, frame)
        command("air_config", groups=[list(names)], seed=scenario["seed"], radios=options)
        for radio in names:
            command("start", radio=radio)
        command("run", ratio="max")
        for radio in names:
            granted = command("lease.grant", radio=radio, principal="script:scenario")
            grants[radio] = {key: granted[key] for key in ("principal", "generation", "secret")}
        for index, event in enumerate(scenario["events"]):
            kind = next((kind for kind in SUPPORTED_INPUTS if kind in event), None)
            if kind is None:
                continue
            fields = dict(event[kind])
            if "target" in fields:
                fields["target"] = numbers[fields["target"]]
            command("fault", kind=kind, radio=numbers[event["radio"]], time_us=event["t_us"], **fields)
            trace.append({**event, "kind": "input", "at_ms": event["t_us"] / 1000, "event_index": index})
        world.add_grant_limit(lambda now: pending_inputs[0][1]["t_us"] if pending_inputs else None)
        world.on_grant_done(inputs_at)
        inputs_at(0, {})
        while world.state != "Ended":
            if time.monotonic() - start > HOST_TIMEOUT_S:
                raise TimeoutError(f"scenario exceeded {HOST_TIMEOUT_S} seconds")
            world.advance()
        if world.reason != "WORLD_END_TIME":
            raise RuntimeError(world.reason)
        for assertion in scenario["assertions"]:
            condition = assertion["condition"]
            if condition == "no_tx":
                actual = sum(e["kind"] == "burst" and e["radio"] == assertion["radio"] and
                             assertion["from_us"] <= e["t_us"] <= assertion["through_us"] for e in trace)
                expected = 0
            elif condition == "udp_delivery":
                actual = sum(e["kind"] == "delivery" and e["radio"] == assertion["radio"] and
                             all(e[key] == assertion[key] for key in ("source", "destination", "source_port", "port"))
                             and e["payload_hex"] == assertion["payload_hex"].lower() for e in trace)
                expected = assertion["count"]
            else:
                views = [e for e in trace if e["kind"] == "view" and e["radio"] == assertion["radio"] and
                         e["t_us"] <= assertion.get("at_us", scenario["duration_us"])]
                field = "screen" if condition == "screen_id" else assertion["field"]
                actual = views[-1]["data"].get(field) if views else None
                expected = assertion["screen"] if condition == "screen_id" else assertion["value"]
            assertions.append({**assertion, "actual": actual,
                               "passed": type(actual) is type(expected) and actual == expected})
    except Exception as error:
        execution_error = {"type": type(error).__name__, "message": str(error)}
    finally:
        world.close()
    trace.sort(key=lambda event: event.get("t_us", event.get("time_us", 0)))
    for index, event in enumerate(trace):
        event["id"] = f"{scenario['name']}:{index}"
    return {"schema_version": 1, "scenario": scenario, "worker_sha256": worker_digest,
            "worker_build": None, "simulator_sha256": simulator_digest,
            "status": "passed" if execution_error is None and all(a["passed"] for a in assertions) else "failed",
            "virtual_elapsed_ms": world.now / 1000, "assertions": assertions, "trace": trace, "state": state,
            "execution_error": execution_error, "model_coverage": "partial", "hardware_validation": "none",
            "components": {**component_status(), "conductor": {
                "model_coverage": "partial", "hardware_validation": "none", "provenance": "documentation-derived",
                "source": "tools/conductor", "source_sha256": conductor_digest,
                "limitations": "Only loss and fixture restart faults are bound; research controls and recording replay are unavailable."}}}


def execute(source, worker, output, expected_worker=None, expected_simulator=None):
    from tools.conductor.parts.resolve import private_folder, write_private
    from scenarios import SCENARIOS, WORLD_SCENARIOS, REFUSALS
    if source.startswith("@"):
        name = source[1:]
        if name in REFUSALS:
            raise ValueError(f"WORLD_SCENARIO_REFUSED: {name}: {REFUSALS[name]}")
        if name not in WORLD_SCENARIOS:
            raise ValueError(f"unknown built-in scenario: {name}")
        original = json.dumps(WORLD_SCENARIOS[name], indent=2) + "\n"
    else:
        original = Path(source).read_text()
    scenario = json.loads(original)
    (validate_v2 if scenario.get("schema_version") == 2 else validate)(scenario)
    output = output.resolve()
    if output.exists():
        raise ValueError("output directory already exists; choose a new run directory")
    result = run(scenario, worker, expected_worker, expected_simulator)
    private_folder(output)
    write_private(output / "original.json", original.encode("utf-8"))
    write_private(output / "scenario.json", (json.dumps(result["scenario"], indent=2) + "\n").encode("utf-8"))
    write_private(output / "trace.json", (json.dumps(result["trace"], indent=2) + "\n").encode("utf-8"))
    write_private(output / "result.json", (json.dumps(result, indent=2) + "\n").encode("utf-8"))
    root = Path(__file__).resolve().parents[2]
    replay = {"worker_sha256": result["worker_sha256"], "simulator_sha256": result["simulator_sha256"],
              "conductor_sha256": result["components"]["conductor"]["source_sha256"],
              "argv": ["python3", "tools/digital-twin/scenario.py", "run",
                       os.path.relpath(output / "scenario.json", root), "--out",
                       os.path.relpath(output.with_name(output.name + "-replay"), root),
                       "--expect-worker-sha256", result["worker_sha256"],
                       "--expect-simulator-sha256", result["simulator_sha256"]]}
    write_private(output / "replay.json", (json.dumps(replay, indent=2) + "\n").encode("utf-8"))
    report = ["# Digital twin run", "", f"Status: **{result['status']}**",
              f"Scenario: `{result['scenario']['name']}`; seed: {result['scenario']['seed']}",
              f"Virtual elapsed: {result['virtual_elapsed_ms']} ms",
              f"Assertions passed: {sum(a['passed'] for a in result['assertions'])}/{len(result['assertions'])}",
              f"Worker SHA-256: `{result['worker_sha256']}`",
              f"Simulator SHA-256: `{result['simulator_sha256']}`", "",
              f"Hardware validation: **{result['hardware_validation']}**",
              f"Decoded audio: **{result['components']['codec']['check_status']}**", "",
              "## Failures", "", "```json",
              json.dumps({"execution_error": result["execution_error"],
                          "assertions": [a for a in result["assertions"] if not a["passed"]]}, indent=2),
              "```", "", "## Model limits", "",
              "These describe model evidence, not which oracles this scenario executed.", "",
              "| Component | Coverage | Hardware validation | Provenance | Limitations |",
              "| --- | --- | --- | --- | --- |"]
    for name, component in result["components"].items():
        report.append(f"| {name} | {component['model_coverage']} | {component['hardware_validation']} | "
                      f"{component['provenance']} | {component['limitations']} |")
    report.extend(["", "## Artifacts", "",
                   "[Result and device state](result.json) · [Event trace](trace.json) · "
                   "[Scenario](scenario.json) · [Replay command](replay.json)", "",
                   "Source paths and hashes are recorded in the result's components map.", ""])
    write_private(output / "report.md", ("\n".join(report)).encode("utf-8"))
    return result


def compare_runs(left, right):
    observed = ("status", "virtual_elapsed_ms", "assertions", "trace", "state", "execution_error",
                "model_coverage", "hardware_validation")
    inputs, workers, builds = [], [], []
    for result in (left, right):
        optional = " worker_build" if "worker_build" in result else ""
        optional += " components" if "components" in result else ""
        fields(result, "schema_version scenario worker_sha256 simulator_sha256 " + " ".join(observed) + optional, "run result")
        integer(result["schema_version"], 1, 1, "result schema_version")
        current = result["scenario"].get("schema_version") == 2
        (validate_v2 if current else validate)(result["scenario"])
        scenario = copy.deepcopy(result["scenario"])
        selected_workers, selected_builds = {}, {}
        for radio in scenario["radios"]:
            selected = radio.pop("worker", None)
            if selected is not None and not isinstance(selected, dict):
                raise ValueError("run result must pin each selected radio worker")
            selected_workers[radio["id"]] = selected["sha256"] if selected else result["worker_sha256"]
            selected_builds[radio["id"]] = selected["build"] if selected else result.get("worker_build")
        inputs.append(scenario)
        workers.append(selected_workers)
        builds.append(selected_builds)
        if result["status"] not in ("passed", "failed"):
            raise ValueError("run result status must be passed or failed")
        elapsed = result["virtual_elapsed_ms"]
        if current:
            if type(elapsed) not in (int, float) or not 0 <= elapsed <= scenario["duration_us"] / 1000:
                raise ValueError("virtual_elapsed_ms: outside scenario duration")
        else:
            integer(elapsed, 0, scenario["duration_ms"], "virtual_elapsed_ms")
        for key in ("worker_sha256", "simulator_sha256"):
            if not isinstance(result[key], str) or not re.fullmatch(r"[0-9a-f]{64}", result[key]):
                raise ValueError(f"invalid {key}")
        if result.get("worker_build") is not None:
            validate_build(result["worker_build"], result["worker_sha256"])
        if not isinstance(result["trace"], list) or not isinstance(result["assertions"], list) or not isinstance(result["state"], dict):
            raise ValueError("run result requires trace/assertion arrays and a state object")
    if inputs[0] != inputs[1]:
        raise ValueError("scenario inputs differ; compare the same scenario and seed")
    changed = [key for key in observed if left[key] != right[key]]
    if left.get("components") != right.get("components"):
        changed.append("components")
    return {"schema_version": 1, "comparison": "different" if changed else "identical", "changed_fields": changed,
            "worker_changed": workers[0] != workers[1],
            "worker_build_changed": builds[0] != builds[1],
            "simulator_changed": left["simulator_sha256"] != right["simulator_sha256"],
            "left": {key: left[key] for key in ("status", "worker_sha256", "simulator_sha256")},
            "right": {key: right[key] for key in ("status", "worker_sha256", "simulator_sha256")}}


def main():
    parser = argparse.ArgumentParser(description="Run a private RadioFoundry host scenario without hardware")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list built-in host scenario names without starting workers")
    compare = commands.add_parser("compare", help="compare saved outcomes for identical scenario inputs")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    reducer = commands.add_parser("minimize", help="remove events while preserving a failure against a passing reference")
    reducer.add_argument("scenario")
    reducer.add_argument("--worker", required=True, type=Path)
    reducer.add_argument("--reference-worker", required=True, type=Path)
    reducer.add_argument("--out", required=True, type=Path)
    reducer.add_argument("--max-attempts", type=int, default=100)
    for name in ("run", "suite"):
        command = commands.add_parser(name, help="execute host scenarios and save results and replay artifacts")
        if name == "run":
            command.add_argument("scenario", help="JSON file path or @built-in-name")
        else:
            command.add_argument("scenarios", nargs="+", help="JSON paths or @built-in-names, in execution order")
        command.add_argument("--worker", type=Path,
                             default=Path(__file__).resolve().parents[2] / "build/vhost/rfoundry_vhost.elf")
        command.add_argument("--out", required=True, type=Path)
        command.add_argument("--expect-worker-sha256")
        command.add_argument("--expect-simulator-sha256")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.conductor.parts.resolve import private_folder, write_private
    try:
        from scenarios import SCENARIOS, WORLD_SCENARIOS, REFUSALS
        if args.command == "minimize":
            from reduction import minimize
            result = minimize(args.scenario, args.worker, args.reference_worker, args.out, args.max_attempts)
            print(json.dumps(result))
            return 0 if result["status"] == "passed" else 1
        if args.command == "compare":
            result = compare_runs(json.loads(args.left.read_text()), json.loads(args.right.read_text()))
            print(json.dumps(result))
            return 0 if result["comparison"] == "identical" else 1
        if args.command == "list":
            print(json.dumps({"schema_version": 1, "scenarios": sorted(set(SCENARIOS) | set(WORLD_SCENARIOS)),
                              "availability": {**REFUSALS, **dict.fromkeys(WORLD_SCENARIOS, "runs")}}))
            return 0
        if args.command == "suite":
            output = args.out.resolve()
            if output.exists():
                raise ValueError("output directory already exists; choose a new run directory")
            private_folder(output)
            cases = []
            result = {"schema_version": 1, "status": "not_run", "requested": len(args.scenarios), "cases": cases}
            write_private(output / "result.json", (json.dumps(result, indent=2) + "\n").encode("utf-8"))
            for index, source in enumerate(args.scenarios):
                destination = output / f"{index:04d}"
                try:
                    case = execute(source, args.worker, destination, args.expect_worker_sha256, args.expect_simulator_sha256)
                except Exception as error:
                    case = {"status": "failed", "error": str(error), "error_type": type(error).__name__}
                    private_folder(destination)
                    write_private(destination / "result.json", (json.dumps(case, indent=2) + "\n").encode("utf-8"))
                cases.append({"index": index, "input": source, "status": case["status"], "directory": destination.name})
                status = "failed" if any(case["status"] != "passed" for case in cases) else (
                    "passed" if len(cases) == len(args.scenarios) else "not_run")
                result = {"schema_version": 1, "status": status, "requested": len(args.scenarios), "cases": cases}
                write_private(output / "result.json", (json.dumps(result, indent=2) + "\n").encode("utf-8"))
        else:
            result = execute(args.scenario, args.worker, args.out, args.expect_worker_sha256, args.expect_simulator_sha256)
        print(json.dumps(result))
        return 0 if result["status"] == "passed" else 1
    except Exception as error:
        print(json.dumps({"status": "failed", "error": str(error), "error_type": type(error).__name__}))
        return 2


def cli_main():
    # A separate session bounds preflight/export too and lets us stop only owned workers.
    launch = ("import runpy,sys,os; path=sys.argv.pop(1); sys.path.insert(0,os.path.dirname(path)); "
              "sys.exit(runpy.run_path(path)['main']())")
    with subprocess.Popen([sys.executable, "-c", launch, str(Path(__file__).resolve()), *sys.argv[1:]],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
        try:
            output, errors = process.communicate(timeout=CLI_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            print(json.dumps({"status": "failed", "error_type": "TimeoutError",
                              "error": f"CLI exceeded {CLI_TIMEOUT_S} seconds; artifacts may be incomplete"}))
            return 124
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        sys.stdout.write(output)
        sys.stderr.write(errors)
        return process.returncode


if __name__ == "__main__":
    sys.exit(cli_main())
