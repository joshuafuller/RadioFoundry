# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# Real input parts and clock; only the channel registry, interrupt controller and RTOS are stubbed.
VENCODER_SRC := $(VHOST_TESTS)vencoderTest.c $(ROOT)/boards/vhost/input_world_vhost.c \
	$(ROOT)/boards/vhost/keypad_vhost.c $(ROOT)/platform/host/vclock.c
VENCODER_INC := -I$(ROOT)/hal -I$(ROOT)/boards/vhost -I$(ROOT)/platform/host \
	-I$(ROOT)/platform/host/freertos -I$(ROOT)/third_party/FreeRTOS-Kernel/include

TESTS += vencoderTest
vencoderTest: $(VENCODER_SRC) $(ROOT)/boards/vhost/vboard.h $(ROOT)/platform/host/vchan.h
	$(CC) $(CFLAGS) -U__attribute__ -Wshadow -Wmissing-prototypes $(VENCODER_INC) -o $@ $(VENCODER_SRC)

run-boards:: vencoderTest
	./vencoderTest
