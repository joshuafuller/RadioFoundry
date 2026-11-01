// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual board's power (vboard.h): a knob, a latch, a full 2S battery. The latch off with the knob off means the
// supply is gone: the run ends 50 ms later (the supply caps hold it that long).
//   --power-off MS   turn the power knob off at MS
#include <stdlib.h>
#include "vboard.h"
#include "vclock.h"
#include "vhost.h"
#include "vport.h"
#include "vtrace.h"

static bool knob_on = true;

static void power_gone(uint32_t arg)
{
	(void)arg;
	vhost_finish("power cut: the latch dropped with the knob off", 0);
}

static void power_event(uint32_t arg)
{
	(void)arg;
	vhost_irq_raise(VHOST_IRQ_POWER);
}

static bool set_power_off(const char *value)
{
	char *end;
	unsigned long long ms = strtoull(value, &end, 0);
	if (*value == '\0' || *end != '\0')
		return false;
	vclock_at(ms * 1000u, VCLOCK_CLASS_WORLD, power_event, 0);
	return true;
}
VHOST_OPTION("--power-off", "MS", set_power_off, "turn the power knob off at MS ms")

static void power_isr(void)
{
	knob_on = false;
	vtrace("BOARD power.knob off");
}

static void p_init(void)
{
	vhost_irq_register(VHOST_IRQ_POWER, "power", power_isr);
	vtrace("BOARD power.init");
}

static void p_latch(bool on)
{
	vtrace("BOARD power.latch %d", on);
	if (!on && !knob_on)
		vclock_at(vclock_now_us() + 50000u, VCLOCK_CLASS_OWN, power_gone, 0);
}

static uint16_t p_mv(void)
{
	return 7900;
}

static uint8_t p_pct(void)
{
	return 90;
}

static int8_t p_volume(void)
{
	return 50;
}

static bool p_switch(void)
{
	static bool said;
	if (!said) {
		said = true;
		vtrace("BOARD power.switch_on first read (housekeeping loop reached)");
	}
	return knob_on;
}

static void p_led(uint8_t which, bool on)
{
	vtrace("BOARD power.led %u %d", which, on);
}

const hal_power_t vboard_power = {
	.init = p_init, .latch = p_latch, .battery_mv = p_mv, .battery_pct = p_pct, .volume = p_volume,
	.switch_on = p_switch, .led = p_led,
};
