// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Real world-input handlers, keypad and clock. The boundary case uses a scan at tick class, as the input task does.
#include <setjmp.h>
#include <stdarg.h>
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

static vchan_handler_fn handlers[VCHAN_STORE + 1];
static const vhost_option_t *encoder_option;
static vhost_isr_t key_isr;
static unsigned irqs, checks, failed;
static jmp_buf finished;
static const char *finish_reason;
static int finish_code;

#define CHECK(c) do { checks++; if (!(c)) { failed++; printf("FAIL line %d: %s\n", __LINE__, #c); } } while (0)

void vchan_register(unsigned kind, vchan_handler_fn handler) { handlers[kind] = handler; }
void vhost_option_register(const vhost_option_t *opt)
{
	if (strcmp(opt->name, "--encoder") == 0)
		encoder_option = opt;
}
void vhost_irq_register(unsigned irq, const char *name, vhost_isr_t isr)
{
	(void)name;
	CHECK(irq == VHOST_IRQ_KEY);
	key_isr = isr;
}
void vhost_irq_raise(unsigned irq)
{
	CHECK(irq == VHOST_IRQ_KEY);
	irqs++;
	if (key_isr != NULL)
		key_isr();
}
void vtrace(const char *fmt, ...)
{
	if (strstr(fmt, "key.scan") == NULL)
		return;
	printf("%012llu ", (unsigned long long)vclock_now_us());
	va_list ap;
	va_start(ap, fmt);
	vprintf(fmt, ap);
	va_end(ap);
	putchar('\n');
}
void vhost_finish(const char *why, int code)
{
	finish_reason = why;
	finish_code = code;
	longjmp(finished, 1);
}
void vstall_progress(void) {}
void vwatchdog_feed(void) {}
void vtick_isr(void) {}
BaseType_t xTaskGetSchedulerState(void) { return taskSCHEDULER_NOT_STARTED; }
void vTaskDelay(TickType_t ticks) { (void)ticks; }
char *pcTaskGetName(TaskHandle_t task) { (void)task; return "test"; }
void __real_vApplicationIdleHook(void);
void __real_vApplicationIdleHook(void) {}

static vchan_message_t messages[32];
static unsigned n_messages;
static void deliver(uint32_t slot) { handlers[messages[slot].kind](&messages[slot]); }

static void post_key(uint64_t time, uint8_t bit, bool down)
{
	unsigned slot = n_messages++;
	messages[slot] = (vchan_message_t){ .kind = VCHAN_KEY, .time_us = time, .length = 2,
		.cls = VCLOCK_CLASS_INPUT, .source = 1, .sequence = slot,
		.payload = { bit, down } };
	vclock_post_stamped(time, 0, VCLOCK_CLASS_INPUT, 1, slot, deliver, slot);
}

static void post_encoder(uint64_t time, int32_t count)
{
	unsigned slot = n_messages++;
	messages[slot] = (vchan_message_t){ .kind = VCHAN_ENC, .time_us = time, .length = 4,
		.cls = VCLOCK_CLASS_INPUT, .source = 1, .sequence = slot };
	for (unsigned i = 0; i < 4; i++)
		messages[slot].payload[i] = (uint8_t)((uint32_t)count >> (8 * i));
	vclock_post_stamped(time, 0, VCLOCK_CLASS_INPUT, 1, slot, deliver, slot);
}

static void expect_bad(vchan_message_t message, const char *reason)
{
	if (setjmp(finished) == 0) {
		handlers[message.kind](&message);
		CHECK(false);
	} else {
		CHECK(finish_code == 4 && strcmp(finish_reason, reason) == 0);
	}
}

static uint32_t scan_at_tick;
static void tick_scan(uint32_t arg) { (void)arg; scan_at_tick = vboard_input.scan(); }
static void expect_pins(uint32_t pins) { CHECK(vboard_input.scan() == pins); }

int main(void)
{
	CHECK(handlers[VCHAN_KEY] != NULL && handlers[VCHAN_ENC] != NULL);
	CHECK(encoder_option != NULL);
	CHECK(vhost_part_encoder() == NULL);
	expect_bad((vchan_message_t){ .kind = VCHAN_ENC, .length = 4 }, "VINPUT_NO_ENCODER");
	CHECK(!encoder_option->set("invalid"));
	CHECK(encoder_option->set("on"));
	vboard_input.encoder = vhost_part_encoder();
	CHECK(vboard_input.encoder != NULL);
	vboard_input.init();

	unsigned before = failed;
	post_encoder(1000, 300);
	CHECK(vboard_input.encoder() == 0);
	vclock_advance_to(1000);
	CHECK(vboard_input.encoder() == 127);
	CHECK(vboard_input.encoder() == 127);
	CHECK(vboard_input.encoder() == 46);
	CHECK(vboard_input.encoder() == 0);
	printf("%s +300 detents: 127, 127, 46 over three scans\n", failed == before ? "PASS" : "FAIL");

	before = failed;
	post_encoder(2000, -300);
	vclock_advance_to(2000);
	CHECK(vboard_input.encoder() == -127);
	CHECK(vboard_input.encoder() == -127);
	CHECK(vboard_input.encoder() == -46);
	post_encoder(3000, INT32_MAX);
	post_encoder(3000, INT32_MAX);
	post_encoder(3000, INT32_MIN);
	post_encoder(3000, -INT32_MAX);
	vclock_advance_to(3000);
	CHECK(vboard_input.encoder() == -1);
	CHECK(vboard_input.encoder() == 0);
	CHECK(irqs == 6);
	printf("%s negative detents and accumulation beyond int32\n", failed == before ? "PASS" : "FAIL");

	before = failed;
	post_key(11000, 0, true);
	post_key(16000, 0, false);
	vclock_advance_to(10000);
	CHECK(vboard_input.scan() == 0);
	vclock_advance_to(20000);
	CHECK(vboard_input.scan() == 0);
	printf("%s key down/up 5 ms apart between two scans is not seen\n", failed == before ? "PASS" : "FAIL");

	before = failed;
	post_key(21000, 4, true);
	post_key(21000, 31, true);
	post_key(22000, 31, false);
	vclock_advance_to(21000);
	CHECK(vboard_input.scan() == ((1u << 4) | (1u << 31)));
	vclock_advance_to(22000);
	CHECK(vboard_input.scan() == (1u << 4));
	post_key(11022000, 4, false);
	vclock_advance_to(11021000);
	CHECK(vboard_input.scan() == (1u << 4));
	vclock_advance_to(11022000);
	CHECK(vboard_input.scan() == 0);
	printf("%s chords, bit 31 and PTT held beyond 10 s\n", failed == before ? "PASS" : "FAIL");

	before = failed;
	expect_bad((vchan_message_t){ .kind = VCHAN_KEY, .length = 1 }, "VINPUT_BAD_KEY");
	expect_bad((vchan_message_t){ .kind = VCHAN_KEY, .length = 2, .payload = { 32, 1 } }, "VINPUT_BAD_KEY");
	expect_bad((vchan_message_t){ .kind = VCHAN_KEY, .length = 2, .payload = { 0, 2 } }, "VINPUT_BAD_KEY");
	expect_bad((vchan_message_t){ .kind = VCHAN_ENC, .length = 3 }, "VINPUT_BAD_ENC");
	CHECK(encoder_option->set("off"));
	CHECK(vhost_part_encoder() == NULL);
	printf("%s malformed payloads and encoder profile\n", failed == before ? "PASS" : "FAIL");

	before = failed;
	post_key(12000000, 0, true);
	vclock_at(12000000, VCLOCK_CLASS_TICK, tick_scan, 0);
	vclock_advance_to(12000000);
	CHECK(scan_at_tick == 1);
	printf("%s key at scan boundary: tick scan pins=%08x, expected 00000001\n",
		failed == before ? "PASS" : "FAIL", (unsigned)scan_at_tick);

	before = failed;
	unsigned slot = n_messages++;
	messages[slot] = (vchan_message_t){ .kind = VCHAN_KEY, .time_us = 12001000, .length = 2,
		.round = 1, .cls = VCLOCK_CLASS_INPUT, .source = 17, .sequence = 100, .payload = { 0, 0 } };
	vclock_post_stamped(12001000, 1, VCLOCK_CLASS_INPUT, 17, 100, deliver, slot);
	vclock_post_stamped(12001000, 0, VCLOCK_CLASS_TICK, 0, 1, expect_pins, 1);
	vclock_post_stamped(12001000, 1, VCLOCK_CLASS_INPUT, 17, 99, expect_pins, 1);
	vclock_post_stamped(12001000, 1, VCLOCK_CLASS_INPUT, 17, 101, expect_pins, 0);
	vclock_advance_to(12001000);
	CHECK(!keypad_vhost_set_now(32, true));
	printf("%s world edge keeps conductor round and sequence\n", failed == before ? "PASS" : "FAIL");
	printf("vencoderTest: %u checks, %u failed\n", checks, failed);
	return failed != 0;
}
