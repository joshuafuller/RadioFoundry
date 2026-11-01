// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// One packet socket for the grant gate and every virtual radio part. No control operation enters the trace.
#include <errno.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include "vchan.h"
#include "vclock.h"
#include "vhost.h"
#include "vstall.h"
#include "vtrace.h"
#include "vboard.h"

#define MAX_INPUTS 64
#define MAX_DONE_HOOKS 16

static int channel_fd = -1;
static uint64_t output_sequence;
static vchan_handler_fn handlers[VCHAN_STORE + 1];
static vchan_done_fn done_hooks[MAX_DONE_HOOKS];
static unsigned n_done_hooks;
static vchan_message_t pending[MAX_INPUTS];
static bool used[MAX_INPUTS];
static bool drained;
static bool draining;
static bool reason_sent;

static uint64_t get_le(const uint8_t *p, unsigned n)
{
	uint64_t value = 0;
	for (unsigned i = 0; i < n; i++)
		value |= (uint64_t)p[i] << (8 * i);
	return value;
}

static void put_le(uint8_t *p, uint64_t value, unsigned n)
{
	for (unsigned i = 0; i < n; i++)
		p[i] = (uint8_t)(value >> (8 * i));
}

static void fail(const char *reason) __attribute__((noreturn));
static void fail(const char *reason)
{
	if (!reason_sent) {
		reason_sent = true;
		vchan_send(VCHAN_RESTART_REASON, reason, strlen(reason));
	}
	vhost_finish(reason, 4);
}

void vchan_send(unsigned kind, const void *buf, size_t len)
{
	if (channel_fd < 0)
		return;
	if (kind < VCHAN_GRANT || kind > VCHAN_STORE)
		fail("VCHAN_UNKNOWN_KIND");
	if (len > VCHAN_PAYLOAD_MAX || (len && buf == NULL))
		fail("VCHAN_BAD_LENGTH");
	uint8_t wire[VCHAN_HEADER_SIZE + VCHAN_PAYLOAD_MAX] = { 0 };
	wire[VCHAN_VERSION_OFF] = VCHAN_VERSION;
	wire[VCHAN_KIND_OFF] = (uint8_t)kind;
	put_le(wire + VCHAN_LENGTH_OFF, len, 2);
	put_le(wire + VCHAN_TIME_OFF, vclock_now_us(), 8);
	int64_t round = vclock_round_handled();
	put_le(wire + VCHAN_ROUND_OFF, round < 0 ? 0 : (uint64_t)round, 4);
	put_le(wire + VCHAN_SEQUENCE_OFF, output_sequence++, 8);
	if (len)
		memcpy(wire + VCHAN_HEADER_SIZE, buf, len);
	ssize_t sent;
	do {
		sent = send(channel_fd, wire, VCHAN_HEADER_SIZE + len, MSG_NOSIGNAL);
	} while (sent < 0 && errno == EINTR);
	if (sent != (ssize_t)(VCHAN_HEADER_SIZE + len)) {
		channel_fd = -1;	// the END trace must not try to use a broken socket
		vhost_finish("VCHAN_SEND_FAILED", 4);
	}
}

static bool input_kind(unsigned kind)
{
	return kind == VCHAN_BYTES_IN || kind == VCHAN_KEY || kind == VCHAN_ENC || kind == VCHAN_ETH_IN ||
		kind == VCHAN_AIR_IN || kind == VCHAN_VIEW || kind == VCHAN_STORE;
}

void vchan_register(unsigned kind, vchan_handler_fn handler)
{
	if (!input_kind(kind) || kind == VCHAN_BYTES_IN || handler == NULL || handlers[kind] != NULL)
		fail("VCHAN_BAD_REGISTRATION");
	handlers[kind] = handler;
}

void vchan_on_done(vchan_done_fn fn)
{
	if (fn == NULL || n_done_hooks == MAX_DONE_HOOKS)
		fail("VCHAN_BAD_DONE_HOOK");
	done_hooks[n_done_hooks++] = fn;
}

static bool set_channel(const char *value)
{
	char *end;
	errno = 0;
	long fd = strtol(value, &end, 10);
	if (channel_fd >= 0 || *value == '\0' || *end != '\0' || errno || fd < 0 || fd > INT_MAX)
		return false;
	int type;
	socklen_t size = sizeof type;
	struct sockaddr_storage address;
	socklen_t address_size = sizeof address;
	if (getsockopt((int)fd, SOL_SOCKET, SO_TYPE, &type, &size) != 0 || type != SOCK_SEQPACKET ||
		getpeername((int)fd, (struct sockaddr *)&address, &address_size) != 0 || address.ss_family != AF_UNIX)
		return false;
	channel_fd = (int)fd;
	vclock_set_horizon(0);
	return true;
}
VHOST_OPTION("--chan", "FD", set_channel, "connected Unix packet socket for virtual time grants")

static void receive(vchan_message_t *message)
{
	uint8_t wire[VCHAN_HEADER_SIZE + VCHAN_PAYLOAD_MAX];
	ssize_t n;
	do {
		n = recv(channel_fd, wire, sizeof wire, MSG_TRUNC);
	} while (n < 0 && errno == EINTR);
	if (n == 0)
		vhost_finish("channel closed", 0);	// the peer still reads the finish hooks' output
	if (n < 0) {
		channel_fd = -1;
		vhost_finish("VCHAN_DISCONNECTED", 4);
	}
	if (n < VCHAN_HEADER_SIZE || n > (ssize_t)sizeof wire)
		fail("VCHAN_BAD_LENGTH");
	if (wire[VCHAN_VERSION_OFF] != VCHAN_VERSION)
		fail("VCHAN_BAD_VERSION");
	message->kind = wire[VCHAN_KIND_OFF];
	if (message->kind < VCHAN_GRANT || message->kind > VCHAN_STORE)
		fail("VCHAN_UNKNOWN_KIND");
	message->length = (uint16_t)get_le(wire + VCHAN_LENGTH_OFF, 2);
	if (message->length > VCHAN_PAYLOAD_MAX || n != VCHAN_HEADER_SIZE + message->length)
		fail("VCHAN_BAD_LENGTH");
	message->time_us = get_le(wire + VCHAN_TIME_OFF, 8);
	message->round = (uint32_t)get_le(wire + VCHAN_ROUND_OFF, 4);
	message->cls = (uint32_t)get_le(wire + VCHAN_CLASS_OFF, 4);
	message->source = (uint32_t)get_le(wire + VCHAN_SOURCE_OFF, 4);
	message->sequence = get_le(wire + VCHAN_SEQUENCE_OFF, 8);
	memcpy(message->payload, wire + VCHAN_HEADER_SIZE, message->length);
}

static void deliver(uint32_t slot)
{
	const vchan_message_t *message = &pending[slot];
	if (message->kind == VCHAN_BYTES_IN) {
		if (!uart_vhost_rx_push(message->time_us, message->payload, message->length))
			fail("VCHAN_UART_FULL");
	} else {
		handlers[message->kind](message);
	}
	used[slot] = false;
}

static void post_input(const vchan_message_t *message)
{
	if (!input_kind(message->kind))
		fail("VCHAN_BAD_DIRECTION");
	if (message->cls != (message->kind == VCHAN_AIR_IN ? VCLOCK_CLASS_AIR : VCLOCK_CLASS_INPUT))
		fail("VCHAN_BAD_CLASS");
	if (message->time_us < vclock_now_us())
		fail("VCHAN_PAST_INPUT");
	if (message->time_us == vclock_now_us()) {
		if (drained)
			fail("VCHAN_INPUT_AFTER_DRAIN");
		if ((int64_t)message->round <= vclock_round_handled())
			fail("VCHAN_PAST_ROUND");
	}
	if (message->time_us == VCLOCK_NEVER)
		fail("VCHAN_BAD_TIME");
	if (message->kind != VCHAN_BYTES_IN && handlers[message->kind] == NULL)
		fail("VCHAN_UNHANDLED_KIND");
	if (message->kind == VCHAN_BYTES_IN && message->length == 0)
		fail("VCHAN_BAD_LENGTH");
	unsigned slot;
	for (slot = 0; slot < MAX_INPUTS && used[slot]; slot++)
		;
	if (slot == MAX_INPUTS)
		fail("VCHAN_INPUT_QUEUE_FULL");
	pending[slot] = *message;
	used[slot] = true;
	vclock_post_stamped(message->time_us, message->round, message->cls, message->source,
		message->sequence, deliver, slot);
}

void vclock_horizon_reached(uint64_t now_us)
{
	if (channel_fd < 0)
		vhost_finish("virtual time limit", 0);
	drained = draining;
	draining = false;
	for (unsigned i = 0; i < n_done_hooks; i++)
		done_hooks[i]();
	uint8_t done[VCHAN_DONE_SIZE];
	put_le(done, vclock_next_due_us(), 8);
	put_le(done + 8, (uint64_t)(vclock_round_handled() + 1), 8);
	vstall_running(false);
	vchan_send(VCHAN_DONE, done, sizeof done);
	for (;;) {
		vchan_message_t message;
		receive(&message);
		if (message.kind == VCHAN_GRANT || message.kind == VCHAN_DRAIN) {
			if (message.length || message.round || message.cls || message.source || message.sequence)
				fail("VCHAN_BAD_CONTROL");
			if (message.kind == VCHAN_GRANT) {
				if (message.time_us <= now_us || message.time_us == VCLOCK_NEVER)
					fail("VCHAN_BAD_GRANT");
				vclock_set_horizon(message.time_us);
			} else {
				if (message.time_us != now_us || drained)
					fail("VCHAN_BAD_DRAIN");
				draining = true;
				vclock_drain_horizon();
			}
			vstall_running(true);
			return;
		}
		post_input(&message);
	}
}

void vhost_uart_tx_hook(const uint8_t *bytes, size_t n)
{
	vchan_send(VCHAN_BYTES_OUT, bytes, n);
}

void vtrace_line_hook(const char *line, size_t n)
{
	vchan_send(VCHAN_TRACE, line, n);
	// vhost_finish publishes its reason in END; this also covers resets from board parts we do not own.
	if (!reason_sent && n >= 28 && memcmp(line + 24, "END ", 4) == 0) {
		reason_sent = true;
		vchan_send(VCHAN_RESTART_REASON, line + 28, n - 28);
	}
}
