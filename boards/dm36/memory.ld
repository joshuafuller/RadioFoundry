/* SPDX-License-Identifier: MIT */
/* Copyright (c) 2026 The RadioFoundry Authors */
/* Unverified layout, taken from the shared board sources: APP_BASE, codec windows and SRAM reservations are
 * hypotheses until the stock firmware is read (docs/spec/hardware-dm36.md). */
__codec_slot = 0x030E8000;
__codec_slot_end = 0x03100000;
/* The codec window (platform/hrc7000/codec_slot.h: the slot's code runs at its start; the size is the largest code
 * among the board's codec maps; tools/boards.py reads these symbols for the codec tools) and the modem SRAM table
 * window, the union of the builds' stock table addresses */
__codec_run_lo = 0x00043A00;
__codec_run_hi = 0x00058000;
__codec_tab_lo = 0x18000E48;
__codec_tab_hi = 0x18003E28;

MEMORY
{
	FLASH  (rx)  : ORIGIN = __app_start, LENGTH = __codec_slot - __app_start
	RAM    (rwx) : ORIGIN = 0x00010000, LENGTH = __codec_run_lo - 0x00010000	/* up to the codec window */
}

INCLUDE common.ld
