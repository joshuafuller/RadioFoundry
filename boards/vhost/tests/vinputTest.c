// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Host test of the virtual board's input and power parts (boards/vhost/{uart,keypad,power}_vhost.c): the
// UART's receive FIFO, the key pin levels and the supply latch. The parts are the real files. What they call is
// faked here: a clock with an event queue, an interrupt controller that can mask, a trace buffer and the few RTOS
// calls the UART makes.
#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "vboard.h"
#include "vclock.h"
#include "vhost.h"
#include "vport.h"
#include "vtrace.h"
#include "FreeRTOS.h"
#include "semphr.h"

// ---- the parts' hooks (their files declare them; vboard.h does not)
bool uart_vhost_rx_push(uint64_t t_us, const uint8_t *b, size_t n);
uint32_t uart_vhost_rx_dropped(void);
bool keypad_vhost_set(uint64_t t_us, uint32_t bit, bool down);
static unsigned hook_calls, hook_bytes;
static uint8_t hook_last[16];
void vhost_uart_tx_hook(const uint8_t *b, size_t n);
void vhost_uart_tx_hook(const uint8_t *b, size_t n)	// the strong one replaces the part's weak default
{
	hook_calls++;
	hook_bytes += (unsigned)n;
	memcpy(hook_last, b, n < sizeof hook_last ? n : sizeof hook_last);
}

// ---- fake option registry
static const vhost_option_t *opts[16];
static unsigned n_opts;
void vhost_option_register(const vhost_option_t *opt)
{
	opts[n_opts++] = opt;
}
static bool option(const char *name, const char *value)
{
	for (unsigned i = 0; i < n_opts; i++)
		if (strcmp(opts[i]->name, name) == 0)
			return opts[i]->set(value);
	fprintf(stderr, "no option %s\n", name);
	exit(1);
}

// ---- fake clock: events in (time, class, queue order)
typedef struct { uint64_t t; unsigned cls; unsigned seq; vclock_event_fn fn; uint32_t arg; } ev_t;
static ev_t evs[64];
static unsigned n_evs, seq;
static uint64_t now;
uint64_t vclock_now_us(void) { return now; }
void vclock_at(uint64_t t, unsigned cls, vclock_event_fn fn, uint32_t arg)
{
	evs[n_evs++] = (ev_t){ t < now ? now : t, cls, seq++, fn, arg };
}
void vclock_advance_to(uint64_t t)
{
	for (;;) {
		int best = -1;
		for (unsigned i = 0; i < n_evs; i++)
			if (evs[i].t <= t && (best < 0 || evs[i].t < evs[best].t ||
			    (evs[i].t == evs[best].t && (evs[i].cls < evs[best].cls ||
			    (evs[i].cls == evs[best].cls && evs[i].seq < evs[best].seq)))))
				best = (int)i;
		if (best < 0)
			break;
		ev_t e = evs[best];
		evs[best] = evs[--n_evs];
		now = e.t;
		e.fn(e.arg);
	}
	now = t;
}

// ---- fake interrupt controller: raise while masked waits for unmask
static vhost_isr_t isrs[VHOST_IRQ_COUNT];
static unsigned pending;
static bool irq_off;
void vhost_irq_register(unsigned irq, const char *name, vhost_isr_t isr)
{
	(void)name;
	isrs[irq] = isr;
}
static void deliver(void)
{
	for (unsigned i = 0; !irq_off && i < VHOST_IRQ_COUNT; i++)
		if (pending & (1u << i)) {
			pending &= ~(1u << i);
			if (isrs[i] != NULL)
				isrs[i]();
		}
}
void vhost_irq_raise(unsigned irq)
{
	pending |= 1u << irq;
	deliver();
}
bool vhost_in_isr(void) { return false; }
static void mask(bool off)
{
	irq_off = off;
	deliver();
}
void vPortEnterCritical(void) { irq_off = true; }
void vPortExitCritical(void) { mask(false); }
void vPortYield(void) {}

// ---- fake RTOS semaphore: a count
static int sem_count;
static int sem_obj;
QueueHandle_t xQueueGenericCreate(const UBaseType_t n, const UBaseType_t sz, const uint8_t type)
{
	(void)n; (void)sz; (void)type;
	return (QueueHandle_t)&sem_obj;
}
BaseType_t xQueueGiveFromISR(QueueHandle_t q, BaseType_t *woken)
{
	(void)q;
	sem_count = 1;
	*woken = pdFALSE;
	return pdTRUE;
}
BaseType_t xQueueSemaphoreTake(QueueHandle_t q, TickType_t t)
{
	(void)q; (void)t;
	int had = sem_count;
	sem_count = 0;
	return had ? pdTRUE : pdFALSE;
}

// ---- fake trace and end of run
static char tr[200][96];
static unsigned n_tr;
void vtrace(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	if (n_tr < 200)
		vsnprintf(tr[n_tr++], sizeof tr[0], fmt, ap);
	va_end(ap);
}
void vtrace_isr(const char *name) { (void)name; }
static unsigned count_trace(const char *needle)
{
	unsigned c = 0;
	for (unsigned i = 0; i < n_tr; i++)
		c += strstr(tr[i], needle) != NULL;
	return c;
}
static jmp_buf finished;
static const char *finish_why;
static int finish_code = -1;
static uint64_t finish_at;
void vhost_finish(const char *why, int code)
{
	finish_why = why;
	finish_code = code;
	finish_at = now;
	longjmp(finished, 1);
}

// ---- checks
static unsigned fails, checks;
#define CHECK(c) do { checks++; if (!(c)) { fails++; printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); } } while (0)

static unsigned read_all(uint8_t *buf, unsigned max)
{
	return (unsigned)vboard_host_port.read(buf, max);
}

int main(void)
{
	uint8_t buf[512];

	// The options exist (the parts register them before main in the real program)
	CHECK(n_opts == 4);
	CHECK(option("--host-port", "on"));
	CHECK(vboard_host_port.start());
	vboard_input.init();
	vboard_power.init();

	// UART 1: two host arrivals while interrupts are off are both read, in order
	irq_off = true;
	CHECK(option("--host", "10:a55a0101000044c5"));
	CHECK(option("--host", "10:e00300"));
	vclock_advance_to(10000);
	CHECK(sem_count == 0);	// the bell has not rung: masked
	mask(false);
	CHECK(sem_count == 1);							// now it has, once
	CHECK(vboard_host_port.wait_rx(1));
	unsigned n = read_all(buf, sizeof buf);
	CHECK(n == 11);
	CHECK(memcmp(buf, "\xa5\x5a\x01\x01\x00\x00\x44\xc5\xe0\x03\x00", 11) == 0);
	CHECK(read_all(buf, sizeof buf) == 0);

	// UART 2: a read of part of a message leaves the rest in the FIFO
	CHECK(uart_vhost_rx_push(20000, (const uint8_t *)"abcdef", 6));
	vclock_advance_to(20000);
	CHECK(read_all(buf, 2) == 2 && memcmp(buf, "ab", 2) == 0);
	CHECK(vboard_host_port.wait_rx(0));
	CHECK(read_all(buf, 10) == 4 && memcmp(buf, "cdef", 4) == 0);

	// UART 3: overflow is counted and traced, never silent; the bytes that fit stay, the oldest first
	CHECK(uart_vhost_rx_dropped() == 0);
	uint8_t big[300];
	for (unsigned i = 0; i < sizeof big; i++)
		big[i] = (uint8_t)i;
	CHECK(uart_vhost_rx_push(30000, big, sizeof big));	// longer than one arrival slot: split, in order
	vclock_advance_to(30000);
	CHECK(uart_vhost_rx_dropped() == 300 - 256);
	CHECK(count_trace("BOARD host.rx_overflow lost=") >= 1);
	CHECK(count_trace("total=44") == 1);
	n = read_all(buf, sizeof buf);
	CHECK(n == 256 && memcmp(buf, big, 256) == 0);
	CHECK(!uart_vhost_rx_push(40000, big, 0));		// nothing to push

	// UART 4: every host write is traced in full, then given to the hook once
	uint8_t w[70];
	memset(w, 0xab, sizeof w);
	unsigned t0 = n_tr;
	CHECK(vboard_host_port.write(w, sizeof w) == sizeof w);
	CHECK(n_tr - t0 == 2);					// 64 + 6 bytes: two lines
	CHECK(hook_calls == 1 && hook_bytes == 70 && hook_last[0] == 0xab);
	CHECK(vboard_host_port.write(w, 0) == 0);
	CHECK(hook_calls == 2 && hook_bytes == 70);

	// Keys 1: a key held across two scans reads down in both
	now = 100000;
	CHECK(option("--key", "101:5:10"));		// 101 ms down, 111 ms up (bit 5 = K_MONITOR)
	CHECK(vboard_input.scan() == 0);
	vclock_advance_to(101000);
	CHECK(vboard_input.scan() == (1u << 5));
	vclock_advance_to(105000);
	CHECK(vboard_input.scan() == (1u << 5));
	vclock_advance_to(111000);
	CHECK(vboard_input.scan() == 0);

	// Keys 2: a press and release inside one scan period follow the pin levels
	CHECK(keypad_vhost_set(120000, 4, true));	// PTT
	CHECK(keypad_vhost_set(120500, 4, false));
	vclock_advance_to(119999);
	CHECK(vboard_input.scan() == 0);
	vclock_advance_to(120200);
	CHECK(vboard_input.scan() == (1u << 4));	// a scan between them sees it down
	vclock_advance_to(121000);
	CHECK(vboard_input.scan() == 0);		// a scan after both sees nothing: the pins went back
	CHECK(keypad_vhost_set(130000, 4, true));
	CHECK(keypad_vhost_set(130400, 4, false));
	vclock_advance_to(131000);
	CHECK(vboard_input.scan() == 0);
	CHECK(!keypad_vhost_set(140000, 32, true));	// no such pin

	// Keys 3: two keys down at once, one released
	CHECK(keypad_vhost_set(150000, 0, true));
	CHECK(keypad_vhost_set(150000, 2, true));
	CHECK(keypad_vhost_set(151000, 0, false));
	vclock_advance_to(150000);
	CHECK(vboard_input.scan() == 5u);
	vclock_advance_to(151000);
	CHECK(vboard_input.scan() == 4u);
	CHECK(keypad_vhost_set(152000, 2, false));
	vclock_advance_to(152000);

	// Power: the latch off with the knob on does nothing; with the knob off the run ends 50 ms later
	CHECK(vboard_power.switch_on());
	vboard_power.latch(false);
	vclock_advance_to(200000);
	CHECK(finish_code == -1);
	CHECK(option("--power-off", "300"));
	vclock_advance_to(300000);
	CHECK(!vboard_power.switch_on());
	if (setjmp(finished) == 0) {
		vboard_power.latch(false);			// at 300000 us
		vclock_advance_to(349999);
		CHECK(finish_code == -1);
		vclock_advance_to(400000);
		CHECK(0);					// the run should have ended
	}
	CHECK(finish_code == 0 && finish_at == 350000);
	CHECK(finish_why != NULL && strstr(finish_why, "latch") != NULL);

	printf("vinputTest: %u checks, %u failed\n", checks, fails);
	return fails != 0;
}
