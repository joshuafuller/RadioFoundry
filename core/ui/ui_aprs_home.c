// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include "config.h"
#if CONFIG_APRS_HOME
#include <string.h>
#include "ui_nav.h"
#include "ui_svc_aprs.h"
#include "fmt.h"
#include "i18n.h"

__attribute__((weak)) void ui_svc_aprs_home(aprs_home_t *h) { memset(h, 0, sizeof *h); }

static const char *blocked(uint8_t block)
{
	switch (block) {
	case APRS_NO_CALL: return TR("Not sending: no callsign");
	case APRS_NO_FREQ: return TR("Not sending: no frequency");
	case APRS_NOT_AMATEUR: return TR("Not sending: outside ham band");
	case APRS_NO_AFSK: return TR("Not sending: no transmitter");
	case APRS_BUSY: return TR("Not sending: busy");
	default: return TR("Not sending: no position");
	}
}

// Keep full engine snapshots off the formatting call chain; this path also serves the host UI.
typedef struct {
	bool available, rf_active, pos_valid, tx_on;
	uint8_t block, via, stations;
	uint16_t next_s;
} status_t;

static __attribute__((noinline)) void status_read(status_t *s)
{
	aprs_view_t v;
	aprs_cfg_t c;
	s->available = ui_svc_aprs_view(&v);
	if (!s->available) return;
	s->rf_active = v.rf_active;
	s->pos_valid = v.pos_valid;
	s->block = v.block;
	s->stations = v.stations;
	s->next_s = v.next_s;
	ui_svc_aprs_cfg(&c);
	s->tx_on = c.tx_on;
	s->via = c.via;
}

#if CONFIG_APRS_RX
static __attribute__((noinline)) uint32_t nearest_station(void)
{
	uint32_t nearest = UINT32_MAX;
	for (uint8_t i = 0; i < ui_svc_aprs_station_count(); i++) {
		aprs_station_t s;
		uint32_t m;
		uint16_t deg;
		if (ui_svc_aprs_station(i, &s) && ui_svc_aprs_range(&s, &m, &deg) && m < nearest)
			nearest = m;
	}
	return nearest;
}
#endif

const char * __attribute__((noinline)) ui_aprs_tx_label(void)
{
	aprs_cfg_t c;
	ui_svc_aprs_cfg(&c);
	return c.tx_on ? TR("APRS TX on") : TR("APRS TX off");
}

void ui_aprs_status(char *out, size_t len, bool second)
{
	status_t v;
	aprs_home_t h;
	sbuf_t b;
	sb_init(&b, out, len);
	status_read(&v);
	if (!v.available)
		return;
	ui_svc_aprs_home(&h);
	// On short Home layouts the first line is all that fits. Prefer repeat evidence
	// to the older local send, but keep a current blocked/not-listening reason first.
	bool repeated = h.digi[0] && h.listening == APRS_LISTEN_ON && !v.rf_active &&
		!(v.tx_on && (!v.pos_valid || (v.via == APRS_VIA_RF && v.block != APRS_OK)));
	if ((!second && repeated) || (second && h.digi[0] && !repeated)) {
		sb_str(&b, TR("Repeated by "));
		sb_str(&b, h.digi);
		return;
	}
	if (second && !repeated) {
#if CONFIG_APRS_RX
		uint32_t nearest = nearest_station();
		sb_u32(&b, v.stations, 1);
		sb_str(&b, TR(" heard"));
		if (nearest != UINT32_MAX) {
			sb_str(&b, TR(", nearest "));
			sb_distance(&b, nearest);
		}
#endif
		return;
	}
	if (v.rf_active) {
		sb_str(&b, TR("APRS sending"));
	} else if (v.tx_on && (!v.pos_valid || (v.via == APRS_VIA_RF && v.block != APRS_OK))) {
		sb_str(&b, blocked(!v.pos_valid ? APRS_NO_POSITION : v.block));
	} else if (h.sent && h.listening == APRS_LISTEN_ON) {
		sb_str(&b, TR("APRS sent "));
		sb_age_long(&b, ui_st.now_ms - h.sent_ms);
		if (v.next_s != 0xFFFFu) {
			sb_str(&b, TR(", next "));
			sb_u32(&b, v.next_s < 60 ? v.next_s : v.next_s / 60u, 1);
			sb_chr(&b, v.next_s < 60 ? 's' : 'm');
		}
	} else if (h.listening == APRS_LISTEN_ON) {
		sb_str(&b, TR("APRS listening"));
	} else {
		const char *why;
		switch (h.listening) {
		case APRS_LISTEN_OFF: why = TR("Not listening: off"); break;
		case APRS_LISTEN_UNAVAILABLE: why = TR("Not listening: no receiver"); break;
		case APRS_LISTEN_CHANNEL: why = TR("Not listening: voice channel"); break;
		case APRS_LISTEN_BUSY: why = TR("Not listening: radio busy"); break;
		default: why = TR("Not listening: receiver busy"); break;
		}
		sb_str(&b, why);
	}
}
#endif
