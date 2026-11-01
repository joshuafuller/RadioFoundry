# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The host FreeRTOS port's unit test (boards/vhost/tests/hostPortTest.c): the real port, kernel and tick handler, the
# six paths the skeleton's runs never take. hostPortTestBreak is the same test against a deliberately wrong port
# (-DHOST_PORT_TEST_BREAK=1: interrupts delivered at raise time); the run passes only when that one fails.
# The kernel is third-party code, built with -w as the firmware build does (CFLAGS_3P in the top Makefile)
HOSTPORT_RTOS := $(ROOT)/third_party/FreeRTOS-Kernel
HOSTPORT_KERNEL := $(HOSTPORT_RTOS)/tasks.c $(HOSTPORT_RTOS)/queue.c $(HOSTPORT_RTOS)/list.c \
	$(HOSTPORT_RTOS)/timers.c $(HOSTPORT_RTOS)/portable/MemMang/heap_4.c
HOSTPORT_INC := -I$(ROOT)/platform/host -I$(ROOT)/platform/host/freertos -I$(HOSTPORT_RTOS)/include -I$(ROOT)/core \
	-DCONFIG_WATCHDOG=0 -DCONFIG_CRASH_LOG=0
HOSTPORT_FLAGS := -std=gnu11 -g -pthread $(COVFLAGS) $(SANFLAGS) $(HOSTPORT_INC)
HOSTPORT_WARN := -Wall -Wextra -Wno-unused-parameter -Wshadow
HOSTPORT_LINK := -pthread -Wl,--wrap=vTaskSuspendAll,--wrap=malloc,--wrap=free,--wrap=pthread_join
HOSTPORT_OBJ := $(patsubst $(HOSTPORT_RTOS)/%.c,out/hostport/%.o,$(HOSTPORT_KERNEL))
HOSTPORT_SRC := $(VHOST_TESTS)hostPortTest.c $(ROOT)/platform/host/freertos/port.c $(ROOT)/platform/host/vtick.c
HOSTPORT_DEPS := $(HOSTPORT_SRC) $(ROOT)/platform/host/freertos/portmacro.h $(ROOT)/platform/host/vport.h \
	$(ROOT)/platform/host/FreeRTOSConfig.h

out/hostport/%.o: $(HOSTPORT_RTOS)/%.c $(ROOT)/platform/host/FreeRTOSConfig.h $(ROOT)/platform/host/freertos/portmacro.h
	@mkdir -p $(dir $@)
	$(CC) $(HOSTPORT_FLAGS) -w -c -o $@ $<

hostPortTest: $(HOSTPORT_DEPS) $(HOSTPORT_OBJ)
	$(CC) $(HOSTPORT_FLAGS) $(HOSTPORT_WARN) -o $@ $(HOSTPORT_SRC) $(HOSTPORT_OBJ) $(HOSTPORT_LINK)

hostPortTestBreak: $(HOSTPORT_DEPS) $(HOSTPORT_OBJ)
	$(CC) $(HOSTPORT_FLAGS) $(HOSTPORT_WARN) -DHOST_PORT_TEST_BREAK=1 -o $@ $(HOSTPORT_SRC) $(HOSTPORT_OBJ) \
		$(HOSTPORT_LINK)

# The host platform is Linux only (glibc threads, /proc, GNU ld link wraps): elsewhere the run says it is skipped
ifeq ($(shell uname -s),Linux)
TESTS += hostPortTest hostPortTestBreak
run-boards:: hostPortTest hostPortTestBreak
	timeout 60 ./hostPortTest
	@if command -v taskset > /dev/null; then cpu=$$(python3 -c 'import os; print(min(os.sched_getaffinity(0)))'); \
		echo "# hostPortTest on allowed CPU $$cpu"; \
		timeout 60 taskset -c $$cpu ./hostPortTest; fi
	@code=0; timeout 60 ./hostPortTestBreak > out/hostPortTestBreak.log 2>&1 || code=$$?; \
	if [ $$code -eq 124 ]; then echo "# hostPortTestBreak: hung (timeout), not a clean failure"; exit 1; fi; \
	if [ $$code -eq 0 ]; then cat out/hostPortTestBreak.log; \
		echo "# hostPortTestBreak: the wrong port passed, so the test does not catch it"; exit 1; \
	else grep -q '^FAIL case_irq_waits_for_critical_end' out/hostPortTestBreak.log || \
		{ cat out/hostPortTestBreak.log; echo "# hostPortTestBreak: failed, but not in case 1"; exit 1; }; \
		echo "# hostPortTestBreak: the wrong port fails case 1, as it must"; fi
else
run-boards::
	@echo "# hostPortTest: skipped, the host platform builds on Linux only"
endif
