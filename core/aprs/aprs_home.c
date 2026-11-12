// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include "config.h"
#if CONFIG_APRS_HOME
#include <string.h>
#include "aprs/aprs_home.h"
#include "aprs/aprs_int.h"

// Main RAM: do not enlarge the engine's CCM state.
static aprs_home_t home;
#if CONFIG_APRS_RX
static uint8_t last_info[AX25_INFO_MAX];
static uint16_t last_len;
static ax25_addr_t last_src, last_dst;
static bool last_valid;
#endif

void aprs_home_reset(void)
{
	memset(&home, 0, sizeof home);
#if CONFIG_APRS_RX
	last_valid = false;
#endif
}

void aprs_home_get(aprs_home_t *out)
{
	LOCK();
	*out = home;
	UNLOCK();
}

void aprs_home_sent(const uint8_t *raw, size_t n, uint32_t now)
{
#if CONFIG_APRS_RX
	ax25_frame_t f;
	if (raw != NULL && (!ax25_decode(raw, n, &f) || !aprs_s.me_ok ||
	    !ax25_addr_equal(&f.src, &aprs_s.me)))
		return; // relaying somebody else's packet is not our own send
	last_valid = raw != NULL;
	if (last_valid) {
		last_src = f.src;
		last_dst = f.dst;
		last_len = f.info_len;
		memcpy(last_info, f.info, last_len);
	}
#else
	(void)raw;
	(void)n;
#endif
	home.sent = true;
	home.sent_ms = now;
	home.digi[0] = 0;
}

void aprs_home_repeat(const ax25_frame_t *f, uint32_t now)
{
#if CONFIG_APRS_RX
	if (!last_valid || now - home.sent_ms > 600000u ||
	    !ax25_addr_equal(&f->src, &last_src) || !ax25_addr_equal(&f->dst, &last_dst) ||
	    f->info_len != last_len || memcmp(f->info, last_info, last_len) != 0)
		return;
	for (uint8_t i = f->ndigi; i > 0; i--) {
		const ax25_addr_t *d = &f->digi[i - 1u];
		if (!d->h)
			continue;
		// An unfilled alias is not a digipeater's identity.
		if (!aprs_callsign_ok(d->call) || strncmp(d->call, "WIDE", 4) == 0 ||
		    strncmp(d->call, "TRACE", 5) == 0)
			return;
		ax25_addr_format(d, false, home.digi);
		home.repeated_ms = now;
		home.repeat_serial++;
		aprs_changed();
		return;
	}
#else
	(void)f;
	(void)now;
#endif
}

void aprs_home_listening(uint8_t state)
{
	if (home.listening != state) {
		home.listening = state;
		aprs_changed();
	}
}
#endif
