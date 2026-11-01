// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The host platform's watchdog (platform/host/vwatchdog.c). The hal_watchdog_* functions are in hal/hal_watchdog.h;
// these are the platform's own.
#ifndef VWATCHDOG_H
#define VWATCHDOG_H

#include <stdint.h>

// The busy wait before the scheduler feeds the watchdog (no idle task yet), as platform/stm32f4/time.c hal_delay_ms
void vwatchdog_feed(void);
// Feeds so far, for the END line
uint32_t vwatchdog_feeds(void);

// From the tick interrupt (vtick.c): the virtual hardware watchdog. When it has run (hal_watchdog_start) for its
// timeout of virtual time without a feed, the radio would reset: the run ends with reason "watchdog" (with the crash
// log, after a hang record naming the task the tick interrupted)
void vwatchdog_tick(void);

#endif
