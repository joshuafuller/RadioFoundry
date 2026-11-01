// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// FreeRTOS for the host platform (the virtual radio): the STM32F4 settings where they mean the same on a host, with the
// deterministic host port in platform/host/freertos.
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#if __has_include("freertos_board.h")
#include "freertos_board.h"
#endif

#include <stdint.h>

#define configUSE_PREEMPTION			1
#define configUSE_PORT_OPTIMISED_TASK_SELECTION	0	// the generic C selection: no CPU instruction to borrow
#define configCPU_CLOCK_HZ			72000000u	// unused by the host port
#define configTICK_RATE_HZ			1000
#define configMAX_PRIORITIES			7
#define configMINIMAL_STACK_SIZE		128
// A word here is 8 bytes (StackType_t is unsigned long on x86_64), so every stack is twice the STM32's bytes; the
// task bodies run on their pthread stacks anyway. The heap is the board's ucHeap (boards/vhost/board.c)
#define configAPPLICATION_ALLOCATED_HEAP	1
#ifndef configTOTAL_HEAP_SIZE
#define configTOTAL_HEAP_SIZE			(256 * 1024)
#endif
#define configMAX_TASK_NAME_LEN			12
#define configTICK_TYPE_WIDTH_IN_BITS		TICK_TYPE_WIDTH_32_BITS	// as on the radios: the tick wraps at 2^32
#define configIDLE_SHOULD_YIELD			1
#define configUSE_MUTEXES			1
#define configUSE_RECURSIVE_MUTEXES		1
#define configUSE_COUNTING_SEMAPHORES		1
#define configQUEUE_REGISTRY_SIZE		0
#define configUSE_TASK_NOTIFICATIONS		1
#define configSUPPORT_STATIC_ALLOCATION		0
#define configSUPPORT_DYNAMIC_ALLOCATION	1
// Always on: the host port moves virtual time from the idle hook. The firmware's own hook (core/app/watchdog.c, with
// CONFIG_WATCHDOG) is reached through -Wl,--wrap=vApplicationIdleHook (platform/host/vclock.c)
#define configUSE_IDLE_HOOK			1
#define configUSE_TICK_HOOK			1
#define configCHECK_FOR_STACK_OVERFLOW		0	// the stack buffer only holds the pthread's handle on the host
#define configUSE_MALLOC_FAILED_HOOK		1
#define configUSE_TIMERS			1
#define configTIMER_TASK_PRIORITY		(configMAX_PRIORITIES - 1)
#define configTIMER_QUEUE_LENGTH		8
#define configTIMER_TASK_STACK_DEPTH		256
#define configUSE_TRACE_FACILITY		0
#define configUSE_STATS_FORMATTING_FUNCTIONS	0

#define INCLUDE_vTaskPrioritySet		1
#define INCLUDE_uxTaskPriorityGet		1
#define INCLUDE_vTaskDelete			1
#define INCLUDE_vTaskSuspend			1
#define INCLUDE_vTaskDelayUntil			1
#define INCLUDE_vTaskDelay			1
#define INCLUDE_xTaskGetSchedulerState		1
#define INCLUDE_xTaskGetCurrentTaskHandle	1
#define INCLUDE_uxTaskGetStackHighWaterMark	1

#if !defined(__ASSEMBLER__)
// The trace (platform/host/vtrace.h): every switch-in, by task name; never a pointer or a host time
void vtrace_switched_in(const char *name);
void vtrace_task_create(const char *name, unsigned prio);
void vtrace_task_delete(const char *name);
#if CONFIG_TASK_STACKS
#include "app/task_stack.h"
#define traceTASK_CREATE(tcb)			do { vtrace_task_create((tcb)->pcTaskName, (unsigned)(tcb)->uxPriority); task_stack_created(tcb); } while (0)
#define traceTASK_DELETE(tcb)			do { vtrace_task_delete((tcb)->pcTaskName); task_stack_deleted(tcb); } while (0)
#else
#define traceTASK_CREATE(tcb)			vtrace_task_create((tcb)->pcTaskName, (unsigned)(tcb)->uxPriority)
#define traceTASK_DELETE(tcb)			vtrace_task_delete((tcb)->pcTaskName)
#endif
#define traceTASK_SWITCHED_IN()			vtrace_switched_in(pxCurrentTCB->pcTaskName)

// A failed assertion stops the run with its place (platform/host/main.c; the trace is flushed first)
void vhost_assert(const char *file, int line) __attribute__((noreturn));
#define configASSERT(x)				do { if (!(x)) { vhost_assert(__FILE__, __LINE__); } } while (0)
#endif

#endif
