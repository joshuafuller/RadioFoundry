// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual DMR modem (dmr_vhost.h): slot edges on the radio's own clock, a TX queue, an outbox and an inbox.
//   --dmr-phase-us N   the radio's slot phase: its slot edges fall where virtual us % 30000 == N (default 0)
//
// Who runs what, as on the radios (platform/stm32f4/hrc/dmr_glue.c:1-15):
//	slot_edge(), rx_event()  the chip and its slot interrupt work: clock events (class 40 and 20). They change the
//	                         modem's state, push events into a ring and ring the bell. No RTOS call
//	dmr_isr()                the bell: wakes the "dmr" task
//	dmr_task()               drains the ring into the stack's callback (vdmr_deliver), at task level
// The bell is one pending bit (vport.h), so two edges while interrupts are off ring it once. That loses nothing: the
// work and the events are in the ring, made at the edge's own time.
//
// vclock has no cancel. Every clock event carries the generation it was made in; start, stop, rx_restart and a new
// slot phase move the generation on, and an event from an old one does nothing. Only one slot edge is ever pending.
//
// Timing rules (docs/spec/dmr-modem.md 2.3, 2.5, 6):
//	own / ASAP   the first burst at the next edge; then every other slot. The slot we start in becomes the timeslot
//	             asked for (direct mode: the first talker sets the timing)
//	aligned      in step with a signal heard in the last VDMR_ALIGN_HOLD_US. When the slots' numbers are known (a
//	             burst heard with TS1 / TS2), only on slots with the timeslot asked for (ETSI TS 102 361-1 5.1.1,
//	             table 5.1: a subscriber on TSx transmits under TSx); else on the slot of the burst heard (a direct
//	             mode reply). With no signal heard lately it is refused at the next edge, as the HR_C6000 does
//	             (core/dmr/call/dmr_call.c, tx_start_data). A simulator bug ignored the timeslot
//	polite       refused at the next edge when the channel was busy lately: a burst with our CC (polite to CC) or any
//	             burst (polite to all) whose slot ended in the last VDMR_BUSY_US
// Receiving follows the signal (passive timing): a burst heard while we don't transmit, more than VDMR_LOCK_US off our
// slot edges, moves our slot phase onto it, so an aligned reply lands in the sender's slots.
#include <stdio.h>
#include <string.h>
#include "dmr_vhost.h"
#include "vboard.h"
#include "vclock.h"
#include "vhost.h"
#include "vport.h"
#include "vtrace.h"
#include "FreeRTOS.h"
#include "task.h"

#define VDMR_ALIGN_HOLD_US	390000u	// how long the slot timing of a reception is kept (dmr_call.c, tx_start_data)
#define VDMR_BUSY_US		(2u * 2u * VDMR_SLOT_US)	// two frames: a call on air sends a burst every frame
#define VDMR_LOCK_US		500u	// a burst this close to one of our edges is in step with us
#define VDMR_RING_DEPTH		32u	// events between the edge and the "dmr" task
#define DMR_TASK_PRIORITY	(configMAX_PRIORITIES - 2)	// as the radios' glue (dmr_glue.c:30)
#define DMR_TASK_STACK		512

typedef enum { TX_IDLE, TX_PENDING, TX_RUN } tx_state_t;

typedef struct {
	dmr_event_t ev;
	dmr_burst_t burst;
} ring_item_t;

static struct {
	bool started;
	uint32_t gen;			// clock events of an older generation do nothing
	dmr_config_t cfg;
	dmr_event_cb_t cb;
	void *ctx;
	int8_t cc_filter;		// -1 any
	int8_t lock_ts;			// 0 both
	// The slot clock: edge number counter was at last_edge_us; the next is pending at last_edge_us + VDMR_SLOT_US
	uint32_t counter;
	uint64_t last_edge_us;
	bool ts_known;			// the slots' numbers: (counter & 1) == ts1_parity is TS1
	uint8_t ts1_parity;
	bool ref;			// a signal heard: at ref_us, on a slot with parity ref_parity
	uint64_t ref_us;
	uint8_t ref_parity;
	bool heard_cc, heard_any;	// for polite access: the end of the last burst with our CC / of any burst
	uint64_t heard_cc_us, heard_any_us;
	// TX
	tx_state_t tx;
	dmr_timing_t timing;
	int8_t tx_ts;
	dmr_access_t access;
	uint8_t tx_parity;		// TX_RUN: our slots have (counter & 1) == tx_parity
	bool ending;			// tx_end: TX_DONE once the queue is empty and the last slot ended
	bool on_air;			// a burst (or an empty slot taken) in the slot that started at the last edge
	dmr_tx_burst_t q[VDMR_QUEUE_DEPTH];
	uint8_t q_head, q_count;
	bool emb_pending, emb_set;
	uint8_t emb_next[9], emb_cur[9];
	uint32_t underruns;
} m;

static vdmr_tx_t outbox[VDMR_OUTBOX_DEPTH];
static unsigned out_head, out_count;
static uint32_t out_dropped;

typedef struct {
	bool used;
	uint64_t t_us;
	dmr_burst_t b;
} inbox_item_t;
static inbox_item_t inbox[VDMR_INBOX_DEPTH];

static ring_item_t ring[VDMR_RING_DEPTH];
static unsigned ring_head, ring_tail;	// free-running
static uint32_t ring_dropped;

static TaskHandle_t task;
static uint32_t phase_us;

static bool set_phase(const char *value)
{
	unsigned v;
	char extra;
	if (sscanf(value, "%u%c", &v, &extra) != 1 || v >= VDMR_SLOT_US)
		return false;
	phase_us = v;
	return true;
}
VHOST_OPTION("--dmr-phase-us", "N", set_phase, "the DMR modem's slot phase: edges at virtual us % 30000 == N")

// ---- names for the trace

static const char *kind_name(uint8_t kind, uint8_t voice_index)
{
	static const char *const voice[6] = { "voice_A", "voice_B", "voice_C", "voice_D", "voice_E", "voice_F" };
	static const char *const data[] = { "pi_header", "voice_lc_header", "terminator_lc", "csbk", "mbc_header",
		"mbc_cont", "data_header", "rate12_data", "rate34_data", "idle", "rate1_data", "usbd" };
	if (kind == DMR_BURST_VOICE)
		return voice_index < 6 ? voice[voice_index] : "voice_?";
	if (kind == DMR_BURST_EMPTY)
		return "empty";
	return kind < sizeof data / sizeof data[0] ? data[kind] : "unknown";
}

static int8_t slot_ts(uint32_t counter)
{
	if (!m.ts_known)
		return 0;
	return (int8_t)((counter & 1u) == m.ts1_parity ? 1 : 2);
}

// ---- events to the stack (the ring)

static void push(uint8_t kind, int8_t ts, uint32_t arg, const dmr_burst_t *b)
{
	if (ring_head - ring_tail == VDMR_RING_DEPTH) {
		ring_dropped++;
		vtrace("BOARD dmr.event_lost kind=%u total=%lu", kind, (unsigned long)ring_dropped);
		return;
	}
	ring_item_t *it = &ring[ring_head % VDMR_RING_DEPTH];
	memset(it, 0, sizeof *it);
	it->ev.kind = kind;
	it->ev.ts = ts;
	it->ev.arg = arg;
	if (b != NULL)
		it->burst = *b;
	ring_head++;
}

void vdmr_deliver(void)
{
	for (;;) {
		ring_item_t it;
		dmr_event_cb_t cb;
		void *ctx;
		taskENTER_CRITICAL();
		bool any = ring_tail != ring_head;
		if (any)
			it = ring[ring_tail++ % VDMR_RING_DEPTH];
		cb = m.cb;
		ctx = m.ctx;
		taskEXIT_CRITICAL();
		if (!any)
			return;
		if (it.ev.kind == DMR_EV_BURST_RX)
			it.ev.burst = &it.burst;	// valid during the callback
		if (cb != NULL)
			cb(&it.ev, ctx);
	}
}

static void dmr_isr(void)
{
	BaseType_t woken = pdFALSE;
	if (task != NULL)
		vTaskNotifyGiveFromISR(task, &woken);
	portYIELD_FROM_ISR(woken);
}

static void dmr_task(void *arg)
{
	(void)arg;
	for (;;) {
		ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
		vdmr_deliver();
	}
}

static void ring_bell(void)
{
	if (ring_head != ring_tail)
		vhost_irq_raise(VHOST_IRQ_DMR);
}

// ---- the outbox

static void outbox_put(const vdmr_tx_t *r)
{
	if (out_count == VDMR_OUTBOX_DEPTH) {
		out_head++;
		out_count--;
		out_dropped++;
		vtrace("BOARD dmr.outbox_full lost=%lu", (unsigned long)out_dropped);
	}
	outbox[(out_head + out_count) % VDMR_OUTBOX_DEPTH] = *r;
	out_count++;
}

bool vdmr_outbox_take(vdmr_tx_t *out)
{
	if (out_count == 0)
		return false;
	*out = outbox[out_head % VDMR_OUTBOX_DEPTH];
	out_head++;
	out_count--;
	return true;
}

uint32_t vdmr_outbox_dropped(void)
{
	return out_dropped;
}

// ---- the slot edge

static void slot_edge(uint32_t gen);

static void tx_stop_queue(void)
{
	m.tx = TX_IDLE;
	m.ending = false;
	m.q_head = m.q_count = 0;
}

// A pending tx_begin, decided at the edge of slot k: refused (TX_REJECTED) or running with its slot parity
static void tx_decide(uint32_t k, uint64_t t)
{
	const char *why = NULL;
	if (m.access == DMR_ACCESS_POLITE_CC && m.heard_cc && t - m.heard_cc_us < VDMR_BUSY_US)
		why = "busy_cc";
	else if (m.access == DMR_ACCESS_POLITE_ALL && m.heard_any && t - m.heard_any_us < VDMR_BUSY_US)
		why = "busy";
	else if (m.timing == DMR_TIMING_ALIGNED && (!m.ref || t - m.ref_us >= VDMR_ALIGN_HOLD_US))
		why = "no_signal";
	if (why != NULL) {
		vtrace("BOARD dmr.tx_rejected slot=%lu why=%s", (unsigned long)k, why);
		tx_stop_queue();
		push(DMR_EV_TX_REJECTED, m.tx_ts, k, NULL);
		return;
	}
	if (m.timing == DMR_TIMING_ALIGNED) {
		if ((m.tx_ts == 1 || m.tx_ts == 2) && m.ts_known)
			m.tx_parity = (uint8_t)(m.tx_ts == 1 ? m.ts1_parity : m.ts1_parity ^ 1u);
		else
			m.tx_parity = m.ref_parity;
	} else {
		m.tx_parity = (uint8_t)(k & 1u);
		if (m.tx_ts == 1 || m.tx_ts == 2) {	// our own timing: the slot we start in is the timeslot asked for
			m.ts_known = true;
			m.ts1_parity = (uint8_t)(m.tx_ts == 1 ? (k & 1u) : (k & 1u) ^ 1u);
		}
	}
	m.tx = TX_RUN;
	vtrace("BOARD dmr.tx_start slot=%lu parity=%u", (unsigned long)k, m.tx_parity);
}

// Our slot k starts at t: one burst off the queue into the outbox
static void tx_slot(uint32_t k, uint64_t t)
{
	if (m.q_count == 0) {
		m.underruns++;	// a real modem sends silence or nothing; nothing goes on air here
		vtrace("BOARD dmr.tx_underrun slot=%lu total=%lu", (unsigned long)k, (unsigned long)m.underruns);
		return;
	}
	dmr_tx_burst_t b = m.q[m.q_head];
	m.q_head = (uint8_t)((m.q_head + 1u) % VDMR_QUEUE_DEPTH);
	m.q_count--;
	m.on_air = true;
	push(DMR_EV_TX_SLOT_STARTED, slot_ts(k), k, NULL);
	if (b.kind == DMR_BURST_EMPTY) {
		vtrace("BOARD dmr.tx slot=%lu ts=%d kind=empty", (unsigned long)k, slot_ts(k));
		return;		// the slot is taken, nothing on air
	}
	bool voice = b.kind == DMR_BURST_VOICE;
	if (voice && b.voice_index == 0 && m.emb_pending) {	// a new embedded LC from this superframe
		memcpy(m.emb_cur, m.emb_next, sizeof m.emb_cur);
		m.emb_pending = false;
		m.emb_set = true;
	}
	vdmr_tx_t r;
	memset(&r, 0, sizeof r);
	r.t_us = t;
	r.slot = k;
	r.ts = slot_ts(k);
	r.cc = m.cfg.cc;
	r.kind = b.kind;
	r.voice_index = b.voice_index;
	r.sync = (voice && b.voice_index != 0) ? (uint8_t)DMR_SYNC_NONE :
		 b.sync_override != DMR_SYNC_NONE ? b.sync_override : (uint8_t)DMR_SYNC_MS;
	r.host_crc = b.host_crc;
	r.emb_valid = voice && m.emb_set;
	if (r.emb_valid)
		memcpy(r.emb_lc, m.emb_cur, sizeof r.emb_lc);
	r.len = b.len <= DMR_PAYLOAD_MAX ? b.len : DMR_PAYLOAD_MAX;
	memcpy(r.payload, b.payload, r.len);
	r.hash = vtrace_hash(r.payload, r.len);
	vtrace("BOARD dmr.tx slot=%lu ts=%d kind=%s len=%u hash=%016llx", (unsigned long)k, r.ts,
	       kind_name(r.kind, r.voice_index), r.len, (unsigned long long)r.hash);
	outbox_put(&r);
}

static void slot_edge(uint32_t gen)
{
	if (gen != m.gen || !m.started)
		return;		// an edge of a slot clock that was stopped or moved
	uint64_t t = vclock_now_us();
	uint32_t k = ++m.counter;
	if (k == 0)
		k = m.counter = 1;	// the counter is never 0 (hal_dmr_modem.h, DMR_EV_BURST_RX)
	m.last_edge_us = t;
	vtrace("BOARD dmr.slot %lu ts=%d", (unsigned long)k, slot_ts(k));
	if (m.on_air) {		// the slot before was ours: its burst has ended
		m.on_air = false;
		push(DMR_EV_TX_SLOT_ENDED, slot_ts(k - 1u), k - 1u, NULL);
	}
	push(DMR_EV_SLOT_TICK, slot_ts(k), k, NULL);
	if (m.tx == TX_RUN && m.ending && m.q_count == 0) {
		vtrace("BOARD dmr.tx_done slot=%lu", (unsigned long)k);
		tx_stop_queue();
		push(DMR_EV_TX_DONE, slot_ts(k), k, NULL);
	}
	if (m.tx == TX_PENDING)
		tx_decide(k, t);
	if (m.tx == TX_RUN && (k & 1u) == m.tx_parity)
		tx_slot(k, t);
	ring_bell();
	vclock_at(t + VDMR_SLOT_US, VCLOCK_CLASS_OWN, slot_edge, m.gen);
}

// ---- receive

static void rx_drop(const dmr_burst_t *b, const char *why)
{
	vtrace("BOARD dmr.rx_drop why=%s cc=%u kind=%s", why, b->cc, kind_name(b->kind, b->voice_index));
}

// The end of a received burst's slot (class 20): everything a burst changes in this radio happens here
static void rx_event(uint32_t idx)
{
	inbox_item_t e = inbox[idx];
	inbox[idx].used = false;
	const dmr_burst_t *b = &e.b;
	uint64_t now = vclock_now_us();
	if (!m.started) {
		rx_drop(b, "off");
		return;
	}
	m.heard_any = true;
	m.heard_any_us = now;
	if (m.tx == TX_RUN || m.on_air) {
		rx_drop(b, "tx");	// no tx_listen: the modem does not receive while it transmits
		return;
	}
	if (m.cc_filter >= 0 && b->cc != (uint8_t)m.cc_filter) {
		rx_drop(b, "cc");
		return;
	}
	if (m.cfg.cc < 0 || b->cc == (uint8_t)m.cfg.cc) {
		m.heard_cc = true;
		m.heard_cc_us = now;
	}
	// Which of our slots it was heard in: the edge nearest its start. Off our edges: follow the signal (passive
	// timing), the next edge at the end of its slot, which is now
	int64_t d = (int64_t)e.t_us - (int64_t)m.last_edge_us;
	int64_t n = d >= 0 ? (d + VDMR_SLOT_US / 2) / VDMR_SLOT_US : -((-d + VDMR_SLOT_US / 2) / VDMR_SLOT_US);
	int64_t off = d - n * (int64_t)VDMR_SLOT_US;
	int64_t kh = (int64_t)m.counter + n;
	if (off > (int64_t)VDMR_LOCK_US || off < -(int64_t)VDMR_LOCK_US) {
		m.gen++;
		m.last_edge_us = now - VDMR_SLOT_US;
		kh = m.counter;
		vtrace("BOARD dmr.rephase next_edge=%llu", (unsigned long long)now);
		vclock_at(now, VCLOCK_CLASS_OWN, slot_edge, m.gen);
	}
	uint32_t k = kh > 0 ? (uint32_t)kh : 0u;
	m.ref = true;
	m.ref_us = now;
	m.ref_parity = (uint8_t)(k & 1u);
	if (b->ts == 1 || b->ts == 2) {
		m.ts_known = true;
		m.ts1_parity = (uint8_t)(b->ts == 1 ? (k & 1u) : (k & 1u) ^ 1u);
	}
	int8_t ts = b->ts != 0 ? b->ts : slot_ts(k);
	if ((m.lock_ts == 1 || m.lock_ts == 2) && ts != 0 && ts != m.lock_ts) {
		rx_drop(b, "ts");
		return;
	}
	dmr_burst_t got = *b;
	got.ts = ts;
	got.in_step = true;
	if (got.len > DMR_PAYLOAD_MAX)
		got.len = DMR_PAYLOAD_MAX;
	vtrace("BOARD dmr.rx slot=%lu ts=%d cc=%u kind=%s len=%u hash=%016llx", (unsigned long)k, ts, got.cc,
	       kind_name(got.kind, got.voice_index), got.len, (unsigned long long)vtrace_hash(got.payload, got.len));
	push(DMR_EV_BURST_RX, ts, k, &got);
	ring_bell();
}

bool vdmr_inbox_put(uint64_t t_us, const dmr_burst_t *b)
{
	uint64_t end = t_us + VDMR_SLOT_US;
	if (b == NULL || end < vclock_now_us())
		return false;
	for (unsigned i = 0; i < VDMR_INBOX_DEPTH; i++) {
		if (!inbox[i].used) {
			inbox[i].used = true;
			inbox[i].t_us = t_us;
			inbox[i].b = *b;
			vclock_post(end, VCLOCK_CLASS_AIR, VCLOCK_SOURCE_RADIO, rx_event, i);
			return true;
		}
	}
	return false;
}

// ---- the ops

static bool d_start(const dmr_config_t *cfg, dmr_event_cb_t cb, void *ctx)
{
	if (task == NULL) {	// the first start: as the radios' glue, which makes its task once (dmr_glue.c:396-398)
		vhost_irq_register(VHOST_IRQ_DMR, "dmr", dmr_isr);
		if (xTaskCreate(dmr_task, "dmr", DMR_TASK_STACK, NULL, DMR_TASK_PRIORITY, &task) != pdPASS) {
			vtrace("BOARD dmr.start refused (no task)");
			task = NULL;
			return false;
		}
	}
	uint64_t now = vclock_now_us();
	uint64_t first = now - now % VDMR_SLOT_US + phase_us;
	if (first <= now)
		first += VDMR_SLOT_US;
	taskENTER_CRITICAL();
	m.gen++;
	m.started = true;
	m.cfg = *cfg;
	m.cb = cb;
	m.ctx = ctx;
	m.cc_filter = cfg->cc;
	m.lock_ts = 0;
	m.ts_known = m.ref = m.heard_cc = m.heard_any = false;
	m.on_air = false;
	m.emb_pending = m.emb_set = false;
	tx_stop_queue();
	m.last_edge_us = first - VDMR_SLOT_US;
	ring_tail = ring_head;
	taskEXIT_CRITICAL();
	vtrace("BOARD dmr.start cc=%d repeater=%u access=%u first_edge=%llu task=%s", cfg->cc, cfg->repeater,
	       (unsigned)cfg->access, (unsigned long long)first,
	       xTaskGetSchedulerState() == taskSCHEDULER_NOT_STARTED ? "main" : pcTaskGetName(NULL));
	vclock_at(first, VCLOCK_CLASS_OWN, slot_edge, m.gen);
	return true;
}

static void d_stop(void)
{
	taskENTER_CRITICAL();
	m.gen++;
	m.started = false;
	m.cb = NULL;
	m.on_air = false;
	tx_stop_queue();
	ring_tail = ring_head;
	taskEXIT_CRITICAL();
	vtrace("BOARD dmr.stop");
}

static void d_configure(const dmr_config_t *cfg)
{
	m.cfg = *cfg;
	vtrace("BOARD dmr.configure cc=%d repeater=%u access=%u", cfg->cc, cfg->repeater, (unsigned)cfg->access);
}

// Drop the timing learned from signals; the slot clock runs on and follows the next signal heard
static void d_rx_restart(void)
{
	m.ts_known = m.ref = false;
	vtrace("BOARD dmr.rx_restart");
}

static void d_rx_lock_slot(int8_t ts)
{
	m.lock_ts = (ts == 1 || ts == 2) ? ts : 0;
	vtrace("BOARD dmr.lock_ts %d", m.lock_ts);
}

// Never false while started: a refusal comes as DMR_EV_TX_REJECTED at the next edge, so the stack meets one path only
static bool d_tx_begin(dmr_timing_t timing, int8_t ts, dmr_access_t access)
{
	if (!m.started) {
		vtrace("BOARD dmr.tx_begin refused (not started)");
		return false;
	}
	taskENTER_CRITICAL();
	tx_stop_queue();
	m.tx = TX_PENDING;
	m.timing = timing;
	m.tx_ts = ts;
	m.access = access;
	taskEXIT_CRITICAL();
	vtrace("BOARD dmr.tx_begin timing=%u ts=%d access=%u", (unsigned)timing, ts, (unsigned)access);
	return true;
}

static bool d_tx_queue(const dmr_tx_burst_t *b)
{
	bool ok;
	taskENTER_CRITICAL();
	ok = m.tx != TX_IDLE && m.q_count < VDMR_QUEUE_DEPTH;
	if (ok) {
		m.q[(m.q_head + m.q_count) % VDMR_QUEUE_DEPTH] = *b;
		m.q_count++;
	}
	taskEXIT_CRITICAL();
	return ok;
}

static uint8_t d_tx_queue_space(void)
{
	return (uint8_t)(VDMR_QUEUE_DEPTH - m.q_count);
}

static void d_tx_end(void)
{
	if (m.tx != TX_IDLE)
		m.ending = true;
	vtrace("BOARD dmr.tx_end queued=%u", m.q_count);
}

static void d_tx_abort(void)
{
	taskENTER_CRITICAL();
	tx_stop_queue();
	taskEXIT_CRITICAL();
	vtrace("BOARD dmr.tx_abort");
}

static void d_set_embedded_lc(const uint8_t lc[9])
{
	memcpy(m.emb_next, lc, sizeof m.emb_next);
	m.emb_pending = true;
}

static void d_set_cc_filter(int8_t cc)
{
	m.cc_filter = cc;
	vtrace("BOARD dmr.cc_filter %d", cc);
}

static bool d_is_transmitting(void)
{
	return m.tx != TX_IDLE || m.on_air;
}

static const hal_dmr_modem_t modem = {
	.start = d_start,
	.stop = d_stop,
	.configure = d_configure,
	.rx_restart = d_rx_restart,
	.rx_lock_slot = d_rx_lock_slot,
	.tx_begin = d_tx_begin,
	.tx_queue = d_tx_queue,
	.tx_queue_space = d_tx_queue_space,
	.tx_end = d_tx_end,
	.tx_abort = d_tx_abort,
	.set_embedded_lc = d_set_embedded_lc,
	.set_cc_filter = d_set_cc_filter,
	.is_transmitting = d_is_transmitting,
	// tx_listen, tx_fill and the split duplex ops stay NULL: this modem can't, so the roles are not offered
};

const hal_dmr_modem_t *vhost_part_dmr(void)
{
	return &modem;
}
