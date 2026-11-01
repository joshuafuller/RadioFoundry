// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual board's parts (boards/vhost): software models behind the board_t the core reads. Each part is its own
// file, boards/vhost/<part>_vhost.c, and every *.c here is built (board.mk BOARD_DIRS), so a new part is a new file.
//
// board.c fills the descriptor at board_early_init, before app_start, so a later part plugs in with no edit of
// board.c: it defines one of the vhost_part_*() hooks below (a weak default returns NULL), registers its options
// (VHOST_OPTION, platform/host/vhost.h) and, when it has start-up work, VBOARD_PART_INIT. Example, a later
// boards/vhost/dmr_vhost.c:
//
//	static const hal_dmr_modem_t modem = { ... };
//	const hal_dmr_modem_t *vhost_part_dmr(void) { return &modem; }
//	static void dmr_start(void) { ... }
//	VBOARD_PART_INIT(dmr_start)
#ifndef VBOARD_H
#define VBOARD_H

#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>
#include "board.h"

// ---- the parts board.c puts in the descriptor (one file each)
extern const hal_display_t vboard_panel;	// display_vhost.c: the 160x128 panel
extern hal_input_t vboard_input;		// keypad_vhost.c: the keys (board.c sets .encoder from vhost_part_encoder)
extern const keymap_entry_t vboard_keymap[];	// keypad_vhost.c
extern const uint8_t vboard_keymap_len;
extern const uint8_t vboard_side_keys[];
extern const char *const vboard_side_key_names[];
extern const uint8_t vboard_side_key_count;
extern const hal_radio_t vboard_fm;		// fm_vhost.c: an FM receiver that hears nothing
extern const hal_power_t vboard_power;		// power_vhost.c: the knob, the latch, a full battery
extern const hal_flash_t vboard_flash;		// flash_vhost.c: NOR flash in memory
extern const hal_stream_t vboard_host_port;	// uart_vhost.c: the host port UART

// The flash's contents before the firmware runs (flash_vhost.c, from board_early_init): erased, then vflash_preload()
void vboard_flash_boot(void);
// Put bytes into the flash as they are, with no NOR rule and no trace line: only for vflash_preload. False when the
// range is outside the flash
bool vflash_load(uint32_t addr, const void *data, uint32_t len);
// Keep the flash across a restart: write it to a private file (mode 0600, via a temp file and rename), or read it
// back. Load leaves the flash unchanged if the file is bad. Both return false on any error
bool flash_vhost_save(const char *path);
bool flash_vhost_load(const char *path);
// Host port bytes in at virtual time t_us (uart_vhost.c; a push longer than its staging buffer is split in order),
// the receive FIFO's overflow count, and a weak hook called once per host write
bool uart_vhost_rx_push(uint64_t t_us, const uint8_t *b, size_t n);
uint32_t uart_vhost_rx_dropped(void);
void vhost_uart_tx_hook(const uint8_t *b, size_t n);
// A key edge at virtual time t_us (keypad_vhost.c): bit is a key map index as for --key
bool keypad_vhost_set(uint64_t t_us, uint32_t bit, bool down);
// Apply inside a world event already posted by vchan with vclock_post_stamped; do not create a later causal round.
bool keypad_vhost_set_now(uint32_t bit, bool down);

// ---- hooks for later parts. Each has a weak default in board.c (or flash_vhost.c) that keeps today's board; a part
// defines the strong one in its own file. One file per hook
const hal_dmr_modem_t *vhost_part_dmr(void);	// the DMR modem (NULL: none, the core treats DMR channels as silent)
const hal_audio_t *vhost_part_audio(void);
const hal_vocoder_t *vhost_part_vocoder(void);
const hal_system_t *vhost_part_system(void);	// chip id, memory reads, reboot modes
const hal_netif_t *vhost_part_netif(void);	// the network personality
const hal_display_t *vhost_part_display(void);	// non-NULL: used instead of vboard_panel (display profiles)
typedef int8_t (*vhost_encoder_fn)(void);
vhost_encoder_fn vhost_part_encoder(void);	// the encoder's detents (NULL: a keypad-only radio)
// Called once from vboard_flash_boot, after the erase: fills the start contents (start storage). Default: nothing
void vflash_preload(void);

// ---- start-up work of a part, run at the end of board_init (from the first task, after the board's own parts), in
// registration order (the link order, which is sorted). Any number of files may register
typedef void (*vboard_init_fn)(void);
void vboard_part_init_register(vboard_init_fn fn);
#define VBOARD_INIT_CAT2(a, b) a##b
#define VBOARD_INIT_CAT(a, b) VBOARD_INIT_CAT2(a, b)
#define VBOARD_PART_INIT(fn)								\
	__attribute__((constructor)) static void VBOARD_INIT_CAT(vboard_init_reg_, __LINE__)(void)	\
	{										\
		vboard_part_init_register(fn);						\
	}

#endif
