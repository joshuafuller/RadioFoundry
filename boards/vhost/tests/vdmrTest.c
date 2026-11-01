// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Host test of the virtual DMR modem (boards/vhost/dmr_vhost.c). The test includes the modem's file, so it
// sees its state. What the modem calls is faked: a clock with the real order key (time, round, class, sequence), an
// interrupt controller, the trace and the few RTOS calls. The "dmr" task is the fake notify: it delivers the events at
// once, as the real task does (it runs right after the interrupt, at the same virtual time).
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../dmr_vhost.c"

static int failures;
#define CHECK(c)                                                                  \
	do {                                                                      \
		if (!(c)) {                                                       \
			printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);        \
			failures++;                                               \
		}                                                                 \
	} while (0)

// ---- fake option registry
static const vhost_option_t *opts[8];
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
	printf("FAIL no option %s\n", name);
	exit(1);
}

// ---- fake clock with the real key: an event made while T is handled and due at T joins the next round
typedef struct { uint64_t t; unsigned round, cls, seq; vclock_event_fn fn; uint32_t arg; } ev_t;
static ev_t evs[128];
static unsigned n_evs, seq_no, cur_round;
static uint64_t now;
uint64_t vclock_now_us(void) { return now; }
void vclock_post(uint64_t t, unsigned cls, unsigned source, vclock_event_fn fn, uint32_t arg)
{
	(void)source;
	if (t < now) {
		printf("FAIL event in the past\n");
		exit(1);
	}
	evs[n_evs++] = (ev_t){ t, t == now ? cur_round + 1 : 0, cls, seq_no++, fn, arg };
}
void vclock_at(uint64_t t, unsigned cls, vclock_event_fn fn, uint32_t arg)
{
	vclock_post(t, cls, VCLOCK_SOURCE_RADIO, fn, arg);
}
static bool ev_before(const ev_t *a, const ev_t *b)
{
	if (a->t != b->t)
		return a->t < b->t;
	if (a->round != b->round)
		return a->round < b->round;
	if (a->cls != b->cls)
		return a->cls < b->cls;
	return a->seq < b->seq;
}
static void advance_to(uint64_t t)
{
	for (;;) {
		int best = -1;
		for (unsigned i = 0; i < n_evs; i++)
			if (evs[i].t <= t && (best < 0 || ev_before(&evs[i], &evs[best])))
				best = (int)i;
		if (best < 0)
			break;
		ev_t e = evs[best];
		evs[best] = evs[--n_evs];
		if (e.t > now)
			cur_round = 0;
		now = e.t;
		cur_round = e.round;
		e.fn(e.arg);
	}
	if (t > now)
		cur_round = 0;
	now = t;
}

// ---- fake interrupt controller and RTOS
static vhost_isr_t isrs[VHOST_IRQ_COUNT];
void vhost_irq_register(unsigned irq, const char *name, vhost_isr_t isr)
{
	(void)name;
	isrs[irq] = isr;
}
void vhost_irq_raise(unsigned irq)
{
	if (isrs[irq] != NULL)
		isrs[irq]();
}
bool vhost_in_isr(void) { return false; }
void vPortEnterCritical(void) {}
void vPortExitCritical(void) {}
void vPortYield(void) {}
static int task_obj;
BaseType_t xTaskCreate(TaskFunction_t fn, const char *const name, const configSTACK_DEPTH_TYPE depth, void *const arg,
		       UBaseType_t prio, TaskHandle_t *const h)
{
	(void)fn; (void)name; (void)depth; (void)arg; (void)prio;
	*h = (TaskHandle_t)&task_obj;
	return pdPASS;
}
void vTaskGenericNotifyGiveFromISR(TaskHandle_t h, UBaseType_t index, BaseType_t *woken)
{
	(void)h; (void)index;
	*woken = pdFALSE;
	vdmr_deliver();		// the "dmr" task runs now
}
uint32_t ulTaskGenericNotifyTake(UBaseType_t index, BaseType_t clear, TickType_t wait)
{
	(void)index; (void)clear; (void)wait;
	return 0;
}
BaseType_t xTaskGetSchedulerState(void) { return taskSCHEDULER_RUNNING; }
char *pcTaskGetName(TaskHandle_t h)
{
	(void)h;
	return (char *)"radio";
}

// ---- fake trace
static char tr[512][128];
static unsigned n_tr;
void vtrace(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	if (n_tr < 512)
		vsnprintf(tr[n_tr++], sizeof tr[0], fmt, ap);
	va_end(ap);
}
uint64_t vtrace_hash(const void *data, size_t n)
{
	const uint8_t *p = data;
	uint64_t h = 0xcbf29ce484222325ull;
	for (size_t i = 0; i < n; i++)
		h = (h ^ p[i]) * 0x100000001b3ull;
	return h;
}
static bool traced(const char *s)
{
	for (unsigned i = 0; i < n_tr; i++)
		if (strstr(tr[i], s) != NULL)
			return true;
	return false;
}

// ---- the stack's side: every event with its time
typedef struct { uint64_t t; dmr_event_t ev; dmr_burst_t b; } got_t;
static got_t got[256];
static unsigned n_got;
static void cb(const dmr_event_t *ev, void *ctx)
{
	(void)ctx;
	got_t *g = &got[n_got < 256 ? n_got++ : 255];
	g->t = now;
	g->ev = *ev;
	if (ev->burst != NULL)
		g->b = *ev->burst;
	g->ev.burst = NULL;
}
static int find(uint8_t kind, unsigned from)
{
	for (unsigned i = from; i < n_got; i++)
		if (got[i].ev.kind == kind)
			return (int)i;
	return -1;
}
static unsigned count(uint8_t kind)
{
	unsigned n = 0;
	for (unsigned i = 0; i < n_got; i++)
		n += got[i].ev.kind == kind;
	return n;
}

static const hal_dmr_modem_t *md;

// A fresh start at time t: the modem stopped and started, the record cleared. Returns the first edge's time
static uint64_t fresh(uint64_t t, int8_t cc)
{
	md->stop();
	advance_to(t);
	n_got = 0;
	n_tr = 0;
	vdmr_tx_t r;
	while (vdmr_outbox_take(&r)) {
	}
	dmr_config_t c = { .own_id = 1234567, .cc = cc, .access = DMR_ACCESS_IMPOLITE, .repeater = false };
	CHECK(md->start(&c, cb, NULL));
	md->set_cc_filter(cc);
	return m.last_edge_us + VDMR_SLOT_US;
}

static dmr_tx_burst_t burst(uint8_t kind, uint8_t index)
{
	dmr_tx_burst_t b;
	memset(&b, 0, sizeof b);
	b.kind = kind;
	b.voice_index = index;
	b.len = kind == DMR_BURST_VOICE ? 27 : 12;
	for (unsigned i = 0; i < b.len; i++)
		b.payload[i] = (uint8_t)(kind * 16 + index + i);
	return b;
}

static dmr_burst_t rx(uint8_t cc, int8_t ts)
{
	dmr_burst_t b;
	memset(&b, 0, sizeof b);
	b.kind = DMR_BURST_VOICE_LC_HEADER;
	b.sync = DMR_SYNC_MS;
	b.cc = cc;
	b.ts = ts;
	b.crc_ok = true;
	b.len = 12;
	return b;
}

static void test_slot_edges(void)
{
	CHECK(option("--dmr-phase-us", "7000"));
	CHECK(!option("--dmr-phase-us", "30000"));
	uint64_t first = fresh(100000, 1);
	CHECK(first == 127000);		// the first edge after the start with us % 30000 == 7000
	advance_to(first + 9 * VDMR_SLOT_US);
	CHECK(count(DMR_EV_SLOT_TICK) == 10);
	int prev = -1;
	for (unsigned i = 0, n = 0; i < n_got; i++) {
		if (got[i].ev.kind != DMR_EV_SLOT_TICK)
			continue;
		CHECK(got[i].t == first + n * VDMR_SLOT_US);
		CHECK(got[i].ev.arg != 0);
		CHECK(prev < 0 || got[i].ev.arg == (uint32_t)prev + 1u);
		prev = (int)got[i].ev.arg;
		n++;
	}
	CHECK(traced("dmr.slot"));
	CHECK(option("--dmr-phase-us", "0"));
}

static void test_outbox_at_slot_start(void)
{
	uint64_t e = fresh(1000000, 1);
	CHECK(md->tx_begin(DMR_TIMING_OWN, 1, DMR_ACCESS_IMPOLITE));
	dmr_tx_burst_t b = burst(DMR_BURST_VOICE_LC_HEADER, 0);
	CHECK(md->tx_queue(&b));
	vdmr_tx_t r;
	advance_to(e - 1);
	CHECK(!vdmr_outbox_take(&r));		// not before its slot
	advance_to(e);
	CHECK(vdmr_outbox_take(&r));
	CHECK(r.t_us == e);
	CHECK(r.kind == DMR_BURST_VOICE_LC_HEADER);
	CHECK(r.ts == 1);
	CHECK(r.sync == DMR_SYNC_MS);
	CHECK(r.len == 12 && memcmp(r.payload, b.payload, 12) == 0);
	int s = find(DMR_EV_TX_SLOT_STARTED, 0);
	int k = find(DMR_EV_SLOT_TICK, 0);
	CHECK(s >= 0 && got[s].t == e && got[s].ev.arg == r.slot);
	CHECK(k >= 0 && got[k].ev.arg == r.slot);
	CHECK(md->is_transmitting());
	CHECK(traced("dmr.tx slot=") && traced("kind=voice_lc_header"));
	md->tx_abort();
}

static void test_queue_depth(void)
{
	fresh(2000000, 1);
	dmr_tx_burst_t b = burst(DMR_BURST_VOICE, 0);
	CHECK(!md->tx_queue(&b));		// no tx_begin: nowhere to send it
	CHECK(md->tx_begin(DMR_TIMING_OWN, 1, DMR_ACCESS_IMPOLITE));
	CHECK(md->tx_queue_space() == 8);
	for (int i = 0; i < 8; i++)
		CHECK(md->tx_queue(&b));
	CHECK(md->tx_queue_space() == 0);
	CHECK(!md->tx_queue(&b));		// the ninth
	md->tx_abort();
}

static void test_tx_done_after_last_slot(void)
{
	uint64_t e = fresh(3000000, 1);
	CHECK(md->tx_begin(DMR_TIMING_OWN, 1, DMR_ACCESS_IMPOLITE));
	dmr_tx_burst_t a = burst(DMR_BURST_VOICE_LC_HEADER, 0), t = burst(DMR_BURST_TERMINATOR_LC, 0);
	CHECK(md->tx_queue(&a));
	CHECK(md->tx_queue(&t));
	md->tx_end();
	advance_to(e + 2 * VDMR_SLOT_US + VDMR_SLOT_US - 1);
	CHECK(count(DMR_EV_TX_SLOT_STARTED) == 2);	// at e and e + 60 ms: every other slot
	CHECK(find(DMR_EV_TX_DONE, 0) < 0);		// the terminator is still on air
	vdmr_tx_t r1, r2;
	CHECK(vdmr_outbox_take(&r1) && vdmr_outbox_take(&r2));
	CHECK(r1.t_us == e && r2.t_us == e + 2 * VDMR_SLOT_US && r2.kind == DMR_BURST_TERMINATOR_LC);
	advance_to(e + 3 * VDMR_SLOT_US);
	int d = find(DMR_EV_TX_DONE, 0);
	CHECK(d >= 0 && got[d].t == e + 3 * VDMR_SLOT_US);	// the end of the last burst's slot
	int en = find(DMR_EV_TX_SLOT_ENDED, (unsigned)find(DMR_EV_TX_SLOT_ENDED, 0) + 1u);
	CHECK(en >= 0 && en < d && got[en].t == got[d].t);
	CHECK(!md->is_transmitting());
	advance_to(e + 8 * VDMR_SLOT_US);
	CHECK(count(DMR_EV_TX_DONE) == 1);
}

static void test_inbox_end_of_slot(void)
{
	uint64_t e = fresh(4000000, 1);
	uint64_t t0 = e + 2 * VDMR_SLOT_US;	// a burst on air from one of our edges
	dmr_burst_t b = rx(1, 0);
	CHECK(vdmr_inbox_put(t0, &b));
	advance_to(t0 + VDMR_SLOT_US - 1);
	CHECK(find(DMR_EV_BURST_RX, 0) < 0);	// not before its slot ends
	advance_to(t0 + VDMR_SLOT_US);
	int i = find(DMR_EV_BURST_RX, 0);
	CHECK(i >= 0 && got[i].t == t0 + VDMR_SLOT_US);
	CHECK(i >= 0 && got[i].b.cc == 1 && got[i].b.in_step && got[i].b.kind == DMR_BURST_VOICE_LC_HEADER);
	// arg: the slot it was heard in, the tick at t0
	int k = find(DMR_EV_SLOT_TICK, 0);
	while (k >= 0 && got[k].t != t0)
		k = find(DMR_EV_SLOT_TICK, (unsigned)k + 1u);
	CHECK(k >= 0 && i >= 0 && got[i].ev.arg == got[k].ev.arg && got[i].ev.arg != 0);
	// air (class 20) before the slot edge (class 40) at the same us
	int k2 = find(DMR_EV_SLOT_TICK, (unsigned)k + 1u);
	CHECK(k2 > i && got[k2].t == got[i].t);
	CHECK(traced("dmr.rx slot="));
	CHECK(!vdmr_inbox_put(now - 2 * VDMR_SLOT_US, &b));	// its slot ended already
}

static void test_cc_filter(void)
{
	uint64_t e = fresh(5000000, 1);
	dmr_burst_t b = rx(2, 0);
	CHECK(vdmr_inbox_put(e, &b));
	advance_to(e + 4 * VDMR_SLOT_US);
	CHECK(find(DMR_EV_BURST_RX, 0) < 0);
	CHECK(traced("dmr.rx_drop why=cc"));
	md->set_cc_filter(-1);			// any
	CHECK(vdmr_inbox_put(now, &b));
	advance_to(now + VDMR_SLOT_US);
	CHECK(find(DMR_EV_BURST_RX, 0) >= 0);
}

static void test_tx_rejected(void)
{
	uint64_t e = fresh(6000000, 1);
	dmr_burst_t b = rx(1, 0);
	CHECK(vdmr_inbox_put(e, &b));		// our CC on the channel, heard at e + 30 ms
	advance_to(e + VDMR_SLOT_US);
	CHECK(md->tx_begin(DMR_TIMING_OWN, 1, DMR_ACCESS_POLITE_CC));	// never refused here
	dmr_tx_burst_t h = burst(DMR_BURST_VOICE_LC_HEADER, 0);
	CHECK(md->tx_queue(&h));
	unsigned before = n_got;
	advance_to(e + 2 * VDMR_SLOT_US);
	int r = find(DMR_EV_TX_REJECTED, before);
	CHECK(r >= 0 && got[r].t == e + 2 * VDMR_SLOT_US);	// at the next edge
	CHECK(find(DMR_EV_TX_SLOT_STARTED, 0) < 0);
	vdmr_tx_t o;
	CHECK(!vdmr_outbox_take(&o));
	CHECK(!md->is_transmitting());
	CHECK(traced("dmr.tx_rejected") && traced("why=busy_cc"));
	// Long after the call: polite access lets it go
	advance_to(e + 20 * VDMR_SLOT_US);
	CHECK(md->tx_begin(DMR_TIMING_OWN, 1, DMR_ACCESS_POLITE_CC));
	CHECK(md->tx_queue(&h));
	advance_to(now + VDMR_SLOT_US);
	CHECK(vdmr_outbox_take(&o));
	md->tx_abort();
}

// Aligned timing puts the burst on the timeslot asked for (ETSI TS 102 361-1 5.1.1)
static void aligned_on(int8_t want)
{
	uint64_t e = fresh(7000000 + (uint64_t)want * 1000000u, 1);
	dmr_burst_t b = rx(1, 2);		// a repeater's TS2, from our edge e
	CHECK(vdmr_inbox_put(e, &b));
	advance_to(e + VDMR_SLOT_US);
	int i = find(DMR_EV_BURST_RX, 0);
	CHECK(i >= 0 && got[i].ev.ts == 2);
	CHECK(md->tx_begin(DMR_TIMING_ALIGNED, want, DMR_ACCESS_IMPOLITE));
	dmr_tx_burst_t h = burst(DMR_BURST_VOICE_LC_HEADER, 0);
	CHECK(md->tx_queue(&h));
	CHECK(md->tx_queue(&h));
	advance_to(e + 6 * VDMR_SLOT_US);
	vdmr_tx_t o1, o2;
	CHECK(vdmr_outbox_take(&o1) && vdmr_outbox_take(&o2));
	CHECK(o1.ts == want && o2.ts == want);
	CHECK(((o1.t_us - e) / VDMR_SLOT_US) % 2u == (want == 2 ? 0u : 1u));	// e was TS2
	CHECK(o2.t_us == o1.t_us + 2 * VDMR_SLOT_US);
	int s = find(DMR_EV_TX_SLOT_STARTED, 0);
	CHECK(s >= 0 && got[s].ev.ts == want);
	md->tx_abort();
}

static void test_aligned(void)
{
	aligned_on(2);
	aligned_on(1);
	// No signal to align to: refused at the next edge
	uint64_t e = fresh(10000000, 1);
	CHECK(md->tx_begin(DMR_TIMING_ALIGNED, 1, DMR_ACCESS_IMPOLITE));
	advance_to(e);
	int r = find(DMR_EV_TX_REJECTED, 0);
	CHECK(r >= 0 && got[r].t == e);
	CHECK(traced("why=no_signal"));
}

// A burst off our edges moves the slot phase onto the signal (passive timing)
static void test_rephase(void)
{
	uint64_t e = fresh(11000000, 1);
	uint64_t t0 = e + 10000;
	dmr_burst_t b = rx(1, 0);
	CHECK(vdmr_inbox_put(t0, &b));
	advance_to(t0 + 4 * VDMR_SLOT_US);
	unsigned n = 0;
	for (unsigned i = 0; i < n_got; i++)
		if (got[i].ev.kind == DMR_EV_SLOT_TICK && got[i].t >= t0 + VDMR_SLOT_US) {
			CHECK((got[i].t - t0) % VDMR_SLOT_US == 0);
			n++;
		}
	CHECK(n == 4);
	CHECK(traced("dmr.rephase"));
	// The old phase's edges are gone: one tick per slot
	CHECK(count(DMR_EV_SLOT_TICK) == 6);	// e, e + 30 ms, then t0 + 30, 60, 90, 120 ms
}

static void test_ops(void)
{
	CHECK(md->tx_listen == NULL && md->tx_fill == NULL);	// can't: the roles are not offered
	md->stop();
	CHECK(!md->tx_begin(DMR_TIMING_OWN, 1, DMR_ACCESS_IMPOLITE));	// not started
	unsigned before = n_got;
	advance_to(now + 10 * VDMR_SLOT_US);
	CHECK(n_got == before);			// stopped: no edges, no events
}

int main(void)
{
	md = vhost_part_dmr();
	test_slot_edges();
	test_outbox_at_slot_start();
	test_queue_depth();
	test_tx_done_after_last_slot();
	test_inbox_end_of_slot();
	test_cc_filter();
	test_tx_rejected();
	test_aligned();
	test_rephase();
	test_ops();
	if (failures != 0) {
		printf("vdmrTest: %d failed\n", failures);
		return 1;
	}
	printf("vdmrTest: all passed\n");
	return 0;
}
