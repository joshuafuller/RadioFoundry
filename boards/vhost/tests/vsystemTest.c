// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual board's system part (boards/vhost/system_vhost.c) behind the debug link's own range check
// (dl_mem_allowed, the function READ_MEM calls before it calls read): the fixed chip id, the bounded fake memory that
// can not reach host memory, and reboot as a run that ends. The flash save and the run's end are stand-ins here.
#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "debuglink/dl_chip.h"
#include "../system_vhost.c"

int main(void);
static int failures;
#define CHECK(c)							\
	do {								\
		if (!(c)) {						\
			printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);	\
			failures++;					\
		}							\
	} while (0)

// ---- stand-ins
static const char *end_reason;
static int end_code;
static jmp_buf ended;
static int save_calls;
static bool save_ok = true;
static char saved_path[64];

void vhost_option_register(const vhost_option_t *opt)
{
	(void)opt;
}

void vtrace(const char *fmt, ...)
{
	(void)fmt;
}

void vhost_finish(const char *why, int code)
{
	end_reason = why;
	end_code = code;
	longjmp(ended, 1);
}

bool flash_vhost_save(const char *path)
{
	save_calls++;
	snprintf(saved_path, sizeof saved_path, "%s", path);
	return save_ok;
}

// What the debug link does for READ_MEM: the range check, then read. False when the check refuses it
static bool read_mem(uint32_t addr, uint32_t n, uint8_t *out)
{
	if (!dl_mem_allowed(addr, n, vhost_part_system()->mem_window))
		return false;
	vhost_part_system()->read(addr, out, n);
	return true;
}

static void test_info_chip(void)
{
	hal_chip_t c, d;
	const hal_system_t *s = vhost_part_system();
	CHECK(s != NULL && s->chip != NULL);
	CHECK(s->chip(&c));
	CHECK(c.idcode == 0x10006413u);
	CHECK(c.uid[0] == 0x0F00D001u && c.uid[1] == 0x0F00D002u && c.uid[2] == 0x0F00D003u);
	CHECK(c.flash_kb == 1024);
	CHECK(c.family == HAL_CHIP_HRC7000);
	CHECK(s->chip(&d));
	CHECK(memcmp(&c, &d, sizeof c) == 0);	// the same every time
	// The reboot token is uid[0]; the ROM DFU path is refused (no option bytes, no ROM bootloader here)
	CHECK(dl_reboot_check(HAL_REBOOT_NORMAL, c.uid[0], &c, false) == DL_REBOOT_OK);
	CHECK(dl_reboot_check(HAL_REBOOT_ROM_DFU, c.uid[0], &c, false) == DL_REBOOT_RDP);
	// The option changes the uid, nothing else
	CHECK(set_chip_uid("1:2:3") && !set_chip_uid("1:2") && !set_chip_uid("1:2:3:4") && !set_chip_uid("1:2:3x"));
	CHECK(s->chip(&c) && c.uid[0] == 1 && c.uid[1] == 2 && c.uid[2] == 3 && c.idcode == d.idcode);
	chip_uid[0] = 0x0F00D001u;
	chip_uid[1] = 0x0F00D002u;
	chip_uid[2] = 0x0F00D003u;
}

static void test_read_mem_inside(void)
{
	uint8_t b[16], one;
	CHECK(read_mem(FAKE_MEM_BASE, 16, b));
	for (uint32_t i = 0; i < 16; i++)
		CHECK(b[i] == fake_byte(i));
	CHECK(read_mem(FAKE_MEM_BASE + 0x123, 4, b));
	for (uint32_t i = 0; i < 4; i++)
		CHECK(b[i] == fake_byte(0x123 + i));
	// The last byte is inside
	CHECK(read_mem(FAKE_MEM_BASE + FAKE_MEM_SIZE - 1u, 1, &one) && one == fake_byte(FAKE_MEM_SIZE - 1u));
	// Same bytes on a second read: generated, not kept state
	uint8_t c[16];
	CHECK(read_mem(FAKE_MEM_BASE, 16, b) && read_mem(FAKE_MEM_BASE, 16, c) && memcmp(b, c, 16) == 0);
	// Not all one value
	bool varied = false;
	for (int i = 1; i < 16; i++)
		varied |= b[i] != b[0];
	CHECK(varied);
}

static void test_read_mem_refused(void)
{
	uint8_t b[64];
	// At the end of the array: the first byte past it, and a read that starts inside and runs over
	CHECK(!read_mem(FAKE_MEM_BASE + FAKE_MEM_SIZE, 1, b));
	CHECK(!read_mem(FAKE_MEM_BASE + FAKE_MEM_SIZE - 1u, 2, b));
	CHECK(!read_mem(FAKE_MEM_BASE + FAKE_MEM_SIZE - 16u, 17, b));
	// Past it
	CHECK(!read_mem(FAKE_MEM_BASE + FAKE_MEM_SIZE + 0x1000u, 4, b));
	CHECK(!read_mem(0xFFFFFFFFu, 1, b));
	CHECK(!read_mem(FAKE_MEM_BASE - 1u, 1, b));
	CHECK(!read_mem(0, 4, b));
	// Wrap-around: addr + len overflows 32 bits
	CHECK(!read_mem(0xFFFFFFF0u, 64, b));
	// A host address: the low 32 bits of the address of main, of a stack buffer and of a string literal
	CHECK(!read_mem((uint32_t)(uintptr_t)&main, 4, b));
	CHECK(!read_mem((uint32_t)(uintptr_t)b, 4, b));
	CHECK(!read_mem((uint32_t)(uintptr_t)"host", 4, b));
	// The one window is the fake block and nothing else, and it is not a registers window
	hal_mem_window_t w;
	CHECK(vhost_part_system()->mem_window(0, &w) && w.lo == FAKE_MEM_BASE && w.hi == FAKE_MEM_BASE + FAKE_MEM_SIZE - 1u);
	CHECK(!w.words);
	CHECK(!vhost_part_system()->mem_window(1, &w));
	// The block lies below where the host puts its code, stack and heap would have to be to be read through it
	CHECK((uintptr_t)&main > FAKE_MEM_BASE + FAKE_MEM_SIZE || (uintptr_t)&main < FAKE_MEM_BASE);
	// If a caller skipped the check, read() still gives nothing from outside the block: zeros, no host read
	uint8_t z[8];
	memset(z, 0xEE, sizeof z);
	vhost_part_system()->read((uint32_t)(uintptr_t)&main, z, 8);
	for (int i = 0; i < 8; i++)
		CHECK(z[i] == 0);
	memset(z, 0xEE, sizeof z);
	vhost_part_system()->read(FAKE_MEM_BASE + FAKE_MEM_SIZE - 4u, z, 8);
	for (int i = 0; i < 4; i++)
		CHECK(z[i] == fake_byte(FAKE_MEM_SIZE - 4u + (uint32_t)i));
	for (int i = 4; i < 8; i++)
		CHECK(z[i] == 0);
	vhost_part_system()->read(FAKE_MEM_BASE - 4u, z, 8);
	for (int i = 0; i < 4; i++)
		CHECK(z[i] == 0);
	for (int i = 4; i < 8; i++)
		CHECK(z[i] == fake_byte((uint32_t)i - 4u));
}

static int reboot_ends(uint8_t mode, bool *returned_false)
{
	end_reason = NULL;
	end_code = -1;
	if (setjmp(ended) == 0) {
		*returned_false = !vhost_part_system()->reboot(mode);
		return 0;
	}
	return 1;
}

static void test_reboot(void)
{
	bool ret;
	// No flash file given: the run ends with the named reason, nothing saved
	save_calls = 0;
	CHECK(reboot_ends(HAL_REBOOT_NORMAL, &ret) == 1);
	CHECK(end_reason != NULL && strcmp(end_reason, "reboot") == 0 && end_code == VHOST_REBOOT_EXIT);
	CHECK(save_calls == 0);
	// With a flash file: saved first, then the run ends
	restart_flash = "state.flash";
	CHECK(reboot_ends(HAL_REBOOT_NORMAL, &ret) == 1);
	CHECK(save_calls == 1 && strcmp(saved_path, "state.flash") == 0);
	CHECK(strcmp(end_reason, "reboot") == 0 && end_code == 4);
	// The save fails: the reboot is not armed and the run goes on
	save_ok = false;
	CHECK(reboot_ends(HAL_REBOOT_NORMAL, &ret) == 0 && ret);
	CHECK(end_reason == NULL);
	save_ok = true;
	// The ROM bootloader mode has no meaning here: not armed
	CHECK(reboot_ends(HAL_REBOOT_ROM_DFU, &ret) == 0 && ret);
	restart_flash = NULL;
}

#define RUN(t)							\
	do {							\
		int before = failures;				\
		t();						\
		printf("%s %s\n", failures == before ? "ok  " : "FAIL", #t);	\
	} while (0)

int main(void)
{
	RUN(test_info_chip);
	RUN(test_read_mem_inside);
	RUN(test_read_mem_refused);
	RUN(test_reboot);
	if (failures) {
		printf("vsystemTest: %d failures\n", failures);
		return 1;
	}
	printf("vsystemTest: PASSED\n");
	return 0;
}
