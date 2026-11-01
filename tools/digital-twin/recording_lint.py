#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Lint v2 recordings and all sibling output files; promote a checked private copy.

storage_carrying takes one complete host write or TFTP packet as bytes. It returns
a reason for storage traffic and raises ValueError for incomplete/opaque traffic.
The recorder must stop on either result; it must never save an undecoded fragment.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import zlib

import scenario

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rfoundry_debug

# The same fixture identities and settings as boards/vhost/storage_vhost.c.
FIXTURE_IDS = range(3100001, 3101000)  # 3100001 to 3100999
FIXTURE_TALKGROUPS = (9,)  # the only group contact ID the editor accepts
SETTINGS = {"vfo", "squelch", "zone_index", "channel", "zone_pos", "brightness", "backlight_s", "beeps"}
SENTINELS = {"callsign": "K7PERSONAL", "dmr_id": "7654321", "position": "37.774929",
             "privacy_key": "0123456789abcdef0123456789abcdef"}
CAPS = {"host": scenario.HOST_MAX_BYTES, "udp": 1024, "flash_corrupt": 512, "corrupt": 33,
        "udp_delivery": 1024, "serial_udp_delivery": 1024, "voice_event": 9}


def storage_carrying(frame: bytes) -> str | None:
    """Decode complete host frames or a TFTP packet; refuse unknown/partial bytes."""
    if not isinstance(frame, bytes) or not frame:
        raise ValueError("frame: expected nonempty bytes")
    offset = 0
    while offset < len(frame):
        data = frame[offset:]
        if data.startswith(b"\xa5\x5a"):
            if len(data) < 8:
                raise ValueError("debug link: incomplete frame")
            cmd, size = data[2], int.from_bytes(data[4:6], "little")
            n = size + 8
            if size > (1024 if cmd & 0x80 else 64) or len(data) < n:
                raise ValueError("debug link: invalid length or incomplete frame")
            if rfoundry_debug.crc16(data[2:n - 2]) != int.from_bytes(data[n - 2:n], "little"):
                raise ValueError("debug link: bad CRC")
            payload = data[6:n - 2]
            if cmd & 0x7f == 0x1c:
                return "debug link CODEPLUG " + ("reply" if cmd & 0x80 else "write")
            if cmd & 0x7f == 0x0f:
                op = payload[0] if payload else -1
                return "debug link STORAGE " + ({0: "info", 1: "read", 2: "claim"}.get(op, "operation"))
            if cmd == 0x11 and payload:
                if payload[0] == 4 and payload[1:] != b"N0SIM":
                    return "fixture identity: APRS callsign"
                if payload[0] == 6 or (payload[0] == 5 and len(payload) > 1 and payload[1] in (4, 5)):
                    return "APRS position input"
            if cmd not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 0x11, 0x1b, 0x1d, 0x1f):
                raise ValueError("debug link: unsupported command; use a generator")
        elif data[0] in (ord("W"), ord("X")):
            if len(data) < 2:
                raise ValueError("CPS write: incomplete header")
            op = data[1]
            if op in (2, 4, 7):
                if len(data) < 8:
                    raise ValueError("CPS write: incomplete header")
                n = 8 + int.from_bytes(data[6:8], "big")
            elif op in (1, 3):
                n = 5 if op == 1 else 2
            else:
                raise ValueError("CPS write: unsupported operation")
            if n > 2048 or len(data) < n:
                raise ValueError("CPS write: invalid length or incomplete frame")
            return "CPS write frame"
        elif data[0] == ord("R"):
            n = 8
            if len(data) < n:
                raise ValueError("CPS read: incomplete frame")
        elif data[0] == ord("C"):
            if len(data) < 2:
                raise ValueError("CPS control: incomplete header")
            op, n = data[1], 2
            if op == ord("P"):
                return "APRS position input"
            if op == 2:
                end = data.find(b"\0", 7, 23)
                n = end + 1 if end >= 0 else 23
            elif op == 6:
                if len(data) < 3:
                    raise ValueError("CPS control: incomplete header")
                n = 7 if data[2] == 7 else 3
            elif op not in (0, 1, 3, 4, 5, 7, 254):
                raise ValueError("CPS control: unsupported operation")
            if len(data) < n:
                raise ValueError("CPS control: incomplete frame")
        elif data.startswith(b"\x00"):
            # DATA has no filename. Without transfer state every DATA packet is refused.
            if len(data) < 4:
                raise ValueError("TFTP: incomplete packet")
            op = int.from_bytes(data[:2], "big")
            if op in (1, 2):
                fields = data[2:].split(b"\0")
                if fields[-1] or len(fields) < 3 or len(fields) % 2 != 1 or not fields[0] or not fields[1]:
                    raise ValueError("TFTP: malformed request")
                name = fields[0].lstrip(b"/").lower()
                if name in (b"codeplug.bin", b"iddb.bin"):
                    return "TFTP codeplug packet" if name == b"codeplug.bin" else "TFTP ID database packet"
                raise ValueError("TFTP: unsupported file")
            if op == 3:
                return "TFTP codeplug DATA packet (unresolved transfer)"
            if op == 4 and len(data) == 4:
                return None
            raise ValueError("TFTP: unsupported packet")
        else:
            raise ValueError("opaque input: unsupported or incomplete frame")
        offset += n
    return None


def generated_bytes(reference):
    """One synthetic fixture: a blank block write, never caller-supplied storage."""
    scenario.exact(reference, "name params sha256", "generator")
    if reference["name"] != "codeplug-blank-block":
        raise ValueError("generator: unknown fixture")
    params = reference["params"]
    scenario.exact(params, "address seq", "generator params")
    scenario.integer(params["address"], 0x20000, 0xa0000, "generator address")
    if params["address"] % 4096:
        raise ValueError("generator address: must be sector aligned")
    scenario.integer(params["seq"], 0, 252, "generator seq")
    # cps_fast.h: BEGIN staged, BLOCK blank, END, COMMIT. Blank format has no stream.
    crc = zlib.crc32(b"\xff" * 4096)
    payloads = (b"\x03\x01", b"\x04" + struct.pack("<IBHI", params["address"], 2, 0, crc), b"\x06", b"\x07")
    data = b"".join(rfoundry_debug.frame(0x1c, params["seq"] + i, p) for i, p in enumerate(payloads))
    if reference["sha256"] != hashlib.sha256(data).hexdigest():
        raise ValueError("generator: byte hash mismatch")
    return data


def fail(rule, record=None, location="recording"):
    record = record or {}
    radio = record.get("radio", record.get("id", "all"))
    stamp = record.get("t_us", record.get("at_us", 0))
    stamp = stamp if type(stamp) is int else 0
    raise ValueError(f"{location}: {rule} at {stamp / 1000000:.3f} s radio {radio}")


def sentinel_bytes(data, record, location):
    for name, value in SENTINELS.items():
        for raw in (value.encode(), value.encode("utf-16-le"), value.encode("utf-16-be")):
            if raw in data or raw.hex().encode() in data.lower():
                fail(f"sentinel personal {name}", record, location)


def privacy(value, record=None, location="recording", kind=""):
    """Check structured inputs and outputs, keeping the nearest radio/time stamp."""
    if isinstance(value, dict):
        record = dict(record or {})
        record.update({k: value[k] for k in ("radio", "t_us", "at_us") if k in value})
        if "dmr_id" in value and "board" in value:
            record["radio"] = value.get("id", "all")
        for key, item in value.items():
            if key == "id" and value.get("op") == "contact" and value.get("type") == "group":
                if item not in FIXTURE_TALKGROUPS:
                    fail("fixture identity: talkgroup outside fixture set", record, location)
            elif key == "dmr_id" or (key == "id" and value.get("op") in ("dmr_id", "contact")):
                if item not in FIXTURE_IDS:
                    fail("fixture identity: DMR ID outside fixture set", record, location)
            if key == "name" and value.get("op") == "radio_name" and item != "N0SIM":
                fail("fixture identity: callsign outside fixture set", record, location)
            if key == "op" and item == "setting":
                name = value.get("name", "")
                if re.search(r"privacy|key", name):
                    fail("privacy key step", record, location)
                if name.startswith("aprs") or name in ("latitude", "longitude", "lat", "lon"):
                    fail("APRS position or server step outside fixture set", record, location)
                if name not in SETTINGS:
                    fail("storage setting outside fixture set", record, location)
            if key in ("hex", "payload_hex", "xor_hex") and isinstance(item, str):
                cap = CAPS.get(kind, 1024)
                if len(item) > 2 * cap:
                    fail(f"byte cap: {kind or key} exceeds {cap} bytes", record, location)
            privacy(item, record, location, key if key in CAPS else value.get("condition", kind))
    elif isinstance(value, list):
        for item in value:
            privacy(item, record, location, kind)
    elif isinstance(value, (str, int)):
        sentinel_bytes(str(value).encode(), record, location)


def lint(value, shareable=False):
    privacy(value)
    checked = copy.deepcopy(value)
    if not isinstance(checked, dict):
        fail("schema: expected an object")
    # The v2 validator currently accepts hex only. Validate the exact reference here,
    # then validate a reconstructed copy with that validator; never save its bytes.
    for index, event in enumerate(checked.get("events", [])):
        if not isinstance(event, dict):
            continue
        for kind, field in (("host", "hex"), ("udp", "payload_hex")):
            payload = event.get(kind)
            if not isinstance(payload, dict):
                continue
            try:
                if "generator" in payload:
                    scenario.exact(payload, "stream generator" if kind == "host" else
                                   "destination port generator", kind)
                    if kind != "host":
                        raise ValueError("generator: this fixture requires the host port")
                    payload[field] = generated_bytes(payload.pop("generator")).hex()
                elif field in payload:
                    scenario.hex_bytes(payload[field], 1, CAPS[kind], kind)
                    reason = storage_carrying(bytes.fromhex(payload[field]))
                    if reason:
                        fail(reason + " stored as bytes", event)
            except ValueError as error:
                if " s radio " in str(error):
                    raise
                fail(str(error), event)
        checked["events"][index] = event
    try:
        scenario.validate_v2(checked)
    except ValueError as error:
        match = re.match(r"(event|radio|checkpoint|assertion) (\d+)", str(error))
        record = None
        if match:
            group = {"event": "events", "radio": "radios", "checkpoint": "checkpoints", "assertion": "assertions"}
            record = checked[group[match[1]]][int(match[2])]
        fail("schema: " + str(error), record)
    if shareable and checked["tier"] != "shareable":
        fail("tier: local is not shareable")
    return value


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                fail("schema: duplicate JSON field", location=str(path))
            result[key] = value
        return result
    return json.loads(path.read_text(), object_pairs_hook=unique)


def scan_outputs(folder):
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            fail("output: symbolic link is not a recording file", location=str(path))
        if path.is_dir():
            continue
        if not path.is_file():
            fail("output: expected a regular file", location=str(path))
        if path.suffix.lower() in (".bin", ".flash", ".raw", ".wav", ".ambe", ".dfu", ".key"):
            fail("output: image, audio or key file", location=str(path))
        if path.suffix == ".json":
            privacy(read_json(path), location=str(path))
        elif path.suffix == ".jsonl":
            for line in path.read_text().splitlines():
                if line.strip():
                    privacy(json.loads(line), location=str(path))
        sentinel_bytes(path.read_bytes(), None, str(path))


def lint_file(path, shareable=False):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        fail("recording: expected a regular file", location=str(path))
    value = read_json(path)
    lint(value, shareable)
    if value["tier"] == "local":
        if path.stat().st_mode & 0o777 != 0o600 or path.parent.stat().st_mode & 0o777 != 0o700:
            fail("local permissions: require file 0600 in folder 0700", location=str(path))
    scan_outputs(path.parent)
    return value


def promote(path):
    path = Path(path)
    value = lint_file(path)
    if value["tier"] != "local":
        fail("promote: expected tier local")
    value["tier"] = "shareable"
    lint(value, shareable=True)
    target = path.with_name(path.stem + ".shareable.json")
    # Exclusive creation preserves both the local source and any previous promotion.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(json.dumps(value, indent=2) + "\n")
    except BaseException:
        target.unlink()
        raise
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shareable", action="store_true")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.paths[0] == "promote":
            if len(args.paths) != 2 or args.shareable:
                parser.error("use promote <local recording>")
            target = promote(args.paths[1])
            print(f"promoted: {target} tier: shareable")
        else:
            for item in args.paths:
                path = Path(item)
                if path.is_dir():
                    scan_outputs(path)
                    paths = sorted(path.rglob("*.json"))
                    if not paths:
                        fail("recording: folder has no v2 recordings", location=str(path))
                else:
                    paths = [path]
                for candidate in paths:
                    lint_file(candidate, args.shareable)
                    print(f"recordings-lint: {candidate}: OK")
    except (ValueError, OSError, UnicodeError) as error:
        print(f"recordings-lint: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
