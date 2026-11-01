// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The trace (platform/host/vtrace.c): the run's log, one line per task switch-in (also a repeat of the same task),
// simulated interrupt, task create and delete, and call into the virtual board. Each line is
// "<virtual us, 12 digits> <kernel tick, 10 digits> <text>", for example
// "000003520000 0000003520 BOARD radio.tx 433500000". Never a pointer, a thread id or a wall time: two runs of one
// scenario give the same bytes. Payloads (a screen, a flash write) go in as vtrace_hash() of their bytes.
#ifndef VTRACE_H
#define VTRACE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// After the options (--trace, --no-tick-log): open the file (stdout by default)
void vtrace_start(void);
void vtrace(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
void vtrace_isr(const char *name);
void vtrace_flush(void);
uint64_t vtrace_lines(void);
// A fixed 64-bit content hash (FNV-1a), the same on every host. Not a CRC: data that carries its own CRC would give
// a CRC that is always the same
uint64_t vtrace_hash(const void *data, size_t n);

// Called with every trace line as written (no newline). Any number of parts register a hook with
// VTRACE_LINE_HOOK(fn); they run in registration order. vtrace_line_hook is the older single hook: a weak default
// does nothing, and at most one file may define it; prefer the registry
typedef void (*vtrace_line_fn)(const char *line, size_t n);
void vtrace_line_hook_register(vtrace_line_fn fn);
void vtrace_line_hook(const char *line, size_t n);
#define VTRACE_HOOK_CAT2(a, b) a##b
#define VTRACE_HOOK_CAT(a, b) VTRACE_HOOK_CAT2(a, b)
#define VTRACE_LINE_HOOK(fn)								\
	__attribute__((constructor)) static void VTRACE_HOOK_CAT(vtrace_hook_reg_, __LINE__)(void)	\
	{										\
		vtrace_line_hook_register(fn);						\
	}

// The kernel's trace hooks (FreeRTOSConfig.h declares these too)
void vtrace_switched_in(const char *name);
void vtrace_task_create(const char *name, unsigned prio);
void vtrace_task_delete(const char *name);

#endif
