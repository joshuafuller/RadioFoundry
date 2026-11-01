// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The trace (vtrace.h). Written by whichever task thread runs (only one ever does), into one stdio buffer flushed at
// the end. No pointer, thread id or host time goes in.
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "vclock.h"
#include "vhost.h"
#include "vstall.h"
#include "vtrace.h"
#include "FreeRTOS.h"
#include "task.h"

#define STUCK_SWITCHES	2000000u	// switches with the clock standing still: something spins without blocking

static FILE *out;
static const char *path;		// --trace FILE; stdout without it
static bool log_ticks = true;		// --no-tick-log: no tick interrupt lines (for speed runs)
static uint64_t lines;
static uint64_t same_time_switches;
static uint64_t last_switch_us;

static bool set_trace(const char *value)
{
	path = value;
	return true;
}
VHOST_OPTION("--trace", "FILE", set_trace, "the trace file (default: stdout)")

static bool set_no_ticks(const char *value)
{
	(void)value;
	log_ticks = false;
	return true;
}
VHOST_OPTION("--no-tick-log", NULL, set_no_ticks, "leave the tick interrupts out of the trace")

__attribute__((weak)) void vtrace_line_hook(const char *line, size_t n)
{
	(void)line;
	(void)n;
}

#define MAX_LINE_HOOKS 8
static vtrace_line_fn line_hooks[MAX_LINE_HOOKS];
static unsigned n_line_hooks;

void vtrace_line_hook_register(vtrace_line_fn fn)
{
	if (n_line_hooks == MAX_LINE_HOOKS) {
		fputs("vhost: too many trace line hooks (raise MAX_LINE_HOOKS in platform/host/vtrace.c)\n", stderr);
		exit(2);	// at load time, before any trace: a part silently left out would hide a failure
	}
	line_hooks[n_line_hooks++] = fn;
}

void vtrace_start(void)
{
	out = path != NULL ? fopen(path, "w") : stdout;
	if (out == NULL) {
		perror(path);
		exit(2);
	}
	static char buf[1 << 20];
	setvbuf(out, buf, _IOFBF, sizeof(buf));
}

void vtrace(const char *fmt, ...)
{
	if (out == NULL)
		return;
	char line[512];
	uint32_t tick = xTaskGetSchedulerState() == taskSCHEDULER_NOT_STARTED ? 0u : (uint32_t)xTaskGetTickCount();
	int n = snprintf(line, sizeof line, "%012llu %010u ", (unsigned long long)vclock_now_us(), tick);
	va_list ap;
	va_start(ap, fmt);
	int m = vsnprintf(line + n, sizeof line - (size_t)n, fmt, ap);
	va_end(ap);
	size_t len = (size_t)n + (m < 0 ? 0u : (size_t)m);
	if (len >= sizeof line)
		len = sizeof line - 1;	// cut, never lost: the callers keep lines short (host.write splits its bytes)
	fwrite(line, 1, len, out);
	fputc('\n', out);
	lines++;
	vstall_progress();
	vtrace_line_hook(line, len);
	for (unsigned i = 0; i < n_line_hooks; i++)
		line_hooks[i](line, len);
}

void vtrace_isr(const char *name)
{
	if (!log_ticks && strcmp(name, "tick") == 0)
		return;
	vtrace("ISR %s", name);
}

uint64_t vtrace_hash(const void *data, size_t n)
{
	const uint8_t *p = data;
	uint64_t h = 0xcbf29ce484222325ull;
	while (n--) {
		h ^= *p++;
		h *= 0x100000001b3ull;
	}
	return h;
}

// Every switch-in, also a repeat of the same task: the kernel picked it again, and that is part of the run
void vtrace_switched_in(const char *name)
{
	uint64_t now = vclock_now_us();
	if (now == last_switch_us) {
		if (++same_time_switches > STUCK_SWITCHES) {
			vtrace("STUCK %llu switches at one virtual time, in %s", (unsigned long long)same_time_switches, name);
			vhost_finish("stuck: the clock stands still while tasks keep switching", 3);
		}
	} else {
		last_switch_us = now;
		same_time_switches = 0;
	}
	vstall_task(name);
	vtrace("IN %s", name);
}

void vtrace_task_create(const char *name, unsigned prio)
{
	vtrace("CREATE %s prio=%u", name, prio);
}

void vtrace_task_delete(const char *name)
{
	vtrace("DELETE %s", name);
}

void vtrace_flush(void)
{
	if (out != NULL)
		fflush(out);
}

uint64_t vtrace_lines(void)
{
	return lines;
}
