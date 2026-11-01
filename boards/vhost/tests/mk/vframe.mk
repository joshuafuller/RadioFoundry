# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
TESTS += vframeTest
vframeTest: $(VHOST_TESTS)vframeTest.c $(ROOT)/platform/host/vframe.c $(ROOT)/platform/host/vframe.h \
	$(VHOST_TESTS)../display_profiles_vhost.c $(VHOST_TESTS)../display_profiles_vhost.h \
	$(CORE)/ui/gfx.c $(CORE)/ui/gfx.h $(ROOT)/platform/host/vchan_proto.h | vhost-elf
	$(CC) $(CFLAGS) -U__attribute__ -Wshadow -I$(ROOT)/hal -I$(VHOST_TESTS).. -I$(ROOT)/platform/host \
		-DGFX_INDEXED=1 -DGFX_FB_BYTES=153600u -DGFX_MAX_ROWS=320u \
		-DVFRAME_ELF='"$(abspath $(VHOST_ELF))"' -DVFRAME_OUT='"$(abspath out)"' \
		-ffunction-sections -fdata-sections -Wl,--gc-sections -Wl,--wrap=gfx_flush -o $@ $(filter %.c,$^)

ifeq ($(shell uname -s),Linux)
run-boards:: vframeTest
	./vframeTest
	./vframeTest --channel
endif
