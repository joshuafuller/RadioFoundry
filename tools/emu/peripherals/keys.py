# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""GPIO key inputs for Renode (Python 2.7/IronPython and Python 3).

Wiring and key matrices come from boards/<id>/emu/config.json. Matrix positions
are unverified on hardware. The channel switch is active-low Gray code.
KEY bits follow tools/conductor/parts/input.py and boards/vhost/vkeys.h,
not the physical board's different key enum. ENC is signed i32 little endian.

Select the board with keys_layout before keys_attach.

After loading the platform, in the monitor:
    include @tools/emu/peripherals/keys.py
    keys_layout "<board>"        # optional: defaults to the Renode default board
    keys_attach
    keys_key 4 1                 # KEY payload 04 01: PTT down
    keys_key 4 0
    keys_button sk2 1            # physical keys absent from the virtual map
    keys_encoder 1              # one clockwise detent
    keys_reset                  # restore released inputs after machine Reset

The bridge can call keys.key(payload) / keys.encoder(payload) on the Keys
instance returned by _current_keys(). Apply inputs between conductor grants;
this driver never advances emulated time. Allow the firmware's scan/debounce
time (20 ms for keys, 30 ms for the switch) before expecting a UI event.
"""

import os
import struct
import sys

# Renode includes this in its monitor scope, from the repository root.
_module_dir = os.path.join(os.getcwd(), "tools", "emu")
if "__file__" in globals():
    _module_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _module_dir)
from board_config import BOARDS, default_board

LAYOUTS = dict((board, config["keys"]) for board, config in BOARDS.items() if "keys" in config)
DEFAULT_LAYOUT = default_board("renode")
CONDUCTOR_KEYS = ("green", "red", "up", "down", "ptt", "sk1",
                  "0", "1", "2", "3", "4", "5", "6", "7", "8", "9")


class Keys(object):
    def __init__(self, machine, layout=DEFAULT_LAYOUT):
        self.layout = LAYOUTS[layout]
        self.matrix = self.layout["matrix"]
        self.rows, self.columns = self.layout["rows"], self.layout["columns"]
        self.channel_pins, self.channel_codes = self.layout["channel_pins"], self.layout["channel_codes"]
        self.ports = dict((port, machine["sysbus.gpioPort" + port]) for port in "ABDE")
        self.reset()
        for port, pin in self.rows:
            self.ports[port].Connections[pin].AddStateChangedHook(self._columns)

    def reset(self):
        self.held = set()
        self.position = 0
        self._columns()
        self._buttons()
        self._channel()

    def key(self, payload):
        data = bytearray(payload)
        if len(data) != 2 or data[0] >= len(CONDUCTOR_KEYS) or data[1] not in (0, 1):
            raise ValueError("KEY requires a supported bit (0..15) and down (0 or 1)")
        self.button(CONDUCTOR_KEYS[data[0]], data[1])

    def button(self, name, down):
        if name not in ("ptt", "ptt_ext") and not any(name in row for row in self.matrix if name is not None):
            raise ValueError("unknown physical key: %s" % name)
        if down not in (0, 1):
            raise ValueError("down must be 0 or 1")
        if down:
            self.held.add(name)
        else:
            self.held.discard(name)
        self._columns()
        self._buttons()

    def encoder(self, payload):
        if len(payload) != 4:
            raise ValueError("ENC requires a signed i32 little-endian payload")
        detents, = struct.unpack("<i", payload)
        # shortcut: only the final position is visible; send single detents >=30 ms apart to count every step.
        self.position = (self.position + detents) % len(self.channel_codes)
        self._channel()

    def _columns(self, _level=None):
        columns = [False] * 8
        for row, (port, pin) in enumerate(self.rows):
            if self.ports[port].Connections[pin].IsSet:
                for column, name in enumerate(self.matrix[row]):
                    columns[column] |= name in self.held
        for (port, pin), level in zip(self.columns, columns):
            self.ports[port].OnGPIO(pin, level)

    def _buttons(self):
        for name in ("ptt", "ptt_ext"):
            port, pin = self.layout[name]
            self.ports[port].OnGPIO(pin, name not in self.held)

    def _channel(self):
        code = self.channel_codes[self.position]
        for bit, (port, pin) in enumerate(self.channel_pins):
            self.ports[port].OnGPIO(pin, not bool(code & (1 << bit)))


_keys_by_machine = {}
_layout = [DEFAULT_LAYOUT]


def _current_keys():
    machine = monitor.Machine
    if machine not in _keys_by_machine:
        from System import UInt32

        keys = Keys(machine, _layout[0])
        for name in "DE":
            port = keys.ports[name]

            def read_input(value, offset, port=port):
                if offset == 0x10:
                    # Renode 1.17 mode writes can overwrite columns driven by a row hook on the same port.
                    keys._columns()
                    return port.ReadDoubleWord(offset)
                return value

            machine.SystemBus.SetHookAfterPeripheralRead[UInt32](port, read_input, None)
        _keys_by_machine[machine] = keys
    return _keys_by_machine[machine]


def mc_keys_layout(name):
    name = str(name)    # the monitor may hand over its own object for a bare word that names the machine
    if name not in LAYOUTS:
        raise ValueError("unknown key layout: %s (one of %s)" % (name, ", ".join(sorted(LAYOUTS))))
    _layout[0] = name


def mc_keys_attach():
    _current_keys()


def mc_keys_key(bit, down):
    _current_keys().key(bytearray((bit, down)))


def mc_keys_button(name, down):
    _current_keys().button(name, down)


def mc_keys_encoder(detents):
    _current_keys().encoder(struct.pack("<i", detents))


def mc_keys_reset():
    _current_keys().reset()
