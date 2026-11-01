// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual board's data flash (vboard.h): 16 MB of NOR, in the open-firmware layout of the STM32 radios.
// Sparse: memory is held only for a 4 KB sector that holds something other than 0xFF. A sector never written, or
// erased, has no memory and reads as 0xFF, so an idle radio costs a few KB, not 16 MB. Erased before the firmware
// runs, then filled by vflash_preload(). A write only clears bits, as on the chip: the new byte is ANDed into the old
// one. A write that asks for a bit to go 0 -> 1 is still done (ANDed) and also gets a flash.set_bit trace line, not an
// error, so a boot that writes 0xFF padding still runs. Each write and erase is a trace line; a write carries the
// content hash of the bytes written (not a CRC: a settings record ends in its own CRC, and the CRC of data plus its
// own CRC is always the same, so a CRC trace could not tell two settings apart).
// --flash-file writes through each completed program/erase, so a process power cut keeps committed flash.
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include "vboard.h"
#include "vhost.h"
#include "vtrace.h"

#define FLASH_SIZE	0x1000000u
#define ERASE		0x1000u
#define SECTORS		(FLASH_SIZE / ERASE)

static uint8_t *sector[SECTORS];	// NULL: the whole sector is 0xFF
static uint32_t held;			// sectors with memory

static const hal_flash_map_t flash_map = {
	.settings_addr = 0x011000u, .settings_size = 0x008000u,
	.journal_addr = 0x019000u,
	.openfw_eeprom = 0x000000u, .openfw_flash = 0x020000u, .openfw_end = 0xE00000u,
	.native_addr = 0xE00000u, .native_size = 0x100000u,
	.staging_addr = 0xF00000u, .staging_size = 0x040000u,
};

__attribute__((weak)) void vflash_preload(void)
{
}

static bool in_range(uint32_t a, uint32_t len)
{
	return a <= FLASH_SIZE && len <= FLASH_SIZE - a;
}

static void drop_all(void)
{
	for (uint32_t i = 0; i < SECTORS; i++) {
		free(sector[i]);
		sector[i] = NULL;
	}
	held = 0;
}

// The sector's memory, made (all 0xFF) when it has none
static uint8_t *hold(uint32_t i)
{
	if (!sector[i]) {
		sector[i] = malloc(ERASE);
		if (!sector[i])
			abort();	// out of host memory: a test rig fault, not a radio state
		memset(sector[i], 0xFF, ERASE);
		held++;
	}
	return sector[i];
}

// Copy len bytes from the flash into out, 0xFF where a sector has no memory
static void copy_out(uint32_t a, uint8_t *out, uint32_t len)
{
	while (len) {
		uint32_t i = a / ERASE, off = a % ERASE, n = ERASE - off;
		if (n > len)
			n = len;
		if (sector[i])
			memcpy(out, sector[i] + off, n);
		else
			memset(out, 0xFF, n);
		a += n;
		out += n;
		len -= n;
	}
}

static bool all_ff(const uint8_t *p, uint32_t n)
{
	for (uint32_t i = 0; i < n; i++)
		if (p[i] != 0xFF)
			return false;
	return true;
}

bool vflash_load(uint32_t addr, const void *data, uint32_t len)
{
	if (!in_range(addr, len))
		return false;
	const uint8_t *s = data;
	while (len) {
		uint32_t i = addr / ERASE, off = addr % ERASE, n = ERASE - off;
		if (n > len)
			n = len;
		if (sector[i] || !all_ff(s, n))	// 0xFF into a sector with no memory changes nothing
			memcpy(hold(i) + off, s, n);
		addr += n;
		s += n;
		len -= n;
	}
	return true;
}

static const char *restore_path;	// --restore-flash: the file a rebooted radio starts from
static const char *flash_path;	// --flash-file: the backing file, also saved before the first firmware write
static bool flash_ready;

static void persist(void)
{
	if (flash_ready && flash_path != NULL && !flash_vhost_save(flash_path)) {
		vtrace("flash save failed: %s", flash_path);
		vhost_finish("flash save failed", 2);
	}
}

void vboard_flash_boot(void)
{
	flash_ready = false;	// Fixture writes must not replace the file we are about to restore.
	drop_all();
	vflash_preload();
	// A reboot keeps the flash: the file the last run saved replaces the start contents. A missing or bad file
	// ends the run, so a reboot never silently comes back with a blank radio
	if (restore_path != NULL && !flash_vhost_load(restore_path)) {
		vtrace("flash restore failed: %s", restore_path);
		vhost_finish("flash restore failed", 2);
	}
	flash_ready = true;
	persist();
}

// For the test: how many sectors hold memory
uint32_t flash_vhost_sectors_held(void);
uint32_t flash_vhost_sectors_held(void)
{
	return held;
}

static bool f_init(void)
{
	vtrace("BOARD flash.init");
	return true;
}

static bool f_read(uint32_t a, void *buf, uint32_t len)
{
	if (!in_range(a, len))
		return false;
	copy_out(a, buf, len);
	return true;
}

static bool f_write(uint32_t a, const void *buf, uint32_t len)
{
	if (!in_range(a, len))
		return false;
	const uint8_t *s = buf;
	bool set_bit = false;
	uint32_t pos = a, left = len;
	while (left) {
		uint32_t i = pos / ERASE, off = pos % ERASE, n = ERASE - off;
		if (n > left)
			n = left;
		const uint8_t *src = s + (pos - a);
		for (uint32_t k = 0; k < n; k++) {
			uint8_t old = sector[i] ? sector[i][off + k] : 0xFF;
			if (src[k] & (uint8_t)~old)
				set_bit = true;
		}
		if (sector[i] || !all_ff(src, n)) {
			uint8_t *m = hold(i);
			for (uint32_t k = 0; k < n; k++)
				m[off + k] &= src[k];	// NOR: programming only clears bits
		}
		pos += n;
		left -= n;
	}
	persist();
	vtrace("BOARD flash.write %06x+%u hash=%016llx", (unsigned)a, (unsigned)len,
		(unsigned long long)vtrace_hash(s, len));
	if (set_bit)
		vtrace("BOARD flash.set_bit %06x+%u", (unsigned)a, (unsigned)len);
	return true;
}

static bool f_erase(uint32_t a)
{
	if (a % ERASE != 0 || a >= FLASH_SIZE)
		return false;
	uint32_t i = a / ERASE;
	if (sector[i]) {
		free(sector[i]);
		sector[i] = NULL;
		held--;
	}
	persist();
	vtrace("BOARD flash.erase %06x", (unsigned)a);
	return true;
}

// ---- keep the flash across a process restart. File: "RFFLASH1", then per held sector a 4-byte little-endian sector
// number and its 4096 bytes. Mode 0600. Written to <path>.tmp and renamed, so a crash never leaves half a file
#define MAGIC "RFFLASH1"


bool flash_vhost_save(const char *path)
{
	char tmp[4096];
	if (snprintf(tmp, sizeof(tmp), "%s.tmp", path) >= (int)sizeof(tmp))
		return false;
	int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0600);
	if (fd < 0)
		return false;
	FILE *f = fdopen(fd, "wb");
	if (!f) {
		close(fd);
		return false;
	}
	bool ok = fchmod(fd, 0600) == 0 && fwrite(MAGIC, 1, 8, f) == 8;
	for (uint32_t i = 0; ok && i < SECTORS; i++) {
		if (!sector[i])
			continue;
		uint8_t n[4] = { (uint8_t)i, (uint8_t)(i >> 8), (uint8_t)(i >> 16), (uint8_t)(i >> 24) };
		ok = fwrite(n, 1, 4, f) == 4 && fwrite(sector[i], 1, ERASE, f) == ERASE;
	}
	ok = (fclose(f) == 0) && ok;
	if (ok)
		ok = rename(tmp, path) == 0;
	if (!ok)
		unlink(tmp);
	return ok;
}

// Replaces the whole flash with the file's. On any fault (no file, bad magic, bad sector number, short read) the
// flash is left as it was and false is returned
bool flash_vhost_load(const char *path)
{
	FILE *f = fopen(path, "rb");
	if (!f)
		return false;
	uint8_t *fresh[SECTORS] = { 0 };
	uint32_t count = 0;
	char magic[8];
	bool ok = fread(magic, 1, 8, f) == 8 && memcmp(magic, MAGIC, 8) == 0;
	while (ok) {
		uint8_t n[4];
		size_t got = fread(n, 1, 4, f);
		if (got == 0)
			break;		// clean end of file
		uint32_t i = (uint32_t)n[0] | (uint32_t)n[1] << 8 | (uint32_t)n[2] << 16 | (uint32_t)n[3] << 24;
		if (got != 4 || i >= SECTORS || fresh[i]) {
			ok = false;
			break;
		}
		fresh[i] = malloc(ERASE);
		if (!fresh[i] || fread(fresh[i], 1, ERASE, f) != ERASE) {
			ok = false;
			break;
		}
		count++;
	}
	fclose(f);
	if (!ok) {
		for (uint32_t i = 0; i < SECTORS; i++)
			free(fresh[i]);
		return false;
	}
	drop_all();
	memcpy(sector, fresh, sizeof(sector));
	held = count;
	return true;
}

const hal_flash_t vboard_flash = {
	.init = f_init, .size = FLASH_SIZE, .erase_size = ERASE, .read = f_read, .write = f_write, .erase = f_erase,
	.map = &flash_map,
};

static bool set_restore_flash(const char *value)
{
	restore_path = value;
	return true;
}
VHOST_OPTION("--restore-flash", "FILE", set_restore_flash, "start with the flash a reboot saved (see --restart-flash)")

static bool set_flash_file(const char *value)
{
	flash_path = value;
	return true;
}
VHOST_OPTION("--flash-file", "FILE", set_flash_file, "save each completed flash write and erase here")
