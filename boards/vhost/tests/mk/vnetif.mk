# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The real network part and clock, with an in-process channel and masked interrupt controller.
TESTS += vnetifTest
vnetifTest: $(VHOST_TESTS)vnetifTest.c $(ROOT)/boards/vhost/netif_vhost.c $(ROOT)/platform/host/vclock.c \
	$(ROOT)/boards/vhost/vboard.h $(ROOT)/hal/hal_netif.h $(ROOT)/platform/host/vchan.h \
	$(ROOT)/platform/host/vchan_proto.h $(ROOT)/platform/host/vclock.h
	$(CC) $(VCLOCK_CFLAGS) -Wshadow -Wmissing-prototypes -DCONFIG_USB_NETWORK=1 \
		-I$(ROOT)/boards/vhost -o $@ $(filter %.c,$^)

run-boards:: vnetifTest
	./vnetifTest
