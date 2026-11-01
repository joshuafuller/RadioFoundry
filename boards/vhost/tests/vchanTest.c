// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The real channel and clock over a Unix packet socket. Each child starts with power-on state.
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>
#include "vchan.h"
#include "vclock.h"
#include "vhost.h"
#include "vport.h"
#include "vstall.h"
#include "vtrace.h"
#include "vwatchdog.h"
#include "vboard.h"
#include "FreeRTOS.h"
#include "task.h"

static const vhost_option_t *channel_option;
void vhost_option_register(const vhost_option_t *option)
{
	if (strcmp(option->name, "--chan") == 0)
		channel_option = option;
}
void vhost_finish(const char *reason, int code)
{
	printf("    reason=%s exit=%d\n", reason, code);
	fflush(stdout);
	_exit(code);
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
	assert(at == vclock_now_us());
	vhost_uart_tx_hook(bytes, n);
	return true;
}

static void echo_handler(const vchan_message_t *message)
{
	assert(message->time_us == vclock_now_us());
	assert(message->round == vclock_round_handled());
	assert(message->source == 19 && message->sequence == (1ull << 40));
	vchan_send(VCHAN_VIEW, message->payload, message->length);
}

static void flush_part(void)
{
	vchan_send(VCHAN_STORE, "flush", 5);
}

static void put(uint8_t *p, uint64_t value, unsigned n)
{
	for (unsigned i = 0; i < n; i++)
		p[i] = (uint8_t)(value >> (8 * i));
}
static uint64_t get(const uint8_t *p, unsigned n)
{
	uint64_t value = 0;
	for (unsigned i = 0; i < n; i++)
		value |= (uint64_t)p[i] << (8 * i);
	return value;
}

typedef struct { int fd; pid_t pid; } radio_t;

static size_t receive_packet(int fd, uint8_t *wire)
{
	ssize_t n = recv(fd, wire, VCHAN_HEADER_SIZE + VCHAN_PAYLOAD_MAX, MSG_TRUNC);
	assert(n >= VCHAN_HEADER_SIZE && n <= VCHAN_HEADER_SIZE + VCHAN_PAYLOAD_MAX);
	assert(wire[0] == VCHAN_VERSION);
	assert((uint64_t)n == VCHAN_HEADER_SIZE + get(wire + 2, 2));
	return (size_t)n;
}

static void expect_done(int fd, uint64_t time, uint64_t next, uint64_t rounds)
{
	uint8_t wire[VCHAN_HEADER_SIZE + VCHAN_PAYLOAD_MAX];
	receive_packet(fd, wire);
	assert(wire[1] == VCHAN_STORE && get(wire + 2, 2) == 5);
	assert(memcmp(wire + VCHAN_HEADER_SIZE, "flush", 5) == 0);
	receive_packet(fd, wire);
	assert(wire[1] == VCHAN_DONE && get(wire + 2, 2) == VCHAN_DONE_SIZE);
	assert(get(wire + 4, 8) == time);
	assert(get(wire + VCHAN_HEADER_SIZE, 8) == next);
	assert(get(wire + VCHAN_HEADER_SIZE + 8, 8) == rounds);
}

static radio_t start_radio(void)
{
	int pair[2];
	assert(socketpair(AF_UNIX, SOCK_SEQPACKET, 0, pair) == 0);
	fflush(stdout);
	pid_t pid = fork();
	assert(pid >= 0);
	if (pid == 0) {
		close(pair[0]);
		alarm(10);
		char fd[24];
		snprintf(fd, sizeof fd, "%d", pair[1]);
		assert(channel_option && channel_option->set(fd));
		vchan_register(VCHAN_KEY, echo_handler);
		vchan_on_done(flush_part);
		vclock_advance_to(100000);
		_exit(9);
	}
	close(pair[1]);
	expect_done(pair[0], 0, VCLOCK_NEVER, 0);
	return (radio_t){ pair[0], pid };
}

// expect_code: 0 for an orderly end (a closed channel lets the radio finish and write its closing STORE, since
// ), 4 for a run the radio already ended with a named error
static void stop_radio(radio_t radio, int expect_code)
{
	close(radio.fd);
	int status;
	assert(waitpid(radio.pid, &status, 0) == radio.pid);
	assert(WIFEXITED(status) && WEXITSTATUS(status) == expect_code);
}

static size_t packet(uint8_t *wire, unsigned kind, uint64_t time, const void *data, size_t n)
{
	memset(wire, 0, VCHAN_HEADER_SIZE);
	wire[0] = VCHAN_VERSION;
	wire[1] = (uint8_t)kind;
	put(wire + 2, n, 2);
	put(wire + 4, time, 8);
	if (n)
		memcpy(wire + VCHAN_HEADER_SIZE, data, n);
	return VCHAN_HEADER_SIZE + n;
}

static void send_packet(int fd, const uint8_t *wire, size_t n)
{
	assert(send(fd, wire, n, MSG_NOSIGNAL) == (ssize_t)n);
}

static void control(int fd, unsigned kind, uint64_t time)
{
	uint8_t wire[VCHAN_HEADER_SIZE];
	send_packet(fd, wire, packet(wire, kind, time, NULL, 0));
}

static void roundtrip(size_t n, unsigned kind)
{
	radio_t radio = start_radio();
	uint8_t wire[VCHAN_HEADER_SIZE + VCHAN_PAYLOAD_MAX], data[VCHAN_PAYLOAD_MAX];
	for (size_t i = 0; i < n; i++)
		data[i] = (uint8_t)i;
	size_t size = packet(wire, kind, 100, data, n);
	put(wire + 12, 7, 4);
	put(wire + 16, VCLOCK_CLASS_INPUT, 4);
	put(wire + 20, 19, 4);
	put(wire + 24, 1ull << 40, 8);
	send_packet(radio.fd, wire, size);
	control(radio.fd, VCHAN_GRANT, 100);
	expect_done(radio.fd, 100, 100, 0);
	control(radio.fd, VCHAN_DRAIN, 100);
	assert(receive_packet(radio.fd, wire) == size);
	assert(wire[1] == (kind == VCHAN_KEY ? VCHAN_VIEW : VCHAN_BYTES_OUT));
	assert(get(wire + 4, 8) == 100 && get(wire + 12, 4) == 7);
	assert(memcmp(wire + VCHAN_HEADER_SIZE, data, n) == 0);
	expect_done(radio.fd, 100, VCLOCK_NEVER, 8);
	control(radio.fd, VCHAN_GRANT, 101);
	expect_done(radio.fd, 101, VCLOCK_NEVER, 0);
	stop_radio(radio, 0);
}

static void named_error(unsigned version, unsigned kind, bool past, bool oversize, const char *reason)
{
	radio_t radio = start_radio();
	uint8_t wire[VCHAN_HEADER_SIZE + VCHAN_PAYLOAD_MAX + 1];
	if (past) {
		control(radio.fd, VCHAN_GRANT, 100);
		expect_done(radio.fd, 100, VCLOCK_NEVER, 0);
	}
	size_t size = packet(wire, kind, 99, "x", 1);
	wire[0] = (uint8_t)version;
	put(wire + 16, VCLOCK_CLASS_INPUT, 4);
	if (oversize) {
		memset(wire + VCHAN_HEADER_SIZE, 0, VCHAN_PAYLOAD_MAX + 1);
		put(wire + 2, VCHAN_PAYLOAD_MAX + 1, 2);
		size = sizeof wire;
	}
	send_packet(radio.fd, wire, size);
	receive_packet(radio.fd, wire);
	assert(wire[1] == VCHAN_RESTART_REASON);
	assert(get(wire + 2, 2) == strlen(reason));
	assert(memcmp(wire + VCHAN_HEADER_SIZE, reason, strlen(reason)) == 0);
	stop_radio(radio, 4);
	printf("PASS %s\n", reason);
}

int main(void)
{
	alarm(30);
	roundtrip(1032, VCHAN_BYTES_IN);
	puts("PASS payload_1032_roundtrip: one packet through BYTES_IN and BYTES_OUT");
	roundtrip(2048, VCHAN_BYTES_IN);
	puts("PASS payload_2048_roundtrip: maximum payload intact");
	roundtrip(1032, VCHAN_KEY);
	puts("PASS registered_stamp_and_done: time, round, source, sequence, VIEW and STORE flush");
	named_error(VCHAN_VERSION, 255, false, false, "VCHAN_UNKNOWN_KIND");
	named_error(VCHAN_VERSION + 1, VCHAN_BYTES_IN, false, false, "VCHAN_BAD_VERSION");
	named_error(VCHAN_VERSION, VCHAN_BYTES_IN, true, false, "VCHAN_PAST_INPUT");
	named_error(VCHAN_VERSION, VCHAN_BYTES_IN, false, true, "VCHAN_BAD_LENGTH");
	puts("# vchanTest: 7 of 7 cases passed");
	return 0;
}
