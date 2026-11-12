# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
ROOT := $(abspath ../..)
OUT := $(ROOT)/build/atak-lab
PYTHON ?= /usr/bin/python3
CFLAGS := -std=gnu99 -O2 -g -Wall -Wextra -Werror -Wshadow -Wno-unused-parameter -I$(ROOT)/core -I$(ROOT)/hal \
	-DCONFIG_NET_PPP=1 -DCONFIG_ZONE_SHARE=0
SOURCES := radio_node.c $(ROOT)/core/dmr/ipGateway.c $(ROOT)/core/dmr/dmrData.c \
	$(ROOT)/core/hotspot/dmrDataFrame.c $(ROOT)/core/dmr/fec/dmrFec.c \
	$(ROOT)/core/net/ppp.c $(ROOT)/core/hostlink/hostlink_ppp.c \
	$(ROOT)/core/dmr/dmrAirQueue.c $(ROOT)/core/dmr/dmrDataService.c \
	$(ROOT)/core/dmr/dmrLrrp.c $(ROOT)/core/dmr/dmr_emergency.c \
	$(ROOT)/core/dmr/call/dmr_lc.c $(ROOT)/core/position/nmea.c

.DEFAULT_GOAL := check
.PHONY: check
check: $(OUT)/radio-node
	uv run --no-project --python "$(PYTHON)" python lab.py --worker "$(OUT)/radio-node" --out "$(OUT)"

$(OUT)/radio-node: $(SOURCES) $(ROOT)/core/dmr/ipGateway.h $(ROOT)/core/dmr/dmrData.h $(ROOT)/core/hotspot/dmrDataFrame.h \
	$(ROOT)/core/net/ppp.h $(ROOT)/core/dmr/dmrAirQueue.h $(ROOT)/core/dmr/dmrDataService.h
	mkdir -p "$(OUT)"
	$(CC) $(CFLAGS) $(SOURCES) -lm -o "$@"
