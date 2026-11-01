// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The registered ETH_IN handler runs on the real clock. Channel transport and RTOS calls are stand-ins;
// no sockets are needed to check that device state retains every frame while interrupts are masked.
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "vboard.h"
#include "vchan.h"
#include "vclock.h"
#include "vhost.h"
#include "vport.h"
#include "vstall.h"
#include "vtrace.h"
#include "vwatchdog.h"
#include "FreeRTOS.h"
#include "task.h"

static vchan_handler_fn input;
static unsigned critical_depth, sent;
static vchan_message_t frames[2], output;
static jmp_buf finish_jump;
static const char *expected_reason;

void vchan_register(unsigned kind, vchan_handler_fn handler)
{
	assert(kind == VCHAN_ETH_IN && input == NULL && handler != NULL);
	input = handler;
}

void vchan_send(unsigned kind, const void *buf, size_t length)
{
	assert(kind == VCHAN_ETH_OUT && length <= sizeof output.payload);
	output.kind = (uint8_t)kind;
	output.time_us = vclock_now_us();
	output.length = (uint16_t)length;
	memcpy(output.payload, buf, length);
	sent++;
}

void vhost_finish(const char *reason, int code)
{
	assert(expected_reason != NULL && strcmp(reason, expected_reason) == 0 && code == 4);
	longjmp(finish_jump, 1);
}

void vhost_option_register(const vhost_option_t *option) { (void)option; }
void vtrace(const char *fmt, ...) { (void)fmt; }
void vstall_progress(void) {}
void vwatchdog_feed(void) {}
void vhost_irq_register(unsigned irq, const char *name, vhost_isr_t isr)
{
	(void)irq; (void)name; (void)isr;
	assert(0 && "the network part needs no interrupt");
}
void vhost_irq_raise(unsigned irq) { (void)irq; assert(0 && "no network interrupt"); }
void vtick_isr(void) {}
void vPortEnterCritical(void) { critical_depth++; }
void vPortExitCritical(void) { assert(critical_depth > 0); critical_depth--; }
BaseType_t xTaskGetSchedulerState(void) { return taskSCHEDULER_NOT_STARTED; }
void vTaskDelay(TickType_t ticks) { (void)ticks; }
char *pcTaskGetName(TaskHandle_t task) { (void)task; return "test"; }
void __real_vApplicationIdleHook(void);
void __real_vApplicationIdleHook(void) {}

static void deliver(uint32_t slot)
{
	assert(critical_depth == 1);
	assert(vclock_now_us() == frames[slot].time_us);
	input(&frames[slot]);
}

static void expect_finish(vchan_message_t *message, const char *reason)
{
	expected_reason = reason;
	if (setjmp(finish_jump) == 0) {
		input(message);
		assert(0 && "invalid input must end the run");
	}
	expected_reason = NULL;
}

int main(void)
{
	const hal_netif_t *netif = vhost_part_netif();
	uint8_t buf[600], host[6], radio_mac[6];
	assert(input != NULL && netif != NULL && netif->frame_max == sizeof buf);
	assert(!netif->selected() && !netif->up());
	assert(!netif->send(buf, sizeof buf));
	assert(netif->select(true) && netif->selected() && netif->up());
	netif->macs(host, radio_mac);
	assert(memcmp(host, "\x02\x56\x48\x00\x00\x02", 6) == 0);
	assert(memcmp(radio_mac, "\x02\x56\x48\x00\x00\x01", 6) == 0);
	netif->tick(0);
	assert(netif->receive(buf, sizeof buf) == 0);
	for (unsigned i = 0; i < 2; i++) {
		frames[i].kind = VCHAN_ETH_IN;
		frames[i].time_us = 123;
		frames[i].length = (uint16_t)(i ? sizeof buf : 60);
		memset(frames[i].payload, (int)(i + 1), frames[i].length);
	}
	// Queue in reverse insertion order; the conductor's sequence determines delivery at this microsecond.
	vclock_post_stamped(123, 0, VCLOCK_CLASS_INPUT, 7, 2, deliver, 1);
	vclock_post_stamped(123, 0, VCLOCK_CLASS_INPUT, 7, 1, deliver, 0);
	vPortEnterCritical();
	vclock_advance_to(122);
	assert(netif->receive(buf, sizeof buf) == 0);
	vclock_advance_to(123);
	assert(critical_depth == 1);
	vPortExitCritical();
	assert(netif->receive(NULL, sizeof buf) == 0);
	assert(netif->receive(buf, -1) == 0);
	assert(netif->receive(buf, 59) == 0);
	for (unsigned i = 0; i < 2; i++) {
		assert(netif->receive(buf, sizeof buf) == frames[i].length);
		assert(memcmp(buf, frames[i].payload, frames[i].length) == 0);
	}
	assert(netif->receive(buf, sizeof buf) == 0);
	puts("PASS: two frames at 123 us, interrupts masked, received whole and in order");

	assert(!netif->send(NULL, 60) && !netif->send(buf, -1));
	assert(!netif->send(buf, 13) && !netif->send(buf, 601));
	assert(sent == 0 && netif->send(buf, sizeof buf) && sent == 1);
	assert(output.kind == VCHAN_ETH_OUT && output.time_us == 123 && output.length == sizeof buf);
	assert(memcmp(output.payload, buf, sizeof buf) == 0);
	assert(netif->select(false) && !netif->selected() && !netif->up());
	input(&frames[0]);
	frames[0].payload[0] = 9;	// the channel may reuse its message after delivery
	assert(netif->receive(buf, sizeof buf) == 0 && !netif->send(buf, 60));
	assert(netif->select(true));
	assert(netif->receive(buf, sizeof buf) == 60 && buf[0] == 1);
	puts("PASS: selection, MACs, frame limits, copied input and stamped ETH_OUT");

	frames[0].length = 0;
	expect_finish(&frames[0], "VNETIF_BAD_LENGTH");
	frames[0].length = 13;
	expect_finish(&frames[0], "VNETIF_BAD_LENGTH");
	frames[0].length = 601;
	expect_finish(&frames[0], "VNETIF_BAD_LENGTH");
	frames[0].length = 60;
	for (unsigned wrap = 0; wrap < 3; wrap++) {
		for (unsigned i = 0; i < 64; i++) {
			frames[0].payload[0] = (uint8_t)i;
			input(&frames[0]);
		}
		expect_finish(&frames[0], "VNETIF_RX_FULL");
		for (unsigned i = 0; i < 64; i++)
			assert(netif->receive(buf, sizeof buf) == 60 && buf[0] == i);
		assert(netif->receive(buf, sizeof buf) == 0);
	}
	hal_netif_stats_t stats;
	netif->stats(&stats);
	assert(stats.rx_frames == 195 && stats.tx_frames == 1 && stats.rx_dropped == 0);
	assert(critical_depth == 0);
	puts("PASS: invalid lengths and queue overflow fail explicitly; three queue wraps preserve order");
	puts("vnetifTest: PASSED");
	return 0;
}
