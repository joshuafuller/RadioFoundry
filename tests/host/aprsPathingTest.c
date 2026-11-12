// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include <assert.h>
#include <stdio.h>
#include "aprs/aprs_pathing.h"

int main(void)
{
	aprs_pathing_t p = {0};
	aprs_cfg_t c = { .tx_on = true, .interval_s = 60, .decay = true };
	aprs_beacon_cfg_t bcfg = { .auto_on = true, .min_s = 30 };
	aprs_beacon_pos_t pos = { .valid = true, .lat = 490000000, .lon = -720000000, .speed_kmh = 0xFFFFu };
	aprs_beacon_t b;
	uint32_t now = UINT32_MAX - 30000u; // exercise the millisecond wrap
	aprs_beacon_init(&b, &bcfg, now);
	aprs_pathing_update(&p, &c, &pos, "Here");
	const uint16_t periods[] = {60, 120, 240, 480, 960, 1800, 1800};
	for (unsigned i = 0; i < sizeof(periods) / sizeof(periods[0]); i++) {
		bcfg.interval_s = aprs_pathing_interval(&p, &c);
		assert(bcfg.interval_s == periods[i]);
		now += (uint32_t)periods[i] * 1000u;
		assert(aprs_beacon_due(&b, &bcfg, &pos, now - 1) == APRS_WHY_NONE);
		assert(aprs_beacon_next_s(&b, &bcfg, &pos, now) == 0);
		assert(aprs_beacon_due(&b, &bcfg, &pos, now) == APRS_WHY_INTERVAL);
		aprs_beacon_sent(&b, APRS_WHY_INTERVAL, &pos, now);
		aprs_pathing_sent(&p, &c, APRS_WHY_INTERVAL);
		aprs_pathing_update(&p, &c, &pos, "Here");
	}
	pos.lon++;
	aprs_pathing_update(&p, &c, &pos, "Here");
	assert(aprs_pathing_interval(&p, &c) == 60);
	aprs_pathing_sent(&p, &c, APRS_WHY_INTERVAL);
	aprs_pathing_update(&p, &c, &pos, "New comment");
	assert(aprs_pathing_interval(&p, &c) == 60);
	c.interval_s = 0;
	aprs_pathing_update(&p, &c, &pos, "New comment");
	assert(aprs_pathing_interval(&p, &c) == 0);
	c.path = APRS_PATH_PROPORTIONAL;
	c.decay = false;
	aprs_pathing_update(&p, &c, &pos, "New comment");
	assert(aprs_pathing_interval(&p, &c) == 60);
	const uint8_t paths[] = {APRS_PATH_NONE, APRS_PATH_WIDE1, APRS_PATH_NONE, APRS_PATH_WIDE1_WIDE2};
	for (unsigned i = 0; i < 8; i++) {
		assert(aprs_pathing_path(&p) == paths[i % 4]);
		aprs_pathing_sent(&p, &c, APRS_WHY_MANUAL);
		assert(aprs_pathing_path(&p) == paths[i % 4]);
		aprs_pathing_sent(&p, &c, APRS_WHY_INTERVAL);
	}
	c.smart = c.decay = true;
	aprs_pathing_update(&p, &c, &pos, "New comment");
	aprs_pathing_sent(&p, &c, APRS_WHY_SMART);
	assert(aprs_pathing_interval(&p, &c) == 60);
	bcfg.smart = true;
	bcfg.slow_s = 1800;
	bcfg.fast_s = 180;
	bcfg.fast_kmh = 90;
	bcfg.slow_kmh = 5;
	bcfg.interval_s = 60;
	aprs_beacon_init(&b, &bcfg, 0);
	assert(aprs_beacon_due(&b, &bcfg, &pos, 60000) == APRS_WHY_NONE);
	assert(aprs_beacon_due(&b, &bcfg, &pos, 1800000) == APRS_WHY_SMART);
	assert(aprs_beacon_next_s(&b, &bcfg, &pos, 60000) == 1740);
	puts("aprsPathingTest: passed");
	return 0;
}
