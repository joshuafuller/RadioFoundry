# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Opt-in navigation only: My beacon and its editors, without saving settings or requesting RF."""
import os

import pytest

from rfoundry_hil import LONG, skip


@pytest.mark.feature("aprs_beacon")
def test_my_beacon_screens(radio):
    if os.environ.get("RFOUNDRY_HIL_APRS_BEACON") != "1":
        skip("opt_in", "set RFOUNDRY_HIL_APRS_BEACON=1 to navigate the radio UI")
    if "APRS_BEACON" not in radio.features():
        skip("not_built", "built without My beacon")
    radio.ensure_awake()
    radio.unlock()
    if radio.screen() == "hotspot":
        skip("state", "the radio is in hotspot mode")

    def screen(name):
        return radio.wait_for(lambda v: v.get("ui.screen") == name, sections=("link",), what=name)

    try:
        radio.press("back", LONG)
        screen("home")
        for key in ("ok", "6", "8"):
            radio.press(key)
        screen("aprs")
        # My beacon precedes KISS and Stations heard, with either backend combination.
        for key in ("up", "up", "up", "ok"):
            radio.press(key)
        screen("aprs-beacon")
        for key, name in (("1", "aprs-symbol"), ("4", "aprs-pace"), ("5", "aprs-reach")):
            radio.press(key)
            screen(name)
            radio.press("back")
            screen("aprs-beacon")
        radio.press("2")
        screen("aprs-comment")
    finally:
        # Long Back cancels text rather than saving an existing or empty comment.
        radio.press("back", LONG)
