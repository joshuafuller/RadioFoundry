# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# Executed inside Renode's IronPython monitor by test_spinor.py; no sockets or radio hardware.
import clr
from System import AppDomain

# Renode registers an ad-hoc assembly with the platform parser, but not with IronPython.
for assembly in AppDomain.CurrentDomain.GetAssemblies():
    if assembly.GetType("Antmicro.Renode.Peripherals.SPI.SpiNor") is not None:
        clr.AddReference(assembly)
        break
from Antmicro.Renode.Peripherals.SPI import SpiNor
from System.IO import File, InvalidDataException


def check_spinor(path):
    flash = SpiNor(path)

    def transaction(header, count=0):
        flash.OnGPIO(0, False)
        for byte in header:
            flash.Transmit(byte)
        result = [int(flash.Transmit(0xFF)) for _ in range(count)]
        flash.OnGPIO(0, True)
        return result

    def header(command, address):
        return [command, (address >> 16) & 255, (address >> 8) & 255, address & 255]

    def read(address, count=1, fast=False):
        return transaction(header(0x0B if fast else 0x03, address) + ([0] if fast else []), count)

    def program(address, data):
        transaction([0x06])
        transaction(header(0x02, address) + data)

    assert flash.Transmit(0x9F) == 255
    assert transaction([0x9F], 3) == [0xEF, 0x40, 0x18]
    assert transaction([0x05], 2) == [0, 0]
    assert transaction([0x35], 1) == [0]
    assert transaction([0x15], 1) == [0x60]
    assert transaction([0x4B, 0, 0, 0, 0], 8) == [0x52, 0x46, 0x45, 0x4D, 0x55, 0, 0, 1]
    transaction([0x06])
    assert transaction([0x05], 2) == [2, 2]
    transaction([0x04])
    transaction(header(0x02, 0x100) + [0])
    assert read(0x100) == [255]
    print("SPI NOR: JEDEC, UID, status and write-enable PASSED")

    transaction([0x06])
    flash.OnGPIO(0, False)
    for byte in header(0x02, 0x1FE) + [0xF0, 0x0F, 0x55, 0xAA]:
        flash.Transmit(byte)
    assert File.ReadAllBytes(path)[0x1FE] == 255
    flash.OnGPIO(0, False)  # repeated low must not restart the command
    flash.OnGPIO(0, True)
    assert transaction([0x05], 1) == [0]
    assert read(0x1FE, 4) == [0xF0, 0x0F, 255, 255]
    assert read(0x100, 2, True) == [0x55, 0xAA]
    program(0x100, [0x0F, 255])
    assert read(0x100, 2) == [5, 0xAA]
    program(0x300, [0] * 256 + [255])
    assert read(0x300, 2) == [255, 0]  # last 256 bytes, not AND of the page buffer
    program(0xFFFFFF, [0x12])
    program(0, [0x34])
    assert read(0xFFFFFF, 2, True) == [0x12, 0x34]
    print("SPI NOR: CS commit, normal/fast read, NOR bits and address/page wrapping PASSED")

    transaction([0x06])
    transaction([0x02, 0, 4])  # incomplete header must not write or consume WEL
    assert transaction([0x05], 1) == [2]
    transaction(header(0x02, 0x400))  # no data
    assert transaction([0x05], 1) == [2]
    flash.OnGPIO(0, False)
    for byte in header(0x02, 0x400) + [0]:
        flash.Transmit(byte)
    flash.Reset()  # an interrupted transaction never reaches the backing file
    flash.OnGPIO(0, True)
    assert read(0x400) == [255] and transaction([0x05], 1) == [0]
    for command, size in ((0x20, 4096), (0x52, 32768), (0xD8, 65536)):
        base = size * 2
        for address in (base - 1, base, base + size - 1, base + size):
            program(address, [0])
        transaction(header(command, base + 123))
        assert read(base) == [0]  # erase also requires WEL
        transaction([0x06])
        transaction(header(command, base + 123) + [0])
        assert read(base) == [0]  # extra bytes invalidate an erase
        transaction(header(command, base + 123))
        assert read(base, size) == [255] * size
        assert read(base - 1) == [0] and read(base + size) == [0]
        assert transaction([0x05], 1) == [0]
    print("SPI NOR: incomplete/reset transactions and 4/32/64 KiB erase boundaries PASSED")

    registers = list(File.ReadAllBytes(path + ".security"))
    for register in range(1, 4):
        expected = registers[(register - 1) * 256:register * 256]
        assert transaction(header(0x48, register << 12) + [0], 256) == expected
        assert transaction(header(0x48, (register << 12) | 255) + [0], 2) == [expected[-1], expected[0]]
    assert transaction(header(0x48, 0) + [0], 1) == [255]
    transaction(header(0x42, 0x1009) + [0])  # unsupported security writes have no effect
    assert transaction(header(0x48, 0x1009) + [0], 1) == [64]
    assert list(File.ReadAllBytes(path + ".security")) == registers
    print("SPI NOR: security registers, dummy byte, wrapping and main-array isolation PASSED")

    program(0x123456, [1, 2, 3])
    flash.Reset()
    assert read(0x123456, 3) == [1, 2, 3]
    saved = File.ReadAllBytes(path)
    assert [int(saved[i]) for i in range(0x123456, 0x123459)] == [1, 2, 3]
    flash = SpiNor(path)
    assert read(0x123456, 3) == [1, 2, 3] and transaction([0x05], 1) == [0]
    try:
        SpiNor(path + ".security")  # rejects the wrong main-array size
        raise AssertionError("short image accepted")
    except InvalidDataException:
        pass
    print("SPI NOR: reset/reopen persistence and image validation PASSED")


check_spinor(spinor_image_path)
print("spinor_check: PASSED")
