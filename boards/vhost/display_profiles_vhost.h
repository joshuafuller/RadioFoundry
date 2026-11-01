// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual board's display profiles (display_profiles_vhost.c). A profile is named by what the panel is, its depth
// and size ("rgb565-160x128", "indexed-240x320", "mono-128x64"), never by a radio. The panel copy is written only by
// the firmware's own flush calls (hal_display_t flush and flush_indexed), so what vdisplay_snapshot() gives is what a
// real panel would show.
#ifndef DISPLAY_PROFILES_VHOST_H
#define DISPLAY_PROFILES_VHOST_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define VDISPLAY_PALETTE_N 16u

typedef struct {
	const char *profile;		// the profile's name
	uint16_t width, height;		// pixels
	uint8_t bpp;			// 16: RGB565 as the firmware wrote it (native byte order). 4: palette indexes, two
					// pixels a byte, high nibble left. 1: rows of bytes, MSB left
	const uint8_t *data;		// the panel copy, stride * height bytes (the live buffer: read it between flushes)
	size_t len;
	bool has_palette;		// bpp 4: palette[] is what the last flush_indexed gave (all zero before the first)
	uint16_t palette[VDISPLAY_PALETTE_N];	// RGB565, a copy
	uint8_t backlight;		// percent, 0 until the firmware sets it
} vdisplay_snapshot_t;

// Choose the profile (the --display-profile option calls this). False, with a reason on stderr, for an unknown name or
// a profile this build's frame buffer cannot hold (build with DISPLAY_PROFILE=<name>, boards/vhost/config.d/display.mk).
// Only before the board starts: it clears the panel
bool vdisplay_select(const char *name);

// The panel now: bytes, palette and backlight, for the FRAME checkpoint
void vdisplay_snapshot(vdisplay_snapshot_t *s);

// CRC-32 (IEEE, as zlib) of the panel bytes only
uint32_t vdisplay_snapshot_crc(const vdisplay_snapshot_t *s);

#endif
