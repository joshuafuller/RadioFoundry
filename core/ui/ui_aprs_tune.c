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
#include "ui_svc_aprs.h"

static aprs_freq_t selected;
static char selected_source[AX25_CALL_LEN + 4];
static uint32_t selected_ms;

void ui_aprs_tune_item(const aprs_freq_t *f, ui_item_t *it)
{
	sbuf_t b;
	sb_init(&b, it->label, sizeof it->label);
	sb_str(&b, TR("Tune to"));
	sb_chr(&b, ' ');
	sb_freq(&b, f->hz);
	sb_str(&b, " MHz");
}

static void tune_select(uint16_t i, bool hold)
{
	(void)i;
	(void)hold;
	const aprs_freq_t *f = &selected;
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
	ui_push(SCR_APRS_TUNED, 0);
}
static uint16_t review_count(void) { return 5; }
static void review_item(uint16_t i, ui_item_t *it)
{
	sbuf_t b;
	sb_init(&b, it->label, sizeof it->label);
	subtone_t tone;
	switch (i) {
	case 0:
		sb_freq(&b, selected.hz);
		sb_str(&b, " MHz");
		break;
	case 1:
		sb_str(&b, TR("Tone"));
		sb_chr(&b, ' ');
		if (!selected.tone_kind) sb_str(&b, TR("None"));
		else if (!aprs_freq_tone(&selected, &tone)) sb_str(&b, TR("Unsupported"));
		else {
			sb_tone(&b, &tone);
			if (tone.type == TONE_CTCSS) sb_str(&b, " Hz");
		}
		break;
	case 2:
		sb_str(&b, TR("Offset"));
		sb_chr(&b, ' ');
		if (!selected.offset) sb_str(&b, TR("Simplex"));
		else {
			sb_chr(&b, selected.offset < 0 ? '-' : '+');
			sb_u32(&b, (selected.offset < 0 ? -selected.offset : selected.offset) * 10u, 1);
			sb_str(&b, " kHz");
		}
		break;
	case 3: sb_str(&b, TR("From ")); sb_str(&b, selected_source); break;
	case 4:
		sb_str(&b, TR("Heard"));
		ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - selected_ms);
		break;
	}
}

void ui_aprs_tune(const aprs_freq_t *f, const char *source, uint32_t ms)
{
	// Freeze the advertised values: packets arriving during review cannot retarget OK.
	selected = *f;
	str_copy(selected_source, sizeof selected_source, source);
	selected_ms = ms;
	ui_push(SCR_APRS_TUNE, 0);
}

static uint16_t tuned_count(void) { return 3; }
static void tuned_item(uint16_t i, ui_item_t *it)
{
	sbuf_t b;
	sb_init(&b, it->label, sizeof it->label);
	if (i == 0) {
		sb_freq(&b, ui_view()->ch.rx_hz);
		sb_str(&b, " MHz");
	} else if (i == 1) {
		bool listening = false;
#if CONFIG_APRS_RX
		listening = ui_svc_aprs_listening();
#endif
		sb_str(&b, listening ? TR("APRS listening") : TR("APRS not listening"));
	} else sb_str(&b, TR("Back to undo"));
}
static bool tuned_key(const input_event_t *ev)
{
	if (ev->action != ACT_BACK || ev->gesture != GESTURE_PRESS) return false;
	radio_req_clear_temporary();
	ui_go_home();
	return true;
}
const ui_scr_def_t ui_aprs_tune_screen = {
	.name = "aprs-tune", .flags = SF_LIST, .prefix = TRN("Tune review"),
	.hint = TRN("OK tune  Back cancel"),
	.count = review_count, .item = review_item, .select = tune_select,
};
const ui_scr_def_t ui_aprs_tuned_screen = {
	.name = "aprs-tuned", .flags = SF_LIST, .prefix = TRN("Temporary tune"),
	.hint = TRN("Back to undo"),
	.count = tuned_count, .item = tuned_item, .key = tuned_key,
};
#endif
