// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The real link wrap, with the firmware status call, trace and channel controlled by this test.
#include <assert.h>
#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ui/ui_app.h"
#include "vchan.h"
#include "vclock.h"
#include "vhost.h"
#include "vtrace.h"
#include "vview.h"

static vtrace_line_fn trace_hook;
static ui_app_status_t next;
static uint64_t now = 1000000;
static unsigned calls, packets, traces;
static uint8_t saved[16][VCHAN_PAYLOAD_MAX];
static size_t lengths[16];
static char last_trace[512];
static jmp_buf failure;
static const char *expected_failure;

void __wrap_ui_app_status(ui_app_status_t *status);
void ui_app_status(ui_app_status_t *status) { *status = next; calls++; }
uint64_t vclock_now_us(void) { return now; }
void vtrace_line_hook_register(vtrace_line_fn fn) { assert(trace_hook == NULL); trace_hook = fn; }
uint64_t vtrace_hash(const void *data, size_t n)
{
	const uint8_t *p = data;
	uint64_t hash = 0;
	while (n--)
		hash = hash * 31 + *p++;
	return hash;
}
void vtrace(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(last_trace, sizeof last_trace, fmt, ap);
	va_end(ap);
	assert(strncmp(last_trace, "VIEW ", 5) == 0);
	traces++;
}
void vchan_send(unsigned kind, const void *buf, size_t len)
{
	assert(kind == VCHAN_VIEW && len <= VCHAN_PAYLOAD_MAX && packets < 16);
	memcpy(saved[packets], buf, len);
	lengths[packets++] = len;
}
void vhost_finish(const char *why, int code)
{
	if (expected_failure == NULL || strcmp(why, expected_failure) != 0 || code != 3) {
		fprintf(stderr, "unexpected finish: %s (%d)\n", why, code);
		abort();
	}
	longjmp(failure, 1);
}
static uint64_t get(const uint8_t *p, unsigned n)
{
	uint64_t value = 0;
	for (unsigned i = 0; i < n; i++)
		value |= (uint64_t)p[i] << (8 * i);
	return value;
}
static void observe(unsigned added)
{
	ui_app_status_t status;
	unsigned before = packets, before_calls = calls;
	__wrap_ui_app_status(&status);
	assert(calls == before_calls + 1 && packets == before + added && traces == packets);
	assert(status.screen == next.screen && status.query == next.query && status.draws == next.draws);
}
static void expect_failure(const char *reason)
{
	expected_failure = reason;
	if (setjmp(failure) == 0) {
		observe(0);
		assert(!"missing failure");
	}
	expected_failure = NULL;
}

int main(void)
{
	char screen[16] = "home", banner[32] = "Incoming", toast[32] = "Saved", query[32] = "12";
	next = (ui_app_status_t){ .screen = screen, .banner = banner, .toast = toast, .query = query,
		.depth = 1, .focus = 258, .count = 513, .locked = true };
	expect_failure("VIEW_NO_FRAME");
	const char *frame = "000001000000 0000001000 FRAME radio=7 boot=3 id=8 us=1000000 crc=00000000";
	trace_hook(frame, strlen(frame));
	observe(1);
	assert(get(saved[0] + VVIEW_RADIO_OFF, 4) == 7 && get(saved[0] + VVIEW_BOOT_OFF, 4) == 3);
	assert(get(saved[0] + VVIEW_ID_OFF, 4) == 8 && get(saved[0] + VVIEW_TIME_OFF, 8) == now);
	assert(saved[0][VVIEW_DEPTH_OFF] == 1 && saved[0][VVIEW_LOCKED_OFF] == 1);
	assert(get(saved[0] + VVIEW_FOCUS_OFF, 2) == 258 && get(saved[0] + VVIEW_COUNT_OFF, 2) == 513);
	const char text[] = "home\0Incoming\0Saved\0" "12";
	assert(lengths[0] == VVIEW_HEADER_SIZE + sizeof text);
	assert(memcmp(saved[0] + VVIEW_HEADER_SIZE, text, sizeof text) == 0);
	assert(strstr(last_trace, "radio=7 boot=3 id=8 us=1000000 screen=home") != NULL);

	strcpy(screen, "hub");
	strcpy(banner, "Overwrite");
	strcpy(toast, "Overwrite");
	strcpy(query, "99");
	assert(memcmp(saved[0] + VVIEW_HEADER_SIZE, text, sizeof text) == 0);
	strcpy(screen, "home");
	strcpy(banner, "Incoming");
	strcpy(toast, "Saved");
	strcpy(query, "12");
	observe(0);
	now++;
	frame = "000001000001 0000001000 FRAME radio=7 boot=3 id=9 us=1000001 crc=00000000";
	trace_hook(frame, strlen(frame));
	next.draws++;
	next.field_use = true;
	next.strip_drawn = true;
	observe(0);
	puts("unchanged status (including a new frame): 0 records; overwritten strings: stored record unchanged");

	strcpy(screen, "hub");
	observe(1);
	assert(strcmp((char *)saved[1] + VVIEW_HEADER_SIZE, "hub") == 0);
	assert(get(saved[1] + VVIEW_ID_OFF, 4) == 9 && get(saved[1] + VVIEW_TIME_OFF, 8) == now);
	puts("screen home -> hub: 1 record, current frame/time, real status returned unchanged");
	next.depth++;
	observe(1);
	next.focus++;
	observe(1);
	next.count++;
	observe(1);
	next.locked = false;
	observe(1);
	strcpy(banner, "Call");
	observe(1);
	strcpy(toast, "OK");
	observe(1);
	strcpy(query, "123");
	observe(1);
	next.banner = next.toast = next.query = NULL;
	observe(1);
	next.banner = next.toast = next.query = "";
	observe(0);

	char large[VCHAN_PAYLOAD_MAX];
	memset(large, 'q', sizeof large);
	size_t room = VCHAN_PAYLOAD_MAX - VVIEW_HEADER_SIZE - strlen(screen) - 1 - 2;
	large[room - 1] = '\0';
	next.query = large;
	observe(1);
	assert(lengths[packets - 1] == VCHAN_PAYLOAD_MAX);
	large[room - 1] = 'q';
	large[room] = '\0';
	unsigned before = packets;
	expect_failure("VIEW_TOO_LARGE");
	assert(packets == before && traces == packets);
	puts("all eight fields detected; null text normalized; full payload preserved; overflow refused: PASSED");
	return 0;
}
