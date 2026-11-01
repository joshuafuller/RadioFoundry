# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The virtual board's input and power parts (uart_vhost.c, keypad_vhost.c, power_vhost.c) on a fake clock, interrupt
# controller and trace: the parts are compiled as they are, with no RTOS. The firmware headers need config.h, which
# the build makes from the board, so the test includes the real ones the vhost build uses (FreeRTOS, hal).
# -U__attribute__ undoes the host tests' blanket '-D__attribute__(x)=', which would drop the parts' constructors
# (their options) and the weak hook
VINPUT_INC := -I$(ROOT)/hal -I$(ROOT)/platform/host -I$(ROOT)/platform/host/freertos \
              -I$(ROOT)/third_party/FreeRTOS-Kernel/include -I$(ROOT)/boards/vhost
VINPUT_SRC := $(VHOST_TESTS)vinputTest.c $(ROOT)/boards/vhost/uart_vhost.c $(ROOT)/boards/vhost/keypad_vhost.c \
              $(ROOT)/boards/vhost/power_vhost.c

TESTS += vinputTest
vinputTest: $(VINPUT_SRC) $(ROOT)/boards/vhost/vboard.h
	$(CC) $(CFLAGS) -U__attribute__ -Wshadow -Wmissing-prototypes $(VINPUT_INC) -o $@ $(VINPUT_SRC)

run-boards:: vinputTest
	./vinputTest
