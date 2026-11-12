// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include "config.h"
#if CONFIG_APRS_RX
#include <string.h>
#include "ui_nav.h"
#include "ui_svc_aprs.h"
#include "aprs/aprs_fmt.h"
#include "fmt.h"

enum { SORT_RECENT, SORT_NEAREST, SORT_CALL, SORT_COUNT };
enum { FILTER_ALL, FILTER_WEATHER, FILTER_MOBILE, FILTER_OBJECT, FILTER_DIGI, FILTER_COUNT };
static const char *const sorts[] = { TRN("Recent"), TRN("Nearest"), TRN("Callsign") };
static const char *const filters[] = { TRN("All"), TRN("Weather"), TRN("Mobiles"), TRN("Objects"), TRN("Digipeaters") };
static const char *const titles[] = { TRN("All"), TRN("Weather only"), TRN("Mobiles only"),
	TRN("Objects only"), TRN("Digipeaters only") };

// Main RAM: only identity and ordering keys, never a second station table.
static struct list_row {
	char call[AX25_CALL_LEN + 4];
	uint32_t age, range;
} rows[APRS_STATIONS];
static uint8_t shown, total, sort, filter;
static bool chosen;

void ui_aprs_list_reset(void)
{
	chosen = false;
	filter = FILTER_ALL;
	shown = 0;
}

static bool matches(const aprs_station_t *s)
{
	switch (filter) {
	case FILTER_WEATHER: return s->type == APRS_T_WEATHER || s->sym_code == '_';
	case FILTER_MOBILE: return s->has_pos && s->speed_kn != 0xFFFFu && s->speed_kn > 0;
	case FILTER_OBJECT: return s->type == APRS_T_OBJECT || s->type == APRS_T_ITEM;
	case FILTER_DIGI: return s->sym_code == '#' && s->sym_table != 0;
	default: return true;
	}
}

static bool before(const struct list_row *a, const struct list_row *b)
{
	if (sort == SORT_NEAREST && a->range != b->range)
		return a->range < b->range;
	if (sort != SORT_CALL && a->age != b->age)
		return a->age < b->age;
	return strcmp(a->call, b->call) < 0;
}

static uint16_t heard_count(void)
{
	ui_frame_t *f = ui_frame();
	char selected[AX25_CALL_LEN + 4] = "";
	if (f->focus > 0 && f->focus <= shown)
		str_copy(selected, sizeof selected, rows[f->focus - 1u].call);
	shown = 0;
	total = ui_svc_aprs_station_count();
	for (uint8_t i = 0; i < total && i < APRS_STATIONS; i++) {
		aprs_station_t s;
		if (!ui_svc_aprs_station(i, &s) || !matches(&s))
			continue;
		struct list_row row;
		str_copy(row.call, sizeof row.call, s.call);
		row.age = ui_st.now_ms - s.heard_ms;
		uint16_t deg;
		if (!ui_svc_aprs_range(&s, &row.range, &deg))
			row.range = UINT32_MAX;
		uint8_t j = shown++;
		while (j > 0 && before(&row, &rows[j - 1u])) {
			rows[j] = rows[j - 1u];
			j--;
		}
		rows[j] = row;
	}
	for (uint8_t i = 0; i < shown; i++) {
		if (strcmp(rows[i].call, selected) == 0) {
			f->focus = i + 1u;
			return shown + 1u;
		}
	}
	f->focus = shown ? 1 : 0;
	return shown + 1u;
}

static void heard_enter(void)
{
	aprs_view_t v;
	if (!chosen)
		sort = ui_svc_aprs_view(&v) && v.pos_valid ? SORT_NEAREST : SORT_RECENT;
	shown = 0;
}

static void heard_item(uint16_t i, ui_item_t *it)
{
	sbuf_t b;
	sb_init(&b, it->label, sizeof it->label);
	if (i == 0) {
		it->flags = UI_ITEM_INFO | UI_ITEM_DIM;
		sb_str(&b, tr(sorts[sort]));
		sb_str(&b, "  ");
		sb_u32(&b, total - shown, 1);
		sb_str(&b, TR(" hidden"));
		return;
	}
	aprs_station_t s;
	if (i > shown || !ui_svc_aprs_station_find(rows[i - 1u].call, &s))
		return;
	uint32_t m, at = s.heard_ms;
	uint16_t deg;
	sb_str(&b, s.call);
	if (ui_svc_aprs_range(&s, &m, &deg)) {
		sb_str(&b, TEXT_ALT_OPEN " ");
		sb_range(&b, m, deg);
		sb_str(&b, TEXT_ALT_NEXT TEXT_ALT_CLOSE);
		at = s.pos_ms;
	}
	ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - at);
	if (!s.direct)
		it->flags |= UI_ITEM_TEMP;
}

static const char *heard_title(void)
{
	static char text[80];
	(void)heard_count();
	sbuf_t b;
	sb_init(&b, text, sizeof text);
	sb_str(&b, tr(titles[filter]));
	sb_str(&b, ", ");
	sb_u32(&b, shown, 1);
	sb_str(&b, TR(" of "));
	sb_u32(&b, total, 1);
	return text;
}

static void heard_select(uint16_t i, bool hold)
{
	if (hold) {
		ui_push(SCR_APRS_FILTER, 0);
	} else if (i > 0 && i <= shown) {
		ui_aprs_station_open(rows[i - 1u].call);
	}
}

static bool heard_key(const input_event_t *ev)
{
	if (ev->gesture != GESTURE_PRESS)
		return false;
	if (ev->action == ACT_QUICK) {
		chosen = true;
		sort = (sort + 1u) % SORT_COUNT;
		(void)heard_count();
		ui_toast(tr(sorts[sort]));
		ui_dirty();
		return true;
	}
	if (ev->action == ACT_CONTEXT) {
		ui_push(SCR_APRS_FILTER, 0);
		return true;
	}
	return false;
}

static uint16_t filter_count(void) { return FILTER_COUNT; }
static void filter_item(uint16_t i, ui_item_t *it)
{
	str_copy(it->label, sizeof it->label, tr(filters[i]));
	it->num = '1' + i;
}
static void filter_select(uint16_t i, bool hold)
{
	(void)hold;
	filter = i;
	ui_pop();
	ui_dirty();
}

const ui_scr_def_t ui_aprs_heard_screen = {
	.name = "aprs-heard", .flags = SF_LIST | SF_FIELD, .prefix = "", .title = heard_title,
	.hint = TRN("* sort  # filter  OK open  Back"),
	.count = heard_count, .item = heard_item, .select = heard_select, .enter = heard_enter, .key = heard_key,
};
const ui_scr_def_t ui_aprs_filter_screen = {
	.name = "aprs-filter", .flags = SF_LIST | SF_NUMBERED | SF_FIELD, .prefix = TRN("Filter"),
	.hint = TRN("OK choose  Back"), .count = filter_count, .item = filter_item, .select = filter_select,
};
#endif
