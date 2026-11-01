# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Renode monitor test: scan GPIO registers as fsmc_keypad.c does, no CPU needed."""


def mc_keys_check():
    bus = monitor.Machine.SystemBus
    a, b, d, e = 0x40020000, 0x40020400, 0x40020C00, 0x40021000

    def mode(base, pins, value):
        word = bus.ReadDoubleWord(base)
        for pin in pins:
            word = (word & ~(3 << (pin * 2))) | (value << (pin * 2))
        bus.WriteDoubleWord(base, word)

    def columns():
        pd = bus.ReadDoubleWord(d + 0x10)
        pe = bus.ReadDoubleWord(e + 0x10)
        return ((pd >> 14) & 3) | ((pd & 3) << 2) | (((pe >> 7) & 15) << 4)

    def scan():
        # Discharge, input scan, then restore the shared LCD bus to AF mode.
        for base, pins, mask in ((d, (14, 15, 0, 1), 0xC003), (e, (7, 8, 9, 10), 0x0780)):
            bus.WriteDoubleWord(base + 0x18, mask << 16)
            mode(base, pins, 1)
            mode(base, pins, 0)
        result = []
        for base, pin in ((a, 6), (d, 2), (d, 3)):
            # The firmware sets BSRR before switching the row to output mode.
            bus.WriteDoubleWord(base + 0x18, 1 << pin)
            mode(base, (pin,), 1)
            result.append(columns())
            bus.WriteDoubleWord(base + 0x18, 1 << (pin + 16))
            assert columns() == 0, "released row left stale column levels"
            mode(base, (pin,), 0)
        mode(d, (14, 15, 0, 1), 2)
        mode(e, (7, 8, 9, 10), 2)
        return result

    def ptt():
        return (bus.ReadDoubleWord(e + 0x10) >> 11) & 3

    def channel():
        raw = ((bus.ReadDoubleWord(e + 0x10) >> 14) & 3)
        raw |= ((bus.ReadDoubleWord(b + 0x10) >> 10) & 3) << 2
        return (~raw) & 15

    assert scan() == [0, 0, 0]
    assert ptt() == 3 and channel() == 0, "missing released pull-up levels"
    expected_matrix = (("1", "2", "3", "4", "5", "6", "0", "star"),
                       ("green", "up", "down", "7", "8", "9", "hash", "red"),
                       (None, None, None, None, None, None, "sk2", "sk1"))
    for row, names in enumerate(expected_matrix):
        for column, name in enumerate(names):
            if name is None:
                continue
            mc_keys_button(name, 1)
            expected = [0, 0, 0]
            expected[row] = 1 << column
            actual = scan()
            assert actual == expected, "wrong matrix position for %s: %r != %r" % (name, actual, expected)
            mc_keys_button(name, 0)
            assert scan() == [0, 0, 0]

    for bit in (0, 7, 5, 4):
        mc_keys_key(bit, 1)
    mc_keys_button("sk2", 1)
    assert scan() == [1, 1, 0xC0] and ptt() == 2
    mc_keys_button("ptt_ext", 1)
    mc_keys_key(4, 0)
    assert ptt() == 1
    for expected in (8, 10, 2, 3, 11, 9, 1, 5, 13, 15, 7, 6, 14, 12, 4, 0):
        mc_keys_encoder(1)
        assert channel() == expected
    mc_keys_encoder(-1)
    assert channel() == 4
    mc_keys_reset()
    assert scan() == [0, 0, 0] and ptt() == 3 and channel() == 0
    print("KEYS_GPIO_SCAN_OK: 18 matrix keys, KEY script, both PTTs, 16 channel positions and wrap")
