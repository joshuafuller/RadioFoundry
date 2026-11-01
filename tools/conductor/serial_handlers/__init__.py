# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""pyserial URL handlers for the virtual radio. tests/hil/hil_plugin.py adds this package to
serial.protocol_handler_packages, so serial_for_url("unix:///path") finds protocol_unix.py here.
It is not in parts/, because the conductor loads every file there."""
