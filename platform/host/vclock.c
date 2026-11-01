// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The one virtual clock (vclock.h). It drives the RTOS tick and hal_millis / hal_micros, so the two can never drift (on
// the radios they are two hardware timers). It stands still while firmware code runs (an infinitely fast CPU) and
// moves only when the idle task runs (to the next due event) or code busy-waits.
#include <stdlib.h>
#include "config.h"
#include "hal_time.h"
#include "vclock.h"
#include "vhost.h"
#include "vport.h"
#include "vstall.h"
#include "vtrace.h"
#include "vwatchdog.h"
#include "FreeRTOS.h"
#include "task.h"

#define TICK_US		(1000000u / configTICK_RATE_HZ)
#define MAX_EVENTS	256
#define STUCK_READS	50000000u	// clock reads with the clock standing still: a busy poll that never blocks

typedef struct {
	uint64_t t;
	uint32_t round;
	unsigned cls;
	unsigned source;
	uint64_t seq;
	vclock_event_fn fn;
	uint32_t arg;
} event_t;

static uint64_t now_us;
// The round being handled at now_us: the round of the last event run at this time, or -1 when none has run yet (the
// clock just moved here). An event posted for now_us joins round handled + 1 (vclock.h, the order key)
static int64_t round_handled = -1;
static event_t events[MAX_EVENTS];	// sorted by key, earliest first
static unsigned n_events;
static uint64_t seq;
static uint64_t reads_since_move;
static uint64_t until_ms = 10000;
static uint64_t horizon = VCLOCK_NEVER;
static bool drain_horizon;

static bool set_until(const char *value)
{
	char *end;
	until_ms = strtoull(value, &end, 0);
	return *value != '\0' && *end == '\0';
}
VHOST_OPTION("--until-ms", "N", set_until, "stop after N ms of virtual time (default 10000)")

uint64_t vclock_now_us(void)
{
	return now_us;
}

int64_t vclock_round_handled(void)
{
	return round_handled;
}

uint64_t vclock_next_due_us(void)
{
	return n_events > 0 ? events[0].t : VCLOCK_NEVER;
}

// The order key (vclock.h): time, then the end of the run after everything else at its time, then round, class, seq
static bool before(const event_t *a, const event_t *b)
{
	if (a->t != b->t)
		return a->t < b->t;
	if ((a->cls == VCLOCK_CLASS_END) != (b->cls == VCLOCK_CLASS_END))
		return b->cls == VCLOCK_CLASS_END;
	if (a->round != b->round)
		return a->round < b->round;
	if (a->cls != b->cls)
		return a->cls < b->cls;
	return a->seq < b->seq;
}

void vclock_post_stamped(uint64_t t_us, uint32_t round, unsigned cls, unsigned source, uint64_t sequence,
	vclock_event_fn fn, uint32_t arg)
{
	if (t_us < now_us) {
		// Moving it to now would hide a cause and effect bug: the event would run after things it should precede
		vtrace("PAST event due at %llu us, class %u, source %u, with the clock at %llu us",
			(unsigned long long)t_us, cls, source, (unsigned long long)now_us);
		vhost_finish("event due in the past", 4);
	}
	if (n_events == MAX_EVENTS) {
		vhost_finish("event queue full", 2);
	}
	event_t e = { t_us, round, cls, source, sequence, fn, arg };
	unsigned i = n_events++;
	while (i > 0 && before(&e, &events[i - 1])) {
		events[i] = events[i - 1];
		i--;
	}
	events[i] = e;
}

void vclock_post(uint64_t t_us, unsigned cls, unsigned source, vclock_event_fn fn, uint32_t arg)
{
	vclock_post_stamped(t_us, t_us == now_us ? (uint32_t)(round_handled + 1) : 0u, cls, source, seq++, fn, arg);
}

void vclock_at(uint64_t t, unsigned cls, vclock_event_fn fn, uint32_t arg)
{
	vclock_post(t, cls, VCLOCK_SOURCE_RADIO, fn, arg);
}

static void move_to(uint64_t t)
{
	if (t > now_us) {
		now_us = t;
		round_handled = -1;	// nothing at the new time has run yet
		reads_since_move = 0;
		vstall_progress();
	}
}

void vclock_set_horizon(uint64_t t_us)
{
	if (t_us < now_us) {
		vtrace("PAST horizon %llu us with the clock at %llu us", (unsigned long long)t_us,
			(unsigned long long)now_us);
		vhost_finish("horizon in the past", 4);
	}
	horizon = t_us;
	drain_horizon = false;
}

void vclock_drain_horizon(void)
{
	if (horizon != now_us || horizon == VCLOCK_NEVER)
		vhost_finish("drain away from horizon", 4);
	drain_horizon = true;
}

__attribute__((weak)) void vclock_horizon_reached(uint64_t at_us)
{
	(void)at_us;
	vhost_finish("virtual time limit", 0);
}

void vclock_advance_to(uint64_t t)
{
	for (;;) {
		bool at_horizon = horizon <= t;
		uint64_t stop = at_horizon ? horizon : t;
		// A drain includes the horizon, also in nested busy waits, but never lets those waits pass it.
		if (n_events > 0 && (events[0].t < stop ||
			(events[0].t == stop && (!at_horizon || drain_horizon)))) {
			event_t e = events[0];
			n_events--;
			for (unsigned i = 0; i < n_events; i++)
				events[i] = events[i + 1];
			move_to(e.t);
			round_handled = e.round;
			e.fn(e.arg);	// may run interrupts and switch tasks; this thread carries on when it runs again
			continue;
		}
		move_to(stop);
		if (!at_horizon)
			return;
		drain_horizon = false;
		vclock_horizon_reached(now_us);
		if (horizon <= now_us && !drain_horizon)
			vhost_finish("horizon not moved", 4);
	}
}

static void end_event(uint32_t arg)
{
	(void)arg;
	vhost_finish("virtual time limit", 0);
}

void vclock_start(void)
{
	vclock_at(until_ms * 1000u, VCLOCK_CLASS_END, end_event, 0);
}

static void tick_event(uint32_t arg)
{
	(void)arg;
	vclock_at(now_us + TICK_US, VCLOCK_CLASS_TICK, tick_event, 0);	// the next one first: the handler may switch
	vhost_irq_raise(VHOST_IRQ_TICK);
}

void vclock_tick_start(void)
{
	vhost_irq_register(VHOST_IRQ_TICK, "tick", vtick_isr);
	vclock_at(now_us + TICK_US, VCLOCK_CLASS_TICK, tick_event, 0);
}

// The idle task: nothing can run until the next hardware event, so jump to it (or to the horizon, if that is sooner)
static void idle_advance(void)
{
	if (n_events == 0 && horizon == VCLOCK_NEVER)
		vhost_finish("idle with nothing left to happen", 3);
	vclock_advance_to(n_events > 0 ? events[0].t : horizon);
}

// The firmware's idle hook (core/app/watchdog.c with CONFIG_WATCHDOG), then the jump. Linked with
// -Wl,--wrap=vApplicationIdleHook (platform.mk) so the firmware's file is unchanged
void __real_vApplicationIdleHook(void);
void __wrap_vApplicationIdleHook(void);
void __wrap_vApplicationIdleHook(void)
{
	__real_vApplicationIdleHook();
	idle_advance();
}
#if !CONFIG_WATCHDOG
void vApplicationIdleHook(void);
void vApplicationIdleHook(void)
{
}
#endif

static void count_read(void)
{
	if (++reads_since_move > STUCK_READS) {
		vtrace("STUCK %u clock reads with the clock standing still, in %s", STUCK_READS,
			xTaskGetSchedulerState() == taskSCHEDULER_NOT_STARTED ? "main" : pcTaskGetName(NULL));
		vhost_finish("stuck: a busy poll on the clock that never blocks", 3);
	}
}

uint32_t hal_millis(void)
{
	count_read();
	return (uint32_t)(now_us / 1000u);
}

uint32_t hal_micros(void)
{
	count_read();
	return (uint32_t)now_us;
}

// A busy wait: time passes, and with interrupts on the tick (and so other tasks) can run inside it, as on the radio
void hal_delay_us(uint32_t us)
{
	vclock_advance_to(now_us + us);
}

// As platform/stm32f4/time.c: a task sleep once the scheduler runs; a busy wait before it (feeding the watchdog each
// ms, no idle task yet) or while it is suspended
void hal_delay_ms(uint32_t ms)
{
	if (xTaskGetSchedulerState() == taskSCHEDULER_RUNNING) {
		vTaskDelay(pdMS_TO_TICKS(ms));
	} else if (xTaskGetSchedulerState() == taskSCHEDULER_NOT_STARTED) {
		for (uint32_t i = 0; i < ms; i++) {
			hal_delay_us(1000u);
			vwatchdog_feed();
		}
	} else {
		hal_delay_us(ms * 1000u);
	}
}
