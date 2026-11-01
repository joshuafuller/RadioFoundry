// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual board's MCU (vboard.h, hal/hal_system.h): what the debug link's INFO_CHIP, READ_MEM and REBOOT see.
//
// Three rules from ADR 0004 rule 6:
//  1. The chip id is fixed. Every run reports the same numbers, so a recording replays the same. The default is a
//     fixture value; --chip-uid changes it for a test that needs a second radio to look different.
//  2. READ_MEM sees only a fake memory block of FAKE_MEM_SIZE bytes. Its bytes are made from the offset by a small
//     formula when asked; no host pointer is ever read, and no host address is ever kept. Example: a request for
//     the address of main() on the host falls outside the block, so the debug link refuses it before read() runs.
//  3. REBOOT is a process restart. The radio's flash is saved first (when --restart-flash gives a file), then the run
//     ends with the reason "reboot" and exit code VHOST_REBOOT_EXIT. The conductor starts the process
//     again. If the flash cannot be saved, the reboot is not armed (reboot() returns false) so no data is lost.
//
//   --chip-uid A:B:C         the 96-bit unique id as three hex words (default: the fixture value below)
//   --restart-flash FILE     the file flash_vhost_save writes before a reboot (the next run loads it)
#include <stdio.h>
#include <string.h>
#include "vboard.h"
#include "vhost.h"
#include "vtrace.h"

#define VHOST_REBOOT_EXIT	4	// the run ended because the radio rebooted (crash restarts use 3)
#define FAKE_MEM_BASE		0x20000000u	// where the block appears to the debug link (the STM32 SRAM base)
#define FAKE_MEM_SIZE		0x4000u		// 16 KiB; the window is FAKE_MEM_BASE .. FAKE_MEM_BASE + FAKE_MEM_SIZE - 1

static uint32_t chip_uid[3] = { 0x0F00D001u, 0x0F00D002u, 0x0F00D003u };	// fixture values, not a real device
static const char *restart_flash;

static bool sys_chip(hal_chip_t *c)
{
	memset(c, 0, sizeof *c);
	c->idcode = 0x10006413u;	// a fixture value
	c->flash_kb = 1024;
	memcpy(c->uid, chip_uid, sizeof c->uid);
	// Not the STM32F4: there is no ROM bootloader here, so the debug link's check refuses the ROM DFU reboot
	c->family = HAL_CHIP_HRC7000;
	return true;
}

// One byte of the fake block, from its offset only
static uint8_t fake_byte(uint32_t off)
{
	return (uint8_t)((off * 167u + (off >> 8) * 29u + 0x5Au) ^ 0xA5u);
}

// The debug link has checked the range against sys_window. A range outside is still safe here: it gets zeros
static void sys_read(uint32_t addr, void *dst, size_t n)
{
	uint8_t *d = dst;
	for (size_t i = 0; i < n; i++) {
		uint32_t off = addr + (uint32_t)i - FAKE_MEM_BASE;
		d[i] = (addr + (uint32_t)i >= FAKE_MEM_BASE && off < FAKE_MEM_SIZE) ? fake_byte(off) : 0;
	}
}

static bool sys_window(unsigned i, hal_mem_window_t *w)
{
	if (i != 0)
		return false;
	w->lo = FAKE_MEM_BASE;
	w->hi = FAKE_MEM_BASE + FAKE_MEM_SIZE - 1u;
	w->words = false;
	return true;
}

static bool sys_reboot(uint8_t mode)
{
	if (mode != HAL_REBOOT_NORMAL)
		return false;
	if (restart_flash != NULL && !flash_vhost_save(restart_flash)) {
		vtrace("REBOOT flash not saved, reboot not armed");
		return false;
	}
	vtrace("REBOOT mode=%u flash_saved=%u", (unsigned)mode, restart_flash != NULL);
	vhost_finish("reboot", VHOST_REBOOT_EXIT);
}

static const hal_system_t vsystem = {
	.chip = sys_chip,
	.read = sys_read,
	.mem_window = sys_window,
	.reboot = sys_reboot,
};

const hal_system_t *vhost_part_system(void)
{
	return &vsystem;
}

static bool set_chip_uid(const char *value)
{
	unsigned a, b, c;
	char extra;
	if (sscanf(value, "%x:%x:%x%c", &a, &b, &c, &extra) != 3)
		return false;
	chip_uid[0] = a;
	chip_uid[1] = b;
	chip_uid[2] = c;
	return true;
}
VHOST_OPTION("--chip-uid", "A:B:C", set_chip_uid, "the chip's 96-bit unique id, three hex words")

static bool set_restart_flash(const char *value)
{
	restart_flash = value;
	return true;
}
VHOST_OPTION("--restart-flash", "FILE", set_restart_flash, "save the flash here before a reboot (the next run loads it)")
