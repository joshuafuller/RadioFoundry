// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Control channel wire contract: docs/dev/virtual-radio-channel.md. Encode fields explicitly, never a C struct.
#ifndef VCHAN_PROTO_H
#define VCHAN_PROTO_H

#include <stdint.h>

#define VCHAN_VERSION 1u
#define VCHAN_HEADER_SIZE 32u
#define VCHAN_PAYLOAD_MAX 2048u
#define VCHAN_DONE_SIZE 16u

// Header offsets, all multibyte fields little endian: version u8, kind u8, length u16, time u64, round u32,
// class u32, source u32, sequence u64. Time is radio-relative microseconds. Inputs use class 30.
enum {
	VCHAN_VERSION_OFF = 0, VCHAN_KIND_OFF = 1, VCHAN_LENGTH_OFF = 2, VCHAN_TIME_OFF = 4,
	VCHAN_ROUND_OFF = 12, VCHAN_CLASS_OFF = 16, VCHAN_SOURCE_OFF = 20, VCHAN_SEQUENCE_OFF = 24,
};

enum vchan_kind {
	VCHAN_GRANT = 1,
	VCHAN_DRAIN = 2,
	VCHAN_DONE = 3,
	VCHAN_RESTART_REASON = 4,
	VCHAN_BYTES_IN = 5,
	VCHAN_BYTES_OUT = 6,
	VCHAN_KEY = 7,
	VCHAN_ENC = 8,
	VCHAN_ETH_IN = 9,
	VCHAN_ETH_OUT = 10,
	VCHAN_FRAME = 11,
	VCHAN_TRACE = 12,
	VCHAN_AIR_OUT = 13,
	VCHAN_AIR_IN = 14,
	VCHAN_VIEW = 15,
	VCHAN_STORE = 16,
};

typedef struct {
	uint64_t time_us;
	uint64_t sequence;
	uint32_t round, cls, source;
	uint16_t length;
	uint8_t kind;
	uint8_t payload[VCHAN_PAYLOAD_MAX];
} vchan_message_t;

#endif
