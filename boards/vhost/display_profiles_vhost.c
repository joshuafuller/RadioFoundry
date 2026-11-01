// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual board's display: any depth the boards have, chosen with --display-profile NAME (default: this build's,
// DISPLAY_PROFILE in boards/vhost/config.d/display.mk). It defines vhost_part_display() (vboard.h). The panel copy is written only by the firmware's flush and flush_indexed,
// at the panel's own depth; the trace gets one line per flush with a hash of the rows.
//
// --screen FILE writes the panel as a PPM image after every flush (indexed through the palette, mono as black on white).
// This file is the board's only display: it replaces the standalone host panel.
//
// A frame buffer has a size fixed at build time (GFX_FB_BYTES, GFX_MAX_ROWS, GFX_INDEXED in core/ui/gfx.h). A profile
// that does not fit the build is refused at start with exit code 2, not drawn wrong.
#include <stdio.h>
#include <string.h>
#include "vboard.h"
#include "vhost.h"
#include "vtrace.h"
#include "display_profiles_vhost.h"

// What the core's frame buffer holds in this build (core/ui/gfx.h). A test may set them before including nothing else
#ifndef GFX_FB_BYTES
#define GFX_FB_BYTES	(160u * 128u * 2u)
#endif
#ifndef GFX_MAX_ROWS
#define GFX_MAX_ROWS	160u
#endif
#ifndef GFX_INDEXED
#define GFX_INDEXED	0
#endif
#ifndef VDISPLAY_DEFAULT_PROFILE
#define VDISPLAY_DEFAULT_PROFILE "rgb565-160x128"
#endif

typedef struct {
	const char *name;
	uint16_t w, h;
	uint8_t bpp;
} profile_t;

// The sizes the boards have (boards/<id>/board.toml hardware.gallery): 160x128, 160x120, 240x320 and 128x64. The insets
// are a build setting of the firmware (CONFIG_DISPLAY_INSET_*), not part of the panel
static const profile_t profiles[] = {
	{ "rgb565-160x128", 160, 128, 16 },
	{ "rgb565-240x320", 240, 320, 16 },
	{ "indexed-160x128", 160, 128, 4 },
	{ "indexed-160x120", 160, 120, 4 },
	{ "indexed-240x320", 240, 320, 4 },
	{ "mono-128x64", 128, 64, 1 },
};
#define N_PROFILES (sizeof profiles / sizeof profiles[0])
#define PANEL_MAX_BYTES (240u * 320u * 2u)

static const profile_t *cur;
static uint8_t panel[PANEL_MAX_BYTES];
static uint16_t palette[VDISPLAY_PALETTE_N];
static uint8_t backlight;
static hal_display_t hal;
static const char *screen_path;

static unsigned stride_of(const profile_t *p)
{
	return p->bpp == 1 ? (p->w + 7u) / 8u : p->bpp == 4 ? (p->w + 1u) / 2u : p->w * 2u;
}

// The core's gfx_init refuses a panel its frame buffer cannot hold; say so at start with the way to fix it
static bool fits(const profile_t *p)
{
	if (p->bpp == 4 && !GFX_INDEXED)
		return false;
	return stride_of(p) * p->h <= GFX_FB_BYTES && p->h <= GFX_MAX_ROWS;
}

static const profile_t *find(const char *name)
{
	for (unsigned i = 0; i < N_PROFILES; i++)
		if (strcmp(profiles[i].name, name) == 0)
			return &profiles[i];
	return NULL;
}

bool vdisplay_select(const char *name)
{
	const profile_t *p = find(name);
	if (p == NULL) {
		fprintf(stderr, "--display-profile: unknown profile '%s'; the profiles are", name);
		for (unsigned i = 0; i < N_PROFILES; i++)
			fprintf(stderr, " %s", profiles[i].name);
		fputc('\n', stderr);
		return false;
	}
	if (!fits(p)) {
		fprintf(stderr, "--display-profile: %s does not fit this build's frame buffer; build with DISPLAY_PROFILE=%s\n",
			name, name);
		return false;
	}
	cur = p;
	memset(panel, 0, sizeof panel);
	memset(palette, 0, sizeof palette);
	backlight = 0;
	return true;
}

static bool set_profile(const char *value)
{
	return vdisplay_select(value);
}
VHOST_OPTION("--display-profile", "NAME", set_profile,
	     "the panel: rgb565-160x128, rgb565-240x320, indexed-160x128, indexed-160x120, indexed-240x320 or mono-128x64")

static bool set_screen(const char *value)
{
	screen_path = value;
	return true;
}
VHOST_OPTION("--screen", "FILE", set_screen, "the panel as a PPM image, rewritten after every flush")

static uint16_t pixel565(const vdisplay_snapshot_t *s, unsigned x, unsigned y)
{
	unsigned stride = (unsigned)(s->len / s->height);
	const uint8_t *row = s->data + (size_t)y * stride;
	if (s->bpp == 16)
		return (uint16_t)(row[x * 2] | row[x * 2 + 1] << 8);	// the host's byte order, as the firmware wrote it
	if (s->bpp == 4)
		return s->palette[(row[x >> 1] >> ((x & 1) ? 0 : 4)) & 0x0Fu];
	return (row[x >> 3] >> (7 - (x & 7))) & 1u ? 0x0000 : 0xFFFF;	// a set bit is ink: black on white
}

static void write_screen(void)
{
	FILE *f = fopen(screen_path, "wb");
	if (f == NULL)
		return;
	vdisplay_snapshot_t s;
	vdisplay_snapshot(&s);
	fprintf(f, "P6\n%u %u\n255\n", s.width, s.height);
	for (unsigned y = 0; y < s.height; y++)
		for (unsigned x = 0; x < s.width; x++) {
			uint16_t c = pixel565(&s, x, y);
			uint8_t rgb[3] = { (uint8_t)((c >> 11) << 3), (uint8_t)(((c >> 5) & 0x3Fu) << 2), (uint8_t)((c & 0x1Fu) << 3) };
			fwrite(rgb, 1, 3, f);
		}
	fclose(f);
}

static void disp_init(void)
{
	vtrace("BOARD display.init %s", cur->name);
}

// Copy rows y0 .. y1-1 into the panel; the rows' bytes are what the trace hashes
static void put_rows(const void *fb, uint16_t y0, uint16_t y1, const char *tag)
{
	unsigned stride = stride_of(cur);
	if (y1 > cur->h)
		y1 = cur->h;
	if (y0 >= y1)
		return;
	const uint8_t *rows = (const uint8_t *)fb + (size_t)y0 * stride;
	size_t n = (size_t)(y1 - y0) * stride;
	memcpy(&panel[(size_t)y0 * stride], rows, n);
	if (tag != NULL)
		vtrace("BOARD display.flush rows=%u..%u hash=%016llx %s", y0, y1, (unsigned long long)vtrace_hash(rows, n), tag);
	else
		vtrace("BOARD display.flush rows=%u..%u hash=%016llx", y0, y1, (unsigned long long)vtrace_hash(rows, n));
	if (screen_path != NULL)
		write_screen();
}

static void disp_flush(const void *fb, uint16_t y0, uint16_t y1)
{
	put_rows(fb, y0, y1, NULL);
}

static void disp_flush_indexed(const void *fb, const uint16_t *pal, uint16_t y0, uint16_t y1)
{
	memcpy(palette, pal, sizeof palette);
	char tag[32];
	snprintf(tag, sizeof tag, "pal=%016llx", (unsigned long long)vtrace_hash(palette, sizeof palette));
	put_rows(fb, y0, y1, tag);
}

static void disp_backlight(uint8_t pct)
{
	backlight = pct > 100u ? 100u : pct;
	vtrace("BOARD display.backlight %u", backlight);
}

const hal_display_t *vhost_part_display(void)
{
	if (cur == NULL && !vdisplay_select(VDISPLAY_DEFAULT_PROFILE))
		vhost_finish("the build's display profile does not fit its frame buffer", 2);
	hal = (hal_display_t){
		.width = cur->w, .height = cur->h, .bpp = cur->bpp,
		.init = disp_init, .set_backlight = disp_backlight,
	};
	// gfx.c calls flush_indexed at 4 bpp and flush otherwise
	if (cur->bpp == 4)
		hal.flush_indexed = disp_flush_indexed;
	else
		hal.flush = disp_flush;
	return &hal;
}

// board.c names vboard_panel as the fallback when no part gives a display; vhost_part_display above always does, so this
// is the default profile's shape only to satisfy the link
const hal_display_t vboard_panel = {
	.width = 160, .height = 128, .bpp = 16,
	.init = disp_init, .flush = disp_flush, .set_backlight = disp_backlight,
};

void vdisplay_snapshot(vdisplay_snapshot_t *s)
{
	if (cur == NULL && !vdisplay_select(VDISPLAY_DEFAULT_PROFILE))
		vhost_finish("the build's display profile does not fit its frame buffer", 2);
	s->profile = cur->name;
	s->width = cur->w;
	s->height = cur->h;
	s->bpp = cur->bpp;
	s->data = panel;
	s->len = (size_t)stride_of(cur) * cur->h;
	s->has_palette = cur->bpp == 4;
	memcpy(s->palette, palette, sizeof s->palette);
	s->backlight = backlight;
}

uint32_t vdisplay_snapshot_crc(const vdisplay_snapshot_t *s)
{
	uint32_t crc = 0xFFFFFFFFu;
	for (size_t i = 0; i < s->len; i++) {
		crc ^= s->data[i];
		for (int k = 0; k < 8; k++)
			crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
	}
	return ~crc;
}
