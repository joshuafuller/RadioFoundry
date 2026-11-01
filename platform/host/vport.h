// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The host port's interrupt API (platform/host/freertos/port.c). Rules (prior art, 03-deterministic-rtos-prior-art.md):
// only one task thread runs at a time; interrupts run only at points the port picks (a raise with interrupts on, the
// end of a critical section, a thread's first start).
#ifndef VPORT_H
#define VPORT_H

#include <stdbool.h>
#include <stdint.h>

// Simulated interrupt sources; a lower number runs first when several are pending
enum {
	VHOST_IRQ_TICK = 0,	// the RTOS tick, every 1000 us of virtual time once the scheduler runs
	VHOST_IRQ_HOST_RX,	// the host port's receive interrupt (boards/vhost/uart_vhost.c)
	VHOST_IRQ_KEY,		// a key edge (boards/vhost/keypad_vhost.c)
	VHOST_IRQ_POWER,	// the power knob (boards/vhost/power_vhost.c)
	// Reserved for later parts, so none of them edits this file. After the four above, so today's traces stay as
	// they are; the DMR slot edge first of these, since a late slot edge loses a burst on the radio
	VHOST_IRQ_DMR,		// the DMR modem's slot edge (boards/vhost/dmr_vhost.c)
	VHOST_IRQ_AUDIO,	// a PCM block done (boards/vhost/audio_vhost.c)
	VHOST_IRQ_NET,		// a network frame received (boards/vhost/netif_vhost.c)
	VHOST_IRQ_SPARE,	// one more part; name it here when it is claimed
	VHOST_IRQ_COUNT = 8
};

typedef void (*vhost_isr_t)(void);
void vhost_irq_register(unsigned irq, const char *name, vhost_isr_t isr);
// Mark a source pending; its handler runs now when interrupts are on, else when they come back on
void vhost_irq_raise(unsigned irq);
bool vhost_in_isr(void);

typedef struct {
	uint64_t criticals;	// outermost critical sections entered by tasks
	uint64_t deferred;	// interrupts raised while masked, run later
	uint64_t suspends;	// vTaskSuspendAll calls from outside the kernel (the firmware's scheduler locks)
} vport_stats_t;
void vport_stats(vport_stats_t *st);

#endif
