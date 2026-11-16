// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#ifndef RFOUNDRY_ATMODULE_H
#define RFOUNDRY_ATMODULE_H
#include "hal_stream.h"

#define ATMODULE_RX_MAX 999u
#define ATMODULE_TX_MAX 508u
#define ATMODULE_QUEUE 1200u

typedef struct {
	const char *version; // NULL accepts a nonempty identity for unqualified bench use
	const char *data_profile; // NULL: a complete channel-00 DATA envelope starts the session
	uint16_t tx_limit; // 1..508; use 20 until measured
	uint32_t ready_ms, command_ms, rx_ms;
} atmodule_profile_t;

typedef struct bt_module {
	const hal_stream_t *uart;
	void (*enable)(bool on);
	void (*flush)(void);
	uint32_t (*errors)(void); // cumulative UART overruns and line errors
	const atmodule_profile_t *profile;
	uint32_t startup_ms, off_ms;
} bt_module_t;

typedef enum {
	ATMODULE_DISABLED, ATMODULE_STARTING, ATMODULE_IDENTIFYING,
	ATMODULE_IDLE, ATMODULE_CONNECTED, ATMODULE_FAULT
} atmodule_state_t;

typedef struct {
	uint32_t malformed, timeouts, unknown, wrong_channel, overruns, refused, drops, ptt;
} atmodule_counts_t;

typedef struct {
	const bt_module_t *module;
	atmodule_state_t state;
	atmodule_counts_t counts;
	uint32_t since, last_rx, command_at, errors, off_at, generation;
	bool powered, pending, version_seen, discard;
	char version[21], line[129];
	size_t line_len, payload_len, payload_at;
	uint8_t payload[ATMODULE_RX_MAX], rx[ATMODULE_QUEUE], tx[ATMODULE_QUEUE];
	size_t rx_len, rx_at, tx_len;
	uint8_t wire[ATMODULE_TX_MAX + 16];
	size_t wire_len, wire_at;
} atmodule_t;

void atmodule_init(atmodule_t *s, const bt_module_t *module, uint32_t now);
bool atmodule_enable(atmodule_t *s, uint32_t now);
void atmodule_disable(atmodule_t *s, uint32_t now);
void atmodule_fault(atmodule_t *s, uint32_t now);
void atmodule_feed(atmodule_t *s, const uint8_t *buf, size_t len, uint32_t now);
void atmodule_step(atmodule_t *s, uint32_t now);
size_t atmodule_read(atmodule_t *s, uint8_t *buf, size_t max);
size_t atmodule_write(atmodule_t *s, const uint8_t *buf, size_t len);

// Single host-task adapter. Bind before start; end_session must reset host protocol and queued gateway state.
// The board's cable remains selected until an owner explicitly installs this stream as the host port.
void atmodule_stream_bind(const bt_module_t *module, uint32_t (*clock_ms)(void), void (*end_session)(void));
extern const hal_stream_t atmodule_stream;
#endif
