# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
TESTS += vairTest
vairTest: $(VHOST_TESTS)vairTest.c $(ROOT)/boards/vhost/dmr_air_vchan.c $(ROOT)/boards/vhost/dmr_vhost.h \
	$(ROOT)/platform/host/vchan.c $(ROOT)/platform/host/vclock.c
	$(CC) $(VCLOCK_CFLAGS) -I$(ROOT)/boards/vhost -I$(ROOT) -o $@ $(VHOST_TESTS)vairTest.c $(ROOT)/platform/host/vclock.c

run-boards:: vairTest
	./vairTest
