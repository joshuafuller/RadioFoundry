# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Read-only APRS Home evidence view; no tune, setting change or transmission."""
import pytest
from rfoundry_hil import skip


@pytest.mark.feature("aprs_home")
def test_aprs_home_evidence(radio):
    if "APRS_HOME" not in radio.features():
        skip("not_built", "built without CONFIG_APRS_HOME")
    view = radio.view(["aprs"])
    assert view["aprs.home_built"] == "1"
    assert view["aprs.sent"] in ("0", "1")
    assert 0 <= int(view["aprs.listening_state"]) <= 6
    assert int(view["aprs.sent_ms"]) >= 0
    assert int(view["aprs.repeat_ms"]) >= 0
    assert int(view["aprs.repeat_serial"]) >= 0
    assert len(view["aprs.repeat_call"]) <= 9
