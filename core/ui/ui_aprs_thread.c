// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include "config.h"
#if CONFIG_APRS_RX
#include <string.h>
#include "ui_nav.h"
#include "ui_svc_aprs.h"
#include "fmt.h"

// Main RAM: neither the engine's CCM state nor the UI task's stack holds these snapshots.
static aprs_thread_msg_t messages[APRS_THREAD_MAX];
static char thread_call[AX25_CALL_LEN + 4];
static uint8_t selected, total;
static uint16_t first, last;

__attribute__((weak)) uint8_t ui_svc_aprs_thread(const char *call, aprs_thread_msg_t out[APRS_THREAD_MAX], bool read)
{
	(void)call; (void)out; (void)read;
	return 0;
}

__attribute__((weak)) bool ui_svc_aprs_message_action(const aprs_thread_msg_t *message, bool retry)
{
	(void)message; (void)retry;
	return false;
}

static bool pending(uint8_t state) { return state == APRS_MSG_QUEUED || state == APRS_MSG_SENDING; }
static bool failed(uint8_t state) { return state == APRS_MSG_NO_ACK || state == APRS_MSG_REJECTED || state == APRS_MSG_CANCELED; }

// One newest-first entry per callsign, with unread reflecting the whole conversation.
static uint8_t conversations(void)
{
	uint8_t n = ui_svc_aprs_thread(NULL, messages, false), kept = 0;
	for (uint8_t i = 0; i < n; i++) {
		uint8_t j = 0;
		while (j < kept && strcmp(messages[j].call, messages[i].call) != 0)
			j++;
		bool read = messages[i].read;
		if (j < kept) {
			read &= messages[j].read;
			memmove(&messages[j], &messages[j + 1], (kept - j - 1u) * sizeof messages[0]);
			kept--;
		}
		messages[kept] = messages[i];
		messages[kept++].read = read;
	}
	return kept;
}

uint8_t ui_aprs_conversation_count(void) { return conversations(); }

bool ui_aprs_conversation(uint8_t i, aprs_msg_t *out)
{
	uint8_t n = conversations();
	if (i >= n)
		return false;
	const aprs_thread_msg_t *m = &messages[n - i - 1u];
	memset(out, 0, sizeof *out);
	str_copy(out->from, sizeof out->from, m->call);
	str_copy(out->text, sizeof out->text, m->text);
	out->ms = m->ms;
	out->read = m->read;
	return true;
}

bool ui_aprs_thread_exists(const char *call)
{
	return ui_svc_aprs_thread(call, messages, false) != 0;
}

void ui_aprs_thread_open(const char *call)
{
	str_copy(thread_call, sizeof thread_call, call);
	first = 0;
	selected = 0;
	(void)ui_svc_aprs_thread(thread_call, messages, true);
	ui_push(SCR_APRS_THREAD, 0);
}

static void message_state(sbuf_t *b, const aprs_thread_msg_t *m)
{
	switch (m->state) {
	case APRS_MSG_QUEUED: sb_str(b, TR("Queued")); break;
	case APRS_MSG_SENDING:
		sb_str(b, TR("Sending"));
		sb_chr(b, '\n');
		sb_str(b, TR("Try "));
		sb_u32(b, m->tries, 1);
		sb_str(b, TR(" of "));
		sb_u32(b, APRS_MSG_TRIES, 1);
		sb_chr(b, '\n');
		sb_str(b, TR("Next in "));
		sb_u32(b, (int32_t)(m->next_ms - ui_st.now_ms) > 0 ?
			(m->next_ms - ui_st.now_ms + 999u) / 1000u : 0, 1);
		sb_str(b, TR(" s"));
		break;
	case APRS_MSG_DELIVERED:
		sb_str(b, TR("Delivered (acked)"));
		sb_chr(b, ' ');
		sb_age_long(b, ui_st.now_ms - m->done_ms);
		break;
	case APRS_MSG_NO_ACK:
		sb_str(b, TR("No ack after "));
		sb_u32(b, m->tries, 1);
		sb_str(b, TR(" tries"));
		break;
	case APRS_MSG_CANCELED: sb_str(b, TR("Canceled")); break;
	case APRS_MSG_REJECTED: sb_str(b, TR("Rejected")); break;
	case APRS_MSG_SENT: sb_str(b, TR("Sent, no ack requested")); break;
	default: break;
	}
}

static void thread_draw(void)
{
	total = ui_svc_aprs_thread(thread_call, messages, true);
	if (selected >= total)
		selected = total ? (uint8_t)(total - 1u) : 0;
	sbuf_t b;
	sb_init(&b, ui_aprs_text, sizeof ui_aprs_text);
	if (total) {
		const aprs_thread_msg_t *m = &messages[selected];
		sb_u32(&b, selected + 1u, 1);
		sb_str(&b, TR(" of "));
		sb_u32(&b, total, 1);
		sb_chr(&b, '\n');
		sb_str(&b, m->state == APRS_MSG_INCOMING ? TR("From ") : TR("To "));
		sb_str(&b, thread_call);
		sb_str(&b, ", ");
		sb_age_long(&b, ui_st.now_ms - m->ms);
		sb_chr(&b, '\n');
		sb_str(&b, m->text);
		sb_chr(&b, '\n');
		if (m->state != APRS_MSG_INCOMING) {
			message_state(&b, m);
			sb_chr(&b, '\n');
		}
	}
	last = (uint16_t)ui_draw_text_page(ui_view(), &ui_st, thread_call, ui_aprs_text, &first, true);
	uint8_t state = total ? messages[selected].state : APRS_MSG_INCOMING;
	ui_draw_hint(pending(state) ? TR("OK reply  # Cancel") :
		failed(state) ? TR("OK reply  # Retry") : TR("OK reply  Back"), false);
}

static bool thread_key(const input_event_t *ev)
{
	if (ev->gesture != GESTURE_PRESS && ev->gesture != GESTURE_REPEAT && ev->gesture != GESTURE_LONG &&
	    ev->action != ACT_ENC_STEP)
		return false;
	if (ev->action == ACT_CONTEXT && total &&
	    (pending(messages[selected].state) || failed(messages[selected].state))) {
		if (!ui_svc_aprs_message_action(&messages[selected], failed(messages[selected].state)))
			ui_toast(TR("Busy"));
		first = 0;
		ui_dirty();
		return true;
	}
	if (ev->action == ACT_SELECT || ev->action == ACT_CONTEXT) {
		ui_aprs_compose(thread_call);
		return true;
	}
	int dir = ev->action == ACT_NAV_UP ? -1 : ev->action == ACT_NAV_DOWN ? 1 :
		ev->action == ACT_ENC_STEP ? ev->value : 0;
	if (!dir)
		return false;
	if (dir > 0 && first == last && selected + 1u < total) {
		selected++;
		first = 0;
	} else if (dir < 0 && first == 0 && selected > 0) {
		selected--;
		first = UINT16_MAX;
	} else {
		int line = first + dir;
		first = (uint16_t)(line < 0 ? 0 : line > last ? last : line);
	}
	ui_dirty();
	return true;
}

const ui_scr_def_t ui_aprs_thread_screen = {
	.name = "aprs-thread", .flags = SF_FIELD, .draw = thread_draw, .key = thread_key,
};
#endif
