// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include "ui_aprs_tune.h"
#if CONFIG_APRS_TUNE
#include "board.h"
#include "radio/tones.h"
#include "radio/radio_am.h"
#include "radio/radio_bcast.h"
#include "fmt.h"
#include "i18n.h"

void ui_aprs_tune_item(const aprs_freq_t *f, ui_item_t *it)
{
	sbuf_t b;
	sb_init(&b, it->label, sizeof it->label);
	sb_str(&b, TR("Tune to"));
	sb_chr(&b, ' ');
	sb_freq(&b, f->hz);
	if (f->tone_kind) {
		sb_chr(&b, ' ');
		sb_chr(&b, f->tone_kind);
		uint16_t v = f->tone_kind == 'D' ? (uint16_t)((f->tone >> 6) * 100u +
			((f->tone >> 3) & 7u) * 10u + (f->tone & 7u)) : f->tone;
		sb_u32(&b, v, 3);
	}
}

void ui_aprs_tune(const aprs_freq_t *f)
{
	int64_t tx = (int64_t)f->hz + (int64_t)f->offset * 10000;
	const char *why = NULL;
	subtone_t tone;
	uint16_t ti = 0;
	if (!board_in_band(f->hz, false) || radio_am_band(f->hz) || radio_bcast_band(f->hz))
		why = TR("This radio can't receive that frequency");
	else if (tx <= 0 || tx > UINT32_MAX || !board_in_band((uint32_t)tx, true))
		why = TR("This radio can't transmit on that frequency");
	else if (!aprs_freq_tone(f, &tone))
		why = TR("This radio can't use that tone");
	else if (ui_view()->state == RS_TX || ui_view()->hotspot)
		why = TR("Radio busy");
	if (why) { ui_toast(why); return; }
	for (; ti < TONES_PICKER_COUNT; ti++) {
		subtone_t t = tones_picker(ti);
		if (t.type == tone.type && t.value == tone.value) break;
	}
	if (ti == TONES_PICKER_COUNT) {
		ui_toast(TR("This radio can't use that tone"));
		return;
	}
	if (!radio_req_fm_tune(f->hz, f->offset, ti, f->narrow)) {
		ui_toast(TR("Radio busy"));
		return;
	}
	ui_go_home();
	ui_toast(TR("Tuned for now. Back to undo"));
}
#endif
