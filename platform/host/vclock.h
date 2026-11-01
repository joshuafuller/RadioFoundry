// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The one virtual clock and its event queue (platform/host/vclock.c). Virtual time is in microseconds from power-on.
// It stands still while firmware code runs and moves only when the idle task runs (to the next due event) or code
// busy-waits (hal_delay_us, hal_delay_ms before the scheduler). The RTOS tick and hal_millis / hal_micros both come from
// it, so the two never drift (docs/dev/virtual-radio-architecture.md, "Order of events").
//
// The order key. Every event has a key of four parts, compared in this order (spec "Order of events"):
//	1. virtual time in us (never rounded to a tick);
//	2. round: an event queued before the clock reached T is round 0. An event made while T is being handled, and due
//	   at T, joins the next round (one more than the event being handled), so no event runs before its cause;
//	3. class (VCLOCK_CLASS_*): at one time and round, a lower class runs first, so the tick (50) runs last;
//	4. sequence: the order it was queued.
// The end of the run (class 99) is outside the rounds: at its time it runs after every other event, of every round.
// Example: at 2000 ms the queue holds a key edge (class 30) and the tick (class 50), both round 0. The key runs first,
// then the tick wakes the input task, which reads the key in its 2000 ms scan. If that scan queued an event due at
// 2000 ms, it would be round 1 and run after the tick, whatever its class.
#ifndef VCLOCK_H
#define VCLOCK_H

#include <stdint.h>

// Event classes: at one time and round, a lower class runs first. The values are the spec's ("Order of events", part 3)
enum {
	VCLOCK_CLASS_WORLD = 10,	// world faults, power, start and stop
	VCLOCK_CLASS_AIR = 20,		// air from other radios
	VCLOCK_CLASS_INPUT = 30,	// client and scenario inputs (keys, host port bytes)
	VCLOCK_CLASS_OWN = 40,		// the radio's own hardware (slot edge, PCM block, supply hold)
	VCLOCK_CLASS_TICK = 50,		// the RTOS tick: last, so what is true at T is in place before code runs at T
	VCLOCK_CLASS_END = 99,		// the end of the run: after every round at its time
};

// Event sources (vclock_post). The radio's own parts are 0; outside sources (the conductor) use fixed ids of their
// own, never process ids. The source is kept with the event for the trace; the order is the key above
enum {
	VCLOCK_SOURCE_RADIO = 0,
};

#define VCLOCK_NEVER	UINT64_MAX	// no horizon (the default), or no event queued (vclock_next_due_us)

// Reading the clock never moves it
uint64_t vclock_now_us(void);
// The last event's round at the current time, or -1 if no event has run at this time yet
int64_t vclock_round_handled(void);
// A hardware event at t: fn runs in the hardware model (no RTOS calls; it changes device state and raises an
// interrupt). cls is one of VCLOCK_CLASS_*. The round comes from the clock state (the key above). An event due before
// the clock's time is a bug inside the virtual radio: the run ends ("event due in the past", exit 4)
typedef void (*vclock_event_fn)(uint32_t arg);
void vclock_post(uint64_t t_us, unsigned cls, unsigned source, vclock_event_fn fn, uint32_t arg);
// A conductor event: keep its round and sequence as given, without advancing vclock_post's local sequence.
// The same time check and order key apply; the caller supplies the causal round and sequence
void vclock_post_stamped(uint64_t t_us, uint32_t round, unsigned cls, unsigned source, uint64_t sequence,
	vclock_event_fn fn, uint32_t arg);
// vclock_post from the radio's own parts (source VCLOCK_SOURCE_RADIO)
void vclock_at(uint64_t t, unsigned cls, vclock_event_fn fn, uint32_t arg);
// The time of the earliest queued event, or VCLOCK_NEVER
uint64_t vclock_next_due_us(void);
// Move the clock to t, running every event due on the way (t included) in key order. Earlier events may switch tasks.
// It stops early at the horizon (below)
void vclock_advance_to(uint64_t t);
// Queue the end of the run (--until-ms, default 10 s); main.c calls it once, after the options
void vclock_start(void);
// The port calls it when the scheduler starts (as the radios' tick timer starts in xPortStartScheduler)
void vclock_tick_start(void);

// The horizon (world control grants): the clock never passes it. When the idle jump or a busy wait reaches it,
// the clock stops at it, before any event due at it runs, and vclock_horizon_reached(now) is called. That hook may
// queue events with vclock_post (one due at the horizon is round 0, so it runs before that time's tick) and must set
// a later horizon or request a drain before it returns; else the run ends ("horizon not moved", exit 4). Default: VCLOCK_NEVER, so in
// standalone host the hook is never called and the run ends at --until-ms by its class 99 event. A horizon before the clock
// ends the run ("horizon in the past", exit 4)
void vclock_set_horizon(uint64_t t_us);
// From the horizon hook: include every event at the current horizon, in all rounds, then call the hook again.
// A busy wait started by an event at T stays suspended at T until a later grant; it never returns early or moves
// past T during the drain. The clock must already be at a finite horizon, else "drain away from horizon", exit 4
void vclock_drain_horizon(void);
// Weak default: end the run as the time limit does ("virtual time limit", exit 0). The conductor defines its own
void vclock_horizon_reached(uint64_t now_us);

// The tick interrupt's body (platform/host/vtick.c): the kernel's tick, and a switch when it wakes a task
void vtick_isr(void);

#endif
