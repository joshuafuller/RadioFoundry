// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// FRAME payload, carried by VCHAN_FRAME (ADR 0007). All integers below are little endian.
// A panel exceeds one channel packet: each chunk repeats this header, followed by panel bytes at OFFSET.
// Chunks are consecutive, with no gaps, from offset zero through SIZE. One completed panel is one checkpoint.
// Panel bytes retain vdisplay_snapshot's native format; PALETTE is 16 RGB565 entries, zero without a palette.
// The conductor supplies --radio-id and --boot-id (u32, both default to 1 for standalone runs).
#ifndef VFRAME_H
#define VFRAME_H

#define VFRAME_HEADER_SIZE 72u
enum {
	VFRAME_RADIO_OFF = 0, VFRAME_BOOT_OFF = 4, VFRAME_ID_OFF = 8, VFRAME_TIME_OFF = 12,
	VFRAME_CRC_OFF = 20, VFRAME_SIZE_OFF = 24, VFRAME_OFFSET_OFF = 28,
	VFRAME_WIDTH_OFF = 32, VFRAME_HEIGHT_OFF = 34, VFRAME_BPP_OFF = 36,
	VFRAME_BACKLIGHT_OFF = 37, VFRAME_HAS_PALETTE_OFF = 38, // byte 39 reserved, zero
	VFRAME_PALETTE_OFF = 40,
};

void __wrap_gfx_flush(void);

#endif
