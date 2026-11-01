# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# USB only unlocks USB_NETWORK; BOARD_USB_DIRS is empty, so no USB stack is built.
CONFIG_USB ?= y
CONFIG_USB_NETWORK ?= y
