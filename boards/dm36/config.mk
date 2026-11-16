# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
include boards/dm32/config.mk
CONFIG_GPS ?= n
BOARD_UNSUPPORTED += GPS # no GPS module in the photos or manual
BOARD_UNSUPPORTED += VOLUME_ENCODER # the encoder is the channel knob (hardware-dm36.md 5.1)
BOARD_NOT_WRITTEN += VIBRATE # motor drive pin unknown
BOARD_UNSUPPORTED += GPS_SKY # no GPS receiver
BOARD_UNSUPPORTED += NAND_STORAGE # data flash is SPI NOR
BOARD_NOT_WRITTEN += VOX # inherited glue has no mic_level hook
BOARD_NOT_WRITTEN += AUDIO_PROBE # inherited glue has no audio_tap hook
# The inherited broadcast receiver would drive unresolved GPIOs 0x19/0x1A.
CONFIG_BCAST_FM ?= n
BOARD_NOT_WRITTEN += BCAST_FM # receiver wiring not established
