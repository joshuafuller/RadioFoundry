# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The virtual board's system part (boards/vhost/system_vhost.c): the fixed chip id, the bounded fake memory behind the
# debug link's own range check (core/debuglink/dl_chip.c), and reboot as a run that ends. The test includes the part's
# file, so no firmware link is needed. It has its own flags: this Makefile's CFLAGS turn __attribute__ off, which
# VHOST_OPTION needs. vhost-reboot sends a real REBOOT frame to the elf and checks how the run ends
VSYSTEM_FLAGS := -std=gnu11 -Wall -Wextra -g -I$(ROOT)/boards/vhost -I$(ROOT)/platform/host -I$(CORE) -I$(ROOT)/hal
TESTS += vsystemTest
vsystemTest: $(VHOST_TESTS)vsystemTest.c $(VHOST_TESTS)../system_vhost.c $(VHOST_TESTS)../vboard.h $(CORE)/debuglink/dl_chip.c
	$(CC) $(VSYSTEM_FLAGS) -o $@ $(VHOST_TESTS)vsystemTest.c $(CORE)/debuglink/dl_chip.c
run-boards:: vsystemTest
	./vsystemTest

# DL_CMD_REBOOT (0x0D), seq 1, mode NORMAL (0), token = the chip's uid[0] 0x0F00D001 (little endian), CRC-16 0x9AFA
VREBOOT_FRAME := a55a0d0105000001d0000ffa9a
ifeq ($(shell uname -s),Linux)
run-boards:: vhost-reboot
endif
# The frame arrives at 1000 ms; the debug link waits 250 ms (REBOOT_DELAY_MS), then the board saves the flash to the
# --restart-flash file and the run ends with reason "reboot", exit code 4. The file must exist with mode 0600
vhost-reboot: vhost-elf
	@mkdir -p out
	@rm -f out/vhost-reboot.flash
	@code=0; $(VHOST_ELF) --until-ms 10000 --no-stall-watch --host-port on --host 1000:$(VREBOOT_FRAME) \
		--restart-flash out/vhost-reboot.flash --trace out/vhost-reboot.log 2> out/vhost-reboot.err || code=$$?; \
	[ $$code -eq 4 ] || { echo "vhost-reboot: exit $$code, not 4"; exit 1; }
	@grep -q ' END reboot ' out/vhost-reboot.log || { echo "vhost-reboot: no END reboot"; exit 1; }
	@[ "$$(stat -c %a out/vhost-reboot.flash)" = 600 ] || { echo "vhost-reboot: flash file missing or not 0600"; exit 1; }
	@us=$$(sed -n 's/^\([0-9]*\) .* END reboot .*/\1/p' out/vhost-reboot.log); \
	[ -n "$$us" ] && [ $$us -ge 1200000 ] && [ $$us -le 1500000 ] || \
		{ echo "vhost-reboot: the run ended at $$us us, outside 1200000..1500000"; exit 1; }
	@grep ' REBOOT \| END ' out/vhost-reboot.log
	@echo "# vhost-reboot: REBOOT saved the flash and ended the run with reason reboot, exit 4: PASSED"
.PHONY: vhost-reboot
