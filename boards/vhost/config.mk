# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The virtual board's feature defaults. The host link on the virtual UART (no USB). The crash log is on
# (platform/host/crash.c; the record is in process memory). No fault trace (it needs the CPU's fault frames). DMR
# stays at its default (on): the core runs its DMR code with board.dmr NULL until a modem part exists
CONFIG_HOSTLINK ?= y
CONFIG_CRASH_LOG ?= y
CONFIG_FAULT_TRACE ?= n
# A later part turns its own switches on in a file of its own (boards/vhost/config.d/<part>.mk), never here
-include $(sort $(wildcard boards/vhost/config.d/*.mk))
