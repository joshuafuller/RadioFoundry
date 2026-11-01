// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The hardware watchdog on the host (hal_watchdog.h, vwatchdog.h). It counts virtual time since the last feed. When
// the timeout passes, vwatchdog_tick ends the run with reason "watchdog" (the radio would reset; a restart uses world
// control). The feeds are counted too, so a run can check the idle task fed it (the END line).
#include "config.h"
#include "hal_watchdog.h"
#include "vclock.h"
#include "vhost.h"
#include "vwatchdog.h"
#if CONFIG_CRASH_LOG
#include "hal_crash.h"
#endif

#define WATCHDOG_TIMEOUT_MS	4000u
#define WATCHDOG_EXIT		3	// as a crash's restart (platform/host/crash.c)

static uint32_t feeds;
static bool running;
static uint64_t fed_us;		// virtual time of the last feed (or the start)

void hal_watchdog_start(void)
{
	running = true;
	fed_us = vclock_now_us();
}

void hal_watchdog_feed(void)
{
	feeds++;
	fed_us = vclock_now_us();
}

bool hal_watchdog_stop(void)
{
	running = false;
	return true;
}

uint32_t hal_watchdog_timeout_ms(void)
{
	return running ? WATCHDOG_TIMEOUT_MS : 0u;
}

uint8_t hal_watchdog_regs(uint32_t *out)
{
	(void)out;
	return 0;
}

bool hal_watchdog_test_load(const uint32_t arg[3])
{
	(void)arg;
	return false;
}

void vwatchdog_feed(void)
{
	hal_watchdog_feed();
}

uint32_t vwatchdog_feeds(void)
{
	return feeds;
}

void vwatchdog_tick(void)
{
	if (!running)
		return;
	const uint64_t gap = vclock_now_us() - fed_us;
	if (gap < (uint64_t)WATCHDOG_TIMEOUT_MS * 1000u)
		return;
#if CONFIG_CRASH_LOG
	hal_crash_hang((uint32_t)(gap / 1000u));	// the record, then the run ends with reason "watchdog"
#else
	vhost_finish("watchdog", WATCHDOG_EXIT);
#endif
}
