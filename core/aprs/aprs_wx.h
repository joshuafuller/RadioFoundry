// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#ifndef RFOUNDRY_APRS_WX_H
#define RFOUNDRY_APRS_WX_H
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include "config.h"

#if CONFIG_APRS_WX
#define APRS_WX_UNKNOWN UINT16_MAX
#define APRS_WX_TEMP_UNKNOWN INT16_MIN
// APRS101 chapter 12 wire units; unknown is distinct from a measured zero.
typedef struct {
	uint16_t wind_dir, wind_mph, gust_mph;
	int16_t temp_f;
	uint16_t rain_hour, rain_day, rain_midnight; // hundredths of an inch
	uint16_t humidity; // percent
	uint32_t pressure; // tenths of hPa; UINT32_MAX unknown
	uint16_t luminosity; // W/m2
} aprs_wx_t;
void aprs_wx_parse(const char *s, size_t n, bool positioned, aprs_wx_t *out);
#endif
#endif
