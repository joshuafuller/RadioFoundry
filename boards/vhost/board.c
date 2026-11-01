// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual board's descriptor (vboard.h). The core reads board_t as on any radio. Here it is filled at
// board_early_init, before app_start, so later parts plug in through the vhost_part_*() hooks with no edit of this
// file. Every call the core makes into a part is written to the trace (platform/host/vtrace.h).
#include <stddef.h>
#include "config.h"
#include "board.h"
#include "vboard.h"
#include "vhost.h"
#include "vport.h"
#include "vtrace.h"
#include "FreeRTOS.h"

uint8_t ucHeap[configTOTAL_HEAP_SIZE];	// configAPPLICATION_ALLOCATED_HEAP, as the STM32 boards

static const band_t bands[] = { { 400000000u, 480000000u, true } };
static const char *const power_names[] = { "1W", "5W" };

// Writable, so board_early_init can fill the hook fields; the core sees it as the const board (hal/board.h)
static board_t vhost_board = {
	.name = "Virtual",
	.bands = bands,
	.band_count = 1,
	.power_count = 2,
	.power_names = power_names,
	.power_default = 0,
	.input = &vboard_input,
	.keymap = vboard_keymap,
	.side_keys = vboard_side_keys,
	.side_key_names = vboard_side_key_names,
	.radio = &vboard_fm,
	.power = &vboard_power,
	.flash = &vboard_flash,
	.host_port = &vboard_host_port,
	.battery_low_mv = 7000,
	.battery_critical_mv = 6800,
};
extern const board_t board __attribute__((alias("vhost_board")));

// ---- the hooks' defaults: no such part
__attribute__((weak)) const hal_dmr_modem_t *vhost_part_dmr(void) { return NULL; }
__attribute__((weak)) const hal_audio_t *vhost_part_audio(void) { return NULL; }
__attribute__((weak)) const hal_vocoder_t *vhost_part_vocoder(void) { return NULL; }
__attribute__((weak)) const hal_system_t *vhost_part_system(void) { return NULL; }
__attribute__((weak)) const hal_netif_t *vhost_part_netif(void) { return NULL; }
__attribute__((weak)) const hal_display_t *vhost_part_display(void) { return NULL; }
__attribute__((weak)) vhost_encoder_fn vhost_part_encoder(void) { return NULL; }

// ---- the parts' start-up work (VBOARD_PART_INIT)
#define MAX_PART_INITS	16
static vboard_init_fn part_inits[MAX_PART_INITS];
static unsigned n_part_inits;
static bool part_inits_full;	// a constructor can't stop the run usefully; board_early_init does, once the trace is open

void vboard_part_init_register(vboard_init_fn fn)
{
	if (n_part_inits == MAX_PART_INITS)
		part_inits_full = true;
	else
		part_inits[n_part_inits++] = fn;
}

// Nothing to switch off: the virtual transmitter only writes trace lines
void board_rf_safe(void)
{
	vtrace("BOARD rf_safe");
}

void board_early_init(void)
{
	vtrace("BOARD early_init");
	if (part_inits_full)
		vhost_finish("too many VBOARD_PART_INIT (MAX_PART_INITS in boards/vhost/board.c)", 2);
	board_t *b = &vhost_board;
	const hal_display_t *d = vhost_part_display();
	b->display = d != NULL ? d : &vboard_panel;
	vboard_input.encoder = vhost_part_encoder();
	b->keymap_len = vboard_keymap_len;
	b->side_key_count = vboard_side_key_count;
	b->dmr = vhost_part_dmr();
	b->audio = vhost_part_audio();
	b->vocoder = vhost_part_vocoder();
	b->system = vhost_part_system();
	b->netif = vhost_part_netif();
	// As the radios: CAP_DMR with the DMR build. The core reads board.dmr (NULL: DMR channels receive nothing and
	// refuse PTT), never the cap, so a board with no modem part yet is safe
	b->caps = CAP_KEYPAD | (CONFIG_DMR ? CAP_DMR : 0) | (b->input->encoder != NULL ? CAP_ENCODER : 0);
	vboard_flash_boot();
}

// From the first task: the board's own parts, which the core never starts (as the radios' board_init), then the
// later parts' start-up work
void board_init(void)
{
	vtrace("BOARD init");
	board.power->init();
	board.flash->init();
	for (unsigned i = 0; i < n_part_inits; i++)
		part_inits[i]();
}
