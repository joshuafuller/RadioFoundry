# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Codec hooks must respect the hal_vocoder_t output bounds."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.emu import codec_stub


@pytest.mark.parametrize("entry,size", [(2, 49), (3, 160 * 2)])
def test_codec_output_preserves_guard_bytes(monkeypatch, entry, size):
    output = 32
    memory = bytearray([0xA5] * (output + size + 8))
    hooks = {}
    cpu = Mock(LR=0x1234, GetRegister=Mock(return_value=SimpleNamespace(RawValue=output)))
    cpu.AddHook.side_effect = lambda address, hook: hooks.setdefault(address, hook)

    def write(address, data):
        memory[address:address + len(data)] = data

    bus = Mock(GetCPUs=Mock(return_value=[cpu]), TryGetAllSymbolAddresses=Mock(return_value=(True, [0])),
               ReadDoubleWord=Mock(side_effect=lambda address: 0x100 + address),
               WriteDoubleWord=Mock(side_effect=lambda address, value: write(address, value.to_bytes(4, "little"))),
               WriteByte=Mock(side_effect=lambda address, value: write(address, bytes([value]))))
    monkeypatch.setattr(codec_stub, "monitor", SimpleNamespace(Machine=SimpleNamespace(SystemBus=bus)), raising=False)
    codec_stub.mc_setup_codec_stub()
    hooks[0x100 + 4 * entry](cpu, None)
    assert memory[output:output + size] == bytes(size)
    assert memory[:output] == bytes([0xA5] * output)
    assert memory[output + size:] == bytes([0xA5] * 8)
    assert cpu.PC == cpu.LR
