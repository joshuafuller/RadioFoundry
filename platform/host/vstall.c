// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The stall watch (vstall.h), moved from the startup spike. If no progress was published for STALL_S wall seconds while
// the radio runs firmware, the firmware spins in plain C with no call into the virtual radio, so the watch says in
// which task and ends the run as "stuck". It never changes a run that makes progress.
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "vhost.h"
#include "vstall.h"
#include "vtrace.h"
#include "FreeRTOS.h"

#define STALL_DEFAULT_S	5
#define STALL_MAX_S	3600

static bool watch = true;	// --no-stall-watch and --debugger: off
static unsigned budget_s = STALL_DEFAULT_S;
static atomic_ullong progress;
static atomic_bool running;
static pthread_mutex_t name_lock = PTHREAD_MUTEX_INITIALIZER;
static char task_name[configMAX_TASK_NAME_LEN + 1] = "main";
// Only the watch thread (or the one test thread that calls vstall_tick) touches these two
static unsigned long long last_progress;
static unsigned still_s;

static bool set_no_watch(const char *value)
{
	(void)value;
	watch = false;
	return true;
}
VHOST_OPTION("--no-stall-watch", NULL, set_no_watch, "no wall-clock stall watch thread")

// A radio started with the debugger option may stop at a breakpoint, which looks like a stall. So the watch is off
static bool set_debugger(const char *value)
{
	(void)value;
	watch = false;
	return true;
}
VHOST_OPTION("--debugger", NULL, set_debugger, "a debugger is attached: the stall watch is off")

static bool set_stall_s(const char *value)
{
	char *end;
	unsigned long n = strtoul(value, &end, 10);
	if (*value == '\0' || *end != '\0' || n < 1 || n > STALL_MAX_S)
		return false;
	budget_s = (unsigned)n;
	return true;
}
VHOST_OPTION("--stall-s", "N", set_stall_s, "wall seconds without progress before the run ends as stuck (default 5)")

void vstall_configure(unsigned budget, bool on)
{
	budget_s = budget;
	watch = on;
	still_s = 0;
	last_progress = atomic_load(&progress);
}

void vstall_task(const char *name)
{
	pthread_mutex_lock(&name_lock);
	strncpy(task_name, name, configMAX_TASK_NAME_LEN);
	task_name[configMAX_TASK_NAME_LEN] = '\0';
	pthread_mutex_unlock(&name_lock);
}

void vstall_progress(void)
{
	atomic_fetch_add_explicit(&progress, 1, memory_order_relaxed);
}

void vstall_running(bool on)
{
	atomic_store(&running, on);
}

bool vstall_tick(void)
{
	unsigned long long p = atomic_load(&progress);
	// Waiting (not running) or moving: start counting again. A radio stopped for a grant gate is not stuck
	if (!watch || p != last_progress || !atomic_load(&running)) {
		still_s = 0;
		last_progress = p;
		return false;
	}
	return ++still_s >= budget_s;
}

void vstall_task_name(char *out, size_t cap)
{
	pthread_mutex_lock(&name_lock);
	snprintf(out, cap, "%s", task_name);
	pthread_mutex_unlock(&name_lock);
}

static void *stall_watch(void *arg)
{
	(void)arg;
	for (;;) {
		sleep(1);
		if (vstall_tick()) {
			char name[sizeof task_name];
			static char why[sizeof task_name + 80];
			vstall_task_name(name, sizeof name);
			snprintf(why, sizeof why, "stuck: task %s ran %u wall s without calling the virtual radio", name,
				budget_s);
			vhost_finish(why, 5);
		}
	}
	return NULL;
}

void vstall_start(void)
{
	if (watch) {
		pthread_t w;
		last_progress = atomic_load(&progress);
		pthread_create(&w, NULL, stall_watch, NULL);
	}
}
