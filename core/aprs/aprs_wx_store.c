// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include <string.h>
#include "aprs_int.h"
#if CONFIG_APRS_WX
_Static_assert(APRS_WX_STATIONS > 0 && APRS_WX_STATIONS <= 255, "weather table size");
// Ordinary .bss: the APRS engine's CPU-only RAM section may be full.
static aprs_wx_station_t stations[APRS_WX_STATIONS];
static uint8_t count;

void aprs_wx_reset(void) { count = 0; }

void aprs_wx_receive(const aprs_station_t *s, const aprs_wx_t *wx)
{
	uint8_t i = 0;
	while (i < count && strcmp(stations[i].call, s->call) != 0) i++;
	if (i == count) {
		if (count < APRS_WX_STATIONS) count++;
		i = (uint8_t)(count - 1u);
	}
	memmove(stations + 1, stations, (size_t)i * sizeof(*stations));
	stations[0] = (aprs_wx_station_t) {
		.heard_ms = s->heard_ms, .has_pos = s->has_pos, .lat = s->lat, .lon = s->lon, .wx = *wx,
	};
	memcpy(stations[0].call, s->call, sizeof s->call);
}

uint8_t aprs_wx_count(void)
{
	LOCK();
	uint8_t n = count;
	UNLOCK();
	return n;
}

bool aprs_wx_station(uint8_t i, aprs_wx_station_t *out)
{
	LOCK();
	bool found = i < count;
	if (found) *out = stations[i];
	UNLOCK();
	return found;
}
#endif
