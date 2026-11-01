# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The virtual clock alone (platform/host/vclock.c): the order key with rounds, a past event ending the run,
# the clock reads, the waits, the end of the run, and the world control horizon hooks. The RTOS and the rest of the host
# platform are stubs in the test. Its own flags, not CFLAGS: CFLAGS defines __attribute__ away, and the clock needs
# its weak hook, its noreturn finish and its option constructor. VCLOCK_SRC may name another copy of the clock (a
# deliberately broken one, to show a case fails)
VCLOCK_SRC ?= $(ROOT)/platform/host/vclock.c
VCLOCK_CFLAGS := -std=gnu99 -Wall -Wextra -Wno-unused-parameter -g $(COVFLAGS) $(SANFLAGS) \
	-I$(ROOT)/platform/host -I$(ROOT)/platform/host/freertos -I$(ROOT)/third_party/FreeRTOS-Kernel/include \
	-I$(ROOT)/core -I$(ROOT)/hal
TESTS   += vclockTest
vclockTest: $(VHOST_TESTS)vclockTest.c $(VCLOCK_SRC) $(ROOT)/platform/host/vclock.h
	$(CC) $(VCLOCK_CFLAGS) -o $@ $(filter %.c,$^)
ifeq ($(shell uname -s),Linux)
run-boards:: vclockTest
	./vclockTest
else
run-boards::
	@echo "# vclockTest: skipped, the host platform builds on Linux only"
endif
