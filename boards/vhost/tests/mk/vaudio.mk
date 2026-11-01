# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The virtual audio parts and real AMBE voice glue, with a controlled clock, interrupts and deferred task.
VAUDIO_SRC := $(VHOST_TESTS)vaudioTest.c $(ROOT)/boards/vhost/audio_vhost.c $(ROOT)/boards/vhost/vocoder_vhost.c \
	$(CORE)/dmr/ambe/ambeVoice.c $(CORE)/dmr/ambe/ambeFec.c
VAUDIO_INC := -I$(ROOT)/hal -I$(ROOT)/platform/host -I$(ROOT)/platform/host/freertos \
	-I$(ROOT)/third_party/FreeRTOS-Kernel/include -I$(ROOT)/boards/vhost

TESTS += vaudioTest
vaudioTest: $(VAUDIO_SRC) $(ROOT)/boards/vhost/vboard.h $(ROOT)/hal/hal_audio.h $(ROOT)/hal/hal_vocoder.h
	$(CC) $(CFLAGS) -U__attribute__ -Wshadow -Wmissing-prototypes $(VAUDIO_INC) -o $@ $(VAUDIO_SRC)

run-boards:: vaudioTest
	./vaudioTest
