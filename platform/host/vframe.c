// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// ADR 0007: observe the completed panel, including a flush with no changed rows.
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ui/gfx.h"
#include "display_profiles_vhost.h"
#include "vchan.h"
#include "vclock.h"
#include "vframe.h"
#include "vhost.h"
#include "vtrace.h"

static uint32_t radio_id = 1, boot_id = 1;

static bool identity(const char *value, uint32_t *id)
{
	if (*value < '0' || *value > '9')
		return false;
	char *end;
	errno = 0;
	unsigned long long n = strtoull(value, &end, 10);
	if (errno || *end || n > UINT32_MAX)
		return false;
	*id = (uint32_t)n;
	return true;
}
static bool set_radio(const char *value) { return identity(value, &radio_id); }
static bool set_boot(const char *value) { return identity(value, &boot_id); }
VHOST_OPTION("--radio-id", "N", set_radio, "FRAME radio identity assigned by the conductor (default: 1)")
VHOST_OPTION("--boot-id", "N", set_boot, "FRAME boot identity assigned by the conductor (default: 1)")

static void put_le(uint8_t *p, uint64_t value, unsigned n)
{
	for (unsigned i = 0; i < n; i++)
		p[i] = (uint8_t)(value >> (8 * i));
}

void __real_gfx_flush(void);
void __wrap_gfx_flush(void)
{
	// Count flush entries separately from completed checkpoints in the boot check.
	vtrace("GFX_FLUSH");
	__real_gfx_flush();
	uint32_t id;
	(void)gfx_frame_idle(&id);
	vdisplay_snapshot_t s;
	vdisplay_snapshot(&s);
	uint64_t now = vclock_now_us();
	uint32_t crc = vdisplay_snapshot_crc(&s);
	char palette[VDISPLAY_PALETTE_N * 4 + 1] = "-";
	if (s.has_palette)
		for (unsigned i = 0; i < VDISPLAY_PALETTE_N; i++)
			snprintf(palette + i * 4, 5, "%04x", s.palette[i]);
	vtrace("FRAME radio=%u boot=%u id=%u us=%llu crc=%08x palette=%s backlight=%u",
		radio_id, boot_id, id, (unsigned long long)now, crc, palette, s.backlight);

	uint8_t wire[VCHAN_PAYLOAD_MAX] = { 0 };
	put_le(wire + VFRAME_RADIO_OFF, radio_id, 4);
	put_le(wire + VFRAME_BOOT_OFF, boot_id, 4);
	put_le(wire + VFRAME_ID_OFF, id, 4);
	put_le(wire + VFRAME_TIME_OFF, now, 8);
	put_le(wire + VFRAME_CRC_OFF, crc, 4);
	put_le(wire + VFRAME_SIZE_OFF, s.len, 4);
	put_le(wire + VFRAME_WIDTH_OFF, s.width, 2);
	put_le(wire + VFRAME_HEIGHT_OFF, s.height, 2);
	wire[VFRAME_BPP_OFF] = s.bpp;
	wire[VFRAME_BACKLIGHT_OFF] = s.backlight;
	wire[VFRAME_HAS_PALETTE_OFF] = s.has_palette;
	if (s.has_palette)
		for (unsigned i = 0; i < VDISPLAY_PALETTE_N; i++)
			put_le(wire + VFRAME_PALETTE_OFF + i * 2, s.palette[i], 2);
	for (size_t off = 0; off < s.len;) {
		size_t n = s.len - off;
		if (n > sizeof wire - VFRAME_HEADER_SIZE)
			n = sizeof wire - VFRAME_HEADER_SIZE;
		put_le(wire + VFRAME_OFFSET_OFF, off, 4);
		memcpy(wire + VFRAME_HEADER_SIZE, s.data + off, n);
		vchan_send(VCHAN_FRAME, wire, VFRAME_HEADER_SIZE + n);
		off += n;
	}
}
