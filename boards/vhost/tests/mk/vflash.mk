# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The virtual flash model (boards/vhost/flash_vhost.c): NOR rules, flash.set_bit, sparse memory, save and load.
# The test includes the model's file, so the build needs no firmware link. The host tests' blanket
# '-D__attribute__(x)=' strips the option constructor (the test calls the setter itself), so its now unused
# registration function would warn: -Wno-unused-function for this one build
vflashTest: $(VHOST_TESTS)vflashTest.c $(VHOST_TESTS)../flash_vhost.c $(VHOST_TESTS)../vboard.h
	$(CC) $(CFLAGS) -Wno-unused-function -I$(ROOT)/boards/vhost -I$(ROOT)/platform/host -I$(ROOT)/core -I$(ROOT)/hal -o $@ $(VHOST_TESTS)vflashTest.c
run-boards:: vflashTest
	./vflashTest
