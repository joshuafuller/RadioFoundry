// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The crash log on the host (hal_crash.h, docs/spec/crash-log.md). On the radio the record lives in memory a reset
// leaves alone. Here it lives in process memory: the run ends at a crash (hal_crash_restart), so the record is read
// by the same run (the debug link's crash view, the test) and is lost with the process. A record that survives a
// process restart is world control (hal_system_t).
//
// The record's address fields are 32 bits wide and a host pointer is 64, so a stack address is cut to its low half.
// The address checks answer for the 32-bit value read as a real address: true only when that exact address is
// mapped now (read from /proc/self/maps). So a cut address never makes crash_commit read memory that is not there.
// On a position independent build the stack and code sit above 4 GB, so the stack words and return address
// candidates stay empty; the task name, the kind and the cause are what a host record carries.
#include "config.h"

#if CONFIG_CRASH_LOG
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include "hal_crash.h"
#include "crash/crash.h"
#include "board.h"
#include "vhost.h"
#include "vtrace.h"

// A fixed id, not a chip: the Thumb bit rule of crash_stack_return (arch CM4 wants odd words) does not hold for x86
// return addresses, and CK803 (even words) is the nearest. The decoder prints "CK803S" for it; the host's records
// are told apart by their build, which is the host binary's
#define VHOST_CRASH_ARCH	CRASH_ARCH_CK803
#define VHOST_CRASH_EXIT	3	// the radio would have restarted; the run ends instead (exit code 3)

static crash_record_t store;
static crash_boot_t boot;

crash_record_t *hal_crash_store(void)
{
	return &store;
}

crash_boot_t *hal_crash_boot(void)
{
	return &boot;
}

uint8_t hal_crash_arch(void)
{
	return VHOST_CRASH_ARCH;
}

// The first start of a process: the "supply" just came up
uint8_t hal_crash_reset_flags(uint32_t *raw)
{
	*raw = 0;
	return CRASH_RESET_POWER;
}

// --- the host's own ranges: the process's mappings, read from /proc/self/maps
#define MAX_RANGES 512
typedef struct {
	uintptr_t lo, hi;
	bool write, exec;
} range_t;

static range_t ranges[MAX_RANGES];
static unsigned nranges;

static unsigned hexval(char c)
{
	if (c >= '0' && c <= '9')
		return (unsigned)(c - '0');
	if (c >= 'a' && c <= 'f')
		return (unsigned)(c - 'a' + 10);
	return 16;
}

// One line: "lo-hi rwxp ..."
static void parse_line(const char *s, size_t n)
{
	uintptr_t v[2] = { 0, 0 };
	size_t i = 0;
	for (int k = 0; k < 2; k++) {
		for (; i < n && hexval(s[i]) < 16; i++)
			v[k] = v[k] << 4 | hexval(s[i]);
		if (k == 0 && (i >= n || s[i] != '-'))
			return;
		i++;
	}
	if (i + 4 > n || nranges == MAX_RANGES || s[i] != 'r')
		return;			// not readable: no use to either check
	ranges[nranges].lo = v[0];
	ranges[nranges].hi = v[1];
	ranges[nranges].write = s[i + 1] == 'w';
	ranges[nranges].exec = s[i + 2] == 'x';
	nranges++;
}

static void refresh(void)
{
	nranges = 0;
	int fd = open("/proc/self/maps", O_RDONLY);
	if (fd < 0)
		return;
	char buf[4096];
	size_t have = 0;
	for (;;) {
		ssize_t got = read(fd, buf + have, sizeof buf - have);
		if (got <= 0)
			break;
		have += (size_t)got;
		size_t start = 0;
		for (size_t i = 0; i < have; i++) {
			if (buf[i] == '\n') {
				parse_line(buf + start, i - start);
				start = i + 1;
			}
		}
		have -= start;
		memmove(buf, buf + start, have);
		if (have == sizeof buf)
			have = 0;	// a line longer than the buffer: drop it
	}
	close(fd);
}

// Read afresh on every call: a stack or the heap may have changed since, and a stale yes would let the capture read
// memory that is gone. This runs only in a crash, so the cost does not matter
static bool mapped(uint32_t a, bool want_exec)
{
	refresh();
	for (unsigned i = 0; i < nranges; i++) {
		const range_t *r = &ranges[i];
		if (a >= r->lo && a < r->hi && (want_exec ? r->exec : r->write))
			return true;
	}
	return false;
}

bool hal_crash_ram_addr(uint32_t a)
{
	return mapped(a, false);
}

bool hal_crash_code_addr(uint32_t a)
{
	return mapped(a, true);
}

// The host port keeps no saved register context per task (a task is a thread): "couldn't be read"
uint32_t hal_crash_task_where(const void *task)
{
	(void)task;
	return 0;
}

// The run ends with a name for why, from the record the capture just wrote (the radio would restart)
static const char *reason_of(const crash_record_t *r)
{
	switch (r->kind) {
	case CRASH_KIND_FAULT:
		return "fault";
	case CRASH_KIND_STACK:
		return "stack overflow";
	case CRASH_KIND_HEAP:
		return "out of heap";
	case CRASH_KIND_ASSERT:
		return "assert";
	case CRASH_KIND_HANG:
		return "watchdog";
	default:
		return "restart";
	}
}

void hal_crash_restart(void)
{
	// The record is gone with the process, so the trace keeps what a reader needs of it
	vtrace("CRASH %s kind=%u cause=%u task=%.*s uptime_ms=%u", reason_of(&store), (unsigned)store.kind,
		(unsigned)store.cause, CRASH_TASK_LEN, store.task, (unsigned)store.uptime_ms);
	vhost_finish(reason_of(&store), VHOST_CRASH_EXIT);
}

// The hang check (core/app/watchdog.c) and the virtual hardware watchdog (vwatchdog.c) call it from the tick: the
// task the tick interrupted is the running one, and crash_commit names it
void hal_crash_hang(uint32_t ms)
{
	static crash_record_t r;
	board_rf_safe();
	memset(&r, 0, sizeof r);
	r.arch = VHOST_CRASH_ARCH;
	r.kind = CRASH_KIND_HANG;
	r.cause = ms;
	crash_commit(&r);
}

#endif
