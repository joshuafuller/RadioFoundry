// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Shared display and init match section 7; bezel insets remain unverified.
#include "board.h"
#include "config.h"
#if CONFIG_GPS
#error "This board has no GPS module"
#endif
#include "dm32.h"
#if CONFIG_BT_MODULE_TRANSPORT
#include "bt/atmodule/atmodule.h"
#include "uart_module.h"
#include "pins.h"
#define BOARD_HAS_BT_MODULE 1
#endif
#include "platform.h"
#include "hrc_afsk.h"
#include "hrc_bcast.h"
#if CONFIG_BCAST_FM
#include "bcast/rda5807m/rda5807m_hal.h"
#endif
#if CONFIG_NET_PPP
#include "hostlink/hostlink_ppp.h"
#endif
#if CONFIG_RX_EXT
static bool rx_ext_on;
#endif
#include "bands.h"
#include "../dm32/board_keys.h"
#define BOARD_EXTRA_CAPS CAP_VOLUME_ENCODER
#if CONFIG_BT_MODULE_TRANSPORT
static void bt_enable(bool on)
{
	hrc_pin_write(PIN_BT_ENABLE, on);
}

static bool bt_start(void)
{
	// UART3 function selection; physical traces still need the owner-authorized bench check.
	IO_DIPLEX0 &= ~(0xFu << 13);
	hrc_module_uart_configure(BT_UART_BASE, IRQ_UART3, BT_UART_BAUD);
	return hrc_module_uart_stream.start();
}

static bool bt_connected(void) { return hrc_module_uart_stream.connected(); }
static size_t bt_read(uint8_t *b, size_t n) { return hrc_module_uart_stream.read(b, n); }
static size_t bt_write(const uint8_t *b, size_t n) { return hrc_module_uart_stream.write(b, n); }
static bool bt_wait(uint32_t ms) { return hrc_module_uart_stream.wait_rx(ms); }
static const hal_stream_t bt_uart = {
	.start = bt_start, .connected = bt_connected, .read = bt_read, .write = bt_write, .wait_rx = bt_wait,
};
static const atmodule_profile_t bt_profile = {
	.version = NULL, .data_profile = NULL, .tx_limit = 20,
	.ready_ms = 5000, .command_ms = 2000, .rx_ms = 500,
};
static const bt_module_t bt_module = {
	.uart = &bt_uart, .enable = bt_enable, .flush = hrc_module_uart_flush,
	.errors = hrc_module_uart_errors, .profile = &bt_profile,
	.startup_ms = 350, .off_ms = 350, // provisional milliseconds; stock tick duration is unknown
};
#endif
const board_t board = {
	.name = "Baofeng DM-36",
#if CONFIG_BT_MODULE_TRANSPORT
	.bt_module = &bt_module,
#endif
#include "../dm32/board_fields.h"
};
