// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include "aprs_objects.h"
#if CONFIG_APRS_RX
#include <string.h>
#include "aprs_int.h"

// Ordinary .bss stays in main RAM; the engine's CCM state must not grow.
static aprs_object_t objects[APRS_OBJECTS];
static aprs_bulletin_t bulletins[APRS_BULLETINS];
static uint8_t nobjects, nbulletins;

void aprs_objects_init(void)
{
	nobjects = nbulletins = 0;
}

static void text_copy(char *dst, size_t cap, const char *src, size_t len)
{
	size_t n = len < cap - 1u ? len : cap - 1u;
	for (size_t i = 0; i < n; i++) {
		dst[i] = src[i] >= 32 && src[i] <= 126 ? src[i] : ' ';
	}
	dst[n] = '\0';
}

// APRS frequency text is ddd.dddMHz, not an arbitrary number in a comment.
static uint32_t frequency(const char *s)
{
	for (size_t i = 0, n = strlen(s); i + 10u <= n; i++) {
		if ((i && s[i - 1] != ' ') || s[i + 3] != '.' || memcmp(s + i + 7, "MHz", 3) != 0 ||
		    (i + 10u < n && s[i + 10] != ' ')) {
			continue;
		}
		uint32_t khz = 0;
		bool valid = true;
		for (size_t k = 0; k < 7; k++) {
			if (k == 3) continue;
			if (s[i + k] < '0' || s[i + k] > '9') { valid = false; break; }
			khz = khz * 10u + (uint32_t)(s[i + k] - '0');
		}
		if (valid && khz) return khz * 1000u;
	}
	return 0;
}

bool aprs_is_bulletin(const aprs_parsed_t *p)
{
	if (p->type != APRS_T_MESSAGE) return false;
	const char *s = p->addressee;
	return (strncmp(s, "NWS-", 4) == 0 && s[4]) ||
	       (strncmp(s, "BLN", 3) == 0 && ((s[3] >= '0' && s[3] <= '9') || (s[3] >= 'A' && s[3] <= 'Z')));
}

void aprs_objects_tick(uint32_t now_ms)
{
	for (uint8_t i = 0; i < nobjects;) {
		if (now_ms - objects[i].ms >= (objects[i].killed ? APRS_REMOVED_MS : APRS_RETAIN_MS)) {
			--nobjects;
			memmove(objects + i, objects + i + 1, (nobjects - i) * sizeof(*objects));
			aprs_changed();
		} else i++;
	}
	for (uint8_t i = 0; i < nbulletins;) {
		if (now_ms - bulletins[i].ms >= APRS_RETAIN_MS) {
			--nbulletins;
			memmove(bulletins + i, bulletins + i + 1, (nbulletins - i) * sizeof(*bulletins));
			aprs_changed();
		} else i++;
	}
}

void aprs_objects_receive(const ax25_frame_t *f, const aprs_parsed_t *p, uint32_t now_ms)
{
	// A gateway is the transport, not the publisher. Unwrap one third-party envelope.
	ax25_frame_t inner;
	aprs_parsed_t parsed;
	if (p->type == APRS_T_THIRD_PARTY) {
		if (f->info_len < 2 || !ax25_from_tnc2((const char *)f->info + 1, f->info_len - 1u, &inner)) return;
		aprs_parse(&inner.dst, inner.info, inner.info_len, &parsed);
		f = &inner;
		p = &parsed;
	}
	char from[AX25_CALL_LEN + 4];
	ax25_addr_format(&f->src, false, from);
	if ((p->type == APRS_T_OBJECT || p->type == APRS_T_ITEM) && p->has_pos && p->name[0]) {
		uint8_t i;
		for (i = 0; i < nobjects; i++) {
			if (!strcmp(objects[i].name, p->name)) break;
		}
		if (i == nobjects) {
			if (nobjects < APRS_OBJECTS) nobjects++;
			i = nobjects - 1u;
		}
		memmove(objects + 1, objects, i * sizeof(*objects));
		aprs_object_t *o = objects;
		memset(o, 0, sizeof(*o));
		text_copy(o->name, sizeof(o->name), p->name, strlen(p->name));
		strcpy(o->owner, from);
		o->pos = p->pos;
		o->ms = now_ms;
		o->killed = p->killed;
		text_copy(o->comment, sizeof(o->comment), p->text, p->text_len);
		o->frequency_hz = frequency(o->comment);
#if CONFIG_APRS_TUNE
		aprs_freq_parse(p->text, p->text_len, o->name, &o->frequency);
#endif
	} else if (aprs_is_bulletin(p)) {
		aprs_bulletin_t b = {0};
		strcpy(b.group, p->addressee);
		strcpy(b.from, from);
		// "acknowledge" and "rejoin" are ordinary bulletin text, never control messages.
		text_copy(b.text, sizeof(b.text), p->text, p->ack || p->rej ? f->info_len - 11u : p->text_len);
		uint8_t i;
		for (i = 0; i < nbulletins; i++) {
			if (!strcmp(bulletins[i].group, b.group) && !strcmp(bulletins[i].from, from)) {
				if (!strcmp(bulletins[i].text, b.text)) return; // repeated bulletin: no second alert
				break;
			}
		}
		if (i == nbulletins) {
			if (nbulletins < APRS_BULLETINS) nbulletins++;
			i = nbulletins - 1u;
		}
		memmove(bulletins + 1, bulletins, i * sizeof(*bulletins));
		b.ms = now_ms;
		b.pending = true;
		bulletins[0] = b;
	}
}

uint8_t aprs_object_count(void) { LOCK(); uint8_t n = nobjects; UNLOCK(); return n; }
uint8_t aprs_bulletin_count(void) { LOCK(); uint8_t n = nbulletins; UNLOCK(); return n; }
bool aprs_object(uint8_t i, aprs_object_t *out)
{
	LOCK();
	bool ok = i < nobjects;
	if (ok) *out = objects[i];
	UNLOCK();
	return ok;
}
bool aprs_bulletin(uint8_t i, aprs_bulletin_t *out)
{
	LOCK();
	bool ok = i < nbulletins;
	if (ok) *out = bulletins[i];
	UNLOCK();
	return ok;
}
bool aprs_bulletin_alert(aprs_bulletin_t *out)
{
	LOCK();
	bool ok = false;
	for (uint8_t i = 0; i < nbulletins; i++) {
		if (bulletins[i].pending) {
			*out = bulletins[i];
			bulletins[i].pending = false;
			ok = true;
			break;
		}
	}
	UNLOCK();
	return ok;
}
#endif
