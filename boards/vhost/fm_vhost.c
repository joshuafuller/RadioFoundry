// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual board's radio (vboard.h): an FM receiver that hears nothing. Every call is a trace line.
#include "vboard.h"
#include "vtrace.h"

static void r_init(void)
{
	vtrace("BOARD radio.init");
}

static void r_mode(radio_mode_t m)
{
	vtrace("BOARD radio.set_mode %d", (int)m);
}

static bool r_rx(uint32_t hz, radio_bw_t bw, const subtone_t *t)
{
	vtrace("BOARD radio.rx %lu bw=%d tone=%u/%u", (unsigned long)hz, (int)bw, t ? t->type : 0u, t ? t->value : 0u);
	return board_in_band(hz, false);
}

static bool r_tx(uint32_t hz, radio_bw_t bw, uint8_t power, const subtone_t *t)
{
	(void)t;
	vtrace("BOARD radio.tx %lu bw=%d power=%u", (unsigned long)hz, (int)bw, power);
	return board_in_band(hz, true);
}

static void r_tx_off(void)
{
	vtrace("BOARD radio.tx_off");
}

static void r_status(radio_status_t *st)
{
	st->rssi_dbm = -127;
	st->noise = 200;
	st->carrier = false;
	st->tone_ok = true;
}

static void r_audio(bool on)
{
	vtrace("BOARD radio.audio %d", on);
}

const hal_radio_t vboard_fm = {
	.init = r_init, .set_mode = r_mode, .rx = r_rx, .tx = r_tx, .tx_off = r_tx_off, .status = r_status,
	.audio = r_audio,
};
