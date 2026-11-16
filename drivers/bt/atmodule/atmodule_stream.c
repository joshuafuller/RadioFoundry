// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include "atmodule.h"

static atmodule_t state;
static uint32_t (*clock_ms)(void);
static void (*end_session)(void);
static bool bound;

void atmodule_stream_bind(const bt_module_t *module, uint32_t (*clock)(void), void (*end)(void))
{
	if (bound) {
		atmodule_disable(&state, clock_ms());
		end_session();
	}
	bound = module != NULL && clock != NULL && end != NULL;
	clock_ms = clock;
	end_session = end;
	if (bound) {
		atmodule_init(&state, module, clock_ms());
	}
}

static void service(void)
{
	if (bound) {
		uint32_t gen = state.generation;
		atmodule_step(&state, clock_ms());
		if (gen != state.generation) {
			end_session();
		}
	}
}

static bool start(void)
{
	return bound && atmodule_enable(&state, clock_ms());
}

static bool connected(void)
{
	return bound && state.state == ATMODULE_CONNECTED;
}

static size_t read_bytes(uint8_t *buf, size_t max)
{
	return bound ? atmodule_read(&state, buf, max) : 0;
}

static size_t write_bytes(const uint8_t *buf, size_t len)
{
	if (!bound) {
		return 0;
	}
	size_t n = atmodule_write(&state, buf, len);
	if (n != len && state.state == ATMODULE_CONNECTED) {
		// Existing host producers may ignore short writes: terminate rather than splice their stream.
		atmodule_fault(&state, clock_ms());
		end_session();
	}
	return n;
}

static bool wait_rx(uint32_t timeout_ms)
{
	service();
	if (bound && state.rx_len == state.rx_at) {
		// The UART owns scheduling; protocol deadlines are checked at least every 5 ms.
		state.module->uart->wait_rx(timeout_ms < 5 ? timeout_ms : 5);
		service();
	}
	return connected() && state.rx_len != state.rx_at;
}

const hal_stream_t atmodule_stream = {
	.start = start, .connected = connected, .read = read_bytes, .write = write_bytes, .wait_rx = wait_rx,
};
