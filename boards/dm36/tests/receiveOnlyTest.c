// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include <assert.h>
#include <stdio.h>
#include "board.h"
#include "fuzz.h"
#include "../pins.h"
#if defined(PIN_GPS_POWER) || defined(PIN_GPS_RXD) || defined(PIN_BCAST_SDA) || defined(PIN_BCAST_SCL)
#error "Unresolved board nets must not inherit peripheral assignments"
#endif
_Static_assert(PIN_BT_ENABLE == 0x30, "Bluetooth enable");
_Static_assert(PIN_BT_RXD == 0x0D && PIN_BT_TXD == 0x0E, "UART3 mux pins");
_Static_assert(BT_UART_BASE == 0x14090000u && BT_UART_BAUD == 115200u, "Bluetooth UART");
_Static_assert(PIN_ENC_A == 0x27 && PIN_ENC_B == 0x26, "Volume encoder inputs");
_Static_assert(PIN_LCD_RESET == 0x42 && PIN_LCD_RD == 0x46 && PIN_KEY_ROW0 == 0x47, "Shared LCD bus");
#define CONFIG_AM_RX 1
#define CONFIG_BCAST_FM 1
#define CONFIG_RX_EXT 1
#include "../bands.h"
static bool rx_ext_on;
const board_t board = {
	.bands = bands, .band_count = sizeof bands / sizeof bands[0],
	.rx_ext_count = 3, .rx_ext_on = &rx_ext_on,
};
int main(void)
{
	fuzz_t f;
	fuzz_init(&f, 0x36);
	unsigned checks = 0;
	for (unsigned ext = 0; ext < 2; ext++) {
		board_rx_ext_set(ext != 0);
		for (unsigned i = 0; i < board.band_count; i++) {
			const uint32_t edge[] = { bands[i].lo_hz - 1, bands[i].lo_hz,
				bands[i].hi_hz, bands[i].hi_hz + 1 };
			assert(!bands[i].tx);
			for (unsigned j = 0; j < sizeof edge / sizeof edge[0]; j++) {
				assert(!board_in_band(edge[j], true));
				assert(board_tx_band(edge[j]) < 0);
				checks += 2;
			}
		}
		assert(board_in_band(146520000, false));
		assert(board_in_band(446000000, false));
		assert(!board_in_band(0, true) && !board_in_band(UINT32_MAX, true));
		for (long i = 0; i < fuzz_iters(10000); i++) {
			uint32_t hz = fuzz_next(&f);
			assert(!board_in_band(hz, true));
			assert(board_tx_band(hz) < 0);
			checks += 2;
		}
	}
	printf("receiveOnlyTest: %u TX checks plus RX and edge assertions passed\n", checks);
	return 0;
}
