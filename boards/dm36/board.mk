# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# Board wiring and layout: docs/spec/hardware-dm36.md.
include boards/dm32/board.mk
BOARD_DIRS := boards/dm36 $(filter-out drivers/bcast/rda5807m drivers/bus/i2c_gpio,$(BOARD_DIRS))
# No open-firmware codeplug: reads beyond the data chip fail without touching stock data.
MCU_DEFS := $(filter-out -DCODEPLUG_OPENFW_%,$(MCU_DEFS))
MCU_DEFS += -DCODEPLUG_OPENFW_EEPROM_BASE=0x1000000u -DCODEPLUG_OPENFW_FLASH_BASE=0x1000000u
# The inherited knob-mod option doesn't apply: this knob is an encoder (hardware-dm36.md 5.1)
BUILD_OPTIONS :=
KNOB_VALUES :=
MCU_DEFS += -DBOARD_EXTERNAL_DESCRIPTOR -Iboards/dm36 -Iboards/dm32
LDSCRIPT := boards/dm36/memory.ld
# Preserve the 48 KiB stock bootloader (hardware-dm36.md 2).
APP_BASE := 0x0300C000
# No persistent write region has been established (hardware-dm36.md 10).
MCU_DEFS += -DBOARD_FLASH_READ_ONLY
# The volume/power knob's pot wiper is on ADC0_IN / PTC15 (measured on the radio, hardware-dm36.md 9.3): the
# shared knob reader (the knob-mod path of the reused sources) drives the firmware volume and the volume bar
MCU_DEFS += -DRFOUNDRY_KNOB_ADC0=1
