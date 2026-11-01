// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual clock (platform/host/vclock.c) alone, against the time model's rules (docs/dev/virtual-radio-architecture.md,
// "Order of events" and "What a wait means"; ):
//	1. the order key: time, round, class, sequence;
//	2. an event due at a past time ends the run with a named reason;
//	3. reading the clock never moves it; hal_millis is us / 1000, hal_micros is us cut to 32 bits;
//	4. the waits: hal_delay_ms is vTaskDelay once the scheduler runs, else a busy wait (feeding the watchdog before
//	   the scheduler); hal_delay_us is a busy wait; a busy wait runs the events due in its span;
//	5. the end of the run comes only after every round at its time;
// and the world control hooks: the horizon and vclock_post.
// The RTOS, the port and the rest of the host platform are stubs here, so each case sees only the clock. Each case
// runs in a child process of its own, so it starts from the clock's power-on state.
#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#include "hal_time.h"
#include "vclock.h"
#include "vhost.h"
#include "vport.h"
#include "vstall.h"
#include "vtrace.h"
#include "vwatchdog.h"
#include "FreeRTOS.h"
#include "task.h"

// ---- Stubs: what the clock calls --------------------------------------------------------------------------------

static const vhost_option_t *until_opt;
void vhost_option_register(const vhost_option_t *opt)
{
	if (strcmp(opt->name, "--until-ms") == 0)
		until_opt = opt;
}

static jmp_buf finish_jmp;
static const char *finish_why;
static int finish_code = -1;
void vhost_finish(const char *why, int code)
{
	finish_why = why;
	finish_code = code;
	longjmp(finish_jmp, 1);
}

static vhost_isr_t isrs[VHOST_IRQ_COUNT];
void vhost_irq_register(unsigned irq, const char *name, vhost_isr_t isr)
{
	(void)name;
	isrs[irq] = isr;
}
void vhost_irq_raise(unsigned irq)
{
	if (isrs[irq])
		isrs[irq]();	// interrupts on: the handler runs now
}

void vstall_progress(void)
{
}

void vtrace(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	printf("    trace: ");
	vprintf(fmt, ap);
	printf("\n");
	va_end(ap);
}

static unsigned feeds;
void vwatchdog_feed(void)
{
	feeds++;
}

static BaseType_t sched_state = taskSCHEDULER_NOT_STARTED;
BaseType_t xTaskGetSchedulerState(void)
{
	return sched_state;
}

static int delay_calls;
static TickType_t delay_ticks;
void vTaskDelay(const TickType_t ticks)
{
	delay_calls++;
	delay_ticks = ticks;
}

char *pcTaskGetName(TaskHandle_t task)
{
	(void)task;
	return "test";
}

void __real_vApplicationIdleHook(void);
void __real_vApplicationIdleHook(void)
{
}

// ---- The log: what ran, in order ---------------------------------------------------------------------------------

#define LOG_MAX 64
static char log_names[LOG_MAX][24];
static uint64_t log_at[LOG_MAX];
static int64_t log_round[LOG_MAX];
static unsigned n_log;

static void log_event(const char *name)
{
	if (n_log < LOG_MAX) {
		snprintf(log_names[n_log], sizeof log_names[n_log], "%s", name);
		log_at[n_log] = vclock_now_us();
		log_round[n_log] = vclock_round_handled();
		n_log++;
	}
}

// The tick interrupt: logged, as the kernel's tick would run here
void vtick_isr(void)
{
	log_event("tick");
}

static void ev_named(uint32_t arg);

// Names for events by arg
static const char *const names[] = { "world", "air", "input", "own", "tick50", "end", "child", "grandchild",
	"key", "in-span", "after-span", "at-horizon", "before-horizon", "stamped" };
enum { N_WORLD, N_AIR, N_INPUT, N_OWN, N_TICK50, N_END, N_CHILD, N_GRANDCHILD, N_KEY, N_IN_SPAN, N_AFTER_SPAN,
	N_AT_HORIZON, N_BEFORE_HORIZON, N_STAMPED };

static void ev_named(uint32_t arg)
{
	log_event(names[arg]);
}

// The end of the run as the clock queues it, but logged instead of ended, so a case can see what ran after it
static void ev_end(uint32_t arg)
{
	(void)arg;
	log_event("end");
}

static int expect_log(const char *const *want, unsigned n)
{
	int ok = n_log == n;
	for (unsigned i = 0; ok && i < n; i++)
		ok = strcmp(log_names[i], want[i]) == 0;
	if (!ok) {
		printf("    ran:");
		for (unsigned i = 0; i < n_log; i++)
			printf(" %s@%llu", log_names[i], (unsigned long long)log_at[i]);
		printf("\n    want:");
		for (unsigned i = 0; i < n; i++)
			printf(" %s", want[i]);
		printf("\n");
	}
	return ok;
}

#define CHECK(cond)	do { if (!(cond)) { printf("    FAIL line %d: %s\n", __LINE__, #cond); return 1; } } while (0)

// ---- Rule 1: the order key ---------------------------------------------------------------------------------------

// Two events at one time with classes 50 and 30: 30 runs first, whatever the queue order
static int rule1_class_order(void)
{
	vclock_at(2000000, VCLOCK_CLASS_TICK, ev_named, N_TICK50);
	vclock_at(2000000, VCLOCK_CLASS_INPUT, ev_named, N_INPUT);
	vclock_advance_to(2000000);
	static const char *const want[] = { "input", "tick50" };
	return !expect_log(want, 2);
}

// Every class in its place, and one class in queue order
static int rule1_all_classes(void)
{
	vclock_at(5000, VCLOCK_CLASS_TICK, ev_named, N_TICK50);
	vclock_at(5000, VCLOCK_CLASS_OWN, ev_named, N_OWN);
	vclock_at(5000, VCLOCK_CLASS_INPUT, ev_named, N_INPUT);
	vclock_at(5000, VCLOCK_CLASS_INPUT, ev_named, N_KEY);
	vclock_at(5000, VCLOCK_CLASS_AIR, ev_named, N_AIR);
	vclock_at(5000, VCLOCK_CLASS_WORLD, ev_named, N_WORLD);
	vclock_at(4999, VCLOCK_CLASS_TICK, ev_named, N_BEFORE_HORIZON);	// earlier time first, whatever the class
	vclock_advance_to(5000);
	static const char *const want[] = { "before-horizon", "world", "air", "input", "key", "own", "tick50" };
	return !expect_log(want, 7);
}

// An event made at T while T runs, due at T, runs after the tick queued before it: its round is 1, the tick's 0.
// Without the round its class (30) would put it before the tick (50), before the cause that made it could matter
static void ev_world_makes_input(uint32_t arg)
{
	(void)arg;
	log_event("world");
	vclock_at(vclock_now_us(), VCLOCK_CLASS_INPUT, ev_named, N_CHILD);
}

static int rule1_round(void)
{
	vclock_tick_start();					// the tick at 1000 us, round 0, class 50
	vclock_at(1000, VCLOCK_CLASS_WORLD, ev_world_makes_input, 0);	// round 0, class 10: runs first
	vclock_advance_to(1000);
	static const char *const want[] = { "world", "tick", "child" };
	return !expect_log(want, 3);
}

// Rounds nest: an event of round 1 that makes one due at T makes round 2, after every round 1 event
static void ev_child_makes_grandchild(uint32_t arg)
{
	(void)arg;
	log_event("child");
	vclock_at(vclock_now_us(), VCLOCK_CLASS_WORLD, ev_named, N_GRANDCHILD);
}

static void ev_world_makes_child(uint32_t arg)
{
	(void)arg;
	log_event("world");
	vclock_at(vclock_now_us(), VCLOCK_CLASS_TICK, ev_child_makes_grandchild, 0);
	vclock_at(vclock_now_us(), VCLOCK_CLASS_OWN, ev_named, N_OWN);
}

static int rule1_rounds_nest(void)
{
	vclock_at(300, VCLOCK_CLASS_WORLD, ev_world_makes_child, 0);
	vclock_at(300, VCLOCK_CLASS_TICK, ev_named, N_TICK50);
	vclock_advance_to(300);
	// round 0: world, tick50; round 1: own (40) before child (50); round 2: grandchild
	static const char *const want[] = { "world", "tick50", "own", "child", "grandchild" };
	return !expect_log(want, 5);
}

// An event made at T for a later time is round 0 there: it keeps its class place against events already queued
static void ev_makes_later(uint32_t arg)
{
	(void)arg;
	log_event("world");
	vclock_at(vclock_now_us() + 10, VCLOCK_CLASS_INPUT, ev_named, N_INPUT);
}

static int rule1_later_is_round0(void)
{
	vclock_at(110, VCLOCK_CLASS_TICK, ev_named, N_TICK50);
	vclock_at(100, VCLOCK_CLASS_WORLD, ev_makes_later, 0);
	vclock_advance_to(200);
	static const char *const want[] = { "world", "input", "tick50" };
	return !expect_log(want, 3);
}

// ---- Rule 2: a past event ends the run ----------------------------------------------------------------------------

static void ev_posts_past(uint32_t arg)
{
	(void)arg;
	log_event("world");
	vclock_at(vclock_now_us() - 1, VCLOCK_CLASS_INPUT, ev_named, N_INPUT);
}

static int rule2_past_ends_run(void)
{
	vclock_at(500, VCLOCK_CLASS_WORLD, ev_posts_past, 0);
	if (setjmp(finish_jmp) == 0) {
		vclock_advance_to(1000);
		printf("    FAIL: the past event did not end the run\n");
		return 1;
	}
	printf("    the run ended: \"%s\", exit %d\n", finish_why, finish_code);
	CHECK(finish_code != 0);
	CHECK(strcmp(finish_why, "event due in the past") == 0);
	CHECK(vclock_now_us() == 500);		// not moved to "now" and run
	static const char *const want[] = { "world" };
	return !expect_log(want, 1);
}

// ---- Rule 3: reading the clock ------------------------------------------------------------------------------------

static int rule3_reads(void)
{
	vclock_advance_to(1234567);
	for (int i = 0; i < 1000; i++) {
		CHECK(hal_millis() == 1234);
		CHECK(hal_micros() == 1234567u);
		CHECK(vclock_now_us() == 1234567);
	}
	// Past 2^32 us (71.6 minutes): hal_micros wraps as the 32-bit timer does, hal_millis does not
	uint64_t t = (1ull << 32) + 5000123u;
	vclock_advance_to(t);
	CHECK(hal_micros() == 5000123u);
	CHECK(hal_millis() == (uint32_t)(t / 1000u));
	CHECK(vclock_now_us() == t);
	return 0;
}

// ---- Rule 4: the waits --------------------------------------------------------------------------------------------

static int rule4_delay_ms_running(void)
{
	sched_state = taskSCHEDULER_RUNNING;
	vclock_at(500, VCLOCK_CLASS_INPUT, ev_named, N_IN_SPAN);
	hal_delay_ms(7);
	CHECK(delay_calls == 1);
	CHECK(delay_ticks == pdMS_TO_TICKS(7));
	CHECK(vclock_now_us() == 0);	// the task sleeps; the clock moves only when every task waits
	CHECK(n_log == 0);
	CHECK(feeds == 0);
	return 0;
}

static int rule4_delay_ms_before_scheduler(void)
{
	sched_state = taskSCHEDULER_NOT_STARTED;
	vclock_at(1500, VCLOCK_CLASS_INPUT, ev_named, N_IN_SPAN);
	vclock_at(3000, VCLOCK_CLASS_INPUT, ev_named, N_IN_SPAN);	// at the span's end: runs
	vclock_at(3001, VCLOCK_CLASS_INPUT, ev_named, N_AFTER_SPAN);
	hal_delay_ms(3);
	CHECK(delay_calls == 0);
	CHECK(vclock_now_us() == 3000);
	CHECK(feeds == 3);		// once a ms, as platform/stm32f4/time.c does at start-up
	static const char *const want[] = { "in-span", "in-span" };
	CHECK(expect_log(want, 2));
	CHECK(log_at[0] == 1500 && log_at[1] == 3000);	// each ran at its own time, not at the wait's end
	return 0;
}

static int rule4_delay_ms_suspended(void)
{
	sched_state = taskSCHEDULER_SUSPENDED;
	vclock_at(2000, VCLOCK_CLASS_INPUT, ev_named, N_IN_SPAN);
	hal_delay_ms(4);
	CHECK(delay_calls == 0);
	CHECK(vclock_now_us() == 4000);
	CHECK(feeds == 0);		// the idle task exists; it feeds
	static const char *const want[] = { "in-span" };
	return !expect_log(want, 1);
}

static int rule4_delay_us(void)
{
	sched_state = taskSCHEDULER_RUNNING;
	vclock_tick_start();		// ticks at 1000, 2000, ...
	hal_delay_us(2500);
	CHECK(delay_calls == 0);
	CHECK(vclock_now_us() == 2500);
	static const char *const want[] = { "tick", "tick" };
	CHECK(expect_log(want, 2));
	CHECK(log_at[0] == 1000 && log_at[1] == 2000);
	return 0;
}

// The idle jump goes to the next due event, no further
static int rule4_idle_jump(void)
{
	void __wrap_vApplicationIdleHook(void);
	vclock_at(4200, VCLOCK_CLASS_INPUT, ev_named, N_INPUT);
	vclock_at(9000, VCLOCK_CLASS_INPUT, ev_named, N_AFTER_SPAN);
	__wrap_vApplicationIdleHook();
	CHECK(vclock_now_us() == 4200);
	static const char *const want[] = { "input" };
	return !expect_log(want, 1);
}

// ---- Rule 5: the end of the run -----------------------------------------------------------------------------------

static void ev_tick_makes_input(uint32_t arg)
{
	(void)arg;
	log_event("tick50");
	vclock_at(vclock_now_us(), VCLOCK_CLASS_INPUT, ev_child_makes_grandchild, 0);
}

static int rule5_end_after_rounds(void)
{
	vclock_at(3000, VCLOCK_CLASS_END, ev_end, 0);		// queued first, lowest sequence
	vclock_at(3000, VCLOCK_CLASS_TICK, ev_tick_makes_input, 0);
	vclock_advance_to(3000);
	// round 0: tick50; round 1: child; round 2: grandchild; then the end
	static const char *const want[] = { "tick50", "child", "grandchild", "end" };
	return !expect_log(want, 4);
}

// The clock's own end event, at --until-ms: the run ends "virtual time limit", exit 0, after the tick at that time
static int rule5_until_ms(void)
{
	CHECK(until_opt != NULL);
	CHECK(until_opt->set("3"));
	vclock_start();
	vclock_tick_start();
	if (setjmp(finish_jmp) == 0) {
		vclock_advance_to(10000);
		printf("    FAIL: the run did not end\n");
		return 1;
	}
	CHECK(strcmp(finish_why, "virtual time limit") == 0);
	CHECK(finish_code == 0);
	CHECK(vclock_now_us() == 3000);
	static const char *const want[] = { "tick", "tick", "tick" };
	CHECK(expect_log(want, 3));
	CHECK(log_at[2] == 3000);
	return 0;
}

// ---- world control hooks: the horizon and vclock_post ------------------------------------------------------------------

// The conductor's hook, here a script: log, stamp an input at the horizon, then set the next horizon (once; after
// that, none)
static int horizon_calls;
static uint64_t horizon_seen;
static uint64_t next_horizon = VCLOCK_NEVER;
static int stamp_input;
static void (*horizon_script)(void);
void vclock_horizon_reached(uint64_t now_us)
{
	horizon_calls++;
	horizon_seen = now_us;
	log_event("horizon");
	if (horizon_script) {
		horizon_script();
		return;
	}
	if (stamp_input)
		vclock_post(now_us, VCLOCK_CLASS_INPUT, 7, ev_named, N_STAMPED);
	vclock_set_horizon(next_horizon);
	next_horizon = VCLOCK_NEVER;
}

// The idle jump stops at the horizon, before any event due at it; an input stamped there runs before its tick
static int hook_horizon_idle(void)
{
	void __wrap_vApplicationIdleHook(void);
	vclock_tick_start();				// tick at 1000
	vclock_at(500, VCLOCK_CLASS_OWN, ev_named, N_BEFORE_HORIZON);
	vclock_set_horizon(1000);
	stamp_input = 1;
	next_horizon = 2000;
	__wrap_vApplicationIdleHook();		// jumps to 500
	__wrap_vApplicationIdleHook();		// the tick at 1000 is at the horizon: stop there, hook, then run it
	CHECK(horizon_calls == 1);
	CHECK(horizon_seen == 1000);
	CHECK(vclock_next_due_us() == 2000);
	static const char *const want[] = { "before-horizon", "horizon", "stamped", "tick" };
	return !expect_log(want, 4);
}

// A busy wait stops at the horizon too, then goes on to its end once the hook moved the horizon
static int hook_horizon_busy_wait(void)
{
	sched_state = taskSCHEDULER_RUNNING;
	vclock_at(1500, VCLOCK_CLASS_INPUT, ev_named, N_AT_HORIZON);
	vclock_at(1200, VCLOCK_CLASS_INPUT, ev_named, N_BEFORE_HORIZON);
	vclock_set_horizon(1500);
	next_horizon = 1800;
	hal_delay_us(2000);			// 0 to 2000: stops at 1500 and at 1800, then goes on to its end
	CHECK(horizon_calls == 2);
	CHECK(vclock_now_us() == 2000);
	static const char *const want[] = { "before-horizon", "horizon", "at-horizon", "horizon" };
	CHECK(expect_log(want, 4));
	CHECK(log_at[1] == 1500 && log_at[2] == 1500 && log_at[3] == 1800);
	return 0;
}

// The default: no horizon, so the hook is never called and the run goes on as in standalone host
static int hook_no_horizon_default(void)
{
	vclock_at(1000000, VCLOCK_CLASS_INPUT, ev_named, N_INPUT);
	vclock_advance_to(2000000);
	CHECK(horizon_calls == 0);
	CHECK(vclock_now_us() == 2000000);
	CHECK(vclock_next_due_us() == VCLOCK_NEVER);
	return 0;
}

// A horizon the hook does not move past the clock ends the run, named
static int hook_horizon_not_moved(void)
{
	vclock_set_horizon(100);
	next_horizon = 100;
	if (setjmp(finish_jmp) == 0) {
		vclock_advance_to(200);
		return 1;
	}
	CHECK(strcmp(finish_why, "horizon not moved") == 0);
	CHECK(finish_code != 0);
	return 0;
}

static uint64_t drained_at;
static int64_t drained_round;
static void drain_then_resume(void)
{
	if (horizon_calls == 1) {
		vclock_drain_horizon();
	} else {
		drained_at = vclock_now_us();
		drained_round = vclock_round_handled();
		vclock_set_horizon(VCLOCK_NEVER);
	}
}

static int hook_drain_rounds(void)
{
	horizon_script = drain_then_resume;
	vclock_set_horizon(100);
	vclock_at(100, VCLOCK_CLASS_WORLD, ev_world_makes_input, 0);
	vclock_at(100, VCLOCK_CLASS_TICK, ev_named, N_TICK50);
	vclock_at(101, VCLOCK_CLASS_INPUT, ev_named, N_AFTER_SPAN);
	vclock_advance_to(100);
	CHECK(horizon_calls == 2 && drained_at == 100 && drained_round == 1);
	CHECK(vclock_next_due_us() == 101);
	static const char *const want[] = { "horizon", "world", "tick50", "child", "horizon" };
	return !expect_log(want, 5);
}

static void busy_at_boundary(uint32_t arg)
{
	(void)arg;
	log_event("busy-start");
	vclock_at(vclock_now_us(), VCLOCK_CLASS_INPUT, ev_named, N_CHILD);
	hal_delay_us(5);
	log_event("busy-end");
}

static int hook_drain_nested_wait(void)
{
	horizon_script = drain_then_resume;
	vclock_set_horizon(100);
	vclock_at(100, VCLOCK_CLASS_INPUT, busy_at_boundary, 0);
	vclock_at(101, VCLOCK_CLASS_INPUT, ev_named, N_AFTER_SPAN);
	hal_delay_us(200);
	CHECK(horizon_calls == 2 && drained_at == 100 && drained_round == 1);
	CHECK(log_at[2] == 100 && log_at[3] == 100 && log_at[5] == 105);
	CHECK(vclock_now_us() == 200);
	static const char *const want[] = { "horizon", "busy-start", "child", "horizon", "after-span", "busy-end" };
	return !expect_log(want, 6);
}

// vclock_post with a source: the round from the clock state, the same key as vclock_at
static void ev_posts_outside(uint32_t arg)
{
	(void)arg;
	log_event("world");
	vclock_post(vclock_now_us(), VCLOCK_CLASS_AIR, 3, ev_named, N_AIR);
}

static int hook_post_round(void)
{
	vclock_post(40, VCLOCK_CLASS_WORLD, 3, ev_posts_outside, 0);
	vclock_post(40, VCLOCK_CLASS_TICK, VCLOCK_SOURCE_RADIO, ev_named, N_TICK50);
	vclock_advance_to(40);
	static const char *const want[] = { "world", "tick50", "air" };
	return !expect_log(want, 3);
}

static int hook_post_stamped_order(void)
{
	vclock_post_stamped(41, 0, VCLOCK_CLASS_WORLD, 7, 0, ev_named, N_AFTER_SPAN);
	vclock_post_stamped(40, 0, VCLOCK_CLASS_END, 7, 0, ev_end, 0);
	vclock_post_stamped(40, 4, VCLOCK_CLASS_INPUT, 7, (1ull << 32) + 1, ev_named, N_INPUT);
	vclock_post_stamped(40, 4, VCLOCK_CLASS_INPUT, 8, 2, ev_named, N_KEY);
	vclock_post_stamped(40, 4, VCLOCK_CLASS_WORLD, 7, 50, ev_named, N_WORLD);
	vclock_post_stamped(40, 3, VCLOCK_CLASS_TICK, 7, 60, ev_named, N_TICK50);
	vclock_advance_to(41);
	static const char *const want[] = { "tick50", "world", "key", "input", "end", "after-span" };
	CHECK(expect_log(want, 6));
	CHECK(log_at[0] == 40 && log_at[5] == 41);
	CHECK(log_round[0] == 3 && log_round[1] == 4 && log_round[2] == 4 && log_round[3] == 4);
	return 0;
}

static int hook_round_handled(void)
{
	CHECK(vclock_round_handled() == -1);
	vclock_advance_to(40);
	CHECK(vclock_round_handled() == -1);
	vclock_post_stamped(40, 7, VCLOCK_CLASS_WORLD, 7, 20, ev_posts_outside, 0);
	CHECK(vclock_round_handled() == -1);
	vclock_advance_to(40);
	static const char *const want[] = { "world", "air" };
	CHECK(expect_log(want, 2));
	CHECK(log_round[0] == 7 && log_round[1] == 8);
	CHECK(vclock_round_handled() == 8);
	vclock_post_stamped(50, UINT32_MAX, VCLOCK_CLASS_INPUT, 7, 21, ev_named, N_INPUT);
	vclock_advance_to(50);
	CHECK(vclock_round_handled() == UINT32_MAX);
	vclock_advance_to(51);
	CHECK(vclock_round_handled() == -1);
	return 0;
}

// A supplied sequence does not consume a local one: the second local input still has sequence 1, before stamp 2
static int hook_post_stamped_local_sequence(void)
{
	vclock_post(40, VCLOCK_CLASS_INPUT, VCLOCK_SOURCE_RADIO, ev_named, N_INPUT);
	vclock_post_stamped(40, 0, VCLOCK_CLASS_INPUT, 7, 2, ev_named, N_STAMPED);
	vclock_post(40, VCLOCK_CLASS_INPUT, VCLOCK_SOURCE_RADIO, ev_named, N_KEY);
	vclock_advance_to(40);
	static const char *const want[] = { "input", "key", "stamped" };
	return !expect_log(want, 3);
}

static int hook_post_stamped_past(void)
{
	vclock_advance_to(500);
	if (setjmp(finish_jmp) == 0) {
		vclock_post_stamped(499, 7, VCLOCK_CLASS_INPUT, 7, 20, ev_named, N_INPUT);
		printf("    FAIL: the stamped past event did not end the run\n");
		return 1;
	}
	printf("    the run ended: \"%s\", exit %d\n", finish_why, finish_code);
	CHECK(finish_code == 4);
	CHECK(strcmp(finish_why, "event due in the past") == 0);
	CHECK(vclock_now_us() == 500);
	CHECK(vclock_next_due_us() == VCLOCK_NEVER);
	CHECK(n_log == 0);
	return 0;
}

// ---- The runner ---------------------------------------------------------------------------------------------------

typedef struct {
	const char *name;
	int (*fn)(void);
} test_case_t;

static const test_case_t cases[] = {
	{ "rule1_class_order: classes 50 and 30 at one time run 30 first", rule1_class_order },
	{ "rule1_all_classes: time, then class 10 20 30 40 50, then queue order", rule1_all_classes },
	{ "rule1_round: an event made at T, due at T, runs after the tick queued before it", rule1_round },
	{ "rule1_rounds_nest: round 2 after every round 1 event", rule1_rounds_nest },
	{ "rule1_later_is_round0: an event made for a later time keeps its class place", rule1_later_is_round0 },
	{ "rule2_past_ends_run: a past event ends the run, non-zero exit and a reason", rule2_past_ends_run },
	{ "rule3_reads: reads never move the clock; ms = us / 1000, us cut to 32 bits", rule3_reads },
	{ "rule4_delay_ms_running: vTaskDelay once the scheduler runs", rule4_delay_ms_running },
	{ "rule4_delay_ms_before_scheduler: busy wait, events in the span, watchdog fed", rule4_delay_ms_before_scheduler },
	{ "rule4_delay_ms_suspended: busy wait, events in the span, no feed", rule4_delay_ms_suspended },
	{ "rule4_delay_us: busy wait, the ticks in the span run", rule4_delay_us },
	{ "rule4_idle_jump: the idle task jumps to the next due event", rule4_idle_jump },
	{ "rule5_end_after_rounds: the end runs after every round at its time", rule5_end_after_rounds },
	{ "rule5_until_ms: --until-ms ends the run after that time's tick, exit 0", rule5_until_ms },
	{ "hook_horizon_idle: idle stops at the horizon; a stamped input runs before the tick", hook_horizon_idle },
	{ "hook_horizon_busy_wait: a busy wait stops at the horizon", hook_horizon_busy_wait },
	{ "hook_no_horizon_default: no horizon by default, standalone host behavior", hook_no_horizon_default },
	{ "hook_horizon_not_moved: a horizon not moved ends the run", hook_horizon_not_moved },
	{ "hook_drain_rounds: drain T handles every round, nothing later", hook_drain_rounds },
	{ "hook_drain_nested_wait: a wait inside drain T stays at T until the next grant", hook_drain_nested_wait },
	{ "hook_post_round: vclock_post takes its round from the clock", hook_post_round },
	{ "hook_post_stamped_order: caller stamps sort by time, round, class and sequence", hook_post_stamped_order },
	{ "hook_round_handled: the handled round is reported and resets when time moves", hook_round_handled },
	{ "hook_post_stamped_local_sequence: caller stamps leave local numbering unchanged", hook_post_stamped_local_sequence },
	{ "hook_post_stamped_past: a stamped past event ends the run with exit 4", hook_post_stamped_past },
};

int main(void)
{
	unsigned failed = 0, n = sizeof cases / sizeof cases[0];
	for (unsigned i = 0; i < n; i++) {
		fflush(stdout);
		pid_t pid = fork();
		if (pid == 0) {
			int r;
			if (setjmp(finish_jmp) == 0) {
				r = cases[i].fn();
			} else {
				printf("    FAIL: the run ended unexpectedly: \"%s\", exit %d\n", finish_why, finish_code);
				r = 1;
			}
			fflush(stdout);
			_exit(r);
		}
		int status = 0;
		waitpid(pid, &status, 0);
		int ok = WIFEXITED(status) && WEXITSTATUS(status) == 0;
		printf("%s %s\n", ok ? "PASS" : "FAIL", cases[i].name);
		failed += !ok;
	}
	printf("# vclockTest: %u of %u cases passed\n", n - failed, n);
	return failed ? 1 : 0;
}
