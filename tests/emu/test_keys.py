# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Run with python3 -m pytest -q tests/emu/test_keys.py.

The Renode test requires $RENODE (default ~/tools/renode/renode). It fails,
never skips, if the executable or runtime is unavailable. No firmware needed.
"""

import importlib.util
import os
from pathlib import Path
import struct
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "tools/emu/peripherals/keys.py"
spec = importlib.util.spec_from_file_location("keys", DRIVER)
keys = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keys)


class Line:
    def __init__(self):
        self.IsSet = False
        self.hooks = []

    def AddStateChangedHook(self, hook):
        self.hooks.append(hook)

    def set(self, level):
        self.IsSet = level
        for hook in self.hooks:
            hook(level)


class Port:
    def __init__(self):
        self.Connections = [Line() for _ in range(16)]
        self.levels = [False] * 16

    def OnGPIO(self, pin, level):
        self.levels[pin] = level


@pytest.fixture
def driver():
    return keys.Keys({"sysbus.gpioPort" + port: Port() for port in "ABDE"})


def columns(driver):
    return sum(int(driver.ports[p].levels[n]) << bit for bit, (p, n) in enumerate(driver.columns))


def scan(driver):
    result = []
    for port, pin in driver.rows:
        line = driver.ports[port].Connections[pin]
        line.set(True)
        result.append(columns(driver))
        line.set(False)
        assert columns(driver) == 0
    return result


def test_key_script_and_pull_defaults(driver):
    assert scan(driver) == [0, 0, 0]
    assert driver.ports["E"].levels[11:13] == [True, True]
    assert all(driver.ports[p].levels[n] for p, n in driver.channel_pins)
    # Conductor payloads: select, digit 1, monitor, PTT. All held together.
    for payload in (b"\x00\x01", b"\x07\x01", b"\x05\x01", b"\x04\x01"):
        driver.key(payload)
    driver.button("sk2", 1)
    assert scan(driver) == [0x01, 0x01, 0xC0]
    assert driver.ports["E"].levels[11:13] == [False, True]
    driver.button("ptt_ext", 1)
    driver.key(b"\x04\x00")
    assert driver.ports["E"].levels[11:13] == [True, False]
    driver.reset()
    assert scan(driver) == [0, 0, 0]
    assert driver.ports["E"].levels[11:13] == [True, True]


@pytest.mark.parametrize("bit,row,column", [
    (0, 1, 0), (1, 1, 7), (2, 1, 1), (3, 1, 2), (5, 2, 7),
    (6, 0, 6), (7, 0, 0), (8, 0, 1), (9, 0, 2), (10, 0, 3),
    (11, 0, 4), (12, 0, 5), (13, 1, 3), (14, 1, 4), (15, 1, 5),
])
def test_conductor_bits(driver, bit, row, column):
    expected = [0, 0, 0]
    expected[row] = 1 << column
    driver.key(bytes((bit, 1)))
    assert scan(driver) == expected
    driver.key(bytes((bit, 0)))
    assert scan(driver) == [0, 0, 0]


@pytest.mark.parametrize("name,row,column", [("star", 0, 7), ("hash", 1, 6), ("sk2", 2, 6)])
def test_physical_keys_without_virtual_bits(driver, name, row, column):
    expected = [0, 0, 0]
    expected[row] = 1 << column
    driver.button(name, 1)
    assert scan(driver) == expected
    driver.button(name, 0)
    assert scan(driver) == [0, 0, 0]


def test_encoder_codes_and_wrap(driver):
    def code():
        return sum(int(not driver.ports[p].levels[n]) << bit for bit, (p, n) in enumerate(driver.channel_pins))

    for expected in (8, 10, 2, 3, 11, 9, 1, 5, 13, 15, 7, 6, 14, 12, 4, 0):
        driver.encoder(struct.pack("<i", 1))
        assert code() == expected
    driver.encoder(struct.pack("<i", -1))
    assert code() == 4
    driver.encoder(struct.pack("<i", 2))
    assert code() == 8


@pytest.mark.parametrize("payload", [b"", b"\x04", b"\x04\x01\x00", b"\x04\x02", b"\x10\x01", b"\xff\x01"])
def test_bad_key_payload_does_not_change_inputs(driver, payload):
    with pytest.raises(ValueError):
        driver.key(payload)
    assert scan(driver) == [0, 0, 0]
    assert driver.ports["E"].levels[11:13] == [True, True]


def test_bad_encoder_and_button_do_not_change_inputs(driver):
    for payload in (b"", b"\x01", b"\x01\x00\x00\x00\x00"):
        with pytest.raises(ValueError):
            driver.encoder(payload)
    for name, down in (("missing", 1), (None, 1), ("ptt", 2)):
        with pytest.raises(ValueError):
            driver.button(name, down)
    assert driver.position == 0
    assert not driver.held


def test_independent_radios(driver):
    other = keys.Keys({"sysbus.gpioPort" + port: Port() for port in "ABDE"})
    driver.key(b"\x00\x01")
    assert scan(other) == [0, 0, 0]


def test_renode_gpio_scan(tmp_path):
    renode = os.environ.get("RENODE", os.path.expanduser("~/tools/renode/renode"))
    platform = tmp_path / "keys.repl"
    platform.write_text("\n".join(
        "gpioPort%s: GPIOPort.STM32_GPIOPort @ sysbus <0x%08x, +0x400>" % (port, address)
        for port, address in (("A", 0x40020000), ("B", 0x40020400), ("D", 0x40020C00), ("E", 0x40021000))))
    script = tmp_path / "keys.resc"
    script.write_text('\n'.join([
        'mach create "keys"',
        'machine LoadPlatformDescription "%s"' % platform,
        'include "%s"' % DRIVER,
        'keys_attach',
        'include "%s"' % (Path(__file__).with_name("keys_scan.py")),
        'keys_check',
    ]))
    result = subprocess.run([renode, "--disable-xwt", "--plain", "--console",
                             "--config", str(tmp_path / "renode.config"),
                             "-e", 'include "%s"; quit' % script],
                            env=dict(os.environ, XDG_CONFIG_HOME=str(tmp_path)),
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "failed" not in output.lower(), output
    assert "KEYS_GPIO_SCAN_OK" in output, output
    print(next(line for line in output.splitlines() if line.startswith("KEYS_GPIO_SCAN_OK")))
