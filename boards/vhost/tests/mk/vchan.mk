# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
TESTS += vchanTest
vchanTest: $(VHOST_TESTS)vchanTest.c $(ROOT)/platform/host/vchan.c $(ROOT)/platform/host/vclock.c \
	$(ROOT)/platform/host/vchan.h $(ROOT)/platform/host/vchan_proto.h $(ROOT)/platform/host/vclock.h
	$(CC) $(VCLOCK_CFLAGS) -I$(ROOT)/boards/vhost -I$(ROOT) -o $@ $(filter %.c,$^)
ifeq ($(shell uname -s),Linux)
run-boards:: vchanTest
	./vchanTest
else
run-boards::
	@echo "# vchanTest: skipped, the host platform builds on Linux only"
endif
