# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
TESTS += vviewTest
vviewTest: $(VHOST_TESTS)vviewTest.c $(ROOT)/platform/host/vview.c $(ROOT)/platform/host/vview.h \
	$(CORE)/ui/ui_app.h $(ROOT)/platform/host/vchan_proto.h
	$(CC) $(CFLAGS) -U__attribute__ -Werror -Wshadow -I$(ROOT)/hal -I$(ROOT)/platform/host \
		-Wl,--wrap=ui_app_status -o $@ $(filter %.c,$^)

run-boards:: vviewTest
	./vviewTest
