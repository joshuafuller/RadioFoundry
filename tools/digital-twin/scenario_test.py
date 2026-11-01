# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Scenario input boundary tests; validation must not launch radio workers."""
import copy
import gc
import hashlib
import json
import tempfile
from collections import deque
from unittest.mock import patch
import sys
from pathlib import Path
import tomllib
import unittest
import warnings

from pathlib import Path
import re

import scenario
from scenario import validate, validate_v2


def registry_profile(gallery, spi_size):
    for path in sorted((Path(__file__).resolve().parents[2] / "boards").glob("*/board.toml")):
        board = tomllib.loads(path.read_text())
        if board["hardware"]["gallery"] == gallery and board["flash"]["backup"]["spi_size"] == spi_size:
            return board["id"]
    raise AssertionError(f"No registry profile for gallery={gallery}, spi_size={spi_size}")


LANDSCAPE_PROFILE = registry_profile("160x120i5", 0x1000000)
PORTRAIT_PROFILE = registry_profile("240x320i10x5", 0x1000000)
UNMODELED_STORAGE_PROFILE = registry_profile("160x128", 0)


VALID = {
    "schema_version": 1, "name": "pair", "seed": 1, "duration_ms": 5000,
    "radios": [{"id": "a", "dmr_id": 1001}, {"id": "b", "dmr_id": 1002}],
    "events": [{"at_ms": 0, "radio": "a", "action": "udp", "destination": "225.0.0.201",
                "port": 6969, "payload_hex": "010203"}],
    "assertions": [{"radio": "b", "condition": "delivery_count", "count": 1}],
}


class ScenarioInput(unittest.TestCase):
    def test_registry_fixture_profiles_preserve_display_and_storage_coverage(self):
        for profile, dimensions, start, end in (
                (LANDSCAPE_PROFILE, (160, 120), 0, 0xE00000),
                (PORTRAIT_PROFILE, (240, 320), 0x700000, 0xF00000)):
            with self.subTest(profile=profile):
                value = copy.deepcopy(VALID)
                value["radios"][0].update(display=profile, flash_map=profile)
                radio = validate(value)["radios"][0]
                self.assertEqual((radio["display"]["width"], radio["display"]["height"]), dimensions)
                self.assertEqual(radio["flash_size"], 0x1000000)
                self.assertEqual(radio["flash_map"], {"openfw_eeprom": start,
                                 "openfw_flash": start + 0x20000, "openfw_end": end})
        value = copy.deepcopy(VALID)
        value["radios"][0]["flash_map"] = UNMODELED_STORAGE_PROFILE
        with self.assertRaisesRegex(ValueError, "NOR capacity is unknown"):
            validate(value)
        with self.assertRaisesRegex(AssertionError, "No registry profile"):
            registry_profile("missing-display-class", 0)

    def test_restart_phase_is_bounded_to_integrated_tick_boundary(self):
        value = copy.deepcopy(VALID)
        value['integrated_core'] = True
        for radio in value['radios']:
            radio['flash_size'] = 0x200000
        value['events'] = [dict(at_ms=100, radio='a', action='restart', phase='after_tick')]
        self.assertEqual(validate(value), value)
        for field, invalid in (('phase', 'during_tick'), ('phase', True), ('phase', []),
                               ('at_ms', 0), ('action', 'storage_reload')):
            candidate = copy.deepcopy(value)
            candidate['events'][0][field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                validate(candidate)
        value['integrated_core'] = False
        with self.assertRaises(ValueError):
            validate(value)

    def test_mmdvm_match_requires_distinct_source_positive_count_and_bounded_window(self):
        value = copy.deepcopy(VALID)
        value['assertions'] = [dict(condition='mmdvm_data_match', radio='a', source_radio='b',
                                    from_ms=0, through_ms=5000, count=7)]
        self.assertEqual(validate(value), value)
        for field, invalid in (('source_radio', 'a'), ('source_radio', 'missing'), ('source_radio', []),
                               ('count', 0), ('count', True), ('from_ms', -10), ('from_ms', 5),
                               ('through_ms', 5010), ('through_ms', '5000')):
            candidate = copy.deepcopy(value)
            candidate['assertions'][0][field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                validate(candidate)

    def test_timed_state_assertions_require_in_range_tick(self):
        value = copy.deepcopy(VALID)
        value['assertions'] = [dict(condition='state_value', radio='a', field='hotspot', value=True, at_ms=20)]
        self.assertEqual(validate(value), value)
        for invalid in (0, -10, 5, 5001, 5010, True, '20'):
            value['assertions'][0]['at_ms'] = invalid
            with self.subTest(at_ms=invalid), self.assertRaises(ValueError):
                validate(value)
        value['assertions'][0]['at_ms'] = 5000
        self.assertEqual(validate(value), value)

    def test_host_network_requires_integrated_boolean(self):
        value = copy.deepcopy(VALID)
        value.update(integrated_core=True, codeplug_fixture="dmr-data")
        for radio in value["radios"]:
            radio.update(flash_size=0x200000, host_network=True)
        self.assertEqual(validate(value), value)
        for invalid in (0, 1, "true", None):
            with self.subTest(invalid=invalid):
                value["radios"][0]["host_network"] = invalid
                with self.assertRaisesRegex(ValueError, "host_network"):
                    validate(value)
        value["radios"][0]["host_network"] = True
        value["integrated_core"] = False
        del value["codeplug_fixture"]
        with self.assertRaisesRegex(ValueError, "host_network"):
            validate(value)

    def test_retired_profile_and_worker_inputs_are_rejected(self):
        for key, selection in (("profile", LANDSCAPE_PROFILE), ("worker", "/not-installed/worker")):
            value = copy.deepcopy(VALID)
            value["integrated_core"] = True
            value["radios"][0][key] = selection
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "retired"):
                validate(value)

    def test_cli_does_not_advertise_retired_profiles(self):
        import subprocess
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("scenario.py")), "--help"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("profiles", result.stdout)

    def test_storage_registry_and_explicit_map_reject_invalid_inputs(self):
        for selection in (UNMODELED_STORAGE_PROFILE, "../fixture-profile", "missing-board"):
            value = copy.deepcopy(VALID)
            value["radios"][0]["flash_map"] = selection
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                validate(value)
        value = copy.deepcopy(VALID)
        value["radios"][0].update(flash_map=PORTRAIT_PROFILE, flash_size=0x200000)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            validate(value)
        value["radios"][0]["flash_size"] = 0x1000000
        normalized = validate(value)
        self.assertEqual(validate(normalized), normalized)
        for key, bad in (("openfw_eeprom", True), ("openfw_eeprom", -4096),
                         ("openfw_flash", 0x720001), ("openfw_flash", 0x701000),
                         ("openfw_end", 0x1001000), ("openfw_end", 0x721000)):
            candidate = copy.deepcopy(normalized)
            candidate["radios"][0]["flash_map"][key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_site_call_inputs_validate_frames_and_require_roaming_fixture(self):
        for action, length in (("site_data", 34), ("site_voice", 33)):
            value = copy.deepcopy(VALID)
            value.update(integrated_core=True, codeplug_fixture="dmr-roaming")
            for radio in value["radios"]:
                radio["flash_size"] = 0x200000
            value["events"] = [{"at_ms": 0, "radio": "a", "action": action,
                                "site": 1, "timeslot": 1, "hex": "00" * length}]
            self.assertEqual(validate(value), value)
            for field, invalid in (("site", 3), ("site", True), ("timeslot", 0), ("timeslot", 3),
                                   ("hex", "00" * 32), ("hex", "xx" * length)):
                candidate = copy.deepcopy(value)
                candidate["events"][0][field] = invalid
                with self.subTest(action=action, field=field), self.assertRaises(ValueError):
                    validate(candidate)
            value["codeplug_fixture"] = "dmr-data"
            with self.assertRaisesRegex(ValueError, "roaming fixture"):
                validate(value)

    def test_roaming_site_inputs_require_fixture_and_bounded_observations(self):
        value = copy.deepcopy(VALID)
        value.update(integrated_core=True, codeplug_fixture="dmr-roaming")
        for radio in value["radios"]:
            radio["flash_size"] = 0x200000
        value["events"] = [{"at_ms": 0, "radio": "a", "action": "site", "site": 0,
                            "available": True, "rssi_dbm": -85, "color_code": 1}]
        self.assertEqual(validate(value), value)
        for field, invalid in (("site", -1), ("site", 3), ("site", True), ("available", 1),
                               ("rssi_dbm", -128), ("rssi_dbm", 0), ("rssi_dbm", -85.5),
                               ("color_code", 16), ("color_code", False)):
            candidate = copy.deepcopy(value)
            candidate["events"][0][field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                validate(candidate)
        for fixture in (None, "dmr-data"):
            candidate = copy.deepcopy(value)
            candidate["codeplug_fixture"] = fixture
            with self.subTest(fixture=fixture), self.assertRaises(ValueError):
                validate(candidate)

    def test_repeater_power_requires_shared_topology_and_boolean(self):
        value = copy.deepcopy(VALID)
        for radio in value["radios"]:
            radio.update(repeater_mode=True, timeslot=2)
        value["events"] = [{"at_ms": 0, "radio": "a", "action": "repeater_power", "enabled": False}]
        with self.assertRaisesRegex(ValueError, "requires a shared repeater"):
            validate(value)
        value["repeater"] = {"hang_ms": 600}
        for enabled in (True, False):
            value["events"][0]["enabled"] = enabled
            self.assertEqual(validate(value), value)
        for invalid in (0, 1, None, "false", []):
            value["events"][0]["enabled"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "expected boolean"):
                validate(value)

    def test_confirmed_ip_policy_requires_boolean(self):
        for enabled in (True, False):
            value = copy.deepcopy(VALID)
            value["radios"][0]["confirmed_ip"] = enabled
            self.assertEqual(validate(value), value)
        for invalid in (0, 1, "true", None, []):
            value = copy.deepcopy(VALID)
            value["radios"][0]["confirmed_ip"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate(value)

    def test_talkgroup_requires_nonzero_24_bit_integer(self):
        for group in (1, 201, 0xFFFFFF):
            value = copy.deepcopy(VALID)
            value["radios"][0]["talkgroup"] = group
            self.assertEqual(validate(value), value)
        for group in (0, -1, 0x1000000, True, 201.0, "201", None):
            value = copy.deepcopy(VALID)
            value["radios"][0]["talkgroup"] = group
            with self.subTest(group=group), self.assertRaises(ValueError):
                validate(value)

    def test_color_code_requires_four_bit_integer(self):
        for code in (0, 1, 15):
            value = copy.deepcopy(VALID)
            value["radios"][0]["color_code"] = code
            self.assertEqual(validate(value), value)
        for code in (-1, 16, True, 1.0, "1", None):
            value = copy.deepcopy(VALID)
            value["radios"][0]["color_code"] = code
            with self.subTest(code=code), self.assertRaises(ValueError):
                validate(value)

    def test_ui_keys_and_screen_ids_are_validated(self):
        value = copy.deepcopy(VALID)
        value["events"] = [{"at_ms": 0, "radio": "a", "action": "key", "key": "select", "gesture": "press"}]
        value["assertions"] = [{"condition": "screen_id", "radio": "a", "at_ms": 100, "screen": "hub"}]
        self.assertEqual(validate(value), value)
        for field, bad in (("key", "ptt"), ("key", []), ("gesture", "repeat"), ("gesture", True)):
            candidate = copy.deepcopy(value)
            candidate["events"][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                validate(candidate)
        for field, bad in (("at_ms", 101), ("at_ms", True), ("screen", ""), ("screen", [])):
            candidate = copy.deepcopy(value)
            candidate["assertions"][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_codeplug_actions_require_supported_flash_and_valid_channel_name(self):
        value = copy.deepcopy(VALID)
        value["radios"][0]["flash_size"] = 0x200000
        value["events"] = [{"at_ms": 0, "radio": "a", "action": "channel_rename", "channel": 1, "name": "Saved"}]
        self.assertEqual(validate(value), value)
        for field, bad in (("channel", 0), ("channel", True), ("channel", 1025),
                           ("name", ""), ("name", "x" * 17), ("name", "bad\nname"), ("name", "é")):
            candidate = copy.deepcopy(value)
            candidate["events"][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                validate(candidate)
        for size in (True, 0x30000, 0x40001, 0x40000, 0x1001000):
            candidate = copy.deepcopy(value)
            candidate["radios"][0]["flash_size"] = size
            with self.subTest(size=size), self.assertRaises(ValueError):
                validate(candidate)
        value["events"] = [{"at_ms": 0, "radio": "a", "action": "flash_corrupt", "offset": 0x1FFFFF, "xor_hex": "01"}]
        self.assertEqual(validate(value), value)
        value["events"][0]["xor_hex"] = "0102"
        with self.assertRaises(ValueError):
            validate(value)

    def test_no_transmit_interval_is_bounded_ordered_and_tick_aligned(self):
        value = copy.deepcopy(VALID)
        value["assertions"] = [{"condition": "no_tx", "radio": "a", "from_ms": 30, "through_ms": 30}]
        self.assertEqual(validate(value), value)
        for start, end in ((True, 30), (-10, 30), (40, 30), (30, 5001), (31, 40), (30, 39)):
            candidate = copy.deepcopy(value)
            candidate["assertions"][0].update(from_ms=start, through_ms=end)
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                validate(candidate)

    @unittest.skip("registry file-handle check is not available yet")
    def test_repeated_registry_resolution_closes_input_files(self):
        value = copy.deepcopy(VALID)
        value["radios"][0]["display"] = LANDSCAPE_PROFILE
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always", ResourceWarning)
            for _ in range(3):
                self.assertEqual(validate(value)["radios"][0]["display"]["width"], 160)
            gc.collect()
        leaked = [str(item.message) for item in observed if issubclass(item.category, ResourceWarning)]
        self.assertEqual(leaked, [])

    def test_display_registry_selector_is_resolved_without_mutating_input(self):
        value = copy.deepcopy(VALID)
        value["radios"][0]["display"] = LANDSCAPE_PROFILE
        resolved = validate(value)
        self.assertEqual(value["radios"][0]["display"], LANDSCAPE_PROFILE)
        self.assertIsInstance(resolved["radios"][0]["display"], dict)
        self.assertEqual(validate(resolved), resolved)
        for bad in ("../fixture-profile", "no-such-twin-board", ""):
            value["radios"][0]["display"] = bad
            with self.subTest(board=bad), self.assertRaises(ValueError):
                validate(value)

    def test_display_configuration_rejects_bad_types_bounds_and_framebuffer_overflow(self):
        value = copy.deepcopy(VALID)
        value["radios"][0]["display"] = {"width": 240, "height": 320, "bpp": 4,
                                         "inset_left": 10, "inset_right": 10,
                                         "inset_top": 5, "inset_bottom": 5}
        self.assertEqual(validate(value), value)
        for field, bad in (("width", True), ("width", 0), ("width", 65536),
                           ("height", 321), ("bpp", 3), ("bpp", 16),
                           ("inset_left", 230), ("inset_top", -1), ("inset_bottom", 256),
                           ("font_aa", 1), ("font_big", "true")):
            candidate = copy.deepcopy(value)
            candidate["radios"][0]["display"][field] = bad
            with self.subTest(field=field, value=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_state_assertion_requires_simple_field_and_scalar_value(self):
        value = copy.deepcopy(VALID)
        value["assertions"] = [{"condition": "state_value", "radio": "a", "field": "brightness", "value": 30}]
        self.assertEqual(validate(value), value)
        for field, bad in (("field", ""), ("field", "state.brightness"), ("field", 0),
                           ("value", None), ("value", []), ("value", {}), ("value", 1.5),
                           ("value", "x" * 129)):
            candidate = copy.deepcopy(value)
            candidate["assertions"][0][field] = bad
            with self.subTest(field=field, value=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_screen_assertion_requires_valid_time_text_and_boolean(self):
        value = copy.deepcopy(VALID)
        value["assertions"] = [{"condition": "screen_text", "radio": "a", "at_ms": 0, "text": "IDLE", "visible": True}]
        self.assertEqual(validate(value), value)
        for field, bad in (("at_ms", 1), ("at_ms", 5000), ("at_ms", True), ("text", ""),
                           ("text", "x" * 129), ("visible", 1), ("radio", "missing")):
            candidate = copy.deepcopy(value)
            candidate["assertions"][0][field] = bad
            with self.subTest(field=field, value=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_flash_corruption_cannot_cross_image_bounds(self):
        value = copy.deepcopy(VALID)
        value["events"].insert(0, {"at_ms": 0, "radio": "a", "action": "flash_corrupt", "offset": 0x3FFFF, "xor_hex": "01"})
        self.assertEqual(validate(value), value)
        for field, bad in (("offset", -1), ("offset", 0x40000), ("offset", True),
                           ("xor_hex", "0102"), ("xor_hex", ""), ("xor_hex", "0g"),
                           ("xor_hex", "01" * 513)):
            candidate = copy.deepcopy(value)
            candidate["events"][0][field] = bad
            with self.subTest(field=field, value=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_storage_fault_and_setting_bounds(self):
        for action, field, good, invalid in (("brightness", "value", 30, (9, 101, True)),
                                             ("flash_cut", "units", 10, (-1, 1000001, True))):
            value = copy.deepcopy(VALID)
            value["events"].insert(0, {"at_ms": 0, "radio": "a", "action": action, field: good})
            self.assertEqual(validate(value), value)
            for bad in invalid:
                candidate = copy.deepcopy(value)
                candidate["events"][0][field] = bad
                with self.subTest(action=action, value=bad), self.assertRaises(ValueError):
                    validate(candidate)

    def test_tune_requires_positive_channel_identity(self):
        value = copy.deepcopy(VALID)
        value["events"].insert(0, {"at_ms": 0, "radio": "b", "action": "tune", "channel": 2})
        self.assertEqual(validate(value), value)
        for bad in (0, -1, 2**32, True, 1.5):
            candidate = copy.deepcopy(value)
            candidate["events"][0]["channel"] = bad
            with self.subTest(channel=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_host_link_requires_boolean_and_known_radio(self):
        value = copy.deepcopy(VALID)
        value["events"].insert(0, {"at_ms": 0, "radio": "b", "action": "host_link", "enabled": False})
        self.assertEqual(validate(value), value)
        for field, bad in (("enabled", 0), ("enabled", "false"), ("radio", "missing")):
            candidate = copy.deepcopy(value)
            candidate["events"][0][field] = bad
            with self.subTest(field=field, value=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_missed_tx_done_count_is_bounded(self):
        value = copy.deepcopy(VALID)
        value["events"].insert(0, {"at_ms": 0, "radio": "a", "action": "miss_tx_done", "count": 1})
        self.assertEqual(validate(value), value)
        for bad in (-1, 65536, True, 0.5):
            candidate = copy.deepcopy(value)
            candidate["events"][0]["count"] = bad
            with self.subTest(count=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_clock_fields_are_bounded_integers(self):
        value = copy.deepcopy(VALID)
        value["radios"][0]["clock"] = {"offset_us": -800, "drift_ppm": -10}
        self.assertEqual(validate(value), value)
        for field, bad in (("offset_us", True), ("offset_us", 2**32 * 1000 + 1),
                           ("offset_us", -(2**32 * 1000) - 1), ("drift_ppm", 1001),
                           ("drift_ppm", -1001), ("drift_ppm", 0.5)):
            candidate = copy.deepcopy(value)
            candidate["radios"][0]["clock"][field] = bad
            with self.subTest(field=field, value=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_delay_requires_bounded_aligned_duration(self):
        value = copy.deepcopy(VALID)
        value["events"].insert(0, {"at_ms": 0, "radio": "a", "action": "delay", "target": "b", "ordinal": 6, "delay_ms": 20})
        self.assertEqual(validate(value), value)
        for bad in (0, -10, 15, True, 3600010):
            candidate = copy.deepcopy(value)
            candidate["events"][0]["delay_ms"] = bad
            with self.subTest(delay=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_duplicate_requires_known_receiver_and_positive_ordinal(self):
        value = copy.deepcopy(VALID)
        value["events"].insert(0, {"at_ms": 0, "radio": "a", "action": "duplicate", "target": "b", "ordinal": 5})
        self.assertEqual(validate(value), value)
        for field, bad in (("ordinal", 0), ("ordinal", True), ("target", "a"), ("target", "missing")):
            candidate = copy.deepcopy(value)
            candidate["events"][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_sync_loss_is_an_instantaneous_event_for_one_known_radio(self):
        value = copy.deepcopy(VALID)
        value["events"].insert(0, {"at_ms": 0, "radio": "b", "action": "sync_loss"})
        self.assertEqual(validate(value), value)
        for fields in ({"enabled": True}, {"radio": "missing"}, {"at_ms": 1}):
            candidate = copy.deepcopy(value)
            candidate["events"][0].update(fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate(candidate)

    def test_voice_assertions_reject_invalid_types_and_frame_lengths(self):
        value = copy.deepcopy(VALID)
        begin = {"radio": "b", "condition": "voice_event", "event": "begin", "count": 1,
                 "source": 1001, "destination": 201, "group": True, "late_entry": False}
        value["assertions"] = [begin]
        self.assertEqual(validate(value), value)
        for field, bad in (("source", 0), ("destination", 0x1000000), ("group", 1),
                           ("late_entry", "false"), ("count", True), ("event", [])):
            candidate = copy.deepcopy(value)
            candidate["assertions"][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                validate(candidate)
        for bad in ("00" * 8, "00" * 10, "zz" * 9):
            value["assertions"] = [{"radio": "b", "condition": "voice_event", "event": "frame", "count": 1, "hex": bad}]
            with self.subTest(frame=bad), self.assertRaises(ValueError):
                validate(value)

    def test_corruption_requires_coded_frame_mask_and_positive_ordinal(self):
        value = copy.deepcopy(VALID)
        value["events"].insert(0, {"at_ms": 0, "radio": "a", "action": "corrupt", "target": "b",
                                    "ordinal": 5, "xor_hex": "01" + "00" * 32})
        self.assertEqual(validate(value), value)
        for field, bad in (("ordinal", 0), ("ordinal", True), ("xor_hex", "ff" * 32),
                           ("xor_hex", "ff" * 34), ("xor_hex", "gg" * 33), ("target", "a")):
            candidate = copy.deepcopy(value)
            candidate["events"][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_loss_rate_requires_bounded_integer_and_distinct_target(self):
        value = copy.deepcopy(VALID)
        value["events"].insert(0, {"at_ms": 0, "radio": "a", "action": "loss", "target": "b", "rate_ppm": 1000000})
        self.assertEqual(validate(value), value)
        for field, bad in (("rate_ppm", -1), ("rate_ppm", 1000001), ("rate_ppm", True),
                           ("rate_ppm", 0.5), ("target", "a"), ("target", "missing")):
            candidate = copy.deepcopy(value)
            candidate["events"][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_link_requires_distinct_known_target_and_boolean_state(self):
        value = copy.deepcopy(VALID)
        value["events"].insert(0, {"at_ms": 0, "radio": "a", "action": "link", "target": "b", "enabled": False})
        self.assertEqual(validate(value), value)
        for field, bad in (("target", "a"), ("target", "missing"), ("target", []), ("enabled", 0)):
            candidate = copy.deepcopy(value)
            candidate["events"][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_udp_assertion_validates_packet_expectations(self):
        value = copy.deepcopy(VALID)
        expected = {"radio": "b", "condition": "udp_delivery", "count": 1,
                    "source": "12.0.3.233", "destination": "225.0.0.201",
                    "source_port": 6969, "port": 6969, "payload_hex": "010203"}
        value["assertions"] = [expected]
        self.assertEqual(validate(value), value)
        for field, bad in (("source", 123), ("destination", "::1"), ("source_port", True),
                           ("port", 65536), ("payload_hex", "0"), ("payload_hex", "00" * 1025)):
            candidate = copy.deepcopy(value)
            candidate["assertions"][0][field] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                validate(candidate)

    def test_queue_limits_match_worker_integer_range(self):
        value = copy.deepcopy(VALID)
        event = {"at_ms": 0, "radio": "a", "action": "queue_limits", "wait_ms": 0, "max_age_ms": 65535}
        value["events"].insert(0, event)
        self.assertEqual(validate(value), value)
        for field in ("wait_ms", "max_age_ms"):
            for invalid in (-1, 65536, True, 1.5, "100"):
                candidate = copy.deepcopy(value)
                candidate["events"][0][field] = invalid
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    validate(candidate)

    def test_valid_input_is_copied_and_preserves_event_order(self):
        original = copy.deepcopy(VALID)
        result = validate(original)
        self.assertEqual(result["events"], original["events"])
        self.assertIsNot(result, original)
        self.assertEqual(original, VALID)

    def test_rejects_invalid_inputs(self):
        mutations = [
            lambda d: d.update(schema_version=2),
            lambda d: d.update(seed=True),
            lambda d: d.update(duration_ms=0),
            lambda d: d.update(hardware=True),
            lambda d: d["radios"][1].update(id="a"),
            lambda d: d["radios"][1].update(dmr_id=1001),
            lambda d: d["radios"][0].update(dmr_id=0),
            lambda d: d["radios"][0].update(clock={"drift_ppm": 10}),
            lambda d: d["events"][0].update(at_ms=5001),
            lambda d: d["events"][0].update(at_ms=15),
            lambda d: d["events"][0].update(radio="missing"),
            lambda d: d["events"][0].update(action="flash"),
            lambda d: d["events"][0].update(payload_hex="zz"),
            lambda d: d["events"][0].update(port=65536),
            lambda d: d["events"][0].update(destination="not-an-ip"),
            lambda d: d["assertions"][0].update(condition="always_pass"),
            lambda d: d["assertions"][0].update(count=-1),
            lambda d: d.update(assertions=[]),
        ]
        for mutate in mutations:
            value = copy.deepcopy(VALID)
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(value)


BOARD = {"id": "vhost", "display": "rgb565-160x128", "flash_size": 0x1000000, "keymap_sha256": "0" * 64}
VALID_V2 = {
    "schema_version": 2, "name": "pair", "tier": "local", "time_model": scenario.TIME_MODEL,
    "class_table": scenario.CLASS_TABLE_VERSION, "view_version": 4, "seed": 1,
    "epoch_utc": "2026-01-01T00:00:00Z", "duration_us": 5000000,
    "radios": [{"id": "a", "dmr_id": 1001, "board": dict(BOARD), "storage": "blank"},
               {"id": "b", "dmr_id": 1002, "board": dict(BOARD),
                "storage": [{"op": "dmr_id", "id": 1002},
                            {"op": "channel", "number": 1, "name": "Simplex", "rx_hz": 433500000,
                             "tx_hz": 433500000, "mode": "dmr", "color_code": 1, "timeslot": 1}]}],
    "events": [
        {"t_us": 1200000, "round": 0, "class": 30, "seq": 0, "source": "scenario", "radio": "a",
         "key": {"bit": 3, "down": True}},
        {"t_us": 1280000, "round": 0, "class": 10, "seq": 1, "source": "scenario", "radio": "a",
         "loss": {"target": "b", "rate_ppm": 1000}},
        {"t_us": 1280000, "round": 0, "class": 30, "seq": 2, "source": "scenario", "radio": "a",
         "key": {"bit": 3, "down": False}},
        {"t_us": 1500000, "round": 0, "class": 30, "seq": 3, "source": "debuglink", "radio": "a",
         "host": {"stream": "host-port", "hex": "ab" * 1032}},
    ],
    "checkpoints": [],
    "assertions": [{"radio": "a", "condition": "screen_id", "at_us": 2000000, "screen": "home"}],
}
RECORDING = dict(copy.deepcopy(VALID_V2),
                 build={"sha256": "1" * 64, "version": "v26.11-3-gabc1234", "commit_time": 1793000000,
                        "conductor_sha256": "2" * 64},
                 end={"t_us": 5000000, "reason": "virtual time limit", "complete": True},
                 checkpoints=[
                     {"kind": "VIEW", "radio": "a", "boot": 0, "frame": 7, "t_us": 2020000, "screen": "hub",
                      "depth": 1, "focus": 2, "count": 6, "locked": False, "banner": "", "toast": "", "query": ""},
                     {"kind": "FRAME", "radio": "a", "boot": 0, "frame": 7, "t_us": 2020000, "crc32": "6b4d7788",
                      "palette": None, "backlight": 100},
                     {"kind": "STORE", "radio": "a", "boot": 0, "t_us": 5000000, "sha256": "3" * 64}])


class SchemaVersion2(unittest.TestCase):
    def refuses(self, value, message):
        with self.assertRaises(ValueError) as caught:
            validate_v2(value)
        self.assertIn(message, str(caught.exception))

    def test_valid_scenario_and_recording_pass_and_are_copied(self):
        for value in (VALID_V2, RECORDING):
            original = copy.deepcopy(value)
            result = validate_v2(value)
            self.assertEqual(result, value)
            self.assertIsNot(result, value)
            self.assertEqual(value, original)

    def test_unknown_field_fails_and_names_it(self):
        for mutate, name in ((lambda d: d.update(image="00ff"), "'image'"),
                             (lambda d: d["radios"][0].update(flash_dump="00"), "'flash_dump'"),
                             (lambda d: d["radios"][0]["board"].update(path="/tmp/x"), "'path'"),
                             (lambda d: d["events"][0].update(phase="after_tick"), "'phase'"),
                             (lambda d: d["events"][0]["key"].update(gesture="long"), "'gesture'"),
                             (lambda d: d["events"][3].update(bytes="00"), "'bytes'"),
                             (lambda d: d["radios"][1]["storage"][0].update(hex="00"), "'hex'"),
                             (lambda d: d["assertions"][0].update(at_ms=2000), "'at_ms'")):
            value = copy.deepcopy(VALID_V2)
            mutate(value)
            with self.subTest(name=name):
                self.refuses(value, "unknown field " + name)
        value = copy.deepcopy(RECORDING)
        value["checkpoints"][1]["pixels"] = "00"
        self.refuses(value, "unknown field 'pixels'")
        value = copy.deepcopy(RECORDING)
        value["build"]["path"] = "/example/worker"
        self.refuses(value, "unknown field 'path'")

    def test_storage_is_only_a_fixture_name_or_editor_steps(self):
        for storage in ("00ff00ff", "flash.bin", {"image": "00ff"}, {"fixture": "blank"}, [], b"\x00".hex(),
                        [{"op": "write", "offset": 0, "hex": "00"}], [{"op": "raw", "data": "00"}],
                        [{"op": "dmr_id", "id": 1001, "hex": "00"}], ["dmr-data"], None, 0):
            value = copy.deepcopy(VALID_V2)
            value["radios"][0]["storage"] = storage
            with self.subTest(storage=storage), self.assertRaises(ValueError) as caught:
                validate_v2(value)
            self.assertIn("storage", str(caught.exception))
        for storage in scenario.STORAGE_FIXTURES:
            value = copy.deepcopy(VALID_V2)
            value["radios"][0]["storage"] = storage
            validate_v2(value)

    def test_channel_contact_is_optional_and_bounded(self):
        for contact in (0, 1, 1024):
            value = copy.deepcopy(VALID_V2)
            value["radios"][1]["storage"][1]["contact"] = contact
            self.assertEqual(validate_v2(value), value)
            storage, _ = scenario.start_storage(value["radios"][1])
            self.assertTrue(storage.endswith(f",dmr,1,1,{contact}"))
        for contact in (-1, 1025, True, "1", None):
            value = copy.deepcopy(VALID_V2)
            value["radios"][1]["storage"][1]["contact"] = contact
            with self.subTest(contact=contact):
                self.refuses(value, "contact")

    def test_stamp_out_of_order_fails(self):
        for first, second in (((1, 0, 30, 5), (0, 0, 30, 6)),	# time goes back
                              ((5, 1, 30, 5), (5, 0, 30, 6)),	# round goes back
                              ((5, 0, 30, 5), (5, 0, 10, 6)),	# class goes back at one time and round
                              ((5, 0, 30, 6), (5, 0, 30, 5)),	# sequence goes back
                              ((5, 0, 30, 5), (5, 0, 30, 5))):	# the same key twice
            value = copy.deepcopy(VALID_V2)
            events = []
            for (t_us, round_, cls, seq), bit in zip((first, second), (1, 2)):
                body = {"key": {"bit": bit, "down": True}} if cls == 30 else {"restart": {}}
                events.append({"t_us": t_us, "round": round_, "class": cls, "seq": seq, "source": "scenario",
                               "radio": "a", **body})
            value["events"] = events
            with self.subTest(first=first, second=second):
                self.refuses(value, "stamp out of order")
        value = copy.deepcopy(VALID_V2)
        value["events"][1], value["events"][2] = value["events"][2], value["events"][1]
        self.refuses(value, "stamp out of order")

    def test_other_time_model_or_class_table_is_refused_by_replay(self):
        for field, other in (("time_model", "adr-0005-tick-first"), ("time_model", "wall-clock"),
                             ("class_table", 2), ("class_table", 0), ("class_table", True), ("class_table", "1")):
            for base in (VALID_V2, RECORDING):
                value = copy.deepcopy(base)
                value[field] = other
                with self.subTest(field=field, other=other):
                    self.refuses(value, "replay refuses")

    def test_class_must_be_in_the_table_and_match_the_input_kind(self):
        for cls, message in ((50, "is class 30"), (99, "is class 30"), (10, "is class 30"), (31, "not in class table")):
            value = copy.deepcopy(VALID_V2)
            value["events"] = [dict(value["events"][0], **{"class": cls})]
            with self.subTest(cls=cls):
                self.refuses(value, message)

    def test_key_edges_alternate_and_host_bytes_have_no_600_byte_cap(self):
        value = copy.deepcopy(VALID_V2)
        value["events"][2]["key"]["down"] = True
        self.refuses(value, "already down")
        value = copy.deepcopy(VALID_V2)
        value["events"][3]["host"]["hex"] = "ab" * scenario.HOST_MAX_BYTES
        validate_v2(value)
        value["events"][3]["host"]["hex"] += "ab"
        self.refuses(value, "host hex")

    def test_recording_needs_build_and_end_and_scenario_has_no_checkpoints(self):
        value = copy.deepcopy(RECORDING)
        del value["end"]
        self.refuses(value, "needs both")
        value = copy.deepcopy(VALID_V2)
        value["checkpoints"] = RECORDING["checkpoints"]
        self.refuses(value, "only a recording")
        value = copy.deepcopy(RECORDING)
        value["checkpoints"].reverse()
        self.refuses(value, "out of order")

    def test_eth_is_a_bounded_class_30_input_with_no_extra_fields(self):
        from tools.conductor.parts import tcppeer

        peer = tcppeer.Peer(b"schema-eth")
        peer.start_dhcp(0)
        frame, = peer.output(0)
        value = copy.deepcopy(VALID_V2)
        event = {**{key: value["events"][0][key] for key in scenario.EVENT_STAMP}, "eth": {"hex": frame.hex()}}
        value["events"] = [event]
        self.assertEqual(validate_v2(value), value)
        event["class"] = 20
        self.refuses(value, "a eth input is class 30")
        event["class"] = 30
        event["eth"]["token"] = "not-recorded"
        self.refuses(value, "unknown field 'token'")
        del event["eth"]["token"]
        for bad in (None, 123, "", "00" * 13, "00" * 601, "gg" * 60, "0", frame[:-1].hex(),
                    (frame + b"secret").hex(), "00" * 60):
            event["eth"]["hex"] = bad
            with self.subTest(hex=bad):
                self.refuses(value, "eth")

    def test_version_1_contract_does_not_accept_version_2(self):
        with self.assertRaises(ValueError):
            validate(copy.deepcopy(VALID_V2))
        with self.assertRaises(ValueError):
            validate_v2(copy.deepcopy(VALID))

    def test_class_table_matches_the_virtual_clock(self):
        header = (Path(scenario.__file__).resolve().parents[2] / "platform/host/vclock.h").read_text()
        values = {int(value) for value in re.findall(r"VCLOCK_CLASS_\w+\s*=\s*(\d+)", header)}
        self.assertEqual(values, set(scenario.CLASS_TABLE))



sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.conductor.channel import DONE, Kind, Message
from tools.conductor.parts.view import HEADER as VIEW_HEADER
from tools.conductor.world import World
from tools.conductor.worldapi import WorldAPI


class ScenarioChannel:
    """Socket-free transport fixture; the real conductor dispatches every command."""
    made = []
    fail_radio = None

    def __init__(self, executable, *args):
        self.args = args
        self.radio = int(args[args.index("--radio-id") + 1])
        if self.radio == self.fail_radio:
            raise OSError("test startup failure")
        self.sent, self.closed = [], False
        payload = VIEW_HEADER.pack(self.radio, 1, 0, 0, 0, 0, 0, 0) + b"home\0\0\0\0"
        self.wire = deque([Message(Kind.VIEW, payload), Message(Kind.DONE, DONE.pack(1000, 0))])
        self.made.append(self)

    def send(self, message):
        self.sent.append(message)
        if message.kind in (Kind.GRANT, Kind.DRAIN):
            self.wire.append(Message(Kind.DONE, DONE.pack(message.time_us + 1000, 0), message.time_us))

    def receive(self):
        return self.wire.popleft()

    def close(self):
        self.closed = True


class WorldExecutor(unittest.TestCase):
    def setUp(self):
        from scenarios import WORLD_SCENARIOS
        self.value = copy.deepcopy(WORLD_SCENARIOS["air_pair"])
        # Boundary tests need only a few grants. Firmware timing is in runner_test.
        self.value["duration_us"] = 5000
        self.value["events"][0]["t_us"] = 0
        self.value["events"][1]["t_us"] = 4000
        self.value["assertions"][0]["through_us"] = 5000
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.binary = Path(self.directory.name) / "vhost"
        self.binary.write_bytes(b"test vhost binary")
        ScenarioChannel.made = []
        ScenarioChannel.fail_radio = None
        factory = patch("tools.conductor.world.World", side_effect=lambda config: World(
            config, channel_factory=ScenarioChannel))
        factory.start()
        self.addCleanup(factory.stop)

    def test_reducer_accepts_current_scenarios_and_keeps_artifacts_private(self):
        import os
        import stat
        from reduction import minimize
        reference = self.binary.with_name("reference")
        reference.write_bytes(b"reference fixture")
        self.value["assertions"] = [{"radio": "a", "condition": "screen_id", "screen": "reference", "at_us": 5000}]
        source = self.binary.with_name("input.json")
        source.write_text(json.dumps(self.value))

        class ReferenceChannel(ScenarioChannel):
            def __init__(self, executable, *args):
                super().__init__(executable, *args)
                if Path(executable) == reference:
                    self.wire[0] = Message(Kind.VIEW, VIEW_HEADER.pack(self.radio, 1, 0, 0, 0, 0, 0, 0)
                                           + b"reference\0\0\0\0")

        output = self.binary.with_name("reduction")
        mask = os.umask(0o022)
        try:
            with patch("tools.conductor.world.World", side_effect=lambda config: World(
                    config, channel_factory=ReferenceChannel)):
                report = minimize(str(source), self.binary, reference, output, 10)
        finally:
            os.umask(mask)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["remaining_events"], 0)
        self.assertTrue(any(trial["accepted"] for trial in report["trials"]))
        for path in [output, *output.rglob("*")]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600, path)

    def test_key_edges_start_storage_and_state_through_world_api(self):
        requests = []
        dispatch = WorldAPI.dispatch

        def capture(api, request):
            requests.append(request)
            return dispatch(api, request)

        self.value["assertions"] += [
            {"radio": "b", "condition": "screen_id", "at_us": 5000, "screen": "home"},
            {"radio": "a", "condition": "state_value", "field": "locked", "value": False}]
        with patch.object(WorldAPI, "dispatch", capture):
            result = scenario.run(self.value, self.binary)
        self.assertEqual(result["status"], "passed", result["execution_error"])
        self.assertEqual([r["command"] for r in requests], ["air_config", "start", "start", "run", "lease.grant", "lease.grant", "key", "key"])
        self.assertEqual([(r["time_us"], r["key"], r["state"]) for r in requests if r["command"] == "key"],
                         [(0, 4, "down"), (4000, 4, "up")])
        self.assertEqual([(m.time_us, m.payload) for m in ScenarioChannel.made[0].sent if m.kind == Kind.KEY],
                         [(0, b"\x04\x01"), (4000, b"\x04\x00")])
        for index, channel in enumerate(ScenarioChannel.made, 1):
            self.assertEqual(channel.args[channel.args.index("--storage") + 1], "dmr-pair")
            self.assertEqual(channel.args[channel.args.index("--radio-index") + 1], str(index))
            self.assertTrue(channel.closed)
        self.assertEqual(result["scenario"], self.value)
        self.assertEqual(result["worker_sha256"], hashlib.sha256(self.binary.read_bytes()).hexdigest())

    def test_encoder_and_editor_steps_reach_vhost(self):
        self.value["events"] = [{**self.value["events"][0], "enc": {"count": -2}}]
        del self.value["events"][0]["key"]
        self.value["radios"][0]["storage"] = [
            {"op": "dmr_id", "id": 3100001}, {"op": "radio_name", "name": "N0SIM"},
            {"op": "channel", "number": 1, "name": "Simplex", "rx_hz": 433500000,
             "tx_hz": 433500000, "mode": "dmr"},
            {"op": "zone", "index": 0, "name": "Pair", "channels": [1]},
            {"op": "contact", "index": 1, "name": "Peer", "id": 3100002, "type": "private"},
            {"op": "setting", "name": "beeps", "value": False}]
        result = scenario.run(self.value, self.binary)
        self.assertEqual(result["status"], "passed", result["execution_error"])
        channel = ScenarioChannel.made[0]
        self.assertEqual(channel.args[channel.args.index("--encoder") + 1], "on")
        self.assertEqual(channel.args[channel.args.index("--storage") + 1],
                         "steps:dmr_id=3100001;radio_name=N0SIM;channel=1,Simplex,433500000,433500000,dmr,1,1,0;"
                         "zone=0,Pair,1;contact=1,Peer,3100002,private,0;setting=beeps,0")
        self.assertEqual([m.payload for m in channel.sent if m.kind == Kind.ENC], [b"\xfe\xff\xff\xff"])

    def udp_scenario(self):
        from convert import convert
        old = copy.deepcopy(VALID)
        old["duration_ms"] = 20
        old["events"][0]["at_ms"] = 10
        old["assertions"] = [{"radio": "a", "condition": "udp_delivery", "count": 1,
                              "source": "12.0.3.233", "destination": "225.0.0.201",
                              "source_port": 6969, "port": 6969, "payload_hex": "010203"}]
        return convert(old)[0]

    def run_udp_scenario(self, value):
        class UdpChannel(ScenarioChannel):
            """Echo Ethernet output only to test the scenario adapter, not firmware delivery."""
            def __init__(self, *args):
                super().__init__(*args)
                self.pending = []

            def send(self, message):
                super().send(message)
                if message.kind == Kind.ETH_IN:
                    self.pending.append(message)
                if message.kind in (Kind.GRANT, Kind.DRAIN):
                    done = self.wire.pop()
                    for event in list(self.pending):
                        if event.time_us < message.time_us or (message.kind == Kind.DRAIN and
                                                              event.time_us == message.time_us):
                            self.pending.remove(event)
                            self.wire.append(Message(Kind.ETH_OUT, event.payload, event.time_us))
                    self.wire.append(done)

        with patch("tools.conductor.world.World", side_effect=lambda config: World(config, channel_factory=UdpChannel)):
            return scenario.run(value, self.binary)

    def test_old_udp_input_and_delivery_map_to_world_and_repeat(self):
        from tools.conductor.parts.udp import decode
        from tools.conductor.parts.dlmux import frame
        value = self.udp_scenario()
        requests = []
        dispatch = WorldAPI.dispatch

        def capture(api, request):
            requests.append(request)
            return dispatch(api, request)

        with patch.object(WorldAPI, "dispatch", capture):
            first = self.run_udp_scenario(value)
        self.assertEqual(first["status"], "passed", first["execution_error"])
        submitted, = [r for r in requests if r["command"] == "udp"]
        self.assertEqual(submitted, {"version": 1, "command": "udp", "radio": 1, "time_us": 10000,
                                    "source": 1, "source_ip": "12.0.3.233", "source_port": 6969,
                                    "destination": "225.0.0.201", "port": 6969, "payload_hex": "010203"})
        channel = ScenarioChannel.made[0]
        packet, = [m for m in channel.sent if m.kind == Kind.ETH_IN]
        self.assertEqual(packet.time_us, 10000)
        self.assertEqual(decode(packet.payload)["source"], "12.0.3.233")
        self.assertEqual(channel.args[channel.args.index("--host") + 1],
                         "0:" + frame(0x0a, 0, b"\x19\x01").hex())
        self.assertNotIn("--host", ScenarioChannel.made[1].args)
        delivered, = [e for e in first["trace"] if e["kind"] == "delivery"]
        self.assertEqual((delivered["radio"], delivered["t_us"]), ("a", 10000))
        self.assertEqual(delivered["payload_sha256"], hashlib.sha256(bytes.fromhex("010203")).hexdigest())
        self.assertEqual(first["assertions"][0]["actual"], 1)
        second = self.run_udp_scenario(value)
        self.assertEqual(second["trace"], first["trace"])
        self.assertEqual(second["assertions"], first["assertions"])

    def test_udp_delivery_matches_every_packet_field_and_radio(self):
        for field, different in (("radio", "b"), ("source", "12.0.3.234"), ("destination", "225.0.0.202"),
                                 ("source_port", 42000), ("port", 3334), ("payload_hex", "010204")):
            value = self.udp_scenario()
            value["assertions"][0][field] = different
            with self.subTest(field=field):
                result = self.run_udp_scenario(value)
                self.assertEqual(result["status"], "failed")
                self.assertIsNone(result["execution_error"])
                self.assertEqual(result["assertions"][0]["actual"], 0)
                if field == "radio":
                    self.assertIn("--host", ScenarioChannel.made[-1].args)

    def test_udp_frame_limit_fails_before_start(self):
        value = self.udp_scenario()
        value["events"][0]["udp"]["payload_hex"] = "ab" * 559
        with self.assertRaisesRegex(ValueError, "WORLD_BAD_UDP_PAYLOAD"):
            scenario.run(value, Path("/missing-vhost"))
        self.assertFalse(ScenarioChannel.made)

    def test_unsupported_inputs_and_observations_fail_before_start(self):
        variants = [
            (lambda v: v["events"][0].update(round=1), "WORLD_INPUT_ROUND_UNAVAILABLE"),
            (lambda v: v["radios"][0].update(clock={"offset_us": 1, "drift_ppm": 0}), "WORLD_CLOCK_UNAVAILABLE"),
            (lambda v: v["radios"][0]["board"].update(keymap_sha256="0" * 64), "WORLD_BOARD_UNAVAILABLE"),
            (lambda v: v.update(epoch_utc="2027-01-01T00:00:00Z"), "WORLD_EPOCH_UNAVAILABLE"),
            (lambda v: v["radios"][0].update(storage="dmr-roaming"), "WORLD_STORAGE_UNAVAILABLE"),
            (lambda v: v["radios"][0].update(dmr_id=1001), "WORLD_STORAGE_IDENTITY_UNAVAILABLE"),
            (lambda v: v["assertions"].append({"condition": "state_value", "radio": "a", "field": "hotspot",
                                              "value": True}), "WORLD_STATE_UNAVAILABLE"),
            (lambda v: v["assertions"].append({"condition": "delivery_count", "radio": "a", "count": 0}),
             "WORLD_ASSERTION_UNAVAILABLE"),
        ]
        for mutate, reason in variants:
            value = copy.deepcopy(self.value)
            mutate(value)
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                scenario.run(value, Path("/missing-vhost"))
        self.value["events"] = [{"t_us": 0, "round": 0, "class": 10, "seq": 0, "source": "scenario",
                                 "radio": "a", "flash_cut": {"units": 1}}]
        with self.assertRaisesRegex(ValueError, "WORLD_INPUT_UNAVAILABLE: flash_cut"):
            scenario.run(self.value, Path("/missing-vhost"))
        self.assertFalse(ScenarioChannel.made)

    def test_storage_delimiter_injection_is_refused(self):
        for name in ("Pair;setting=x", "Pair,other"):
            self.value["radios"][0]["storage"] = [{"op": "zone", "index": 0, "name": name, "channels": [1]}]
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "WORLD_STORAGE_DELIMITER_UNAVAILABLE"):
                scenario.run(self.value, self.binary)

    def test_failed_assertion_and_partial_startup_are_failures(self):
        self.value["assertions"] = [{"condition": "screen_id", "radio": "a", "at_us": 0, "screen": "hub"}]
        result = scenario.run(self.value, self.binary)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["execution_error"])
        self.assertFalse(result["assertions"][0]["passed"])
        ScenarioChannel.made = []
        ScenarioChannel.fail_radio = 2
        result = scenario.run(self.value, self.binary)
        self.assertEqual(result["status"], "failed")
        self.assertIn("test startup failure", result["execution_error"]["message"])
        self.assertEqual(len(ScenarioChannel.made), 1)
        self.assertTrue(ScenarioChannel.made[0].closed)

    def test_hash_pins_include_conductor_models_and_binary(self):
        first = scenario.run(self.value, self.binary)
        self.binary.write_bytes(b"different binary")
        with self.assertRaisesRegex(ValueError, "vhost binary hash"):
            scenario.run(self.value, self.binary, first["worker_sha256"])
        second = scenario.run(self.value, self.binary)
        self.assertNotEqual(first["simulator_sha256"], second["simulator_sha256"])
        read_bytes = Path.read_bytes

        def changed(path):
            return read_bytes(path) + (b"# changed model" if path.name == "airmodel.py" else b"")

        with patch.object(Path, "read_bytes", changed):
            with self.assertRaisesRegex(ValueError, "simulator hash"):
                scenario.run(self.value, self.binary, expected_simulator=second["simulator_sha256"])
            third = scenario.run(self.value, self.binary)
        self.assertNotEqual(second["components"]["conductor"]["source_sha256"],
                            third["components"]["conductor"]["source_sha256"])

    def test_artifacts_keep_hashes_and_no_absolute_home_paths(self):
        source = Path(self.directory.name) / "input.json"
        source.write_text(json.dumps(self.value))
        output = Path(self.directory.name) / "run"
        result = scenario.execute(str(source), self.binary, output)
        self.assertEqual(result["status"], "passed", result["execution_error"])
        for name, expected in (("trace", result["trace"]), ("result", result), ("scenario", self.value)):
            self.assertEqual(json.loads((output / (name + ".json")).read_text()), expected)
        replay = json.loads((output / "replay.json").read_text())
        self.assertEqual(replay["worker_sha256"], result["worker_sha256"])
        self.assertEqual(replay["conductor_sha256"], result["components"]["conductor"]["source_sha256"])
        self.assertNotIn(str(Path.home()), json.dumps(replay))
        self.assertIn("--expect-simulator-sha256", replay["argv"])
        with self.assertRaisesRegex(ValueError, "output directory already exists"):
            scenario.execute(str(source), self.binary, output)

    def test_every_named_scenario_runs_or_has_a_named_refusal(self):
        from scenarios import SCENARIOS, WORLD_SCENARIOS, REFUSALS
        self.assertEqual(set(SCENARIOS), set(REFUSALS))
        self.assertTrue(WORLD_SCENARIOS)
        for name, value in WORLD_SCENARIOS.items():
            self.assertEqual(validate_v2(value), value)
        for name, reason in REFUSALS.items():
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                scenario.execute("@" + name, Path("/missing-vhost"), Path(self.directory.name) / name)
            self.assertIn(reason, str(caught.exception))

    def check_timeout(self):
        # Also called by the runner test in place of its old worker socket watchdog.
        self.setUp()
        try:
            with patch.object(scenario, "HOST_TIMEOUT_S", -1):
                result = scenario.run(self.value, self.binary)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["execution_error"]["type"], "TimeoutError")
            self.assertTrue(ScenarioChannel.made and all(c.closed for c in ScenarioChannel.made))
            self.assertEqual(scenario.CLI_TIMEOUT_S, 90)
        finally:
            self.doCleanups()


if __name__ == "__main__":
    unittest.main()
