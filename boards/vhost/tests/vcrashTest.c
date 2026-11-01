// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The host platform's crash functions (platform/host/crash.c): the record's home, the address checks, and how a
// stored record ends the run. The capture (crash_commit), the board and the run's end are stand-ins here.
#define _GNU_SOURCE
#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include "hal_crash.h"
#include "crash/crash.h"

static int failures;
#define CHECK(c)							\
	do {								\
		if (!(c)) {						\
			printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);	\
			failures++;					\
		}							\
	} while (0)

// ---- stand-ins
static int rf_safe_calls;
static const char *end_reason;
static int end_code;
static jmp_buf ended;

void board_rf_safe(void)
{
	rf_safe_calls++;
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

// What the real one ends with: the record in the store, then the restart
void crash_commit(crash_record_t *r)
{
	r->magic = CRASH_MAGIC;
	*hal_crash_store() = *r;
	hal_crash_restart();
}

// Run fn; the stand-in vhost_finish ends it. Returns true when the run ended
static bool ends(void (*fn)(void))
{
	end_reason = NULL;
	if (setjmp(ended) == 0) {
		fn();
		return false;
	}
	return true;
}

static void do_restart(void)
{
	hal_crash_restart();
}

static void do_hang(void)
{
	hal_crash_hang(1234);
}

static void kind_ends(uint8_t kind, const char *reason)
{
	hal_crash_store()->kind = kind;
	CHECK(ends(do_restart));
	CHECK(end_reason != NULL && strcmp(end_reason, reason) == 0);
	CHECK(end_code == 3);
}

int main(void)
{
	// The record's home and the restart words: one place, in process memory
	crash_record_t *s = hal_crash_store();
	CHECK(s != NULL && s == hal_crash_store());
	CHECK(sizeof(crash_record_t) == 256);
	crash_boot_t *b = hal_crash_boot();
	CHECK(b != NULL && b == hal_crash_boot());
	CHECK((void *)b != (void *)s);
	CHECK(hal_crash_arch() == hal_crash_arch());
	uint32_t raw = 77;
	CHECK(hal_crash_reset_flags(&raw) == CRASH_RESET_POWER && raw == 0);
	CHECK(hal_crash_task_where(s) == 0);

	// Store and read
	memset(s, 0, sizeof *s);
	s->magic = CRASH_MAGIC;
	s->cause = 0xC0FFEE;
	memcpy(s->task, "radio", 5);
	crash_record_t *again = hal_crash_store();
	CHECK(again->magic == CRASH_MAGIC && again->cause == 0xC0FFEE && strcmp(again->task, "radio") == 0);

	// Address checks, on mappings the test makes below 4 GB (the record's address fields are 32 bits wide)
#ifdef MAP_32BIT
	const size_t page = 4096;
	uint8_t *rw = mmap(NULL, page, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_32BIT, -1, 0);
	uint8_t *rx = mmap(NULL, page, PROT_READ | PROT_EXEC, MAP_PRIVATE | MAP_ANONYMOUS | MAP_32BIT, -1, 0);
	CHECK(rw != MAP_FAILED && rx != MAP_FAILED);
	uint32_t w = (uint32_t)(uintptr_t)rw, x = (uint32_t)(uintptr_t)rx;
	CHECK(hal_crash_ram_addr(w) && hal_crash_ram_addr(w + (uint32_t)page - 1u));
	CHECK(!hal_crash_code_addr(w));		// data is not code
	CHECK(hal_crash_code_addr(x) && hal_crash_code_addr(x + (uint32_t)page - 1u));
	CHECK(!hal_crash_ram_addr(x));		// code is not writable RAM
	munmap(rw, page);			// gone: the answer follows the process, not an old read
	CHECK(!hal_crash_ram_addr(w));
	munmap(rx, page);
	CHECK(!hal_crash_code_addr(x));
#else
	printf("# vcrashTest: no MAP_32BIT here, the mapped address checks are skipped\n");
#endif
	CHECK(!hal_crash_ram_addr(0) && !hal_crash_code_addr(0));
	CHECK(!hal_crash_ram_addr(0xFFFFFFFFu) && !hal_crash_code_addr(0xFFFFFFFFu));
	CHECK(!hal_crash_ram_addr(0xFFFFF000u) && !hal_crash_code_addr(0xFFFFF000u));

	// The restart ends the run with a name for the kind of record
	kind_ends(CRASH_KIND_FAULT, "fault");
	kind_ends(CRASH_KIND_STACK, "stack overflow");
	kind_ends(CRASH_KIND_HEAP, "out of heap");
	kind_ends(CRASH_KIND_ASSERT, "assert");
	kind_ends(CRASH_KIND_HANG, "watchdog");

	// The hang: the transmitter off first, a HANG record with the time, then the run ends with reason "watchdog"
	rf_safe_calls = 0;
	CHECK(ends(do_hang));
	CHECK(rf_safe_calls >= 1);
	CHECK(end_reason != NULL && strcmp(end_reason, "watchdog") == 0);
	CHECK(hal_crash_store()->kind == CRASH_KIND_HANG && hal_crash_store()->cause == 1234);
	CHECK(hal_crash_store()->arch == hal_crash_arch());

	if (failures == 0)
		printf("# vcrashTest: PASSED\n");
	return failures != 0;
}
