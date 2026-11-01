// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The host FreeRTOS port's unit test (platform/host/freertos/port.c): the paths the walking skeleton's runs never take.
//
// What it builds: the real port, the real kernel (third_party/FreeRTOS-Kernel), the real tick handler
// (platform/host/vtick.c) and the platform's FreeRTOSConfig.h, with this file in place of the rest of the host
// platform. Time moves only when every task waits: the idle hook here raises one tick, as the virtual clock does.
//
// One task, "ctl", runs the six cases in turn. Each case writes what happened, in order, to a short trace ("irq1,
// irq2") and compares it with the order the radio's CPU would give. Only one task thread runs at a time, so plain
// globals are safe here.
//
// Make -DHOST_PORT_TEST_BREAK=1 builds a wrong port (interrupts delivered at raise time); case 1 must then fail.
#define _GNU_SOURCE
#include <pthread.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "FreeRTOS.h"
#include "task.h"
#include "vclock.h"
#include "vport.h"

// --- What this file stands in for: the rest of the host platform -------------------------------------------------

uint8_t ucHeap[configTOTAL_HEAP_SIZE];	// configAPPLICATION_ALLOCATED_HEAP (boards/vhost/board.c on the radio)

void vclock_tick_start(void) {}
void vwatchdog_tick(void) {}	// vtick.c's virtual hardware watchdog (vwatchdog.c): not under test here
void vstall_start(void) {}
void vstall_progress(void) {}
void vtrace_isr(const char *name) { (void)name; }
void vtrace_switched_in(const char *name) { (void)name; }
void vtrace_task_create(const char *name, unsigned prio) { (void)name; (void)prio; }
void vtrace_task_delete(const char *name) { (void)name; }

void vhost_finish(const char *why, int code)
{
	printf("# hostPortTest: finished early: %s\n", why);
	fflush(stdout);
	exit(code != 0 ? code : 1);
}

void vhost_assert(const char *file, int line)
{
	printf("FAIL configASSERT at %s:%d\n", file, line);
	fflush(stdout);
	exit(1);
}

void vApplicationMallocFailedHook(void);
void vApplicationMallocFailedHook(void)
{
	printf("FAIL the FreeRTOS heap ran out\n");
	exit(1);
}

// When every task waits, one tick of time goes by (platform/host/vclock.c jumps the same way from its idle hook)
void vApplicationIdleHook(void);
void vApplicationIdleHook(void)
{
	vhost_irq_raise(VHOST_IRQ_TICK);
}

// Host memory the port takes with malloc (each task's wake-up event), counted through -Wl,--wrap=malloc,--wrap=free
static long live_mallocs;
void *__real_malloc(size_t n);
void __real_free(void *p);
void *__wrap_malloc(size_t n);
void __wrap_free(void *p);
void *__wrap_malloc(size_t n)
{
	void *p = __real_malloc(n);

	if (p != NULL) {
		live_mallocs++;
	}
	return p;
}

void __wrap_free(void *p)
{
	if (p != NULL) {
		live_mallocs--;
	}
	__real_free(p);
}

// Threads the port joined (vPortCancelThread), through -Wl,--wrap=pthread_join: a thread that has left but was never
// joined keeps its pthread resources, which /proc's thread count does not show
static long joins;
int __real_pthread_join(pthread_t t, void **ret);
int __wrap_pthread_join(pthread_t t, void **ret);
int __wrap_pthread_join(pthread_t t, void **ret)
{
	int r = __real_pthread_join(t, ret);

	if (r == 0) {
		joins++;
	}
	return r;
}

// --- Checks and the trace -----------------------------------------------------------------------------------------

static int failures;
static int case_failures;
static char trace[512];

#define CHECK(cond)                                                                      \
	do {                                                                             \
		if (!(cond)) {                                                           \
			printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);           \
			case_failures++;                                                 \
		}                                                                        \
	} while (0)

#define CHECK_TRACE(want)                                                                \
	do {                                                                             \
		if (strcmp(trace, (want)) != 0) {                                        \
			printf("FAIL %s:%d: trace \"%s\", want \"%s\"\n", __FILE__, __LINE__, \
			       trace, (want));                                           \
			case_failures++;                                                 \
		}                                                                        \
	} while (0)

static void note(const char *what)
{
	if (trace[0] != '\0') {
		strncat(trace, ",", sizeof(trace) - strlen(trace) - 1);
	}
	strncat(trace, what, sizeof(trace) - strlen(trace) - 1);
}

static void run_case(const char *name, void (*body)(void))
{
	trace[0] = '\0';
	case_failures = 0;
	body();
	printf("%s %s\n", case_failures == 0 ? "PASS" : "FAIL", name);
	failures += case_failures;
}

// The process's live threads, from /proc. A joined thread can stay counted for a moment after pthread_join returns
// (the kernel drops it from the count a little after it wakes the joiner), so wait up to 2 s for the count wanted
static int threads_now(void)
{
	FILE *f = fopen("/proc/self/status", "r");
	char line[128];
	int n = -1;

	if (f == NULL) {
		return -1;
	}
	while (fgets(line, sizeof(line), f) != NULL) {
		if (sscanf(line, "Threads: %d", &n) == 1) {
			break;
		}
	}
	fclose(f);
	return n;
}

static int threads_settle(int want)
{
	const struct timespec ms = { 0, 1000000 };
	int n = threads_now();

	for (int i = 0; i < 2000 && n != want; i++) {
		nanosleep(&ms, NULL);
		n = threads_now();
	}
	return n;
}

static TaskHandle_t spawn(TaskFunction_t fn, const char *name, UBaseType_t prio)
{
	TaskHandle_t h = NULL;

	if (xTaskCreate(fn, name, configMINIMAL_STACK_SIZE, NULL, prio, &h) != pdPASS) {
		printf("FAIL xTaskCreate(%s)\n", name);
		exit(1);
	}
	return h;
}

// Priorities: "ctl" runs the cases at 2; a helper at 3 runs as soon as it is ready, one at 1 never runs on its own
#define PRIO_LOW	1
#define PRIO_CTL	2
#define PRIO_HIGH	3

// --- Case 1: an interrupt raised inside a critical section runs exactly when the section ends, highest first -----

static void isr_rx(void) { note(vhost_in_isr() ? "irq1" : "irq1-outside-isr"); }
static void isr_key(void) { note(vhost_in_isr() ? "irq2" : "irq2-outside-isr"); }
static void isr_power(void) { note(vhost_in_isr() ? "irq3" : "irq3-outside-isr"); }

static void case_irq_waits_for_critical_end(void)
{
	vport_stats_t before;
	vport_stats_t after;

	vhost_irq_register(VHOST_IRQ_HOST_RX, "rx", isr_rx);
	vhost_irq_register(VHOST_IRQ_KEY, "key", isr_key);
	vhost_irq_register(VHOST_IRQ_POWER, "power", isr_power);
	vport_stats(&before);

	// Raised lowest first, from inside an outer and a nested section: nothing may run until the outer one ends
	taskENTER_CRITICAL();
	vhost_irq_raise(VHOST_IRQ_POWER);
	vhost_irq_raise(VHOST_IRQ_HOST_RX);
	taskENTER_CRITICAL();
	vhost_irq_raise(VHOST_IRQ_KEY);
	taskEXIT_CRITICAL();
	note("inner-end");
	taskEXIT_CRITICAL();
	note("outer-end");
	CHECK_TRACE("inner-end,irq1,irq2,irq3,outer-end");

	vport_stats(&after);
	CHECK(after.deferred - before.deferred == 3);
	CHECK(after.criticals - before.criticals == 1);

	// The same from the masks a handler uses (taskENTER_CRITICAL_FROM_ISR) and from portDISABLE_INTERRUPTS
	trace[0] = '\0';
	UBaseType_t mask = taskENTER_CRITICAL_FROM_ISR();
	vhost_irq_raise(VHOST_IRQ_KEY);
	note("masked");
	taskEXIT_CRITICAL_FROM_ISR(mask);
	portDISABLE_INTERRUPTS();
	vhost_irq_raise(VHOST_IRQ_POWER);
	vhost_irq_raise(VHOST_IRQ_HOST_RX);
	note("disabled");
	portENABLE_INTERRUPTS();
	note("enabled");
	CHECK_TRACE("masked,irq2,disabled,irq1,irq3,enabled");

	// A mask taken while interrupts were already off gives them back off: the raise stays pending
	trace[0] = '\0';
	portDISABLE_INTERRUPTS();
	mask = taskENTER_CRITICAL_FROM_ISR();
	vhost_irq_raise(VHOST_IRQ_KEY);
	taskEXIT_CRITICAL_FROM_ISR(mask);
	note("still-off");
	portENABLE_INTERRUPTS();
	note("on");
	CHECK_TRACE("still-off,irq2,on");

	// With interrupts on, a raise runs its handler at once
	trace[0] = '\0';
	vhost_irq_raise(VHOST_IRQ_KEY);
	note("after-raise");
	CHECK_TRACE("irq2,after-raise");
}

// --- Case 2: ticks pended under vTaskSuspendAll are all delivered on resume ---------------------------------------

static bool late_woke;
static TickType_t late_tick;

static void task_late(void *arg)
{
	(void)arg;
	vTaskDelay(3);
	late_woke = true;
	late_tick = xTaskGetTickCount();
	note("late-woke");
	vTaskDelete(NULL);
}

static void case_ticks_pended_under_suspend(void)
{
	vport_stats_t before;
	vport_stats_t after;
	TickType_t t0;

	late_woke = false;
	spawn(task_late, "late", PRIO_HIGH);	// runs now and waits for 3 ticks
	t0 = xTaskGetTickCount();

	vport_stats(&before);
	vTaskSuspendAll();
	vport_stats(&after);
	CHECK(after.suspends - before.suspends == 1);	// this file's call went through the port's counting wrap
	for (int i = 0; i < 5; i++) {
		vhost_irq_raise(VHOST_IRQ_TICK);	// the tick handler runs, but the kernel only counts it as pended
	}
	CHECK(xTaskGetTickCount() == t0);
	CHECK(!late_woke);
	note("resume");
	(void)xTaskResumeAll();
	note("resumed");

	CHECK(xTaskGetTickCount() == t0 + 5);
	CHECK(late_tick == t0 + 5);	// woken at t0 + 3, but the five pended ticks are all counted before it runs
	CHECK_TRACE("resume,late-woke,resumed");
}

// --- Case 3: a notify wait times out at the right tick, and a give before it wakes the task at once --------------

static TaskHandle_t waiter;
static bool waiter_done;
static uint32_t waiter_got;
static TickType_t waiter_t0;
static TickType_t waiter_t1;

static void task_waiter(void *arg)
{
	(void)arg;
	waiter_t0 = xTaskGetTickCount();
	waiter_got = ulTaskNotifyTake(pdTRUE, 10);
	waiter_t1 = xTaskGetTickCount();
	note(waiter_got != 0 ? "waiter-given" : "waiter-timeout");
	waiter_done = true;
	vTaskDelete(NULL);
}

static void case_notify_timeout(void)
{
	// Nobody gives: the wait ends at exactly t0 + 10
	waiter_done = false;
	waiter = spawn(task_waiter, "waiter", PRIO_HIGH);
	while (!waiter_done) {
		vTaskDelay(1);
	}
	CHECK(waiter_got == 0);
	CHECK(waiter_t1 - waiter_t0 == 10);
	CHECK_TRACE("waiter-timeout");

	// A give at t0 + 3 wakes it before the give call returns: it is higher than ctl
	trace[0] = '\0';
	waiter_done = false;
	waiter = spawn(task_waiter, "waiter", PRIO_HIGH);
	vTaskDelay(3);
	note("give");
	xTaskNotifyGive(waiter);
	note("after-give");
	CHECK(waiter_done);
	CHECK(waiter_got == 1);
	CHECK(waiter_t1 - waiter_t0 == 3);
	CHECK_TRACE("give,waiter-given,after-give");
}

// --- Case 4: a task delete, of itself and of another, frees its thread with no leak and no use after free --------

static void task_self_delete(void *arg)
{
	(void)arg;
	note("self-ran");
	vTaskDelete(NULL);
	note("self-after-delete");	// never: the thread left in the switch
	for (;;) {
	}
}

static void task_blocked(void *arg)
{
	(void)arg;
	note("blocked-ran");
	(void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
	note("blocked-woke");	// never: deleted while it waits
	for (;;) {
	}
}

static void task_never_runs(void *arg)
{
	(void)arg;
	note("never-ran");
	for (;;) {
	}
}

static void case_task_delete(void)
{
	int threads0;
	long joins0;
	long mallocs0;
	size_t heap0;
	TaskHandle_t h;

	// The tasks earlier cases left by deleting themselves are freed by the idle task: let it run first
	vTaskDelay(2);
	threads0 = threads_now();
	joins0 = joins;
	mallocs0 = live_mallocs;
	heap0 = xPortGetFreeHeapSize();

	// Another task that has never run: parked at its start fence
	h = spawn(task_never_runs, "never", PRIO_LOW);
	CHECK(threads_now() == threads0 + 1);
	CHECK(live_mallocs == mallocs0 + 1);
	vTaskDelete(h);
	CHECK(threads_settle(threads0) == threads0);
	CHECK(joins == joins0 + 1);
	CHECK(live_mallocs == mallocs0);
	CHECK(xPortGetFreeHeapSize() == heap0);

	// Another task that has run and now waits
	h = spawn(task_blocked, "blocked", PRIO_HIGH);
	CHECK(threads_now() == threads0 + 1);
	vTaskDelete(h);
	CHECK(threads_settle(threads0) == threads0);
	CHECK(joins == joins0 + 2);
	CHECK(live_mallocs == mallocs0);
	CHECK(xPortGetFreeHeapSize() == heap0);

	// A task deleting itself: its thread leaves at once, before the idle task has run (ctl has not waited yet), and
	// the idle task joins it and frees the rest on its next run
	(void)spawn(task_self_delete, "selfdel", PRIO_HIGH);
	CHECK(threads_settle(threads0) == threads0);
	CHECK(joins == joins0 + 2);
	CHECK(live_mallocs == mallocs0 + 1);
	vTaskDelay(2);
	CHECK(joins == joins0 + 3);
	CHECK(live_mallocs == mallocs0);
	CHECK(xPortGetFreeHeapSize() == heap0);

	CHECK_TRACE("blocked-ran,self-ran");
}

// --- Case 5: a switch asked for inside an interrupt handler waits for the handler's end ---------------------------

static TaskHandle_t woken;

static void isr_rx_named(void)
{
	char buf[32];

	// Still before the switch: the interrupted task is the current one, as before PendSV on the radio's CPU
	snprintf(buf, sizeof(buf), "rx-in-%s", pcTaskGetName(NULL));
	note(buf);
}

static void isr_key_gives(void)
{
	BaseType_t higher = pdFALSE;

	vTaskNotifyGiveFromISR(woken, &higher);
	note(higher != pdFALSE ? "isr-gave" : "isr-gave-no-switch");
	portYIELD_FROM_ISR(higher);
	note("isr-after-yield");
	vhost_irq_raise(VHOST_IRQ_HOST_RX);	// pending: inside a handler, interrupts are masked
	note("isr-end");
}

static void task_woken(void *arg)
{
	(void)arg;
	(void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
	note(vhost_in_isr() ? "woken-in-isr" : "woken");
	vTaskDelete(NULL);
}

static void case_switch_waits_for_isr_end(void)
{
	vhost_irq_register(VHOST_IRQ_KEY, "key", isr_key_gives);
	vhost_irq_register(VHOST_IRQ_HOST_RX, "rx", isr_rx_named);
	woken = spawn(task_woken, "woken", PRIO_HIGH);	// runs now and waits
	vhost_irq_raise(VHOST_IRQ_KEY);
	note("ctl-after-raise");
	CHECK_TRACE("isr-gave,isr-after-yield,isr-end,rx-in-ctl,woken,ctl-after-raise");
	CHECK(!vhost_in_isr());
}

// --- Case 6: task yields wait for unmasking, after every pending interrupt ---------------------------------------

static void task_bumper(void *arg)
{
	(void)arg;
	for (;;) {
		note("bumper");
		(void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
	}
}

static void case_switch_waits_for_critical_end(void)
{
	TaskHandle_t bumper;
	UBaseType_t mask;

	vhost_irq_register(VHOST_IRQ_HOST_RX, "rx", isr_rx_named);
	// Creating a higher task requests a switch; nested exits and repeated yields must leave it pending.
	taskENTER_CRITICAL();
	taskENTER_CRITICAL();
	taskENTER_CRITICAL();
	bumper = spawn(task_bumper, "bumper", PRIO_HIGH);
	note("created");
	taskYIELD();
	taskYIELD();
	vhost_irq_raise(VHOST_IRQ_HOST_RX);
	taskEXIT_CRITICAL();
	note("end3");
	taskEXIT_CRITICAL();
	note("end2");
	taskEXIT_CRITICAL();
	note("outer-end");
	CHECK_TRACE("created,end3,end2,rx-in-ctl,bumper,outer-end");

	// Wake the same task: restoring an already-off mask must not deliver its pending switch either.
	trace[0] = '\0';
	taskENTER_CRITICAL();
	xTaskNotifyGive(bumper);
	note("given");
	mask = taskENTER_CRITICAL_FROM_ISR();
	taskYIELD();
	vhost_irq_raise(VHOST_IRQ_HOST_RX);
	taskEXIT_CRITICAL_FROM_ISR(mask);
	note("still-off");
	taskEXIT_CRITICAL();
	note("outer-end");
	CHECK_TRACE("given,still-off,rx-in-ctl,bumper,outer-end");
	vTaskDelete(bumper);

	// The two raw interrupt masks have no nesting count; their unmask paths must also release the switch.
	trace[0] = '\0';
	bumper = spawn(task_bumper, "bumper", PRIO_CTL);
	portDISABLE_INTERRUPTS();
	taskYIELD();
	vhost_irq_raise(VHOST_IRQ_HOST_RX);
	note("disabled");
	portENABLE_INTERRUPTS();
	note("enabled");
	CHECK_TRACE("disabled,rx-in-ctl,bumper,enabled");
	vTaskDelete(bumper);

	trace[0] = '\0';
	bumper = spawn(task_bumper, "bumper", PRIO_CTL);
	mask = taskENTER_CRITICAL_FROM_ISR();
	taskYIELD();
	vhost_irq_raise(VHOST_IRQ_HOST_RX);
	note("masked");
	taskEXIT_CRITICAL_FROM_ISR(mask);
	note("unmasked");
	CHECK_TRACE("masked,rx-in-ctl,bumper,unmasked");
	vTaskDelete(bumper);
}

// --- The cases in order, on one task ------------------------------------------------------------------------------

static void task_ctl(void *arg)
{
	(void)arg;
	run_case("case_irq_waits_for_critical_end", case_irq_waits_for_critical_end);
	run_case("case_ticks_pended_under_suspend", case_ticks_pended_under_suspend);
	run_case("case_notify_timeout", case_notify_timeout);
	run_case("case_task_delete", case_task_delete);
	run_case("case_switch_waits_for_isr_end", case_switch_waits_for_isr_end);
	run_case("case_switch_waits_for_critical_end", case_switch_waits_for_critical_end);
	printf("# hostPortTest: %s\n", failures == 0 ? "6 cases PASSED" : "FAILED");
	fflush(stdout);
	exit(failures == 0 ? 0 : 1);
}

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	vhost_irq_register(VHOST_IRQ_TICK, "tick", vtick_isr);
	spawn(task_ctl, "ctl", PRIO_CTL);
	vTaskStartScheduler();
	printf("FAIL the scheduler returned\n");
	return 1;
}
