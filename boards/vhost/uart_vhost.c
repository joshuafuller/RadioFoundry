// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual board's host port (vboard.h): a UART whose receive interrupt gives a semaphore the host task waits on.
// Every byte the firmware writes is a trace line.
//   --host-port on   the host port starts (else start() fails and the host task deletes itself)
//   --host MS:HEX    bytes from the host at MS: they land in the receive FIFO, then the interrupt wakes the host task
// Hooks for later parts (declare them in the part's own file, they are not in vboard.h): uart_vhost_rx_push (bytes at
// a virtual time), uart_vhost_rx_dropped (the overflow count), the weak vhost_uart_tx_hook (every host write).
#include <stdio.h>
#include <string.h>
#include "vboard.h"
#include "vclock.h"
#include "vhost.h"
#include "vport.h"
#include "vtrace.h"
#include "FreeRTOS.h"
#include "semphr.h"

// The receive FIFO is device state, like the UART's on the radio: bytes land in it at their virtual time and stay
// there until the firmware reads them. The interrupt is only a bell (it gives the semaphore), so two arrivals while
// interrupts are off lose nothing. A full FIFO drops the new byte, counts it and traces it
#define RX_FIFO_SIZE	256u	// bytes; a power of two
#define MAX_ARRIVALS	64u	// arrivals waiting for their time
#define ARRIVAL_BYTES	128u	// bytes in one arrival (a longer push is split, in order, at one time)

static bool host_enabled;
static SemaphoreHandle_t rx_sem;
static uint8_t rx_fifo[RX_FIFO_SIZE];
static volatile unsigned rx_head, rx_tail;	// free-running; the fill is head - tail
static uint32_t rx_dropped;			// bytes the FIFO had no room for, over the whole run
static uint8_t arrival_data[MAX_ARRIVALS][ARRIVAL_BYTES];
static uint8_t arrival_len[MAX_ARRIVALS];	// 0: the slot is free
static unsigned host_msgs;

// The bytes of arrival arg reach the FIFO, then the bell rings
static void arrival_event(uint32_t arg)
{
	unsigned lost = 0;
	for (unsigned i = 0; i < arrival_len[arg]; i++) {
		if (rx_head - rx_tail == RX_FIFO_SIZE)
			lost++;
		else
			rx_fifo[rx_head++ % RX_FIFO_SIZE] = arrival_data[arg][i];
	}
	arrival_len[arg] = 0;
	if (lost != 0) {
		rx_dropped += lost;
		vtrace("BOARD host.rx_overflow lost=%u total=%lu", lost, (unsigned long)rx_dropped);
	}
	vhost_irq_raise(VHOST_IRQ_HOST_RX);
}

// Bytes from the host arrive at virtual time t_us (class 30, input). False when n is 0 or too many arrivals wait.
// For the scenario option below and for later parts (the control channel's debug link)
bool uart_vhost_rx_push(uint64_t t_us, const uint8_t *b, size_t n)
{
	unsigned need = (unsigned)((n + ARRIVAL_BYTES - 1) / ARRIVAL_BYTES), slot[MAX_ARRIVALS], got = 0;
	if (n == 0)
		return false;
	for (unsigned i = 0; i < MAX_ARRIVALS && got < need; i++)
		if (arrival_len[i] == 0)
			slot[got++] = i;
	if (got < need)
		return false;
	for (unsigned k = 0; k < need; k++) {
		size_t part = n - k * ARRIVAL_BYTES < ARRIVAL_BYTES ? n - k * ARRIVAL_BYTES : ARRIVAL_BYTES;
		memcpy(arrival_data[slot[k]], b + k * ARRIVAL_BYTES, part);
		arrival_len[slot[k]] = (uint8_t)part;
	}
	for (unsigned k = 0; k < need; k++)
		vclock_at(t_us, VCLOCK_CLASS_INPUT, arrival_event, slot[k]);
	return true;
}

// Bytes the FIFO dropped so far
uint32_t uart_vhost_rx_dropped(void)
{
	return rx_dropped;
}

// Called for every host write, with all its bytes, after the trace lines. Do not call the host port from it. A later
// part (the control channel) defines the strong one
__attribute__((weak)) void vhost_uart_tx_hook(const uint8_t *b, size_t n);
__attribute__((weak)) void vhost_uart_tx_hook(const uint8_t *b, size_t n)
{
	(void)b;
	(void)n;
}

static bool set_host_port(const char *value)
{
	host_enabled = strcmp(value, "on") == 0;
	return host_enabled || strcmp(value, "off") == 0;
}
VHOST_OPTION("--host-port", "on|off", set_host_port, "the host port starts (default off)")

#define MAX_HOST_MSGS 16

static bool set_host(const char *value)
{
	unsigned ms;
	char hex[260];
	uint8_t msg[ARRIVAL_BYTES];
	if (host_msgs == MAX_HOST_MSGS || sscanf(value, "%u:%259s", &ms, hex) != 2)
		return false;
	size_t n = strlen(hex) / 2;
	if (n > sizeof(msg) || strlen(hex) % 2 != 0)
		return false;
	for (size_t i = 0; i < n; i++) {
		unsigned b;
		if (sscanf(hex + 2 * i, "%2x", &b) != 1)
			return false;
		msg[i] = (uint8_t)b;
	}
	host_msgs++;
	return uart_vhost_rx_push((uint64_t)ms * 1000u, msg, n);
}
VHOST_OPTION("--host", "MS:HEX", set_host, "bytes from the host at MS ms")

static void host_rx_isr(void)
{
	BaseType_t woken = pdFALSE;
	if (rx_sem != NULL)
		xSemaphoreGiveFromISR(rx_sem, &woken);
	portYIELD_FROM_ISR(woken);
}

static bool h_start(void)
{
	vtrace("BOARD host.start %s", host_enabled ? "up" : "refused");
	if (!host_enabled)
		return false;
	rx_sem = xSemaphoreCreateBinary();
	vhost_irq_register(VHOST_IRQ_HOST_RX, "host_rx", host_rx_isr);
	return rx_sem != NULL;
}

static bool h_connected(void)
{
	return host_enabled;
}

static size_t h_read(uint8_t *buf, size_t max)
{
	size_t n = 0;
	taskENTER_CRITICAL();
	while (n < max && rx_tail != rx_head)
		buf[n++] = rx_fifo[rx_tail++ % RX_FIFO_SIZE];
	taskEXIT_CRITICAL();
	return n;
}

static bool h_wait_rx(uint32_t ms)
{
	if (rx_tail != rx_head)
		return true;
	(void)xSemaphoreTake(rx_sem, pdMS_TO_TICKS(ms));
	return rx_tail != rx_head;
}

// Every byte, in lines of 64 (defined for len 0 too)
static size_t h_write(const uint8_t *buf, size_t len)
{
	size_t i = 0;
	do {
		char hex[2 * 64 + 1] = "";
		size_t n = len - i < 64 ? len - i : 64;
		for (size_t k = 0; k < n; k++)
			snprintf(hex + 2 * k, 3, "%02x", buf[i + k]);
		vtrace("BOARD host.write %zu @%zu %s", len, i, hex);
		i += n;
	} while (i < len);
	vhost_uart_tx_hook(buf, len);
	return len;
}

const hal_stream_t vboard_host_port = {
	.start = h_start, .connected = h_connected, .read = h_read, .wait_rx = h_wait_rx, .write = h_write,
};
