// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#ifndef BOARD_VOLUME_H
#define BOARD_VOLUME_H

// hardware-dm36.md 9.1: levels 8-10 saturate at 7; level 0 is muted by the shared amplifier gate.
static unsigned volume_index(unsigned step)
{
	return step == 0 ? 0 : step > 7 ? 6 : step - 1;
}

static uint16_t board_volume_fm(unsigned step)
{
	static const uint8_t gain[] = { 2, 4, 6, 8, 12, 14, 15 };
	return 0xB280u | gain[volume_index(step)];
}

static uint8_t board_volume_codec(unsigned step)
{
	static const uint8_t gain[] = { 0x8B, 0x86, 0x81, 0xB7, 0xB5, 0xB3, 0xB1 };
	return gain[volume_index(step)];
}
#endif
