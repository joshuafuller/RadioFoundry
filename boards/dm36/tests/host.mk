# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
HERE := $(dir $(lastword $(MAKEFILE_LIST)))
TESTS += receiveOnlyTest
receiveOnlyTest: $(HERE)receiveOnlyTest.c $(HERE)../bands.h $(ROOT)/hal/board.h fuzz.h
	$(CC) $(CFLAGS) -I$(ROOT)/hal -I. -o $@ $<
run-boards:: receiveOnlyTest
	./receiveOnlyTest
