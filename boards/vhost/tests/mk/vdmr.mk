# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The virtual DMR modem (boards/vhost/dmr_vhost.c) on a fake clock, interrupt controller, trace and RTOS: the test
# includes the modem's file, so it needs no firmware link. -U__attribute__ undoes the host tests' blanket
# '-D__attribute__(x)=', which would drop the modem's option constructor
VDMR_INC := -I$(ROOT)/hal -I$(ROOT)/platform/host -I$(ROOT)/platform/host/freertos \
            -I$(ROOT)/third_party/FreeRTOS-Kernel/include -I$(ROOT)/boards/vhost

TESTS += vdmrTest
vdmrTest: $(VHOST_TESTS)vdmrTest.c $(ROOT)/boards/vhost/dmr_vhost.c $(ROOT)/boards/vhost/dmr_vhost.h
	$(CC) $(CFLAGS) -U__attribute__ -Wshadow -Wmissing-prototypes $(VDMR_INC) -o $@ $(VHOST_TESTS)vdmrTest.c

run-boards:: vdmrTest
	./vdmrTest
