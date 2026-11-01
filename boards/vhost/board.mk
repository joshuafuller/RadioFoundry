# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The virtual board (docs/dev/virtual-radio-architecture.md): the firmware's core on a PC, on the host platform. Every
# *.c here is built, so a new part (boards/vhost/<part>_vhost.c) needs no edit of this file
PLATFORM   := host
APP_BASE   := 0
BOARD_DIRS := boards/vhost
# Not a radio: no board.toml, never in the board registry, the site or the web flasher (tools/boards.py board_dirs).
# tools/ci.sh builds its default configuration only
BOARD_VIRTUAL := y
