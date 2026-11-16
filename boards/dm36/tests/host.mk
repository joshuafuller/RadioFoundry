# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
HERE := $(dir $(lastword $(MAKEFILE_LIST)))
TESTS += receiveOnlyTest
receiveOnlyTest: $(HERE)receiveOnlyTest.c $(HERE)../bands.h $(HERE)../pins.h $(HERE)../../dm32/pins.h $(ROOT)/hal/board.h fuzz.h
	$(CC) $(CFLAGS) -I$(ROOT)/hal -I. -o $@ $<
run-boards:: receiveOnlyTest
	./receiveOnlyTest
