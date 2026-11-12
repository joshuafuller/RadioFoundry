// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include "aprs_wx.h"

#if CONFIG_APRS_WX
static uint32_t number(const char *s, size_t n)
{
	uint32_t v = 0;
	for (size_t i = 0; i < n; i++) {
		if (s[i] < '0' || s[i] > '9') {
			return UINT32_MAX;
		}
		v = v * 10u + (unsigned)(s[i] - '0');
	}
	return v;
}

void aprs_wx_parse(const char *s, size_t n, bool positioned, aprs_wx_t *w)
{
	*w = (aprs_wx_t) {
		.wind_dir = APRS_WX_UNKNOWN, .wind_mph = APRS_WX_UNKNOWN, .gust_mph = APRS_WX_UNKNOWN,
		.temp_f = APRS_WX_TEMP_UNKNOWN, .rain_hour = APRS_WX_UNKNOWN, .rain_day = APRS_WX_UNKNOWN,
		.rain_midnight = APRS_WX_UNKNOWN, .humidity = APRS_WX_UNKNOWN, .pressure = UINT32_MAX,
		.luminosity = APRS_WX_UNKNOWN,
	};
	if (positioned && n >= 7 && s[3] == '/') {
		w->wind_dir = number(s, 3);
		w->wind_mph = number(s + 4, 3);
		s += 7;
		n -= 7;
	}
	while (n > 0) {
		char tag = *s++;
		n--;
		size_t width = tag == 'h' ? 2 : tag == 'b' ? 5 : 3;
		uint16_t *field;
		switch (tag) {
		case 'c': field = &w->wind_dir; break;
		case 's': field = positioned ? NULL : &w->wind_mph; break;
		case '#': field = NULL; break;
		case 'g': field = &w->gust_mph; break;
		case 't': field = NULL; break;
		case 'r': field = &w->rain_hour; break;
		case 'p': field = &w->rain_day; break;
		case 'P': field = &w->rain_midnight; break;
		case 'h': field = &w->humidity; break;
		case 'b': field = NULL; break;
		case 'L': case 'l': field = &w->luminosity; break;
		default: goto done; // The software/unit suffix is not weather data.
		}
		if (n < width) {
			break;
		}
		uint32_t v = number(s, width);
		if (tag == 't') {
			if (s[0] == '-') {
				v = number(s + 1, 2);
				w->temp_f = v == UINT32_MAX ? APRS_WX_TEMP_UNKNOWN : -(int16_t)v;
			} else {
				w->temp_f = v == UINT32_MAX ? APRS_WX_TEMP_UNKNOWN : (int16_t)v;
			}
		} else if (tag == 'b') {
			w->pressure = v;
		} else if (field != NULL) {
			if (tag == 'h' && v == 0) v = 100;
			if (tag == 'l' && v != UINT32_MAX) v += 1000;
			*field = v;
		}
		s += width;
		n -= width;
	}
done:
	if (w->wind_dir > 360) w->wind_dir = APRS_WX_UNKNOWN;
}
#endif
