# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
ROWS = (("A", 6), ("D", 2), ("D", 3))
COLUMNS = (("D", 14), ("D", 15), ("D", 0), ("D", 1), ("E", 7), ("E", 8), ("E", 9), ("E", 10))
MATRIX = (("1", "2", "3", "4", "5", "6", "0", "star"),
          ("green", "up", "down", "7", "8", "9", "hash", "red"),
          (None, None, None, None, None, None, "sk2", "sk1"))
BIT_KEYS = ("green", "red", "up", "down", "ptt", None, "0", "1", "2", "3", "4", "5", "6", "7", "8", "9")
_emukeys = {}

class EmuKeys(object):
    def __init__(self, machine):
        self.ports = dict((p, machine["sysbus.gpioPort" + p]) for p in "ADE")
        self.held = set()
        for port, pin in ROWS:
            self.ports[port].Connections[pin].AddStateChangedHook(self.columns)
        # A write to a port's BSRR rewrites that port's input levels after the hook ran, so a row on port D wiped the
        # columns set for it on port D (green, up, down never arrived). Setting the columns again just before each
        # IDR read of ports D and E fixes that
        bus = machine.SystemBus
        from Antmicro.Renode.Peripherals.Bus import SysbusAccessWidth, Access
        for idr in (0x40020C10, 0x40021010):
            bus.AddWatchpointHook(idr, SysbusAccessWidth.DoubleWord, Access.Read,
                                  lambda cpu, addr, width, value: self.columns())

    def columns(self, _level=None):
        levels = [False] * 8
        for row, (port, pin) in enumerate(ROWS):
            if self.ports[port].Connections[pin].IsSet:
                for col, name in enumerate(MATRIX[row]):
                    levels[col] = levels[col] or name in self.held
        for (port, pin), level in zip(COLUMNS, levels):
            self.ports[port].OnGPIO(pin, level)

    def key(self, bit, down):
        name = BIT_KEYS[bit]
        if name is None:
            raise Exception("emukey: KEY bit %d has no MD-380 key in the built-in driver" % bit)
        if down:
            self.held.add(name)
        else:
            self.held.discard(name)
        self.columns()
        self.ports["E"].OnGPIO(11, "ptt" not in self.held)

def mc_emukey(bit, down):
    m = monitor.Machine
    if m not in _emukeys:
        _emukeys[m] = EmuKeys(m)
    _emukeys[m].key(int(bit), int(down))
