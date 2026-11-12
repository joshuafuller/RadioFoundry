# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# Build-only hypotheses; see docs/spec/hardware-dm36.md.
include boards/dm32/board.mk
BOARD_DIRS := boards/dm36 $(BOARD_DIRS)
MCU_DEFS += -DBOARD_EXTERNAL_DESCRIPTOR -Iboards/dm32
LDSCRIPT := boards/dm36/memory.ld
