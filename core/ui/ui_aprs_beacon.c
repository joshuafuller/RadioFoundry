// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// My beacon: edit the existing APRS settings without arming transmit as a side effect.
#include "config.h"
#if CONFIG_APRS_BEACON
#include <string.h>
#include "ui_nav.h"
#include "ui_svc_aprs.h"
#include "fmt.h"

// Table/code assignments: aprs.org/symbols/symbolsX.txt. Names only, no bitmap assets.
static const struct {
	char table, code;
	const char *name;
} symbols[] = {
	{ '/', '[', TRN("On foot") },
	{ '/', '>', TRN("Car") },
	{ '/', 'b', TRN("Bicycle") },
	{ '/', 'k', TRN("Truck") },
	{ '/', '-', TRN("Home") },
	{ '/', 's', TRN("Boat") },
	{ '/', ';', TRN("Camp") },
	{ '/', 'E', TRN("Event") },
	{ '/', '<', TRN("Motorcycle") },
	{ '/', 'j', TRN("Jeep") },
	{ '\\', 'k', TRN("Sport utility vehicle") },
	{ '/', 'v', TRN("Van") },
	{ '/', 'R', TRN("Recreational vehicle") },
	{ '/', 'U', TRN("Bus") },
	{ '/', 'u', TRN("Large truck") },
	{ '/', 'F', TRN("Tractor") },
	{ '/', '=', TRN("Train") },
	{ '/', 'C', TRN("Canoe") },
	{ '/', 'Y', TRN("Sailboat") },
	{ '/', '\'', TRN("Small aircraft") },
	{ '/', '^', TRN("Large aircraft") },
	{ '/', 'X', TRN("Helicopter") },
	{ '/', 'g', TRN("Glider") },
	{ '/', 'O', TRN("Balloon") },
	{ '/', 'e', TRN("Horse") },
	{ '/', '*', TRN("Snowmobile") },
	{ '/', ')', TRN("Wheelchair") },
	{ '/', '_', TRN("Weather station") },
	{ '/', '#', TRN("Digipeater") },
	{ '/', 'r', TRN("Repeater") },
	{ '/', 'a', TRN("Ambulance") },
	{ '/', 'f', TRN("Fire truck") },
	{ '/', 'd', TRN("Fire station") },
	{ '/', 'P', TRN("Police") },
	{ '/', 'A', TRN("Aid station") },
	{ '/', 'h', TRN("Hospital") },
	{ '/', 'c', TRN("Command post") },
	{ '/', 'o', TRN("Emergency center") },
	{ '\\', 'z', TRN("Shelter") },
	{ '/', 'l', TRN("Laptop") },
};
#define N_SYMBOLS (sizeof(symbols) / sizeof(symbols[0]))

static const char *symbol_name(const aprs_cfg_t *c)
{
	for (unsigned i = 0; i < N_SYMBOLS; i++) {
		if (symbols[i].table == c->sym_table && symbols[i].code == c->sym_code) {
			return tr(symbols[i].name);
		}
	}
	return TR("Custom");
}

static const char *const reach_names[APRS_PATH_COUNT] = {
	TRN("Direct"), TRN("Local relay"), TRN("Local then wide"), TRN("Wide one hop"), TRN("Wide two hops"), TRN("Proportional"),
};
static const uint16_t pace_seconds[] = { 0, 60, 120, 300, 600, 1800 };
static const char *const pace_names[] = {
	TRN("Smart"), TRN("1 min"), TRN("2 min"), TRN("5 min"), TRN("10 min"), TRN("30 min"),
};
static char comment[APRS_COMMENT_MAX + 1];

static const char *beacon_body(void)
{
	char *out = ui_aprs_text;
	aprs_cfg_t c;
	ui_svc_aprs_cfg(&c);
	ui_svc_aprs_comment(comment);
	sbuf_t b;
	ui_aprs_beacon_status();
	size_t used = strlen(out);
	sb_init(&b, out + used, sizeof ui_aprs_text - used);
	sb_str(&b, " / ");
	sb_str(&b, symbol_name(&c));
	if (comment[0]) {
		sb_str(&b, " / ");
		sb_str(&b, comment);
	}
	return out;
}

static uint16_t beacon_count(void) { return 5; }
static void beacon_item(uint16_t i, ui_item_t *it)
{
	static const char *const names[] = { TRN("Symbol"), TRN("Comment"), TRN("Beacons"), TRN("Pace"), TRN("Reach") };
	if (i >= beacon_count()) {
		return;
	}
	aprs_cfg_t c;
	ui_svc_aprs_cfg(&c);
	it->num = (char)('1' + i);
	str_copy(it->label, sizeof it->label, tr(names[i]));
	const char *value = "";
	switch (i) {
	case 0: value = symbol_name(&c); break;
	case 1:
		ui_svc_aprs_comment(comment);
		value = comment[0] ? comment : TR("None");
		break;
	case 2: value = c.tx_on ? TR("Automatic") : TR("Off"); break;
	case 3:
		if (c.smart) {
			value = TR("Smart");
		} else {
			sbuf_t b;
			sb_init(&b, it->value, sizeof it->value);
			sb_u32(&b, c.interval_s, 1);
			sb_str(&b, " s");
			return;
		}
		break;
	case 4: value = tr(reach_names[c.path < APRS_PATH_COUNT ? c.path : 0]); break;
	}
	str_copy(it->value, sizeof it->value, value);
}

static void comment_kept(void)
{
	if (!ui_svc_aprs_set_comment(comment)) {
		ui_toast(TR("Comment not saved"));
	}
}

static void beacon_select(uint16_t i, bool hold)
{
	(void)hold;
	aprs_cfg_t c;
	ui_svc_aprs_cfg(&c);
	switch (i) {
	case 0: ui_push(SCR_APRS_SYMBOL, 0); return;
	case 1:
		ui_svc_aprs_comment(comment);
		ui_aprs_comment_edit(comment, comment_kept);
		return;
	case 2: c.tx_on = !c.tx_on; break;
	case 3: ui_push(SCR_APRS_PACE, 0); return;
	case 4: ui_push(SCR_APRS_REACH, 0); return;
	default: return;
	}
	ui_svc_aprs_set_cfg(&c);
	ui_dirty();
}

const ui_scr_def_t ui_aprs_beacon_screen = {
	.name = "aprs-beacon", .flags = SF_LIST | SF_NUMBERED, .prefix = TRN("My beacon"),
	.hint = TRN("OK change  Back"), .body = beacon_body,
	.count = beacon_count, .item = beacon_item, .select = beacon_select,
};

static uint16_t symbol_count(void) { return N_SYMBOLS; }
static void symbol_item(uint16_t i, ui_item_t *it)
{
	if (i < N_SYMBOLS) {
		str_copy(it->label, sizeof it->label, tr(symbols[i].name));
	}
}
static void symbol_enter(void)
{
	aprs_cfg_t c;
	ui_svc_aprs_cfg(&c);
	for (unsigned i = 0; i < N_SYMBOLS; i++) {
		if (symbols[i].table == c.sym_table && symbols[i].code == c.sym_code) {
			ui_frame()->focus = (uint16_t)i;
			break;
		}
	}
}
static void symbol_select(uint16_t i, bool hold)
{
	(void)hold;
	if (i >= N_SYMBOLS) {
		return;
	}
	aprs_cfg_t c;
	ui_svc_aprs_cfg(&c);
	c.sym_table = symbols[i].table;
	c.sym_code = symbols[i].code;
	ui_svc_aprs_set_cfg(&c);
	ui_pop();
}
const ui_scr_def_t ui_aprs_symbol_screen = {
	.name = "aprs-symbol", .flags = SF_LIST, .prefix = TRN("Symbol"), .hint = TRN("OK choose  Back"),
	.count = symbol_count, .item = symbol_item, .select = symbol_select, .enter = symbol_enter,
};

static uint16_t pace_count(void) { return sizeof(pace_seconds) / sizeof(pace_seconds[0]); }
static void pace_item(uint16_t i, ui_item_t *it)
{
	if (i < pace_count()) {
		it->num = (char)('1' + i);
		str_copy(it->label, sizeof it->label, tr(pace_names[i]));
	}
}
static void pace_select(uint16_t i, bool hold)
{
	(void)hold;
	if (i >= pace_count()) {
		return;
	}
	aprs_cfg_t c;
	ui_svc_aprs_cfg(&c);
	c.smart = i == 0;
	if (i != 0) {
		c.interval_s = pace_seconds[i];
	}
	ui_svc_aprs_set_cfg(&c);
	ui_pop();
}
const ui_scr_def_t ui_aprs_pace_screen = {
	.name = "aprs-pace", .flags = SF_LIST | SF_NUMBERED, .prefix = TRN("Pace"), .hint = TRN("OK choose  Back"),
	.count = pace_count, .item = pace_item, .select = pace_select,
};

static uint16_t reach_count(void) { return APRS_PATH_COUNT; }
static void reach_item(uint16_t i, ui_item_t *it)
{
	if (i < APRS_PATH_COUNT) {
		it->num = (char)('1' + i);
		str_copy(it->label, sizeof it->label, tr(reach_names[i]));
	}
}
static void reach_select(uint16_t i, bool hold)
{
	(void)hold;
	if (i >= APRS_PATH_COUNT) {
		return;
	}
	aprs_cfg_t c;
	ui_svc_aprs_cfg(&c);
	c.path = (uint8_t)i;
	ui_svc_aprs_set_cfg(&c);
	ui_pop();
}
const ui_scr_def_t ui_aprs_reach_screen = {
	.name = "aprs-reach", .flags = SF_LIST | SF_NUMBERED, .prefix = TRN("Reach"), .hint = TRN("OK choose  Back"),
	.count = reach_count, .item = reach_item, .select = reach_select,
};
#endif
