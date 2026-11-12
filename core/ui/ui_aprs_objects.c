// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include "config.h"
#if CONFIG_APRS_RX
#include <string.h>
#include "ui_nav.h"
#include "ui_svc_aprs.h"
#include "fmt.h"
#include "i18n.h"

#define WEAK __attribute__((weak))
WEAK uint8_t ui_svc_aprs_object_count(void) { return 0; }
WEAK bool ui_svc_aprs_object(uint8_t i, aprs_object_t *o) { (void)i; (void)o; return false; }
WEAK uint8_t ui_svc_aprs_bulletin_count(void) { return 0; }
WEAK bool ui_svc_aprs_bulletin(uint8_t i, aprs_bulletin_t *b) { (void)i; (void)b; return false; }
WEAK bool ui_svc_aprs_bulletin_alert(aprs_bulletin_t *b) { (void)b; return false; }

static char object_name[APRS_NAME_MAX + 1], object_owner[AX25_CALL_LEN + 4];
static char bulletin_group[APRS_ADDRESSEE_LEN + 1], bulletin_from[AX25_CALL_LEN + 4];

static bool object_find(aprs_object_t *o)
{
	for (uint8_t i = 0; i < ui_svc_aprs_object_count(); i++) {
		if (ui_svc_aprs_object(i, o) && !strcmp(o->name, object_name) && !strcmp(o->owner, object_owner)) return true;
	}
	return false;
}
static bool bulletin_find(aprs_bulletin_t *b)
{
	for (uint8_t i = 0; i < ui_svc_aprs_bulletin_count(); i++) {
		if (ui_svc_aprs_bulletin(i, b) && !strcmp(b->group, bulletin_group) && !strcmp(b->from, bulletin_from)) return true;
	}
	return false;
}
// The temporary station is only for ranging, not for formatting the object row.
static __attribute__((noinline)) bool object_range(const aprs_object_t *o, uint32_t *m, uint16_t *deg)
{
	aprs_station_t s = { .has_pos = true, .lat = o->pos.lat, .lon = o->pos.lon };
	return ui_svc_aprs_range(&s, m, deg);
}
static uint16_t objects_count(void) { return ui_svc_aprs_object_count(); }
static void objects_item(uint16_t i, ui_item_t *it)
{
	aprs_object_t o;
	if (!ui_svc_aprs_object((uint8_t)i, &o)) return;
	str_copy(it->label, sizeof it->label, o.name);
	sbuf_t b;
	sb_init(&b, it->value, sizeof it->value);
	uint32_t m;
	uint16_t deg;
	if (o.killed) sb_str(&b, TR("Removed"));
	else if (object_range(&o, &m, &deg)) sb_range(&b, m, deg);
	else ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - o.ms);
}
static void objects_select(uint16_t i, bool hold)
{
	(void)hold;
	aprs_object_t o;
	if (!ui_svc_aprs_object((uint8_t)i, &o)) return;
	str_copy(object_name, sizeof object_name, o.name);
	str_copy(object_owner, sizeof object_owner, o.owner);
	ui_push(SCR_APRS_OBJECT, 0);
}
const ui_scr_def_t ui_aprs_objects_screen = {
	.name = "aprs-objects", .flags = SF_LIST | SF_FIELD, .prefix = TRN("Objects"),
	.hint = TRN("OK choose  Back"), .empty = TRN("No objects heard"),
	.count = objects_count, .item = objects_item, .select = objects_select,
};

// Short text rows keep all retained text reachable on the narrowest display.
#define TEXT_ROW 16u
static void text_row(ui_item_t *it, const char *text, uint16_t row)
{
	size_t offset = row * TEXT_ROW;
	if (offset < strlen(text)) str_copy(it->label, TEXT_ROW + 1u, text + offset);
}
static uint16_t object_count(void)
{
	aprs_object_t o;
	return object_find(&o) ? 5u + (strlen(o.comment) + TEXT_ROW - 1u) / TEXT_ROW : 1;
}
static void object_item(uint16_t i, ui_item_t *it)
{
	aprs_object_t o;
	if (!object_find(&o)) {
		str_copy(it->label, sizeof it->label, TR("No longer in list"));
		return;
	}
	sbuf_t b;
	sb_init(&b, it->value, sizeof it->value);
	const char *label = "";
	uint32_t m;
	uint16_t deg;
	switch (i) {
	case 0: label = TR("From "); sb_str(&b, o.owner); break;
	case 1: label = o.killed ? TR("Removed") : TR("Live"); ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - o.ms); break;
	case 2:
		label = TR("Distance");
		if (object_range(&o, &m, &deg)) sb_range(&b, m, deg);
		else sb_str(&b, TR("No position"));
		break;
	case 3:
		label = TR("Frequency");
		if (o.frequency_hz) sb_freq(&b, o.frequency_hz);
		else sb_str(&b, TR("Unknown"));
		break;
	case 4: label = TR("Comment"); break;
	default: text_row(it, o.comment, i - 5u); return;
	}
	str_copy(it->label, sizeof it->label, label);
}
static const char *object_title(void) { return object_name; }
const ui_scr_def_t ui_aprs_object_screen = {
	.name = "aprs-object", .flags = SF_LIST | SF_FIELD, .prefix = TRN("Object"), .hint = TRN("Back"),
	.title = object_title, .count = object_count, .item = object_item,
};

static const char *group_name(const char *group)
{
	if (group[4]) return group + 4;
	return group[3] >= 'A' && group[3] <= 'Z' ? TR("Announcement") : TR("Bulletin");
}

static uint16_t bulletins_count(void) { return ui_svc_aprs_bulletin_count(); }
static void bulletins_item(uint16_t i, ui_item_t *it)
{
	aprs_bulletin_t b;
	if (!ui_svc_aprs_bulletin((uint8_t)i, &b)) return;
	sbuf_t s;
	sb_init(&s, it->label, sizeof it->label);
	sb_str(&s, group_name(b.group));
	sb_chr(&s, ' ');
	sb_str(&s, b.from);
	ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - b.ms);
}
static void bulletins_select(uint16_t i, bool hold)
{
	(void)hold;
	aprs_bulletin_t b;
	if (!ui_svc_aprs_bulletin((uint8_t)i, &b)) return;
	str_copy(bulletin_group, sizeof bulletin_group, b.group);
	str_copy(bulletin_from, sizeof bulletin_from, b.from);
	ui_push(SCR_APRS_BULLETIN, 0);
}
const ui_scr_def_t ui_aprs_bulletins_screen = {
	.name = "aprs-bulletins", .flags = SF_LIST | SF_FIELD, .prefix = TRN("Bulletins"),
	.hint = TRN("OK choose  Back"), .empty = TRN("No bulletins heard"),
	.count = bulletins_count, .item = bulletins_item, .select = bulletins_select,
};
static uint16_t bulletin_count(void)
{
	aprs_bulletin_t b;
	return bulletin_find(&b) ? 2u + (strlen(b.text) + TEXT_ROW - 1u) / TEXT_ROW : 1;
}
static void bulletin_item(uint16_t i, ui_item_t *it)
{
	aprs_bulletin_t b;
	if (!bulletin_find(&b)) {
		str_copy(it->label, sizeof it->label, TR("No longer in list"));
		return;
	}
	if (i == 0) {
		str_copy(it->label, sizeof it->label, TR("From "));
		str_copy(it->value, sizeof it->value, b.from);
	} else if (i == 1) {
		str_copy(it->label, sizeof it->label, TR("Heard"));
		ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - b.ms);
	} else text_row(it, b.text, i - 2u);
}
static const char *bulletin_title(void) { return group_name(bulletin_group); }
const ui_scr_def_t ui_aprs_bulletin_screen = {
	.name = "aprs-bulletin", .flags = SF_LIST | SF_FIELD, .prefix = TRN("Bulletin"), .hint = TRN("Back"),
	.title = bulletin_title, .count = bulletin_count, .item = bulletin_item,
};
#endif
