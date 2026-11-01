// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual board's keys (vboard.h): the pin state the scan reads, changed by events at their virtual time.
//   --key MS:BIT:HOLD   press key BIT (the key map below) at MS for HOLD ms: a key edge interrupt each way
// No VFO toggle key: tests/host/keyassignTest.c expects exactly one among the radios' key maps (boards/*/board.c).
#include <stdio.h>
#include "vboard.h"
#include "vkeys.h"
#include "vclock.h"
#include "vhost.h"
#include "vport.h"
#include "vtrace.h"

enum { K_SELECT, K_BACK, K_UP, K_DOWN, K_PTT, K_MONITOR, K_0, K_1, K_2, K_3, K_4, K_5, K_6, K_7, K_8, K_9 };
// The key list for the web control page (vkeys.h) must count the same way
#define SAME_KEY(k) (K_##k == (int)KEY_##k)
_Static_assert(SAME_KEY(SELECT) && SAME_KEY(BACK) && SAME_KEY(UP) && SAME_KEY(DOWN) && SAME_KEY(PTT) &&
	       SAME_KEY(MONITOR) && SAME_KEY(0) && SAME_KEY(1) && SAME_KEY(2) && SAME_KEY(3) && SAME_KEY(4) &&
	       SAME_KEY(5) && SAME_KEY(6) && SAME_KEY(7) && SAME_KEY(8) && SAME_KEY(9) && K_9 + 1 == (int)KEY_COUNT,
	       "boards/vhost/vkeys.h and the key map differ");

const keymap_entry_t vboard_keymap[] = {
	{ K_SELECT, ACT_SELECT, ACT_SELECT, false, false, 0 },
	{ K_BACK, ACT_BACK, ACT_BACK, false, false, 0 },
	{ K_UP, ACT_NAV_UP, ACT_NAV_UP, false, true, 0 },
	{ K_DOWN, ACT_NAV_DOWN, ACT_NAV_DOWN, false, true, 0 },
	{ K_PTT, ACT_PTT, ACT_PTT, true, false, 0 },
	{ K_MONITOR, ACT_MONITOR, ACT_MONITOR, true, false, 0 },
	{ K_0, ACT_DIGIT_0, ACT_DIGIT_0, false, false, 0 },
	{ K_1, ACT_DIGIT_1, ACT_DIGIT_1, false, false, 0 },
	{ K_2, ACT_DIGIT_2, ACT_DIGIT_2, false, false, 0 },
	{ K_3, ACT_DIGIT_3, ACT_DIGIT_3, false, false, 0 },
	{ K_4, ACT_DIGIT_4, ACT_DIGIT_4, false, false, 0 },
	{ K_5, ACT_DIGIT_5, ACT_DIGIT_5, false, false, 0 },
	{ K_6, ACT_DIGIT_6, ACT_DIGIT_6, false, false, 0 },
	{ K_7, ACT_DIGIT_7, ACT_DIGIT_7, false, false, 0 },
	{ K_8, ACT_DIGIT_8, ACT_DIGIT_8, false, false, 0 },
	{ K_9, ACT_DIGIT_9, ACT_DIGIT_9, false, false, 0 },
};
const uint8_t vboard_keymap_len = sizeof vboard_keymap / sizeof vboard_keymap[0];
const uint8_t vboard_side_keys[] = { K_MONITOR };
const char *const vboard_side_key_names[] = { "SK1" };
const uint8_t vboard_side_key_count = sizeof vboard_side_keys;

// The pins are device state: a press or release changes a level at its virtual time (class 30) and rings the key
// interrupt, but the scan reads the levels, never the edge. A press and release between two scans is seen only if a
// scan falls between them, as on the radio
static volatile uint32_t key_pins;	// what the scan reads

bool keypad_vhost_set_now(uint32_t bit, bool down)
{
	if (bit > 31)
		return false;
	uint32_t mask = 1u << bit;
	key_pins = down ? (key_pins | mask) : (key_pins & ~mask);
	vhost_irq_raise(VHOST_IRQ_KEY);
	return true;
}

static void key_event(uint32_t arg)
{
	keypad_vhost_set_now(arg & 0xFFu, (arg & 0x100u) != 0);
}

// Key BIT (an index into the key map above; PTT is K_PTT) goes down or up at virtual time t_us. False when BIT is
// not 0..31. For the --key option below and for later parts (world key events)
bool keypad_vhost_set(uint64_t t_us, uint32_t bit, bool down)
{
	if (bit > 31)
		return false;
	vclock_at(t_us, VCLOCK_CLASS_INPUT, key_event, bit | (down ? 0x100u : 0u));
	return true;
}

static bool set_key(const char *value)
{
	unsigned ms, bit, hold;
	char extra;
	if (sscanf(value, "%u:%u:%u%c", &ms, &bit, &hold, &extra) != 3 || bit > 15)
		return false;
	return keypad_vhost_set((uint64_t)ms * 1000u, bit, true) &&
	       keypad_vhost_set((uint64_t)(ms + hold) * 1000u, bit, false);
}
VHOST_OPTION("--key", "MS:BIT:HOLD", set_key, "press key BIT at MS ms for HOLD ms")

static void key_isr(void)
{
	vtrace("BOARD key pins=%04x", (unsigned)key_pins);
}

static void in_init(void)
{
	vhost_irq_register(VHOST_IRQ_KEY, "key", key_isr);
	vtrace("BOARD input.init");
}

static uint32_t in_scan(void)
{
	vtrace("BOARD key.scan pins=%08x", (unsigned)key_pins);
	return key_pins;
}

// .encoder is set by board.c from vhost_part_encoder() (NULL: no encoder)
hal_input_t vboard_input = { .init = in_init, .scan = in_scan };
