# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The host platform (the virtual radio, docs/dev/virtual-radio-architecture.md): the host gcc, the FreeRTOS kernel (MIT)
# with the deterministic host port in platform/host/freertos. No ST HAL, CMSIS or TinyUSB: none of their paths is on
# the include line. Every *.c in platform/host and platform/host/freertos is built (PLATFORM_DIRS), so a new part of
# the platform is a new file, with no edit here
CC := gcc
OBJCOPY := objcopy
SIZE := size
CPUFLAGS :=
STACK_OVERHEAD := 0
RTOS := third_party/FreeRTOS-Kernel
PLATFORM_DIRS := platform/host platform/host/freertos
PLATFORM_INC := -I$(RTOS)/include -Iplatform/host/freertos
PLATFORM_3P_SRC := $(RTOS)/tasks.c $(RTOS)/queue.c $(RTOS)/list.c $(RTOS)/timers.c $(RTOS)/event_groups.c \
	$(RTOS)/portable/MemMang/heap_4.c
# A host program has no memory map: no linker script, image address or region usage (the Makefile's LDMEMORY)
LDSCRIPT :=
LDMEMORY :=
# Link wraps (-Wl,--wrap=<symbol>: calls from other objects reach __wrap_<symbol>, which may call __real_<symbol>).
# The port's two: the idle hook (the clock's jump, vclock.c) and the scheduler lock (counted, port.c). A later part
# adds its own in a fragment of its own, platform/host/mk/<part>.mk or platform/host/wrap/<part>.mk, with
# "VHOST_WRAPS += <symbol>" (and "VHOST_LDFLAGS += ..." for other link flags), so it never edits this file
VHOST_WRAPS := vApplicationIdleHook vTaskSuspendAll
VHOST_LDFLAGS :=
-include $(sort $(wildcard platform/host/mk/*.mk platform/host/wrap/*.mk))
# The host C library (glibc) and its threads, the wraps, and the map's cross reference table (which object pulls in
# each symbol), for the check that no test program copies a task loop. Read by the Makefile's LDFLAGS, after this
VHOST_COMMA := ,
LDSPECS := -pthread $(addprefix -Wl$(VHOST_COMMA)--wrap=,$(VHOST_WRAPS)) -Wl,--cref $(VHOST_LDFLAGS)
