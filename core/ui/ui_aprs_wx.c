// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include <string.h>
#include "ui_nav.h"
#include "ui_svc_aprs.h"
#include "fmt.h"
#include "i18n.h"

#if CONFIG_APRS_WX
__attribute__((weak)) uint8_t ui_svc_aprs_wx_count(void) { return 0; }
__attribute__((weak)) bool ui_svc_aprs_wx_station(uint8_t i, aprs_wx_station_t *out)
{
	(void)i; (void)out; return false;
}

// Freeze row identities while browsing; incoming reports may reorder the engine's table.
static char calls[APRS_WX_STATIONS][AX25_CALL_LEN + 4];
static char selected[AX25_CALL_LEN + 4];
static uint8_t count;
static bool nearest;

static bool find(const char *call, aprs_wx_station_t *s)
{
	for (uint8_t i = 0; i < ui_svc_aprs_wx_count(); i++) {
		if (ui_svc_aprs_wx_station(i, s) && strcmp(s->call, call) == 0) return true;
	}
	return false;
}

static uint32_t distance(const aprs_wx_station_t *s)
{
	aprs_station_t station = { .has_pos = s->has_pos, .lat = s->lat, .lon = s->lon };
	uint32_t m;
	uint16_t deg;
	return ui_svc_aprs_range(&station, &m, &deg) ? m : UINT32_MAX;
}

static void refresh(void)
{
	uint32_t ranges[APRS_WX_STATIONS];
	count = 0;
	for (uint8_t i = 0; i < ui_svc_aprs_wx_count() && count < APRS_WX_STATIONS; i++) {
		aprs_wx_station_t s;
		if (!ui_svc_aprs_wx_station(i, &s)) continue;
		uint32_t m = nearest ? distance(&s) : 0;
		uint8_t j = count++;
		while (j > 0 && ranges[j - 1] > m) {
			ranges[j] = ranges[j - 1];
			memcpy(calls[j], calls[j - 1], sizeof calls[j]);
			j--;
		}
		ranges[j] = m;
		str_copy(calls[j], sizeof calls[j], s.call);
	}
}

static void enter(void) { nearest = false; refresh(); }
static uint16_t list_count(void) { return count + 1u; }
static const char *body(void) { return count == 0 ? TR("No weather reports") : NULL; }
static void list_item(uint16_t i, ui_item_t *it)
{
	if (i == 0) {
		str_copy(it->label, sizeof it->label, TR("Sort"));
		str_copy(it->value, sizeof it->value, nearest ? TR("Nearest") : TR("Newest"));
		return;
	}
	if (i > count) return;
	str_copy(it->label, sizeof it->label, calls[i - 1]);
	aprs_wx_station_t s;
	if (find(calls[i - 1], &s)) ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - s.heard_ms);
}
static void list_select(uint16_t i, bool hold)
{
	(void)hold;
	if (i == 0) {
		nearest = !nearest;
		refresh();
		ui_dirty();
	} else if (i <= count) {
		str_copy(selected, sizeof selected, calls[i - 1]);
		ui_push(SCR_APRS_WEATHER_DETAIL, 0);
	}
}

static uint32_t measurement(const aprs_wx_t *w, unsigned row)
{
	switch (row) {
	case 0: return w->temp_f == APRS_WX_TEMP_UNKNOWN ? UINT32_MAX : 0;
	case 1: return w->wind_mph == APRS_WX_UNKNOWN ? UINT32_MAX : w->wind_mph;
	case 2: return w->wind_dir == APRS_WX_UNKNOWN ? UINT32_MAX : w->wind_dir;
	case 3: return w->gust_mph == APRS_WX_UNKNOWN ? UINT32_MAX : w->gust_mph;
	case 4: return 0;
	case 5: return w->rain_hour == APRS_WX_UNKNOWN ? UINT32_MAX : w->rain_hour;
	case 6: return w->rain_day == APRS_WX_UNKNOWN ? UINT32_MAX : w->rain_day;
	case 7: return w->rain_midnight == APRS_WX_UNKNOWN ? UINT32_MAX : w->rain_midnight;
	case 8: return w->humidity == APRS_WX_UNKNOWN ? UINT32_MAX : w->humidity;
	case 9: return w->pressure;
	case 10: return w->luminosity == APRS_WX_UNKNOWN ? UINT32_MAX : w->luminosity;
	default: return UINT32_MAX;
	}
}

static uint16_t detail_count(void)
{
	aprs_wx_station_t s;
	if (!find(selected, &s)) return 1;
	uint16_t n = 0;
	for (unsigned row = 0; row < 11; row++)
		if (measurement(&s.wx, row) != UINT32_MAX) n++;
	return n;
}
static const char *title(void) { return selected; }
static void detail_item(uint16_t i, ui_item_t *it)
{
	static const char *const names[] = {
		TRN("Temperature"), TRN("Wind"), TRN("Wind from"), TRN("Gust"), TRN("Report age"),
		TRN("Rain last hour"), TRN("Rain last 24h"), TRN("Rain since midnight"),
		TRN("Humidity"), TRN("Pressure"), TRN("Luminosity"),
	};
	if (i >= 11) return;
	aprs_wx_station_t s;
	if (!find(selected, &s)) {
		str_copy(it->value, sizeof it->value, TR("Unknown"));
		return;
	}
	const aprs_wx_t *w = &s.wx;
	unsigned row;
	uint32_t v = UINT32_MAX;
	for (row = 0; row < 11; row++) {
		v = measurement(w, row);
		if (v != UINT32_MAX && i-- == 0) break;
	}
	if (row == 11) return;
	i = row;
	str_copy(it->label, sizeof it->label, tr(names[i]));
	if (i == 4) { ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - s.heard_ms); return; }
	sbuf_t b;
	sb_init(&b, it->value, sizeof it->value);
	switch (i) {
	case 0: sb_temperature(&b, (int16_t)((w->temp_f - 32) * 50 / 9)); break;
	case 1: case 3: sb_speed(&b, (v * 1609344u + 500u) / 1000u); break;
	case 2: sb_compass(&b, (uint16_t)v); break;
	case 5: case 6: case 7: sb_rain(&b, v); break;
	case 8: sb_u32(&b, v, 1); sb_chr(&b, '%'); break;
	case 9: sb_pressure(&b, v); break;
	case 10: sb_u32(&b, v, 1); sb_str(&b, " W/m2"); break;
	}
}

const ui_scr_def_t ui_aprs_weather_screen = {
	.name = "aprs-weather", .flags = SF_LIST | SF_FIELD, .prefix = TRN("Weather"),
	.hint = TRN("OK choose  Back"), .enter = enter,
	.count = list_count, .item = list_item, .select = list_select, .body = body,
};
const ui_scr_def_t ui_aprs_weather_detail_screen = {
	.name = "aprs-weather-detail", .flags = SF_LIST | SF_FIELD, .prefix = TRN("Weather"),
	.hint = TRN("Up/Down scroll  Back"), .title = title, .count = detail_count, .item = detail_item,
};
#endif
