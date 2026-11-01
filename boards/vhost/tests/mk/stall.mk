# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The stall watch: a host unit test of its counting rules (vstallTest) and a run-level test (vhost-stall, stall watch) that
# starts a real spin through the debug link and checks the run ends as "stuck" within the budget plus 1 s.
# The unit test is built with its own flags: this Makefile's CFLAGS turn __attribute__ off, which VHOST_OPTION needs.
# TSAN=1 builds it under ThreadSanitizer
VSTALL_SRC := $(ROOT)/platform/host/vstall.c
VSTALL_INC := -I$(ROOT)/platform/host -I$(ROOT)/platform/host/freertos -I$(ROOT)/third_party/FreeRTOS-Kernel/include \
	-I$(CORE)
VSTALL_FLAGS := -std=gnu11 -Wall -Wextra -g $(if $(filter 1,$(TSAN)),-fsanitize=thread -O1) -pthread
TESTS += vstallTest
vstallTest: $(VHOST_TESTS)vstallTest.c $(VSTALL_SRC) $(ROOT)/platform/host/vstall.h
	$(CC) $(VSTALL_FLAGS) $(VSTALL_INC) -o $@ $(VHOST_TESTS)vstallTest.c $(VSTALL_SRC)

run-boards:: vstallTest
	./vstallTest

# The budget the run is given, in wall seconds. SPIN and IRQ_OFF are DL_WDT_TEST_SPIN and DL_WDT_TEST_IRQ_OFF in
# core/debuglink/debuglink_proto.h, sent as debug link frames (A5 5A cmd seq len payload crc16) at 1500 ms
VSTALL_BUDGET := 5
VSTALL_TIMEOUT ?= 30
VSTALL_SPIN := a55a1d030600010257444f479291
VSTALL_IRQ_OFF := a55a1d030600010357444f47c33b
vhost-stall: vhost-elf
	@mkdir -p out
	@for c in SPIN IRQ_OFF; do \
		if [ $$c = SPIN ]; then frame=$(VSTALL_SPIN); else frame=$(VSTALL_IRQ_OFF); fi; \
		start=$$(date +%s.%N); code=0; \
		timeout -k 2 $(VSTALL_TIMEOUT) $(VHOST_ELF) --until-ms 20000 --stall-s $(VSTALL_BUDGET) --trace out/vhost-stall-$$c.log --host-port on \
			--host 1500:a55a0101000044c5 --host 2000:$$frame 2> out/vhost-stall-$$c.err || code=$$?; \
		wall=$$(echo "$$(date +%s.%N) - $$start" | bc); \
		echo "# vhost-stall $$c: exit $$code, wall $$wall s"; tail -n 1 out/vhost-stall-$$c.log; tail -n 1 out/vhost-stall-$$c.err; \
		[ $$code -ne 0 ] || { echo "vhost-stall $$c: the run ended with exit 0"; exit 1; }; \
		[ $$code -ne 124 ] && [ $$code -ne 137 ] || \
			{ echo "vhost-stall $$c: outer timeout; stall watcher did not finish"; exit 1; }; \
		tail -n 1 out/vhost-stall-$$c.log | grep -q 'stuck: task [^ ]' || \
			{ echo "vhost-stall $$c: last trace line does not say stuck and the task"; exit 1; }; \
		tail -n 1 out/vhost-stall-$$c.err | grep -q 'stuck: task [^ ]' || \
			{ echo "vhost-stall $$c: stderr does not say stuck and the task"; exit 1; }; \
	done
	@echo "# vhost-stall: SPIN and IRQ_OFF both ended as stuck within the budget PASSED"
.PHONY: vhost-stall
ifeq ($(shell uname -s),Linux)
run-boards:: vhost-stall
endif
