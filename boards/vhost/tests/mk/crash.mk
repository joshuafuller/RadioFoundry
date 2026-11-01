# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The crash platform and the watchdog tick on the host (platform/host/crash.c, vtick.c, vwatchdog.c): vcrashTest checks
# the crash functions alone; vhost-watchdog starves the watchdog through the debug link and checks the run ends with
# reason "watchdog" and a record naming the task. The host platform is Linux only, as the skeleton's run
VCRASH_SRC := $(VHOST_TESTS)vcrashTest.c $(ROOT)/platform/host/crash.c
TESTS += vcrashTest
vcrashTest: $(VCRASH_SRC) $(ROOT)/hal/hal_crash.h $(ROOT)/core/crash/crash.h
	$(CC) $(CFLAGS) -I$(ROOT)/hal -I$(ROOT)/platform/host -DCONFIG_CRASH_LOG=1 -Wshadow -o $@ $(VCRASH_SRC)

# DL_CMD_WATCHDOG (0x1D), op TEST (1), kind STARVE (1), token 0x474F4457 (little endian), seq 1, CRC-16 0x27B9
VWDT_FRAME := a55a1d010600010157444f4727b9
ifeq ($(shell uname -s),Linux)
run-boards:: vhost-watchdog
run-boards:: vcrashTest
	./vcrashTest
endif
# The frame arrives at 1000 ms; the debug link waits 100 ms (WDT_TEST_DELAY_MS), then the idle task stops feeding. The
# virtual watchdog's period is 4000 ms from the last feed, so the run must end between 4100 and 5100 ms, with the
# CRASH line (platform/host/crash.c) naming a watchdog record of kind 5 (CRASH_KIND_HANG) on the IDLE task
vhost-watchdog: vhost-elf
	@mkdir -p out
	@code=0; $(VHOST_ELF) --until-ms 10000 --no-stall-watch --host-port on --host 1000:$(VWDT_FRAME) \
		--trace out/vhost-watchdog.log 2> out/vhost-watchdog.err || code=$$?; \
	[ $$code -eq 3 ] || { echo "vhost-watchdog: exit $$code, not 3"; exit 1; }
	@grep -q ' END watchdog ' out/vhost-watchdog.log || { echo "vhost-watchdog: no END watchdog"; exit 1; }
	@grep -q ' CRASH watchdog kind=5 cause=[0-9]* task=IDLE ' out/vhost-watchdog.log || \
		{ echo "vhost-watchdog: no watchdog record naming IDLE"; exit 1; }
	@us=$$(sed -n 's/^\([0-9]*\) .* END watchdog .*/\1/p' out/vhost-watchdog.log); \
	[ -n "$$us" ] && [ $$us -ge 4100000 ] && [ $$us -le 5100000 ] || \
		{ echo "vhost-watchdog: the run ended at $$us us, outside 4100000..5100000"; exit 1; }
	@grep ' CRASH \| END ' out/vhost-watchdog.log
	@echo "# vhost-watchdog: the starve ended the run with reason watchdog inside the period, the record names IDLE: PASSED"
.PHONY: vhost-watchdog
