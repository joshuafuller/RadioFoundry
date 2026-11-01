# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Privacy refusals, exact protocol parsing, generator checks and promotion."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

import recording_lint as lint

FIXTURE = Path(__file__).resolve().parents[2] / "tests/recordings/ping.json"
PING = bytes.fromhex("a55a0101000044c5")
CODEPLUG = lint.rfoundry_debug.frame(0x1c, 1, b"\x05\x00\x00\xff")
CLAIM = lint.rfoundry_debug.frame(0x0f, 1, b"\x02\x01\xd0\x00\x0f")
CPS_WRITE = b"X\x02\x00\x02\x00\x00\x00\x02\xff\xff"
TFTP_WRITE = b"\x00\x02codeplug.bin\x00octet\x00"


def recording():
    return json.loads(FIXTURE.read_text())


def event(data, kind="host"):
    payload = {"stream": "host-port", "hex": data.hex()} if kind == "host" else {
        "destination": "10.0.0.1", "port": 69, "payload_hex": data.hex()}
    return dict(t_us=12400000, round=0, seq=0, source="scenario", radio="a", **{"class": 30, kind: payload})


def blank_reference():
    # Independently frame the firmware's four blank-block operations (cps_fast.h).
    payloads = [b"\x03\x01", b"\x04" + struct.pack("<IBHI", 0x20000, 2, 0, zlib.crc32(b"\xff" * 4096)),
                b"\x06", b"\x07"]
    data = b"".join(lint.rfoundry_debug.frame(0x1c, i + 1, p) for i, p in enumerate(payloads))
    return {"name": "codeplug-blank-block", "params": {"address": 0x20000, "seq": 1},
            "sha256": hashlib.sha256(data).hexdigest()}, data


class RecordingLint(unittest.TestCase):
    def refuses(self, value, rule, time="12.400", radio="a", shareable=True):
        with self.assertRaises(ValueError) as caught:
            lint.lint(value, shareable)
        message = str(caught.exception)
        self.assertIn(rule, message)
        self.assertIn(f"at {time} s radio {radio}", message)
        return message

    def test_required_raw_input_refusals_in_both_tiers(self):
        for data, kind, rule in ((CODEPLUG, "host", "debug link CODEPLUG write"),
                                 (CLAIM, "host", "debug link STORAGE claim"),
                                 (CPS_WRITE, "host", "CPS write frame"),
                                 (TFTP_WRITE, "udp", "TFTP codeplug packet")):
            for tier in ("local", "shareable"):
                with self.subTest(rule=rule, tier=tier):
                    value = recording()
                    value["tier"], value["events"] = tier, [event(data, kind)]
                    message = self.refuses(value, rule, shareable=tier == "shareable")
                    if tier == "shareable":
                        print(message)

    def test_fixture_identity_and_storage_steps(self):
        for step, rule in (({"op": "dmr_id", "id": 1234567}, "fixture identity"),
                           ({"op": "radio_name", "name": "K1REAL"}, "fixture identity"),
                           ({"op": "setting", "name": "privacy_key", "value": "aabb"}, "privacy key step"),
                           ({"op": "setting", "name": "aprs_lat", "value": 123}, "APRS position"),
                           ({"op": "setting", "name": "aprs_is_server", "value": "example.org"}, "APRS position")):
            with self.subTest(step=step):
                value = recording()
                value["radios"][0]["storage"] = [step]
                print(self.refuses(value, rule, time="0.000"))
        value = recording()
        value["radios"][0]["dmr_id"] = 1234567
        self.refuses(value, "fixture identity", time="0.000")
        value = recording()
        value["radios"][0]["storage"] = [{"op": "dmr_id", "id": 3100001},
                                           {"op": "radio_name", "name": "N0SIM"},
                                           {"op": "setting", "name": "brightness", "value": 50}]
        self.assertEqual(lint.lint(value, True), value)

    def test_group_fixture_identities_and_talkgroup(self):
        """dmr-group: identities 3100001..3100999 and talkgroup 9 pass; one past either end, or another TG, fail."""
        group = {"op": "contact", "index": 1, "name": "TG9", "id": 9, "type": "group", "timeslot": 1}
        value = recording()
        value["radios"][0]["dmr_id"] = 3100999
        value["radios"][0]["storage"] = [{"op": "dmr_id", "id": 3100004}, group]
        self.assertEqual(lint.lint(value, True), value)
        for step in ({"op": "dmr_id", "id": 3101000}, {"op": "dmr_id", "id": 3100000}, {**group, "id": 91},
                     {**group, "id": 3100002}, {**group, "type": "private"}):
            with self.subTest(step=step):
                value = recording()
                value["radios"][0]["storage"] = [step]
                print(self.refuses(value, "fixture identity", time="0.000"))

    def test_byte_caps_and_local_tier(self):
        value = recording()
        value["events"] = [event(b"x" * (lint.scenario.HOST_MAX_BYTES + 1))]
        print(self.refuses(value, "byte cap"))
        value["events"] = [event(b"x" * 1025, "udp")]
        self.refuses(value, "byte cap")
        value["events"] = [dict(t_us=12400000, round=0, seq=0, source="scenario", radio="a",
                                 **{"class": 10, "flash_corrupt": {"offset": 0, "xor_hex": "ff" * 513}})]
        self.refuses(value, "byte cap")
        value = recording()
        value["tier"] = "local"
        print(self.refuses(value, "tier: local", time="0.000", radio="all"))

    def test_generator_passes_for_same_bytes_that_raw_form_refuses(self):
        reference, data = blank_reference()
        value = recording()
        value["events"] = [event(data)]
        self.refuses(value, "CODEPLUG write")
        value["events"][0]["host"] = {"stream": "host-port", "generator": reference}
        before = copy.deepcopy(value)
        self.assertEqual(lint.lint(value, True), before)
        self.assertEqual(value, before)
        self.assertEqual(lint.generated_bytes(reference), data)
        print("generator: same CODEPLUG write passes with verified SHA-256; raw bytes refused")
        for field, replacement in (("sha256", "0" * 64), ("name", "unknown"), ("hex", "ff")):
            bad = copy.deepcopy(value)
            bad["events"][0]["host"]["generator"][field] = replacement
            self.refuses(bad, "generator")

    def test_decoder_parses_boundaries_crc_lengths_and_tftp(self):
        self.assertIsNone(lint.storage_carrying(PING + PING))
        self.assertEqual(lint.storage_carrying(PING + CLAIM), "debug link STORAGE claim")
        self.assertEqual(lint.storage_carrying(b"\x00\x02/CODEPLUG.BIN\0octet\0tsize\x00716384\0"),
                         "TFTP codeplug packet")
        self.assertIn("DATA", lint.storage_carrying(b"\0\x03\0\x01xx"))
        self.assertIn("CPS", lint.storage_carrying(CPS_WRITE.replace(b"X", b"W", 1)))
        for data in (PING[:-1], PING[:-1] + b"\0", CODEPLUG[:5], CPS_WRITE[:7], CPS_WRITE[:-1],
                     b"\0\x02codeplug.bin", b"junk" + CLAIM, b"W", b"C\x06", b"\xa5"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                lint.storage_carrying(data)

    def test_unknown_fields_and_image_references_fail(self):
        value = recording()
        value["radios"][0]["storage"] = {"image": "private.bin"}
        self.refuses(value, "schema", time="0.000")
        value = recording()
        value["image"] = "ff"
        self.refuses(value, "unknown field", time="0.000", radio="all")

    def test_sentinel_in_view_output_and_encoded_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "recording.json"
            path.write_text(json.dumps(recording()))
            view = folder / "view.json"
            view.write_text(json.dumps({"kind": "VIEW", "radio": "a", "t_us": 12400000,
                                        "banner": lint.SENTINELS["callsign"]}))
            with self.assertRaisesRegex(ValueError, "sentinel personal callsign at 12.400 s radio a") as caught:
                lint.lint_file(path, True)
            print(str(caught.exception).replace(str(folder), "<output>"))
            view.unlink()
            for encoding in ("ascii", "utf-16-le", "utf-16-be"):
                output = folder / "trace.log"
                output.write_bytes(lint.SENTINELS["callsign"].encode(encoding))
                with self.assertRaisesRegex(ValueError, "sentinel personal callsign"):
                    lint.lint_file(path, True)
            output.write_text(lint.SENTINELS["callsign"].encode().hex())
            with self.assertRaisesRegex(ValueError, "sentinel personal callsign"):
                lint.lint_file(path, True)

    def test_promotion_preserves_source_checks_outputs_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "local.json"
            value = recording()
            value["tier"] = "local"
            original = json.dumps(value)
            path.write_text(original)
            path.chmod(0o600)
            target = lint.promote(path)
            self.assertEqual(json.loads(target.read_text())["tier"], "shareable")
            self.assertEqual(path.read_text(), original)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                lint.promote(path)
            target.unlink()
            (folder / "view.log").write_text(lint.SENTINELS["callsign"])
            with self.assertRaisesRegex(ValueError, "sentinel"):
                lint.promote(path)
            self.assertFalse(target.exists())
            (folder / "view.log").unlink()
            value["events"] = [event(CODEPLUG)]
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "CODEPLUG"):
                lint.promote(path)
            self.assertFalse(target.exists())

    def test_local_modes_symlinks_dump_outputs_and_duplicate_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "local.json"
            value = recording()
            value["tier"] = "local"
            path.write_text(json.dumps(value))
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "local permissions"):
                lint.lint_file(path)
            path.chmod(0o600)
            folder.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "local permissions"):
                lint.lint_file(path)
            folder.chmod(0o700)
            output = folder / "flash.bin"
            output.write_bytes(b"\xff")
            with self.assertRaisesRegex(ValueError, "image, audio or key"):
                lint.lint_file(path)
            output.unlink()
            output.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                lint.lint_file(path)
            output.unlink()
            path.write_text('{"tier":"local","tier":"shareable"}')
            with self.assertRaisesRegex(ValueError, "duplicate JSON field"):
                lint.lint_file(path)


if __name__ == "__main__":
    unittest.main()
