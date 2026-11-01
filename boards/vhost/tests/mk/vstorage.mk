# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# Start storage uses the production editors, settings and journal over the virtual NOR.
VSTORAGE_SRC := $(VHOST_TESTS)../storage_vhost.c $(VHOST_TESTS)../flash_vhost.c \
	$(CORE)/codeplug/codeplug.c $(CORE)/settings/settings.c $(CORE)/storage/storage.c
VSTORAGE_FLAGS := -std=gnu11 -Wall -Wextra -Werror -Wshadow -g -O2 $(SANFLAGS) $(COVFLAGS) \
	-I$(ROOT)/boards/vhost -I$(ROOT)/platform/host -I$(CORE) -I$(ROOT)/hal
TESTS += vstorageTest
vstorageTest: $(VHOST_TESTS)vstorageTest.c $(VSTORAGE_SRC) $(VHOST_TESTS)../storage_vhost.h $(VHOST_TESTS)../vboard.h
	$(CC) $(VSTORAGE_FLAGS) -o $@ $(VHOST_TESTS)vstorageTest.c $(VSTORAGE_SRC)
run-boards:: vstorageTest
	./vstorageTest
