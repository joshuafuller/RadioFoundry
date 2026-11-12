// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Stationary decay and the direct / one-hop / direct / two-hop beacon cycle (aprs.md 7).
#include "aprs/aprs_pathing.h"
#if CONFIG_APRS_PATHING
#include <string.h>

void aprs_pathing_update(aprs_pathing_t *p, const aprs_cfg_t *c, const aprs_beacon_pos_t *pos,
			 const char *comment)
{
	uint16_t base = c->path == APRS_PATH_PROPORTIONAL ? 60 : c->interval_s;
	if (c->decay && base > 1800)
		base = 1800;
	bool policy = p->base_s != base || p->smart != c->smart || p->decay != c->decay ||
		      p->path != c->path || p->on != c->tx_on;
	if (policy || !p->valid || (pos->valid && (p->lat != pos->lat || p->lon != pos->lon)) ||
	    strcmp(p->comment, comment) != 0) {
		p->interval_s = base;
	}
	if (policy)
		p->slot = 0;
	p->base_s = base;
	p->smart = c->smart;
	p->decay = c->decay;
	p->path = c->path;
	p->on = c->tx_on;
	if (pos->valid) {
		p->valid = true;
		p->lat = pos->lat;
		p->lon = pos->lon;
	}
	strncpy(p->comment, comment, sizeof(p->comment) - 1u);
	p->comment[sizeof(p->comment) - 1u] = '\0';
}

uint16_t aprs_pathing_interval(const aprs_pathing_t *p, const aprs_cfg_t *c)
{
	return c->decay && !c->smart ? p->interval_s : p->base_s;
}

uint8_t aprs_pathing_path(const aprs_pathing_t *p)
{
	return p->slot == 3 ? APRS_PATH_WIDE1_WIDE2 : p->slot == 1 ? APRS_PATH_WIDE1 : APRS_PATH_NONE;
}

void aprs_pathing_sent(aprs_pathing_t *p, const aprs_cfg_t *c, uint8_t why)
{
	if (c->decay && !c->smart) {
		uint32_t next = (uint32_t)p->interval_s * 2u;
		p->interval_s = next > 1800 ? 1800 : (uint16_t)next;
	}
	if (why != APRS_WHY_MANUAL)
		p->slot = (uint8_t)((p->slot + 1u) % 4u);
}
#endif
