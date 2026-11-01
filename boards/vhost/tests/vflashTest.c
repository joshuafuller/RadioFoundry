// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Host test of the virtual board's sparse NOR flash (boards/vhost/flash_vhost.c): erase, clear-only writes, the
// flash.set_bit trace line, memory held only for sectors that hold something, and save and load across a restart.
// The model's file is included so its statics can be read; vtrace is a small recorder here.
#include <stdarg.h>
#include <stdio.h>
#include <setjmp.h>
#include <sys/stat.h>
#include "../flash_vhost.c"

static const vhost_option_t *restore_option;	// stays NULL when the build strips __attribute__ (tests/host)
static jmp_buf ended;
static int end_code;

void vhost_option_register(const vhost_option_t *opt)
{
	if (strcmp(opt->name, "--restore-flash") == 0)
		restore_option = opt;
}

void vhost_finish(const char *why, int code)
{
	(void)why;
	end_code = code;
	longjmp(ended, 1);
}

static char lines[64][160];
static int nlines;

void vtrace(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	if (nlines < 64)
		vsnprintf(lines[nlines++], sizeof(lines[0]), fmt, ap);
	va_end(ap);
}

uint64_t vtrace_hash(const void *data, size_t n)	// the same FNV-1a as platform/host/vtrace.c
{
	uint64_t h = 0xcbf29ce484222325ull;
	for (size_t i = 0; i < n; i++)
		h = (h ^ ((const uint8_t *)data)[i]) * 0x100000001b3ull;
	return h;
}

static int failures;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); failures++; } } while (0)

static int count_lines(const char *what)
{
	int n = 0;
	for (int i = 0; i < nlines; i++)
		if (strstr(lines[i], what))
			n++;
	return n;
}

static void fresh(void)
{
	vboard_flash_boot();
	nlines = 0;
}

static void test_blank_and_erase(void)
{
	fresh();
	uint8_t b[16];
	CHECK(vboard_flash.read(0x2000, b, 16));
	for (int i = 0; i < 16; i++)
		CHECK(b[i] == 0xFF);
	CHECK(flash_vhost_sectors_held() == 0);
	uint8_t d[4] = { 1, 2, 3, 4 };
	CHECK(vboard_flash.write(0x3000, d, 4));
	CHECK(vboard_flash.erase(0x3000));
	CHECK(vboard_flash.read(0x3000, b, 4));
	CHECK(b[0] == 0xFF && b[3] == 0xFF);
	CHECK(flash_vhost_sectors_held() == 0);	// erase gives the memory back
	CHECK(!vboard_flash.erase(0x3001));	// not on a sector start
	CHECK(!vboard_flash.read(FLASH_SIZE - 2, b, 4));	// past the end
}

static void test_write_clears_bits(void)
{
	fresh();
	uint8_t a = 0xF0, b = 0x3C, r;
	CHECK(vboard_flash.write(0x5000, &a, 1));
	CHECK(vboard_flash.write(0x5000, &b, 1));
	CHECK(vboard_flash.read(0x5000, &r, 1));
	CHECK(r == (0xF0 & 0x3C));
	CHECK(count_lines("flash.set_bit") == 1);	// 0x3C wants bits 2,3 back to 1: ANDed, and named once
	CHECK(count_lines("flash.write") == 2);
}

static void test_set_bit_line(void)
{
	fresh();
	uint8_t z = 0x00, f[2] = { 0xFF, 0xFF }, r[2];
	CHECK(vboard_flash.write(0x6000, &z, 1));
	nlines = 0;
	CHECK(vboard_flash.write(0x6000, f, 2));	// byte 0 wants 0->1; byte 1 is fresh
	CHECK(count_lines("flash.set_bit 006000+2") == 1);
	CHECK(vboard_flash.read(0x6000, r, 2));
	CHECK(r[0] == 0x00 && r[1] == 0xFF);	// not an error: stays 0x00
	fresh();
	CHECK(vboard_flash.write(0x7000, f, 2));	// 0xFF padding over erased flash: no set_bit, and no memory
	CHECK(count_lines("flash.set_bit") == 0);
	CHECK(flash_vhost_sectors_held() == 0);
}

static void test_sparse(void)
{
	fresh();
	CHECK(flash_vhost_sectors_held() == 0);
	uint8_t d = 0x12;
	CHECK(vboard_flash.write(0x011000, &d, 1));
	CHECK(vboard_flash.write(0xE00010, &d, 1));
	CHECK(flash_vhost_sectors_held() == 2);	// 2 of 4096 sectors
	uint8_t span[8] = { 1, 1, 1, 1, 2, 2, 2, 2 }, r[8];
	CHECK(vboard_flash.write(0x1FFC, span, 8));	// across a sector edge
	CHECK(flash_vhost_sectors_held() == 4);
	CHECK(vboard_flash.read(0x1FFC, r, 8) && memcmp(r, span, 8) == 0);
}

static void test_hash_by_content(void)
{
	fresh();
	uint8_t a[4] = { 1, 2, 3, 4 }, b[4] = { 1, 2, 3, 5 };
	CHECK(vboard_flash.write(0x8000, a, 4));
	CHECK(vboard_flash.write(0x9000, b, 4));
	CHECK(nlines == 2 && strcmp(strstr(lines[0], "hash="), strstr(lines[1], "hash=")) != 0);	// different bytes, different hash
	char h[17];
	snprintf(h, sizeof(h), "%016llx", (unsigned long long)vtrace_hash(a, 4));
	CHECK(strstr(lines[0], h) != NULL);
}

static void test_preload_and_save_load(void)
{
	fresh();
	uint8_t d[3] = { 9, 8, 7 }, r[3];
	CHECK(vflash_load(0x10FFF, d, 3));	// crosses a sector edge, no trace line
	CHECK(nlines == 0);
	CHECK(!vflash_load(FLASH_SIZE - 1, d, 3));
	uint8_t w = 0x5A;
	CHECK(vboard_flash.write(0xC00000, &w, 1));
	const char *path = "vflashTest.img.tmpfile";
	unlink(path);
	CHECK(flash_vhost_save(path));
	struct stat st;
	CHECK(stat(path, &st) == 0 && (st.st_mode & 0777) == 0600);
	uint32_t held = flash_vhost_sectors_held();
	fresh();
	CHECK(flash_vhost_sectors_held() == 0);
	CHECK(flash_vhost_load(path));
	CHECK(flash_vhost_sectors_held() == held);
	CHECK(vboard_flash.read(0x10FFF, r, 3) && memcmp(r, d, 3) == 0);
	CHECK(vboard_flash.read(0xC00000, r, 1) && r[0] == 0x5A);
	// a bad file leaves the flash as it was
	FILE *f = fopen(path, "wb");
	fwrite("NOTFLASH", 1, 8, f);
	fclose(f);
	CHECK(!flash_vhost_load(path));
	CHECK(flash_vhost_sectors_held() == held);
	CHECK(!flash_vhost_load("vflashTest.no-such-file"));
	unlink(path);
}

static void test_restore_option(void)
{
	fresh();
	CHECK(restore_option == NULL || strcmp(restore_option->name, "--restore-flash") == 0);
	uint8_t d[3] = { 1, 2, 3 }, r[3];
	CHECK(vflash_load(0x20000, d, 3));
	const char *path = "vflashTest.restore.tmpfile";
	unlink(path);
	CHECK(flash_vhost_save(path));
	// A new run with the option starts from the saved flash, not from blank
	CHECK(set_restore_flash(path));
	vboard_flash_boot();
	CHECK(vboard_flash.read(0x20000, r, 3) && memcmp(r, d, 3) == 0);
	// A bad file ends the run with exit code 2; it never comes back blank
	CHECK(set_restore_flash("vflashTest.no-such-file"));
	end_code = 0;
	if (setjmp(ended) == 0)
		vboard_flash_boot();
	CHECK(end_code == 2);
	CHECK(set_restore_flash(NULL));	// later tests boot blank again
	fresh();
	CHECK(vboard_flash.read(0x20000, r, 3) && r[0] == 0xFF);
	unlink(path);
}

static void test_write_through(void)
{
	const char *path = "vflashTest.powercut.tmpfile";
	const char *tmp = "vflashTest.powercut.tmpfile.tmp";
	CHECK(set_flash_file(path));
	fresh();
	struct stat st;
	CHECK(stat(path, &st) == 0 && (st.st_mode & 0777) == 0600 && st.st_size == 8);
	uint8_t data[2] = { 0xF0, 0x0F }, read[2];
	CHECK(vboard_flash.write(ERASE - 1, data, sizeof data));
	drop_all();	// Lose RAM without an orderly save.
	CHECK(flash_vhost_load(path));
	CHECK(vboard_flash.read(ERASE - 1, read, sizeof read) && memcmp(data, read, sizeof data) == 0);
	uint8_t bits = 0x3C;
	CHECK(vboard_flash.write(ERASE - 1, &bits, 1));
	drop_all();
	CHECK(flash_vhost_load(path));
	CHECK(vboard_flash.read(ERASE - 1, read, sizeof read) && read[0] == 0x30 && read[1] == 0x0F);
	CHECK(vboard_flash.erase(0));
	drop_all();
	CHECK(flash_vhost_load(path));
	CHECK(vboard_flash.read(ERASE - 1, read, sizeof read) && read[0] == 0xFF && read[1] == 0x0F);
	// An incomplete replacement cannot hide the last completed operation.
	FILE *f = fopen(tmp, "wb");
	CHECK(f != NULL);
	if (f) {
		CHECK(fwrite("RFF", 1, 3, f) == 3);
		CHECK(fclose(f) == 0);
	}
	CHECK(set_restore_flash(path));
	vboard_flash_boot();
	CHECK(vboard_flash.read(ERASE - 1, read, sizeof read) && read[0] == 0xFF && read[1] == 0x0F);
	// A host write failure ends the run before the operation can report success.
	CHECK(mkdir(tmp, 0700) == 0);
	end_code = 0;
	if (setjmp(ended) == 0) {
		vboard_flash.erase(ERASE);
		CHECK(false);
	}
	CHECK(end_code == 2);
	drop_all();
	CHECK(flash_vhost_load(path));
	CHECK(vboard_flash.read(ERASE, read, 1) && read[0] == 0x0F);
	CHECK(rmdir(tmp) == 0);
	CHECK(set_restore_flash(NULL));
	CHECK(set_flash_file(NULL));
	unlink(path);
	puts("write-through: program, NOR bits, erase, incomplete file and save failure: PASSED");
}

int main(void)
{
	test_blank_and_erase();
	test_write_clears_bits();
	test_set_bit_line();
	test_sparse();
	test_hash_by_content();
	test_preload_and_save_load();
	test_restore_option();
	test_write_through();
	if (failures) {
		printf("vflashTest: %d FAILED\n", failures);
		return 1;
	}
	printf("vflashTest: PASSED\n");
	return 0;
}
