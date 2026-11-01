// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Start storage and STORE checkpoints (storage_vhost.h; docs/spec/settings-store.md).
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "storage_vhost.h"
#include "vboard.h"
#include "vhost.h"
#include "vtrace.h"
#include "codeplug/codeplug.h"
#include "settings/settings.h"
#include "storage/storage.h"

// Test identities: 3100001 to 3100999. Group contacts use the fixture talkgroup only (storage_vhost.h).
#define FIXTURE_ID_FIRST 3100001u
#define FIXTURE_ID_LAST 3100999u
#define FIXTURE_TALKGROUP 9u

static const char *start_storage = "blank";
static uint32_t radio_index = 1;
static bool loaded;

static bool number(const char *s, uint32_t lo, uint32_t hi, uint32_t *out)
{
	uint32_t n = 0;
	if (*s == '\0')
		return false;
	for (; *s; s++) {
		if (*s < '0' || *s > '9' || n > (UINT32_MAX - (unsigned)(*s - '0')) / 10u)
			return false;
		n = n * 10u + (unsigned)(*s - '0');
	}
	*out = n;
	return n >= lo && n <= hi;
}

static bool name_ok(const char *s)
{
	size_t n = strlen(s);
	if (n == 0 || n > CODEPLUG_NAME_LEN)
		return false;
	for (; *s; s++)
		if (*s < ' ' || *s > '~')
			return false;
	return true;
}

static bool setting(const char *key, const char *value, bool apply)
{
	static const struct {
		const char *key;
		size_t offset, size;
		uint32_t lo, hi;
	} fields[] = {
#define FIELD(k, lo_, hi_) { #k, offsetof(settings_t, k), sizeof(((settings_t *)0)->k), lo_, hi_ }
		FIELD(vfo, 0, 1), FIELD(squelch, 0, 9), FIELD(zone_index, 0, CODEPLUG_ZONES_MAX - 1),
		FIELD(channel, 0, CODEPLUG_CHANNELS_MAX), FIELD(zone_pos, 0, CODEPLUG_ZONE_MAX - 1),
		FIELD(brightness, 10, 100), FIELD(backlight_s, 0, 255), FIELD(beeps, 0, 1),
#undef FIELD
	};
	for (unsigned i = 0; i < sizeof fields / sizeof fields[0]; i++) {
		if (strcmp(key, fields[i].key) != 0)
			continue;
		uint32_t v;
		if (!number(value, fields[i].lo, fields[i].hi, &v) &&
		    !(strcmp(key, "squelch") == 0 && number(value, 255, 255, &v)))
			return false;
		if (apply) {
			uint8_t *p = (uint8_t *)settings_begin() + fields[i].offset;
			if (fields[i].size == 1)
				*p = (uint8_t)v;
			else {
				uint16_t u = (uint16_t)v;
				memcpy(p, &u, sizeof u);
			}
			settings_end(0);
		}
		return true;
	}
	return false;
}

static bool edit(char *text, bool apply)
{
	char *args = strchr(text, '=');
	if (args == NULL)
		return false;
	*args++ = '\0';
	char *f[CODEPLUG_ZONE_MAX + 2];
	unsigned n = 0;
	while (args != NULL) {
		if (n == sizeof f / sizeof f[0])
			return false;
		f[n] = strsep(&args, ",");
		if (*f[n++] == '\0')
			return false;
	}
	uint32_t a, b, c, d, e, g;
	if (strcmp(text, "radio_name") == 0)
		return n == 1 && strcmp(f[0], "N0SIM") == 0 && (!apply || codeplug_set_radio_name(f[0]));
	if (strcmp(text, "setting") == 0)
		return n == 2 && setting(f[0], f[1], apply);
	if (strcmp(text, "zone") == 0) {
		if (n < 3 || !number(f[0], 0, CODEPLUG_ZONES_MAX - 1, &a) || !name_ok(f[1]))
			return false;
		zone_t z = { .count = (uint16_t)(n - 2) };
		strcpy(z.name, f[1]);
		for (unsigned i = 2; i < n; i++) {
			if (!number(f[i], 1, CODEPLUG_CHANNELS_MAX, &b))
				return false;
			z.channels[i - 2] = (uint16_t)b;
		}
		return !apply || codeplug_set_zone((uint16_t)a, &z);
	}
	if (strcmp(text, "channel") == 0) {
		if (n != 8 || !number(f[0], 1, CODEPLUG_CHANNELS_MAX, &a) || !name_ok(f[1]) ||
		    !number(f[2], 1, 999999999u, &b) || !number(f[3], 1, 999999999u, &c) ||
		    (strcmp(f[4], "fm") != 0 && strcmp(f[4], "dmr") != 0) ||
		    !number(f[5], 0, 15, &d) || !number(f[6], 1, 2, &e) || !number(f[7], 0, 1024, &g))
			return false;
#if !CONFIG_DMR
		if (strcmp(f[4], "dmr") == 0)
			return false;
#endif
		channel_t ch = { .rx_hz = b, .tx_hz = c, .mode = strcmp(f[4], "dmr") == 0 ? RADIO_MODE_DMR : RADIO_MODE_FM,
			.bw = RADIO_BW_12K5, .power = CODEPLUG_POWER_DEFAULT, .color_code = (uint8_t)d,
			.timeslot = (uint8_t)e, .contact = (uint16_t)g, .dmo = b == c };
		strcpy(ch.name, f[1]);
		return !apply || codeplug_set_channel((uint16_t)a, &ch);
	}
#if CONFIG_DMR
	if (strcmp(text, "dmr_id") == 0)
		return n == 1 && number(f[0], FIXTURE_ID_FIRST, FIXTURE_ID_LAST, &a) && (!apply || codeplug_set_dmr_id(a));
	if (strcmp(text, "contact") == 0) {
		if (n != 5 || (strcmp(f[3], "private") != 0 && strcmp(f[3], "group") != 0))
			return false;
		bool group = strcmp(f[3], "group") == 0;
		if (!number(f[0], 1, CODEPLUG_CONTACTS_MAX, &a) || !name_ok(f[1]) ||
		    !(group ? number(f[2], FIXTURE_TALKGROUP, FIXTURE_TALKGROUP, &b)
			    : number(f[2], FIXTURE_ID_FIRST, FIXTURE_ID_LAST, &b)) || !number(f[4], 0, 2, &c))
			return false;
		contact_t contact = { .id = b, .type = group ? CONTACT_GROUP : CONTACT_PRIVATE,
			.timeslot = (uint8_t)c };
		strcpy(contact.name, f[1]);
		return !apply || codeplug_set_contact((uint16_t)a, &contact);
	}
#endif
	return false;
}

static bool edits(const char *text, bool apply)
{
	char *copy = strdup(text);
	if (copy == NULL)
		return false;
	char *rest = copy;
	bool ok = true;
	while (ok && rest != NULL)
		ok = edit(strsep(&rest, ";"), apply);
	free(copy);
	return ok;
}

static bool set_storage(const char *value)
{
	bool ok = strcmp(value, "blank") == 0 || (strncmp(value, "steps:", 6) == 0 && edits(value + 6, false));
#if CONFIG_DMR
	ok = ok || strcmp(value, "dmr-pair") == 0 || strcmp(value, "dmr-group") == 0;
#endif
	if (ok)
		start_storage = value;
	return ok;
}
VHOST_OPTION("--storage", "FIXTURE|steps:EDITS", set_storage,
	     "start from blank, dmr-pair, dmr-group, or editor steps (storage_vhost.h)")

static bool set_index(const char *value)
{
	uint32_t n;
	if (!number(value, 1, FIXTURE_ID_LAST - FIXTURE_ID_FIRST + 1, &n))
		return false;
	radio_index = n;
	return true;
}
VHOST_OPTION("--radio-index", "1..999", set_index, "the fixture member: dmr-pair 1|2, dmr-group 1..999 (default: 1)")

void vflash_preload(void)
{
	loaded = false;
	if (strcmp(start_storage, "blank") != 0) {
		if (!storage_init())
			vhost_finish("start storage: storage_init failed", 2);
		settings_init();
		codeplug_init();
		const char *steps = start_storage + 6;
#if CONFIG_DMR
		char pair[256];
		if (strcmp(start_storage, "dmr-pair") == 0) {
			if (radio_index > 2)
				vhost_finish("start storage: dmr-pair needs --radio-index 1 or 2", 2);
			snprintf(pair, sizeof pair,
				"channel=1,Simplex,433500000,433500000,dmr,1,1,1;zone=0,Pair,1;"
				"contact=1,Peer,%u,private,1;dmr_id=%u;radio_name=N0SIM;setting=channel,1",
				(unsigned)(3100003u - radio_index), (unsigned)(3100000u + radio_index));
			steps = pair;
		}
		if (strcmp(start_storage, "dmr-group") == 0) {
			snprintf(pair, sizeof pair,
				"channel=1,Group,433500000,433500000,dmr,1,1,1;zone=0,Group,1;"
				"contact=1,TG9,%u,group,1;dmr_id=%u;radio_name=N0SIM;setting=channel,1",
				FIXTURE_TALKGROUP, (unsigned)(FIXTURE_ID_FIRST - 1u + radio_index));
			steps = pair;
		}
#endif
		if (!edits(steps, true) || !settings_flush())
			vhost_finish("start storage: editor step failed", 2);
	}
	loaded = true;
	vstorage_checkpoint();
}

// SHA-256, FIPS 180-4 sections 4.2.2, 5.3.3, 6.2. No external library or flash image copy.
// https://nvlpubs.nist.gov/nistpubs/FIPS/NIST.FIPS.180-4.pdf
static uint32_t rotr(uint32_t v, unsigned n) { return (v >> n) | (v << (32u - n)); }

static void sha_block(uint32_t h[8], const uint8_t block[64])
{
	static const uint32_t k[64] = {
		0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
		0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
		0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
		0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
		0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
		0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
		0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
		0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
	};
	uint32_t w[64];
	for (unsigned i = 0; i < 16; i++)
		w[i] = ((uint32_t)block[4*i] << 24) | ((uint32_t)block[4*i+1] << 16) |
		       ((uint32_t)block[4*i+2] << 8) | block[4*i+3];
	for (unsigned i = 16; i < 64; i++) {
		uint32_t a = w[i-15], b = w[i-2];
		w[i] = w[i-16] + (rotr(a, 7) ^ rotr(a, 18) ^ (a >> 3)) + w[i-7] +
		       (rotr(b, 17) ^ rotr(b, 19) ^ (b >> 10));
	}
	uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], z = h[7];
	for (unsigned i = 0; i < 64; i++) {
		uint32_t t = z + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + k[i] + w[i];
		uint32_t u = (rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
		z = g; g = f; f = e; e = d + t; d = c; c = b; b = a; a = t + u;
	}
	h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += z;
}

bool vstorage_digest(char out[65])
{
	uint32_t h[8] = { 0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19 };
	uint8_t block[64];
	// Virtual NOR sizes are whole sectors, hence whole SHA blocks.
	if (vboard_flash.size % sizeof block != 0)
		return false;
	for (uint32_t at = 0; at < vboard_flash.size; at += sizeof block) {
		if (!vboard_flash.read(at, block, sizeof block))
			return false;
		sha_block(h, block);
	}
	memset(block, 0, sizeof block);
	block[0] = 0x80;
	uint64_t bits = (uint64_t)vboard_flash.size * 8u;
	for (unsigned i = 0; i < 8; i++)
		block[63-i] = (uint8_t)(bits >> (i*8));
	sha_block(h, block);
	for (unsigned i = 0; i < 8; i++)
		snprintf(out + 8*i, 9, "%08x", (unsigned)h[i]);
	return true;
}

void vstorage_checkpoint(void)
{
	char digest[65];
	if (!vstorage_digest(digest)) {
		loaded = false;
		vhost_finish("STORE: flash read failed", 2);
	}
	vtrace("STORE %s", digest);
}

// The closing STORE line, just before END (platform/host/vhost.h VHOST_FINISH_HOOK), so END stays the last line
static void store_at_end(void)
{
	if (loaded)
		vstorage_checkpoint();
}
VHOST_FINISH_HOOK(store_at_end)
