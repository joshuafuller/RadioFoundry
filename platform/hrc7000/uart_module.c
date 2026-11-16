// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include "config.h"
#if CONFIG_BT_MODULE_TRANSPORT
#include "platform.h"
#include "uart_module.h"
#include "FreeRTOS.h"
#include "task.h"

#define RING_SIZE 2048u
static uint8_t ring[RING_SIZE];
static volatile uint32_t head, tail, errors;
static uint32_t uart_base, uart_baud;
static unsigned uart_irq;
static bool started;

void hrc_module_uart_configure(uint32_t base, unsigned irq, uint32_t baud)
{
	uart_base = base;
	uart_irq = irq;
	uart_baud = baud;
}

static void receive(uint8_t b, bool flagged)
{
	uint32_t next = (head + 1u) & (RING_SIZE - 1u);
	if (flagged || next == tail) {
		errors++;
	} else {
		ring[head] = b;
		head = next;
	}
}

static void interrupt(void)
{
	(void)hrc_uart_rx_isr(uart_base, receive);
}

void hrc_module_uart_flush(void)
{
	if (started) {
		hrc_irq_enable(uart_irq, false);
		head = tail = 0;
		hrc_uart_rx_clear(uart_base);
		hrc_irq_enable(uart_irq, true);
	}
}

uint32_t hrc_module_uart_errors(void)
{
	return errors;
}

static bool start(void)
{
	if (!uart_base) {
		return false;
	}
	if (!started) {
		hrc_uart_setup(uart_base, uart_baud);
		hrc_irq_register(uart_irq, interrupt);
		started = true;
	}
	hrc_module_uart_flush();
	hrc_uart_rx_irq(uart_base, true);
	return true;
}

static bool connected(void)
{
	return started;
}

static size_t read_bytes(uint8_t *buf, size_t max)
{
	size_t n = 0;
	while (n < max && tail != head) {
		buf[n++] = ring[tail];
		tail = (tail + 1u) & (RING_SIZE - 1u);
	}
	return n;
}

static size_t write_bytes(const uint8_t *buf, size_t len)
{
	size_t n = 0;
	while (n < len && (REG32(uart_base + 0x7Cu) & 2u)) {
		REG32(uart_base) = buf[n++];
	}
	return n;
}

static bool wait_rx(uint32_t timeout_ms)
{
	if (head == tail && timeout_ms) {
		vTaskDelay(pdMS_TO_TICKS(timeout_ms));
	}
	return head != tail;
}

const hal_stream_t hrc_module_uart_stream = {
	.start = start, .connected = connected, .read = read_bytes, .write = write_bytes, .wait_rx = wait_rx,
};
#endif
