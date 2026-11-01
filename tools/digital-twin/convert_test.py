# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Version 1 to version 2 conversion: each action is mapped or refused with a named reason. Starts no worker."""
import contextlib
import copy
import io
import json
import unittest

import convert
import scenario
import scenarios

V1 = {
    "schema_version": 1, "name": "pair", "seed": 7, "duration_ms": 5000,
    "radios": [{"id": "a", "dmr_id": 1001}, {"id": "b", "dmr_id": 1002}],
    "events": [
        {"at_ms": 1200, "radio": "a", "action": "key", "key": "down", "gesture": "press"},
        {"at_ms": 1200, "radio": "a", "action": "loss", "target": "b", "rate_ppm": 1000},
        {"at_ms": 1500, "radio": "a", "action": "serial_input", "hex": "e00300"},
        {"at_ms": 2000, "radio": "a", "action": "key", "key": "select", "gesture": "long"},
        {"at_ms": 3000, "radio": "b", "action": "ptt", "down": True},
        {"at_ms": 3100, "radio": "a", "action": "screen"},
        {"at_ms": 3500, "radio": "b", "action": "ptt", "down": False},
        {"at_ms": 3600, "radio": "a", "action": "delay", "target": "b", "ordinal": 2, "delay_ms": 30},
    ],
    "assertions": [{"radio": "a", "condition": "screen_id", "at_ms": 4000, "screen": "home"},
                   {"radio": "b", "condition": "no_tx", "from_ms": 0, "through_ms": 2990}],
}


def events_of(result):
    return [(event["t_us"], event["class"], next(k for k in event if k in scenario.EVENT_KINDS),
             event[next(k for k in event if k in scenario.EVENT_KINDS)]) for event in result["events"]]


class Convert(unittest.TestCase):
    def setUp(self):
        self.board = convert.board_profile()
        self.bits = self.board["actions"]

    def test_key_map_comes_from_the_virtual_board(self):
        # boards/vhost/keypad_vhost.c: K_SELECT, K_BACK, K_UP, K_DOWN, K_PTT, ... in enum order
        self.assertEqual((self.bits["ACT_SELECT"], self.bits["ACT_BACK"], self.bits["ACT_NAV_UP"],
                          self.bits["ACT_NAV_DOWN"], self.bits["ACT_PTT"]), (0, 1, 2, 3, 4))
        self.assertNotIn("ACT_NAV_LEFT", self.bits)

    def test_valid_version_1_becomes_a_version_2_file_the_validator_accepts(self):
        result, flags = convert.convert(copy.deepcopy(V1))
        self.assertEqual(scenario.validate_v2(result), result)
        self.assertEqual((result["schema_version"], result["tier"], result["time_model"], result["class_table"],
                          result["duration_us"], result["checkpoints"]),
                         (2, "local", scenario.TIME_MODEL, scenario.CLASS_TABLE_VERSION, 5000000, []))
        self.assertNotIn("build", result)
        down, select, ptt = self.bits["ACT_NAV_DOWN"], self.bits["ACT_SELECT"], self.bits["ACT_PTT"]
        self.assertEqual(events_of(result), [
            (1200000, 10, "loss", {"target": "b", "rate_ppm": 1000}),	# class 10 before class 30 at one time
            (1200000, 30, "key", {"bit": down, "down": True}),
            (1280000, 30, "key", {"bit": down, "down": False}),		# press: held 80 ms
            (1500000, 30, "host", {"stream": "host-port", "hex": "e00300"}),
            (2000000, 30, "key", {"bit": select, "down": True}),
            (2720000, 30, "key", {"bit": select, "down": False}),		# long: held 600 + 120 ms
            (3000000, 30, "key", {"bit": ptt, "down": True}),
            (3500000, 30, "key", {"bit": ptt, "down": False}),
            (3600000, 10, "delay", {"target": "b", "ordinal": 2, "delay_us": 30000}),
        ])
        self.assertEqual([event["seq"] for event in result["events"]], list(range(9)))
        self.assertEqual({(event["round"], event["source"]) for event in result["events"]}, {(0, "scenario")})
        self.assertEqual(result["assertions"],
                         [{"radio": "a", "condition": "screen_id", "at_us": 4000000, "screen": "home"},
                          {"radio": "b", "condition": "no_tx", "from_us": 0, "through_us": 2990000}])
        self.assertTrue(any("screen" in flag and "dropped" in flag for flag in flags))
        self.assertFalse(any("SPLASH" in flag for flag in flags))

    def test_issue_example_key_stamp(self):
        value = copy.deepcopy(V1)
        value["events"] = [{"at_ms": 1200, "radio": "a", "action": "key", "key": "down", "gesture": "press"}]
        result, _flags = convert.convert(value)
        self.assertEqual(result["events"][0], {"t_us": 1200000, "round": 0, "class": 30, "seq": 0,
                                               "source": "scenario", "radio": "a",
                                               "key": {"bit": self.bits["ACT_NAV_DOWN"], "down": True}})

    def test_splash_trap_flags_keys_before_one_second(self):
        value = copy.deepcopy(V1)
        value["events"] = [{"at_ms": 990, "radio": "a", "action": "key", "key": "up", "gesture": "press"},
                           {"at_ms": 1000, "radio": "a", "action": "key", "key": "down", "gesture": "press"}]
        _result, flags = convert.convert(value)
        splash = [flag for flag in flags if "SPLASH TRAP" in flag]
        self.assertEqual(len(splash), 1)
        self.assertIn("990 ms", splash[0])

    def test_splash_trap_comes_back_after_a_restart_on_that_radio_only(self):
        value = copy.deepcopy(V1)
        value["events"] = [{"at_ms": 2000, "radio": "a", "action": "restart"},
                           {"at_ms": 2500, "radio": "a", "action": "key", "key": "up", "gesture": "press"},
                           {"at_ms": 2500, "radio": "b", "action": "key", "key": "up", "gesture": "press"},
                           {"at_ms": 2600, "radio": "a", "action": "ptt", "down": True},
                           {"at_ms": 3100, "radio": "a", "action": "ptt", "down": False}]
        _result, flags = convert.convert(value)
        splash = [flag for flag in flags if "SPLASH TRAP" in flag]
        self.assertEqual(len(splash), 2, splash)
        self.assertTrue(splash[0].startswith("event 1 (key at 2500 ms)"))
        self.assertTrue(splash[1].startswith("event 3 (ptt at 2600 ms)"))
        self.assertIn("radio core", splash[1])

    def test_after_tick_phase_lands_one_microsecond_later(self):
        value = copy.deepcopy(V1)
        value.update(integrated_core=True, radios=[dict(radio, flash_size=0x200000) for radio in V1["radios"]])
        value["events"] = [{"at_ms": 100, "radio": "a", "action": "restart", "phase": "after_tick"}]
        result, flags = convert.convert(value)
        self.assertEqual(events_of(result), [(100001, 10, "restart", {})])
        self.assertTrue(any("after_tick" in flag for flag in flags))

    def test_direct_calls_and_research_controls_are_refused_with_named_reasons(self):
        cases = [
            ({"action": "tune", "channel": 2}, "tune at 100 ms): calls the radio core directly"),
            ({"action": "brightness", "value": 50}, "brightness at 100 ms): sets the backlight"),
            ({"action": "settings_save"}, "settings_save at 100 ms)"),
            ({"action": "storage_reload"}, "storage_reload at 100 ms)"),
            ({"action": "sync_loss"}, "sync_loss at 100 ms): a research modem model control"),
            ({"action": "watchdog_starve"}, "watchdog_starve at 100 ms)"),
            ({"action": "key", "key": "left", "gesture": "press"}, "'left' (ACT_NAV_LEFT) has no bit"),
            ({"action": "key", "key": "context", "gesture": "long"}, "'context' (ACT_CONTEXT) has no bit"),
        ]
        for fields, reason in cases:
            value = copy.deepcopy(V1)
            value["events"] = [{"at_ms": 100, "radio": "a", **fields}]
            with self.subTest(action=fields["action"]), self.assertRaises(convert.Refused) as caught:
                convert.convert(value)
            self.assertTrue(any(reason in text for text in caught.exception.args[0]), caught.exception.args[0])

    def test_overlapping_presses_and_repeated_ptt_are_refused(self):
        value = copy.deepcopy(V1)
        value["events"] = [{"at_ms": 100, "radio": "a", "action": "key", "key": "select", "gesture": "long"},
                           {"at_ms": 500, "radio": "a", "action": "key", "key": "select", "gesture": "press"}]
        with self.assertRaises(convert.Refused) as caught:
            convert.convert(value)
        self.assertIn("still held", caught.exception.args[0][0])
        value["events"] = [{"at_ms": 100, "radio": "a", "action": "ptt", "down": True},
                           {"at_ms": 200, "radio": "a", "action": "ptt", "down": True}]
        with self.assertRaises(convert.Refused) as caught:
            convert.convert(value)
        self.assertIn("ptt is already down", caught.exception.args[0][0])

    def test_research_radio_fields_are_refused(self):
        value = copy.deepcopy(V1)
        value["radios"][0]["color_code"] = 3
        with self.assertRaises(convert.Refused) as caught:
            convert.convert(value)
        self.assertIn("radio a field color_code", caught.exception.args[0][0])

    def test_every_saved_scenario_converts_or_is_refused(self):
        for name, v1 in scenarios.SCENARIOS.items():
            with self.subTest(name=name):
                try:
                    result, _flags = convert.convert(copy.deepcopy(v1))
                except convert.Refused as error:
                    self.assertTrue(error.args[0] and all(isinstance(reason, str) for reason in error.args[0]))
                else:
                    self.assertEqual(scenario.validate_v2(result), result)

    def test_command_line_prints_a_file_or_the_refusal(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(convert.main(["ui-settings-recovery"]), 0)
        scenario.validate_v2(json.loads(out.getvalue()))
        self.assertIn("SPLASH TRAP", err.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(convert.main(["radio-fault-recovery"]), 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("refused: event 0 (brightness at 0 ms)", err.getvalue())


if __name__ == "__main__":
    unittest.main()
