// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Host entry: the command line options, then the radio's own main order (as platform/stm32f4/main.c), then the
// scheduler. Each option belongs to the module that uses it (VHOST_OPTION, vhost.h); --help lists them all.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "config.h"
#include "board.h"
#include "hal_watchdog.h"
#include "vhost.h"
#include "vclock.h"
#include "vport.h"
#include "vstall.h"
#include "vtrace.h"
#include "vwatchdog.h"
#include "FreeRTOS.h"
#include "task.h"

void app_start(void);	// core/app: creates the tasks

// ---- the option registry, filled by the modules' constructors before main runs
#define MAX_OPTIONS	64
static const vhost_option_t *options[MAX_OPTIONS];
static unsigned n_options;
static const char *registry_error;	// reported by main: a constructor can't print a useful error and stop
static char registry_msg[160];

void vhost_option_register(const vhost_option_t *opt)
{
	if (n_options == MAX_OPTIONS) {
		registry_error = "too many options (MAX_OPTIONS in platform/host/main.c)";
		return;
	}
	for (unsigned i = 0; i < n_options; i++) {
		if (strcmp(options[i]->name, opt->name) == 0) {
			snprintf(registry_msg, sizeof registry_msg, "option %s registered twice (\"%s\" and \"%s\")",
				opt->name, options[i]->help, opt->help);
			registry_error = registry_msg;
			return;
		}
	}
	options[n_options++] = opt;
}

static const vhost_option_t *find_option(const char *name)
{
	for (unsigned i = 0; i < n_options; i++) {
		if (strcmp(options[i]->name, name) == 0)
			return options[i];
	}
	return NULL;
}

static int by_name(const void *a, const void *b)
{
	return strcmp((*(const vhost_option_t *const *)a)->name, (*(const vhost_option_t *const *)b)->name);
}

static void usage(FILE *f)
{
	const vhost_option_t *sorted[MAX_OPTIONS];
	memcpy(sorted, options, n_options * sizeof options[0]);
	qsort(sorted, n_options, sizeof sorted[0], by_name);
	fprintf(f, "usage: rfoundry_vhost.elf [option ...]\n");
	for (unsigned i = 0; i < n_options; i++) {
		char head[48];
		snprintf(head, sizeof head, "%s%s%s", sorted[i]->name, sorted[i]->arg ? " " : "",
			sorted[i]->arg ? sorted[i]->arg : "");
		fprintf(f, "  %-24s %s\n", head, sorted[i]->help);
	}
}

// Every option to its handler, in command line order. 0 when all were taken, else the exit code
static int parse_options(int argc, char **argv)
{
	if (registry_error != NULL) {
		fprintf(stderr, "vhost: %s\n", registry_error);
		return 2;
	}
	for (int i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--help") == 0) {
			usage(stdout);
			exit(0);
		}
		const vhost_option_t *opt = find_option(argv[i]);
		if (opt == NULL) {
			fprintf(stderr, "vhost: unknown option %s (--help lists them)\n", argv[i]);
			return 2;
		}
		const char *value = NULL;
		if (opt->arg != NULL) {
			if (i + 1 >= argc) {
				fprintf(stderr, "vhost: %s needs a value (%s)\n", argv[i], opt->arg);
				return 2;
			}
			value = argv[++i];
		}
		if (!opt->set(value)) {
			fprintf(stderr, "vhost: bad value for %s: %s\n", opt->name, value != NULL ? value : "");
			return 2;
		}
	}
	return 0;
}

// ---- the end of the run
static struct timespec wall_start;	// for the speed line on stderr only; never in the trace

#define MAX_FINISH_HOOKS 8
static vhost_finish_fn finish_hooks[MAX_FINISH_HOOKS];
static unsigned n_finish_hooks;

void vhost_finish_hook_register(vhost_finish_fn fn)
{
	if (n_finish_hooks == MAX_FINISH_HOOKS) {
		fputs("vhost: too many finish hooks (raise MAX_FINISH_HOOKS in platform/host/main.c)\n", stderr);
		exit(2);
	}
	finish_hooks[n_finish_hooks++] = fn;
}

void vhost_finish(const char *why, int code)
{
	static bool finishing;	// a hook that calls vhost_finish ends the run with its reason, without the other hooks
	if (!finishing) {
		finishing = true;
		for (unsigned i = 0; i < n_finish_hooks; i++)
			finish_hooks[i]();
	}
	vstall_running(false);
	vport_stats_t st;
	vport_stats(&st);
	vtrace("END %s (watchdog feeds %u, critical sections %llu, deferred interrupts %llu, vTaskSuspendAll %llu)", why,
		(unsigned)vwatchdog_feeds(), (unsigned long long)st.criticals, (unsigned long long)st.deferred,
		(unsigned long long)st.suspends);
	vtrace_flush();
	struct timespec w;
	clock_gettime(CLOCK_MONOTONIC, &w);
	double wall = (double)(w.tv_sec - wall_start.tv_sec) + (double)(w.tv_nsec - wall_start.tv_nsec) / 1e9;
	double virt = (double)vclock_now_us() / 1e6;
	fprintf(stderr, "vhost: %s; virtual %.3f s, wall %.3f s, %.1f virtual s per wall s, %llu trace lines\n", why,
		virt, wall, wall > 0 ? virt / wall : 0.0, (unsigned long long)vtrace_lines());
	fflush(stderr);
	_exit(code);
}

// configASSERT (FreeRTOSConfig.h): the run stops with the place, the trace flushed first
void vhost_assert(const char *file, int line)
{
	vtrace("ASSERT %s:%d", file, line);
	fprintf(stderr, "vhost: assert %s:%d\n", file, line);
	vhost_finish("assert", 4);
}

// With the crash log, core/crash has its own (as platform/stm32f4/main.c leaves its hook to it)
#if !CONFIG_CRASH_LOG
void vApplicationMallocFailedHook(void);
void vApplicationMallocFailedHook(void)
{
	vhost_finish("malloc failed (the RTOS heap)", 4);
}
#endif

int main(int argc, char **argv)
{
	int code = parse_options(argc, argv);
	if (code != 0)
		return code;
	clock_gettime(CLOCK_MONOTONIC, &wall_start);
	vtrace_start();
	vclock_start();
	vstall_running(true);	// firmware code runs from here on
	vtrace("BOOT main");
	board_rf_safe();	// transmitter off before anything that could hang (docs/spec/crash-log.md 8)
#if CONFIG_WATCHDOG
	hal_watchdog_start();
#endif
	board_early_init();	// its busy waits feed (hal_delay_ms before the scheduler)
#if CONFIG_WATCHDOG
	hal_watchdog_feed();
#endif
	app_start();
#if CONFIG_WATCHDOG
	hal_watchdog_feed();	// the start-up task's first work runs before the idle task does
#endif
	vtrace("SCHEDULER start");
	vTaskStartScheduler();
	vhost_finish("vTaskStartScheduler returned", 4);
}
