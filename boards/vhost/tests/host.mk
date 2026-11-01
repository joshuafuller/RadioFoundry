# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The virtual board's host tests, included by tests/host/Makefile and run from there. Each part's tests are a fragment
# of their own, boards/vhost/tests/mk/<part>.mk, so a new one never edits this file or a shared makefile. A fragment
# reads its sources under $(VHOST_TESTS) (this directory; every board's host.mk sets HERE, so HERE may name another
# board's by the time a recipe runs) and adds its runs to run-boards::
VHOST_TESTS := $(dir $(lastword $(MAKEFILE_LIST)))
ifeq ($(wildcard $(ROOT)/third_party/FreeRTOS-Kernel/include/FreeRTOS.h),)
run-boards::
	@echo "# virtual board tests: skipped, FreeRTOS missing; run tools/checkout-submodules.sh"
else
# The virtual radio, built by the top Makefile. A run-level test depends on it; the build is quick when nothing changed.
# MAKEFLAGS is cleared so a host test setting (CC, SANITIZE=1, COVERAGE=1) never reaches the firmware build
VHOST_ELF := $(ROOT)/build/vhost/rfoundry_vhost.elf
vhost-elf:
	@mkdir -p out
	@env -u MAKEFLAGS -u MFLAGS $(MAKE) -s -C $(ROOT) BOARD=vhost > out/vhost-build.log 2>&1 || \
		{ cat out/vhost-build.log; exit 1; }
.PHONY: vhost-elf
include $(sort $(wildcard $(VHOST_TESTS)mk/*.mk))
endif
