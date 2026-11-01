// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The tick interrupt's body (vclock.h). The clock raises it every 1000 us of virtual time once the scheduler runs.
#include "config.h"
#include "vclock.h"
#include "vwatchdog.h"
#include "FreeRTOS.h"
#include "task.h"
#if CONFIG_WATCHDOG && CONFIG_CRASH_LOG
#include "app/watchdog.h"
#endif

void vtick_isr(void)
{
#if CONFIG_WATCHDOG && CONFIG_CRASH_LOG
	watchdog_tick();	// the hang check first, as platform/stm32f4/time.c SysTick_Handler
#endif
	vwatchdog_tick();	// then the virtual hardware watchdog: the reset the hang check could not prevent
	if (xTaskIncrementTick() != pdFALSE) {
		portYIELD_FROM_ISR(pdTRUE);
	}
}

void vApplicationTickHook(void);
void vApplicationTickHook(void)
{
}
