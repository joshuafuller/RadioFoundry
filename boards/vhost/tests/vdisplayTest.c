// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual board's display profiles (boards/vhost/display_profiles_vhost.c), one named case per profile: a known
// flush gives a known panel CRC, a partial flush writes only its rows, and at 4 bpp a palette change changes the
// snapshot. The expected CRCs are zlib crc32 of the pattern, worked out apart from the code (python3: zlib.crc32).
// Built with a frame buffer that holds every profile (the makefile sets GFX_*), the trace and option registry stubbed.
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "vboard.h"
#include "vhost.h"
#include "vtrace.h"
#include "display_profiles_vhost.h"

static int failures, checks;
#define CHECK(c) do { checks++; if (!(c)) { failures++; printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); } } while (0)

// ---- stubs for the host platform
static int n_options;
static bool (*option_set)(const char *), (*screen_set)(const char *);
void vhost_option_register(const vhost_option_t *opt)
{
	n_options++;
	if (strcmp(opt->name, "--display-profile") == 0)
		option_set = opt->set;
	if (strcmp(opt->name, "--screen") == 0)
		screen_set = opt->set;
}
static char last_line[256];
static int n_lines;
void vtrace(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(last_line, sizeof last_line, fmt, ap);
	va_end(ap);
	n_lines++;
}
uint64_t vtrace_hash(const void *data, size_t n)
{
	uint64_t h = 1469598103934665603ull;	// FNV-1a, as platform/host/vtrace.c
	for (size_t i = 0; i < n; i++)
		h = (h ^ ((const uint8_t *)data)[i]) * 1099511628211ull;
	return h;
}
void vhost_finish(const char *why, int code)
{
	printf("vhost_finish(%s, %d)\n", why, code);
	exit(1);
}

static uint8_t fb[240 * 320 * 2];

typedef struct {
	const char *name;
	uint16_t w, h;
	uint8_t bpp;
	size_t len;
	uint32_t crc_pattern;	// crc32 of byte i = (i * 7 + 3) & 0xFF over len bytes
	uint32_t crc_row1;	// crc32 of a zeroed panel with row 1 = 0xA5 bytes
} case_t;

static const case_t cases[] = {
	{ "rgb565-160x128", 160, 128, 16, 40960, 0x2e629cde, 0xc0d32a6a },
	{ "rgb565-240x320", 240, 320, 16, 153600, 0xdc77e933, 0xd4b0c081 },
	{ "indexed-160x128", 160, 128, 4, 10240, 0x58daed8a, 0xd216481e },
	{ "indexed-160x120", 160, 120, 4, 9600, 0xde68da14, 0x692f5f6b },
	{ "indexed-240x320", 240, 320, 4, 38400, 0x7869b9be, 0xcc400afb },
	{ "mono-128x64", 128, 64, 1, 1024, 0x5d3de8ed, 0x3aab593a },
};

static void show(const hal_display_t *d, const uint16_t *pal, uint16_t y0, uint16_t y1)
{
	if (d->bpp == 4)
		d->flush_indexed(fb, pal, y0, y1);
	else
		d->flush(fb, y0, y1);
}

static void run_case(const case_t *c)
{
	printf("case %s\n", c->name);
	CHECK(vdisplay_select(c->name));
	const hal_display_t *d = vhost_part_display();
	CHECK(d != NULL && d->width == c->w && d->height == c->h && d->bpp == c->bpp);
	CHECK(d->init != NULL && d->set_backlight != NULL);
	CHECK((c->bpp == 4) == (d->flush_indexed != NULL) && (c->bpp != 4) == (d->flush != NULL));
	d->init();

	vdisplay_snapshot_t s;
	vdisplay_snapshot(&s);
	CHECK(strcmp(s.profile, c->name) == 0 && s.len == c->len && s.bpp == c->bpp && s.backlight == 0);
	CHECK(s.has_palette == (c->bpp == 4));

	// A known full flush gives a known panel CRC
	uint16_t pal_a[16], pal_b[16];
	for (unsigned i = 0; i < 16; i++) {
		pal_a[i] = (uint16_t)(i * 0x0841u);
		pal_b[i] = (uint16_t)(0xFFFFu - i * 0x0841u);
	}
	for (size_t i = 0; i < c->len; i++)
		fb[i] = (uint8_t)(i * 7 + 3);
	show(d, pal_a, 0, c->h);
	vdisplay_snapshot(&s);
	CHECK(vdisplay_snapshot_crc(&s) == c->crc_pattern);
	CHECK(strstr(last_line, "display.flush rows=0..") != NULL);
	if (c->bpp == 4) {
		// A palette change with no row change changes the snapshot, not the pixel bytes
		CHECK(memcmp(s.palette, pal_a, sizeof pal_a) == 0);
		show(d, pal_b, 0, c->h);
		vdisplay_snapshot_t t;
		vdisplay_snapshot(&t);
		CHECK(memcmp(t.palette, pal_b, sizeof pal_b) == 0 && memcmp(t.palette, s.palette, sizeof pal_a) != 0);
		CHECK(vdisplay_snapshot_crc(&t) == c->crc_pattern);
	}

	// A partial flush writes only its rows: a fresh panel, row 1 only
	CHECK(vdisplay_select(c->name));
	unsigned stride = c->len / c->h;
	memset(fb, 0, c->len);
	memset(fb + stride, 0xA5, stride);
	show(d, pal_a, 1, 2);
	vdisplay_snapshot(&s);
	CHECK(vdisplay_snapshot_crc(&s) == c->crc_row1);
	// Rows past the panel are cut, an empty band writes nothing
	n_lines = 0;
	show(d, pal_a, 5, 5);
	CHECK(vdisplay_snapshot_crc(&s) == c->crc_row1);
	show(d, pal_a, c->h - 1, (uint16_t)(c->h + 40));
	vdisplay_snapshot(&s);
	CHECK(s.len == c->len);

	// The backlight is kept, at most 100
	d->set_backlight(60);
	vdisplay_snapshot(&s);
	CHECK(s.backlight == 60);
	d->set_backlight(250);
	vdisplay_snapshot(&s);
	CHECK(s.backlight == 100);
}

// --screen: every flush rewrites the PPM; mono is black ink on white
static void screen_case(void)
{
	printf("case --screen\n");
	CHECK(vdisplay_select("mono-128x64") && screen_set("vdisplayTest.ppm"));
	const hal_display_t *d = vhost_part_display();
	memset(fb, 0, 1024);
	fb[0] = 0x80;		// pixel (0,0) is ink
	d->flush(fb, 0, 64);
	FILE *f = fopen("vdisplayTest.ppm", "rb");
	CHECK(f != NULL);
	if (f != NULL) {
		char head[16] = { 0 };
		unsigned char px[6];
		CHECK(fread(head, 1, 14, f) == 14 && memcmp(head, "P6\n128 64\n255\n", 14) == 0);
		CHECK(fread(px, 1, 6, f) == 6 && px[0] == 0 && px[3] == 0xF8 && px[4] == 0xFC);
		fclose(f);
		remove("vdisplayTest.ppm");
	}
	screen_set(NULL);
}

int main(void)
{
	CHECK(option_set != NULL && screen_set != NULL && n_options == 2);
	CHECK(!vdisplay_select("no-such-profile"));
	CHECK(option_set != NULL && option_set("mono-128x64") && !option_set("rgb565-9x9"));
	for (unsigned i = 0; i < sizeof cases / sizeof cases[0]; i++)
		run_case(&cases[i]);
	screen_case();
	printf("%d checks, %d failures\n", checks, failures);
	if (failures == 0)
		printf("vdisplayTest PASSED\n");
	return failures != 0;
}
