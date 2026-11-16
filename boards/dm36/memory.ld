/* SPDX-License-Identifier: MIT */
/* Copyright (c) 2026 The RadioFoundry Authors */
/* APP_BASE is 0x0300C000; the 48 KiB stock bootloader stays outside FLASH.
 * Keep the inherited codec flash/RAM/table windows: section 2 does not separate codec from modem/voice data. */
__codec_slot = 0x030E8000;
__codec_slot_end = 0x03100000;
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
