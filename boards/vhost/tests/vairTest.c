// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Real channel validation and clock ordering, with a packet sink instead of sockets and a fake modem inbox.
#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define send vair_send
#include "../../../platform/host/vchan.c"
#undef send
#include "../dmr_air_vchan.c"
#include "vport.h"
#include "vwatchdog.h"
#include "FreeRTOS.h"
#include "task.h"

static jmp_buf refused;
static const char *expected_error;
static uint8_t packet[VCHAN_HEADER_SIZE + VCHAN_PAYLOAD_MAX];
static size_t packet_len;
static unsigned sends, inbox_count, order[4], n_order;
static uint64_t received_start;
static dmr_burst_t received;
static vdmr_tx_t outgoing;
static bool have_outgoing, inbox_full;

ssize_t vair_send(int fd, const void *data, size_t len, int flags)
{
	(void)fd; (void)flags;
	assert(len <= sizeof packet);
	memcpy(packet, data, len);
	packet_len = len;
	sends++;
	return (ssize_t)len;
}

void vhost_option_register(const vhost_option_t *option) { (void)option; }
void vhost_finish(const char *reason, int code)
{
	assert(code == 4 && expected_error != NULL && strcmp(reason, expected_error) == 0);
	longjmp(refused, 1);
}
void vtrace(const char *fmt, ...) { (void)fmt; }
void vstall_progress(void) {}
void vstall_running(bool on) { (void)on; }
void vwatchdog_feed(void) {}
void vhost_irq_register(unsigned irq, const char *name, vhost_isr_t isr) { (void)irq; (void)name; (void)isr; }
void vhost_irq_raise(unsigned irq) { (void)irq; }
void vtick_isr(void) {}
BaseType_t xTaskGetSchedulerState(void) { return taskSCHEDULER_NOT_STARTED; }
void vTaskDelay(TickType_t ticks) { (void)ticks; }
char *pcTaskGetName(TaskHandle_t task) { (void)task; return "test"; }
void __real_vApplicationIdleHook(void);
void __real_vApplicationIdleHook(void) {}
bool uart_vhost_rx_push(uint64_t at, const uint8_t *bytes, size_t n)
{
	(void)at; (void)bytes; (void)n;
	return true;
}
bool vdmr_outbox_take(vdmr_tx_t *out)
{
	if (!have_outgoing)
		return false;
	*out = outgoing;
	have_outgoing = false;
	return true;
}
bool vdmr_inbox_put(uint64_t start, const dmr_burst_t *burst)
{
	if (inbox_full)
		return false;
	received_start = start;
	received = *burst;
	inbox_count++;
	if (n_order < 4)
		order[n_order++] = VCLOCK_CLASS_AIR;
	return true;
}

static void key(const vchan_message_t *message)
{
	assert(message->cls == VCLOCK_CLASS_INPUT);
	order[n_order++] = message->cls;
}

static vchan_message_t encoded(void)
{
	have_outgoing = true;
	unsigned before = sends;
	for (unsigned i = 0; i < n_done_hooks; i++)
		done_hooks[i]();
	assert(sends == before + 1 && packet[VCHAN_KIND_OFF] == VCHAN_AIR_OUT);
	assert(packet_len == VCHAN_HEADER_SIZE + AIR_PREFIX + outgoing.len);
	vchan_message_t message = { .kind = VCHAN_AIR_IN, .cls = VCLOCK_CLASS_AIR,
		.time_us = 1000, .source = 1, .sequence = 9, .length = AIR_PREFIX + outgoing.len };
	memcpy(message.payload, packet + VCHAN_HEADER_SIZE, message.length);
	return message;
}

static void reject(vchan_message_t message, const char *reason, bool post)
{
	expected_error = reason;
	if (setjmp(refused) == 0) {
		if (post)
			post_input(&message);
		else
			air_in(&message);
		assert(!"invalid air input accepted");
	}
	expected_error = NULL;
}

int main(void)
{
	channel_fd = 99;
	outgoing = (vdmr_tx_t){ .t_us = 500, .ts = 1, .cc = 1, .kind = DMR_BURST_VOICE_LC_HEADER,
		.sync = DMR_SYNC_MS, .len = 12, .host_crc = true };
	for (unsigned i = 0; i < outgoing.len; i++)
		outgoing.payload[i] = (uint8_t)(0x80 + i);
	vchan_message_t message = encoded();
	// A later sequence still sorts ahead of the key because AIR_IN keeps class 20 at the clock.
	vchan_register(VCHAN_KEY, key);
	vchan_message_t press = { .kind = VCHAN_KEY, .cls = VCLOCK_CLASS_INPUT, .time_us = 1000, .sequence = 1 };
	post_input(&press);
	post_input(&message);
	vclock_advance_to(999);
	assert(inbox_count == 0);
	vclock_advance_to(1000);
	assert(inbox_count == 1 && n_order == 2 && order[0] == 20 && order[1] == 30);
	assert(received_start == 500 && received.kind == outgoing.kind && received.crc_ok && received.lc_valid);
	assert(received.ts == 1 && received.cc == 1 && received.sync == DMR_SYNC_MS && received.len == 12);
	assert(memcmp(received.payload, outgoing.payload, 12) == 0);
	assert(memcmp(received.lc, outgoing.payload, 9) == 0);
	puts("PASS air_out_at_done_and_class20_before_key: slot start 500, delivery 1000, payload and LC intact");

	outgoing.kind = DMR_BURST_VOICE;
	outgoing.voice_index = 5;
	outgoing.len = 27;
	outgoing.emb_valid = true;
	memset(outgoing.emb_lc, 0x34, 9);
	message = encoded();
	air_in(&message);
	assert(received.lc_valid && received.voice_index == 5 && received.len == 27);
	assert(memcmp(received.lc, outgoing.emb_lc, 9) == 0);
	message.payload[11] = 1;
	air_in(&message);
	assert(!received.lc_valid);
	message.payload[13] |= 4;
	air_in(&message);
	assert(!received.crc_ok && !received.lc_valid);
	puts("PASS voice_and_corruption: embedded LC only on F, corrupted data never marked CRC-good");

	message.cls = VCLOCK_CLASS_INPUT;
	reject(message, "VCHAN_BAD_CLASS", true);
	press.cls = VCLOCK_CLASS_AIR;
	reject(press, "VCHAN_BAD_CLASS", true);
	message.cls = VCLOCK_CLASS_AIR;
	message.length--;
	reject(message, "VCHAN_BAD_AIR", false);
	message.length++;
	message.payload[0] = 3;
	reject(message, "VCHAN_BAD_AIR", false);
	message.payload[0] = 1;
	memset(message.payload + 2, 255, 8);
	reject(message, "VCHAN_BAD_AIR_TIME", false);
	message = encoded();
	inbox_full = true;
	reject(message, "VCHAN_AIR_INBOX_FULL", false);
	inbox_full = false;
	puts("PASS invalid_air: wrong classes, malformed records, overflowing time and full inbox refused");
	puts("vairTest: all passed");
	return 0;
}
