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

#if CONFIG_APRS_RX
static __attribute__((noinline)) bool last_arrival(sbuf_t *b)
{
	aprs_packet_t p;
	if (!ui_svc_aprs_packet(0, &p)) return false;
	sb_str(b, p.call);
	sb_chr(b, ' ');
	sb_age(b, ui_st.now_ms - p.ms);
	return true;
}
#endif

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
	uint8_t block, via;
	uint32_t freq_hz;
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
	s->freq_hz = v.freq_hz;
	ui_svc_aprs_cfg(&c);
	s->tx_on = c.tx_on;
	s->via = c.via;
}

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
	if (second) {
		if (h.digi[0]) {
			sb_str(&b, TR("Repeated by "));
			sb_str(&b, h.digi);
			return;
		}
#if CONFIG_APRS_RX
		if (last_arrival(&b)) return;
#endif
		if (v.rf_active)
			sb_str(&b, TR("APRS sending"));
		else if (v.tx_on && (!v.pos_valid || (v.via == APRS_VIA_RF && v.block != APRS_OK)))
			sb_str(&b, blocked(!v.pos_valid ? APRS_NO_POSITION : v.block));
		else
			sb_str(&b, TR("No APRS heard yet"));
		return;
	}
	if (h.listening == APRS_LISTEN_ON) {
		sb_str(&b, TR("Listening "));
		sb_freq(&b, v.freq_hz);
		sb_str(&b, " MHz");
	} else {
		const char *why;
		switch (h.listening) {
		case APRS_LISTEN_OFF: why = TR("Not listening: off"); break;
		case APRS_LISTEN_UNAVAILABLE: why = TR(TEXT_ALT_OPEN "Not listening: no receiver" TEXT_ALT_NEXT "No APRS receiver" TEXT_ALT_CLOSE); break;
		case APRS_LISTEN_CHANNEL: why = TR(TEXT_ALT_OPEN "Not listening: voice channel" TEXT_ALT_NEXT "APRS off: voice channel" TEXT_ALT_CLOSE); break;
		case APRS_LISTEN_SHARED: why = TR("Listening: time share"); break;
		case APRS_LISTEN_BUSY: why = TR("Not listening: radio busy"); break;
		default: why = TR(TEXT_ALT_OPEN "Not listening: receiver busy" TEXT_ALT_NEXT "APRS receiver busy" TEXT_ALT_CLOSE); break;
		}
		sb_str(&b, why);
	}
}
#endif
