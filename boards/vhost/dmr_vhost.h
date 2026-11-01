// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual DMR modem (boards/vhost/dmr_vhost.c): a hal_dmr_modem_t whose slot edges are events on the radio's own
// virtual clock, with an outbox of the bursts it sends and an inbox for bursts from other radios.
//
// What a DMR modem is. The chip that turns bursts into radio signals and back. A burst is one 30 ms slot of DMR data
// (ETSI TS 102 361-1 4.2; docs/spec/dmr-modem.md 1.2). Slots alternate TS1, TS2, TS1, ...; one talker sends one burst
// every 60 ms, on every other slot (dmr-modem.md 1.6). The firmware's DMR stack (core/dmr/call/dmr_call.c) talks to
// the modem only through hal_dmr_modem_t (hal/hal_dmr_modem.h): it queues bursts and gets events.
//
// How this one works. Example: the radio starts the modem at 4 610 000 us with the phase option 0. The first slot
// edge is at 4 620 000 us, the next at 4 650 000 us, and so on, each one an event of class 40 (the radio's own
// hardware, ADR 0005). At each edge the modem model:
//	1. ends the burst sent in the slot before (DMR_EV_TX_SLOT_ENDED), or after the last one DMR_EV_TX_DONE;
//	2. decides a pending tx_begin: accepted, or refused with DMR_EV_TX_REJECTED (never a false from tx_begin);
//	3. on our slot, takes one burst off the TX queue (depth 8), writes it to the outbox with this edge's time and
//	   raises DMR_EV_TX_SLOT_STARTED;
//	4. raises DMR_EV_SLOT_TICK, and rings the modem interrupt (VHOST_IRQ_DMR).
// The interrupt only wakes the "dmr" task, which calls the stack's callback from task level, never from the
// interrupt (hal_dmr_modem.h:86), as the radios' glue does (platform/stm32f4/hrc/dmr_glue.c:397).
//
// Not modelled, and so NULL in the ops, so the firmware never offers the role: tx_listen and tx_fill (an SFR's gaps)
// and the split duplex bs_* ops. That is an explicit "can't", never a fake success.
#ifndef DMR_VHOST_H
#define DMR_VHOST_H

#include <stdbool.h>
#include <stdint.h>
#include "hal_dmr_modem.h"

#define VDMR_SLOT_US		30000u	// one slot (ETSI TS 102 361-1 clause 4.2: 30 ms)
#define VDMR_QUEUE_DEPTH	8u	// the TX queue (hal_dmr_modem.h, tx_queue: "false if the queue is full (depth >= 8)")
#define VDMR_OUTBOX_DEPTH	64u	// sent bursts kept for the conductor; one second of a call is about 17
#define VDMR_INBOX_DEPTH	32u	// received bursts waiting for the end of their slot

// The lookahead: the shortest time from a burst starting on air at one radio to the first event this modem raises in
// any receiver (07-brief, "How long a grant may be"). The world controller may grant every radio up to this much time
// at once, once a later check proves it (docs/dev/virtual-radio-architecture.md:191-192, 210-212). Stated here, not
// yet used by anyone. The argument, one step at a time:
//	1. A sending radio's burst exists only from its slot edge on. slot_edge() in dmr_vhost.c pops it off the TX queue
//	   and writes the outbox record at that edge, stamped with the edge's own time t (the burst's start on air).
//	   Nothing about a burst leaves the radio before t: tx_queue() and tx_begin() only change local state.
//	2. A receiver hears a burst only through vdmr_inbox_put(t, ...), and the modem raises its event at the end of that
//	   slot, t + VDMR_SLOT_US, not at the start: vdmr_inbox_put() posts rx_event() then. This is the contract's
//	   timing: "within 10 ms after the burst's slot ends" (docs/spec/dmr-modem.md:314), "reported at the end of their
//	   slot" as the host test sim does (tests/host/sim/split_sim_modem.c:293, the next boundary).
//	3. Everything a received burst changes in a receiver happens in rx_event(): DMR_EV_BURST_RX, the busy state that
//	   polite access reads, the timeslot learned for aligned timing and the slot phase it follows. The modem has no
//	   carrier sense at the burst's start (no RSSI event), so polite-to-all also sees a burst only at its end. A
//	   carrier event at the start would cut this lookahead to 0; adding one must change this constant.
//	4. Air time is 0 in the world (radios share one air with no distance), and each radio's clock offset is 0 in the
//	   first slice (virtual-radio-architecture.md:170, "Per-radio offset and drift ... First slice: zero").
//	5. So a burst that starts at t inside a grant [g, g + L) gives its first receiver event at t + 30 000 >= g + 30 000.
//	   With L <= 30 000 that is at or after the grant's end, and events due at the end run in the next grant
//	   (adr/0005-time-model.md:40-41, item 6; virtual-radio-architecture.md:196-198). The conductor can carry the burst over the barrier in time.
#define VDMR_LOOKAHEAD_US	VDMR_SLOT_US

// One burst this radio sent, as the conductor glue reads it
typedef struct {
	uint64_t t_us;		// this radio's virtual us at the burst's slot edge: the burst is on air from here for one slot
	uint32_t slot;		// the modem's slot counter at that edge (as DMR_EV_SLOT_TICK's arg, never 0)
	int8_t ts;		// 1 or 2 when the modem knows the slot's number, else 0
	int8_t cc;		// the color code configured, -1 when the stack asked for any (it then filters itself)
	uint8_t kind;		// dmr_burst_kind_t
	uint8_t voice_index;	// voice: 0..5 (A..F)
	uint8_t sync;		// dmr_sync_family_t: MS sync (or the stack's override) on bursts with sync, NONE on voice B-F
	bool host_crc;		// the payload carries its own CRC
	bool emb_valid;		// voice: emb_lc holds the embedded LC of this superframe (B-E carry it in four parts)
	uint8_t emb_lc[9];
	uint8_t len;
	uint8_t payload[DMR_PAYLOAD_MAX];
	uint64_t hash;		// vtrace_hash of the payload, as in the trace line
} vdmr_tx_t;

// The oldest burst in the outbox, removed. False when empty. A full outbox drops its oldest record, counts it and
// writes a trace line (with no conductor, nobody takes them)
bool vdmr_outbox_take(vdmr_tx_t *out);
uint32_t vdmr_outbox_dropped(void);

// A burst from another radio. t_us is the burst's start on air in this radio's virtual us; the modem reports it at the
// end of its slot, t_us + VDMR_SLOT_US (class 20, air), through the CC filter and the slot lock. b->ts is the slot's
// number when the sender's framing shows it (1 or 2), else 0. False when the slot ends before the clock's time or the
// inbox is full
bool vdmr_inbox_put(uint64_t t_us, const dmr_burst_t *b);

// The "dmr" task's work: every event the modem raised since the last call, to the stack's callback, in order. Called
// only from that task (and by the unit test, which has no tasks)
void vdmr_deliver(void);

#endif
