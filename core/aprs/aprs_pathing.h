// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#ifndef RFOUNDRY_APRS_PATHING_H
#define RFOUNDRY_APRS_PATHING_H

#include "aprs/aprs.h"
#include "aprs/aprs_beacon.h"
#include "codeplug/codeplug.h"

#if CONFIG_APRS_PATHING
typedef struct {
	bool valid, smart, decay, on;
	uint16_t base_s, interval_s;
	uint8_t path, slot;
	int32_t lat, lon;
	char comment[APRS_COMMENT_MAX + 1];
} aprs_pathing_t;

void aprs_pathing_update(aprs_pathing_t *p, const aprs_cfg_t *c, const aprs_beacon_pos_t *pos,
			 const char *comment);
uint16_t aprs_pathing_interval(const aprs_pathing_t *p, const aprs_cfg_t *c);
uint8_t aprs_pathing_path(const aprs_pathing_t *p);
void aprs_pathing_sent(aprs_pathing_t *p, const aprs_cfg_t *c, uint8_t why);
#endif
#endif
