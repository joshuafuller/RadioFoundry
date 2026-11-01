# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""HIL starts after the splash, when the firmware can handle its first key."""

import io
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.conductor import run_hil
from tools.conductor.parts.dlmux import Parser, REPLY_MAX, frame


def test_pytest_waits_for_the_first_ui_draw(monkeypatch):
    world = MagicMock()
    world.call.side_effect = [None, {"endpoint": "one", "url": "unix:///test.sock", "principal": "hil:one"},
                              {"generation": 1}, None, None]
    monkeypatch.setattr(run_hil, "WorldClient", lambda path: world)
    conductor = MagicMock(stdout=io.StringIO("/tmp/world.sock\n"))
    monkeypatch.setattr(run_hil.subprocess, "Popen", lambda *args, **kwargs: conductor)
    link = MagicMock()
    link.__enter__.return_value = link
    # ui.screen says home even during the splash; a missing draw count is not readiness either.
    link.view.side_effect = [{"ui.screen": "home"}, {"ui.screen": "home", "ui.draws": "0"},
                             {"ui.screen": "home", "ui.draws": "1"}]
    monkeypatch.setattr(run_hil, "DebugLink", lambda port: link)
    clock = [0.0]
    monkeypatch.setattr(run_hil, "time", SimpleNamespace(monotonic=lambda: clock[0],
                       sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))

    def pytest_run(args, **kwargs):
        assert link.view.call_count == 3, "pytest started before the UI left its splash"
        link.__exit__.assert_called_once()  # the HIL endpoint admits only one connection at a time
        assert args[-2:] == ["--radio-port", "unix:///test.sock"]
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(run_hil.subprocess, "run", pytest_run)
    assert run_hil.main(["tests/hil/test_ui.py::test_keys_move_the_channel", "-q"]) == 7
    assert all(call.args == (["link"],) for call in link.view.call_args_list)
    assert world.call.call_args_list[-2].args == ("hil.release",)
    assert world.call.call_args_list[-1].args == ("end",)


def test_ui_wait_times_out_and_closes_the_link(monkeypatch):
    link = MagicMock()
    link.__enter__.return_value = link
    link.view.return_value = {"ui.screen": "home", "ui.draws": "0"}
    monkeypatch.setattr(run_hil, "DebugLink", lambda port: link)
    clock = [0.0]
    monkeypatch.setattr(run_hil, "time", SimpleNamespace(monotonic=lambda: clock[0],
                       sleep=lambda seconds: clock.__setitem__(0, clock[0] + 5)))
    with pytest.raises(TimeoutError, match="UI.*splash"):
        run_hil.wait_for_ui("unix:///test.sock")
    link.__exit__.assert_called_once()


def test_firmware_keys_after_splash(vhost_elf, tmp_path):
    trace = tmp_path / "keys.trace"
    args = [str(vhost_elf), "--host-port", "on", "--until-ms", "3200", "--trace", str(trace),
            "--key", "2600:0:120"]  # a 120 ms physical OK works once the splash has ended
    commands = [(150, 6, bytes((0, 5, 0, 0))), (300, 9, b"\x06"), (1100, 9, b"\x06"),
                (1200, 6, bytes((0, 1, 0, 0))), (1400, 9, b"\x00"),
                (1500, 6, bytes((0, 5, 0, 0))), (1700, 9, b"\x06"),
                (1800, 6, bytes((0, 6, 0, 0))), (2000, 6, bytes((0, 7, 0, 0))),
                (2200, 9, b"\x06"), (2300, 6, bytes((0, 6, 0, 0))), (2900, 9, b"\x06")]
    for seq, (ms, cmd, payload) in enumerate(commands):
        args += ["--host", f"{ms}:{frame(cmd, seq, payload).hex()}"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    parser, replies = Parser(REPLY_MAX), {}
    for line in trace.read_text().splitlines():
        if " BOARD host.write " in line:
            for kind, cmd, seq, payload in parser.feed(bytes.fromhex(line.split()[-1])):
                assert kind == "frame"
                assert payload[0] == 0, (cmd, seq, payload)
                replies[seq] = dict(text.split("=", 1) for text in payload[6:].decode().splitlines()
                                    if "=" in text) if cmd == 0x89 else payload
    assert len(replies) == len(commands)
    assert replies[1]["ui.screen"] == "home" and replies[1]["ui.draws"] == "0"
    assert replies[2]["ui.screen"] == "home" and int(replies[2]["ui.draws"]) > 0
    assert replies[4]["zone_pos"] == "1", "UP did not change channel"
    assert replies[6]["ui.screen"] == "hub", "debug link OK did not open the Hub"
    assert replies[9]["ui.screen"] == "quick", "debug link QUICK did not open Quick settings"
    assert replies[11]["ui.screen"] == "hub", "a 120 ms keypad OK did not open the Hub"
