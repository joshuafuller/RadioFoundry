// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Tools > APRS (docs/spec/aprs.md 10): what APRS is doing (callsign, frequency, position, next beacon, or why nothing
// can be sent) and the everyday choices, one row each: send now, automatic beacons, the frequency (region), the RF
// power (as the channel, or a level of its own), the digipeater path (the hops), the interval, SmartBeaconing, the
// way out (radio / DMR / both), KISS on the host port, and the stations heard (receive builds). Where the position
// comes from is the position service's (Settings > Location, ui_position.c). The rest (symbol, comment, digital
// destination, iGate, digipeater) is set with qdmr, the debug link or tools/rfoundry_debug.py (aprs.md 11).
#include <string.h>
#include "ui_nav.h"
#include "ui_svc_aprs.h"
#include "ui_aprs_tune.h"
#include "fmt.h"
#include "i18n.h"
#include "config.h"
#include "board.h"
#include "aprs/aprs_fmt.h"

// The services when APRS isn't built (and in the UI simulator): nothing there
#define WEAK __attribute__((weak))
#if CONFIG_APRS_WX
extern const ui_scr_def_t ui_aprs_weather_screen;
#endif
WEAK bool ui_svc_aprs_view(aprs_view_t *v) { memset(v, 0, sizeof(*v)); return false; }
WEAK void ui_svc_aprs_cfg(aprs_cfg_t *c) { memset(c, 0, sizeof(*c)); }
WEAK void ui_svc_aprs_set_cfg(const aprs_cfg_t *c) { (void)c; }
WEAK bool ui_svc_aprs_send_now(void) { return false; }
WEAK bool ui_svc_aprs_send_message(const char *to, const char *text, uint8_t flags)
{
	(void)to;
	(void)text;
	(void)flags;
	return false;
}
WEAK uint8_t ui_svc_aprs_msg_count(void) { return 0; }
WEAK bool ui_svc_aprs_msg(uint8_t i, aprs_msg_t *out, bool read)
{
	(void)i; (void)out; (void)read; return false;
}
WEAK bool ui_svc_aprs_msg_delete(uint8_t i) { (void)i; return false; }
WEAK uint8_t ui_svc_aprs_station_count(void) { return 0; }
WEAK bool ui_svc_aprs_station(uint8_t i, aprs_station_t *out) { (void)i; (void)out; return false; }
WEAK bool ui_svc_aprs_station_find(const char *call, aprs_station_t *out) { (void)call; (void)out; return false; }
WEAK bool ui_svc_aprs_range(const aprs_station_t *s, uint32_t *m, uint16_t *deg) { (void)s; (void)m; (void)deg; return false; }

enum { AP_SEND, AP_AUTO, AP_FREQ, AP_POWER, AP_PATH, AP_INTERVAL, AP_SMART, AP_VIA, AP_KISS, AP_HEARD, AP_DECAY,
#if CONFIG_APRS_RX
	AP_RECENT, AP_OBJECTS, AP_BULLETINS,
#endif
#if CONFIG_APRS_HOME
	AP_MESSAGES,
#endif
	AP_BEACON, AP_WEATHER,
#if CONFIG_APRS_TUNE
	AP_MY_FREQ,
#endif
	AP_COUNT };

// The rows this build shows, in order: the packet rows only with the AX.25 backend (CONFIG_APRS_AFSK), "Send via"
// only when there is a choice (both backends built)
static const uint8_t rows[] = {
#if CONFIG_APRS_HOME
	AP_HEARD, AP_MESSAGES, AP_WEATHER, AP_SEND, AP_BEACON, AP_AUTO,
#else
	AP_SEND, AP_AUTO,
#endif
#if CONFIG_APRS_AFSK
	AP_FREQ, AP_POWER, AP_PATH,
#endif
	AP_INTERVAL, AP_SMART,
#if CONFIG_APRS_AFSK && CONFIG_APRS_DMR
	AP_VIA,
#endif
#if CONFIG_APRS_PATHING
	AP_DECAY,
#endif
#if CONFIG_APRS_WX && !CONFIG_APRS_HOME
	AP_WEATHER,
#endif
#if CONFIG_APRS_BEACON && !CONFIG_APRS_HOME
	AP_BEACON,
#endif
	AP_KISS,
#if CONFIG_APRS_TUNE
	AP_MY_FREQ,
#endif
#if CONFIG_APRS_AFSK && CONFIG_APRS_RX
	AP_OBJECTS, AP_BULLETINS, AP_RECENT,
#endif
#if !CONFIG_APRS_HOME && CONFIG_APRS_AFSK
	AP_HEARD,
#endif

};
#define N_ROWS	((uint16_t)(sizeof(rows) / sizeof(rows[0])))

// Beacons go out on RF only: no DMR backend, or the setting says so
static bool rf_only(const aprs_cfg_t *c)
{
	return !CONFIG_APRS_DMR || c->via == APRS_VIA_RF;
}

static const uint16_t intervals[] = { 0, 60, 120, 300, 600, 1800 };
static const char *const interval_names[] = { TRN("Off"), TRN("1 min"), TRN("2 min"), TRN("5 min"),
						       TRN("10 min"), TRN("30 min") };
#define N_INTERVALS	(sizeof(intervals) / sizeof(intervals[0]))
// aprs_path_t order; the text is aprs_path_text()'s, kept here so the page builds without APRS (the simulator)
static const char *const path_names[APRS_PATH_COUNT] = { TRN("None"), "WIDE1-1", "WIDE1-1,WIDE2-1", "WIDE2-1",
							   "WIDE2-2", TRN("Proportional") };
static const char *const via_names[] = { "AFSK", "DMR", TRN("Both") };
static const char *const pos_names[] = { TRN("Auto"), "GPS", TRN("Computer"), TRN("Fixed") };
static const char *const region_names[APRS_REGION_COUNT] = { TRN("N America"), TRN("Europe"), TRN("Australia"),
							      TRN("New Zealand"), TRN("Japan"), TRN("China"),
							      TRN("Custom") };

// The radio can send APRS on RF (a board with an AFSK transmit path; some only receive)
static bool rf_send(void)
{
	return board.afsk != NULL && board.afsk->tx_start != NULL;
}

static const char *why_text(uint8_t block)
{
	switch (block) {
	case APRS_NO_CALL: return TR("Set your callsign (the radio name) in qdmr.");
	case APRS_NO_FREQ: return TR("No APRS frequency: pick a region.");
	case APRS_NOT_AMATEUR: return TR("Pick a frequency in a ham band this radio sends on.");
	case APRS_NO_AFSK: return TR("No APRS tones on this radio; DMR positions work.");
	default: return NULL;
	}
}

char ui_aprs_text[400];

const char *ui_aprs_beacon_status(void)
{
	char *t = ui_aprs_text;
	sbuf_t b;
	aprs_view_t v;
	sb_init(&b, t, sizeof ui_aprs_text);
	if (!ui_svc_aprs_view(&v)) {
		sb_str(&b, TR("APRS: not in this build"));
		return t;
	}
#if !CONFIG_APRS_HOME && CONFIG_APRS_RX && CONFIG_UI_COLOR
	if (gfx_bpp() != 1) {
		sb_str(&b, TR("# radar"));
		sb_chr(&b, '\n');
	}
#endif
	if (CONFIG_APRS_AFSK) {		// who and where on RF: the AX.25 backend's
		sb_str(&b, v.call[0] != '\0' ? v.call : TR("No callsign"));
		sb_str(&b, TR(" on "));
		sb_freq(&b, v.freq_hz);
		if (v.rf_active) {
			sb_str(&b, TR(" (sending)"));
		}
		const char *why = why_text(v.block);
		if (why != NULL) {
			sb_chr(&b, '\n');
			sb_str(&b, why);
		}
		sb_chr(&b, '\n');
	}
	if (v.pos_valid) {
		sb_str(&b, tr(pos_names[v.pos_src < 4 ? v.pos_src : 0]));
		sb_chr(&b, ' ');
		sb_degrees(&b, v.lat);
		sb_chr(&b, ' ');
		sb_degrees(&b, v.lon);
	} else {
		sb_str(&b, TR("No position"));
	}
	if (v.next_s != 0xFFFFu) {
		sb_chr(&b, '\n');
		sb_str(&b, TR("Next in "));
		sb_mmss(&b, v.next_s);
	}
	if (v.rf_sent + v.dmr_sent != 0) {
		sb_str(&b, TR("  Sent "));
		sb_u32(&b, v.rf_sent + v.dmr_sent, 1);
	}
	return t;
}

static uint16_t aprs_count(void) { return N_ROWS; }

static uint8_t interval_index(uint16_t s)
{
	for (uint8_t i = 0; i < N_INTERVALS; i++) {
		if (intervals[i] >= s) {
			return i;
		}
	}
	return (uint8_t)(N_INTERVALS - 1u);
}

#if CONFIG_APRS_HOME
// Remember identities viewed in Stations; new packets from a viewed station are not new stations.
#if CONFIG_APRS_RX
static char seen_calls[APRS_STATIONS][AX25_CALL_LEN + 4];
#endif
static uint8_t stations_new(void)
{
	uint8_t n = 0;
#if CONFIG_APRS_RX
	for (uint8_t i = 0; i < ui_svc_aprs_station_count(); i++) {
		aprs_station_t st;
		if (!ui_svc_aprs_station(i, &st))
			continue;
		bool seen = false;
		for (unsigned j = 0; j < APRS_STATIONS; j++)
			seen |= strcmp(st.call, seen_calls[j]) == 0;
		n += !seen;
	}
#endif
	return n;
}
static void stations_seen(void)
{
#if CONFIG_APRS_RX
	memset(seen_calls, 0, sizeof seen_calls);
	for (uint8_t i = 0; i < APRS_STATIONS; i++) {
		aprs_station_t st;
		if (ui_svc_aprs_station(i, &st))
			str_copy(seen_calls[i], sizeof seen_calls[i], st.call);
	}
#endif
}

#endif

static void aprs_item(uint16_t i, ui_item_t *it)
{
	static const char *const names[] = { TRN("Send position now"), TRN("Automatic beacons"), TRN("Frequency"),
					     TRN("Power"), TRN("Path"), TRN("Interval"), TRN("SmartBeaconing"),
		TRN("Send via"), TRN("KISS on USB"),
#if CONFIG_APRS_HOME
		TRN("Stations"),
#else
		TRN("Stations heard"),
#endif
		TRN("Decay"),
#if CONFIG_APRS_RX
		TRN("Recent packets"), TRN("Objects"), TRN("Bulletins"),
#endif
#if CONFIG_APRS_HOME
		TRN("Messages"),
#endif
		TRN("My beacon"), TRN("Weather"),
#if CONFIG_APRS_TUNE
					     TRN("My frequency in my beacon"),
#endif
	};
	aprs_view_t v;
	aprs_cfg_t c;
	bool built = ui_svc_aprs_view(&v);
	ui_svc_aprs_cfg(&c);
	it->num = i < 10u ? (char)('0' + (i + 1u) % 10u) : 0;	// rows after the 10th have no digit
	i = i < N_ROWS ? rows[i] : AP_COUNT;
	if (i >= AP_COUNT) {
		return;
	}
#if CONFIG_APRS_WX
	if (i == AP_WEATHER) {
		str_copy(it->label, sizeof it->label, tr(ui_aprs_weather_screen.prefix));
		if (!built) it->flags |= UI_ITEM_DIM;
		return;
	}
#endif
	// Without the AX.25 backend the host port is no TNC: it only takes NMEA positions
	str_copy(it->label, sizeof it->label, i == AP_KISS && !CONFIG_APRS_AFSK ? TR("NMEA on USB") : tr(names[i]));
	sbuf_t b;
	sb_init(&b, it->value, sizeof it->value);
	if (!built) {
		it->flags |= UI_ITEM_DIM;
		return;
	}
	switch (i) {
#if CONFIG_APRS_HOME
	case AP_MESSAGES:
		sb_u32(&b, v.unread, 1);
		sb_str(&b, TR(" unread"));
		break;
#endif
	case AP_SEND:
		if (v.block != APRS_OK && rf_only(&c)) {
			it->flags |= UI_ITEM_DIM;
		}
		break;
	case AP_AUTO: sb_str(&b, c.tx_on ? TR("On") : TR("Off")); break;
	case AP_FREQ:
		if (board.afsk == NULL && !CONFIG_APRS_RX) {
			it->flags |= UI_ITEM_DIM;	// no APRS on RF here
		} else if (c.region == APRS_REGION_CUSTOM) {
			sb_freq(&b, c.freq_hz);
		} else {
			sb_str(&b, tr(region_names[c.region < APRS_REGION_COUNT ? c.region : 0]));
		}
		break;
	case AP_POWER:
		if (!rf_send()) {
			it->flags |= UI_ITEM_DIM;	// no APRS on RF here (no transmit tone path)
		} else {
			sb_str(&b, c.power == 0 ? TR("As channel") : ui_power_name((uint8_t)(c.power - 1u), v.freq_hz));
		}
		break;
	case AP_PATH:
		if (!rf_send()) {
			it->flags |= UI_ITEM_DIM;	// the path is for RF packets only
		} else {
			sb_str(&b, tr(path_names[c.path < APRS_PATH_COUNT ? c.path : 0]));
		}
		break;
	case AP_INTERVAL:
	case AP_DECAY:
		if (c.smart) {
			it->flags |= UI_ITEM_DIM;
			sb_str(&b, TR("Smart on"));
		} else if (i == AP_INTERVAL && CONFIG_APRS_PATHING && c.path == APRS_PATH_PROPORTIONAL) {
			it->flags |= UI_ITEM_DIM;
			sb_str(&b, TR("Path: 1 min"));
		} else if (i == AP_DECAY) {
			sb_str(&b, c.decay ? TR("On") : TR("Off"));
		} else {
			sb_str(&b, tr(interval_names[interval_index(c.interval_s)]));
		}
		break;
	case AP_SMART: sb_str(&b, c.smart ? TR("On") : TR("Off")); break;
	case AP_VIA: sb_str(&b, tr(via_names[c.via < APRS_VIA_COUNT ? c.via : 0])); break;
#if CONFIG_APRS_TUNE
	case AP_MY_FREQ: sb_str(&b, c.my_frequency ? TR("On") : TR("Off")); break;
#endif
	case AP_KISS: sb_str(&b, c.kiss ? TR("On") : TR("Off")); break;
#if CONFIG_APRS_RX
	case AP_RECENT: sb_u32(&b, ui_svc_aprs_packet_count(), 1); break;
#endif
	case AP_HEARD:
		if (!CONFIG_APRS_RX) {
			it->flags |= UI_ITEM_DIM;
		} else {
			sb_u32(&b, v.stations, 1);
#if CONFIG_APRS_HOME
			sb_str(&b, ", ");
			sb_u32(&b, stations_new(), 1);
			sb_str(&b, TR(" new"));
#endif
		}
		break;
	default: break;
	}
}

static void aprs_select(uint16_t i, bool hold)
{
	(void)hold;
	aprs_view_t v;
	aprs_cfg_t c;
	if (!ui_svc_aprs_view(&v)) {
		ui_toast(TR("APRS: not in this build"));
		return;
	}
	ui_svc_aprs_cfg(&c);
	switch (i < N_ROWS ? rows[i] : AP_COUNT) {
#if CONFIG_APRS_HOME
	case AP_MESSAGES:
		ui_push(SCR_MESSAGES, 0);
		return;
	case AP_BEACON:
		ui_push(SCR_APRS_BEACON, 0);
		return;
#endif
	case AP_SEND:
		if (ui_svc_aprs_send_now()) {
			ui_toast(TR("Sending position"));
		} else {
			ui_toast(v.block == APRS_NO_CALL ? TR("Set your callsign first") : TR("Can't send now"));
		}
		return;
	case AP_AUTO:
		c.tx_on = !c.tx_on;
		ui_toast(c.tx_on ? TR("Beacons on") : TR("Beacons off"));
		break;
	case AP_FREQ:
		if (board.afsk == NULL && !CONFIG_APRS_RX) {
			return;
		}
		c.region = (uint8_t)((c.region + 1u) % APRS_REGION_COUNT);
		if (c.region == APRS_REGION_CUSTOM && c.freq_hz == 0) {
			c.region = 0;	// no custom frequency set (qdmr / debug link): skip it
		}
		break;
	case AP_POWER:
		if (!rf_send()) {
			return;
		}
		c.power = (uint8_t)((c.power + 1u) % (board.power_count + 1u));	// as the channel, then each level
		break;
	case AP_PATH:
		if (!rf_send()) {
			return;
		}
		c.path = (uint8_t)((c.path + 1u) % (CONFIG_APRS_PATHING ? APRS_PATH_COUNT : APRS_PATH_PROPORTIONAL));	// the local proportional policy follows the literal paths
		break;
	case AP_INTERVAL:
	case AP_DECAY:
		if (c.smart) {
			ui_toast(TR("SmartBeaconing sets timing"));
			return;
		}
		if (rows[i] == AP_INTERVAL) {
			if (CONFIG_APRS_PATHING && c.path == APRS_PATH_PROPORTIONAL) {
				ui_toast(TR("Path uses one-minute slots"));
				return;
			}
			c.interval_s = intervals[(interval_index(c.interval_s) + 1u) % N_INTERVALS];
		} else {
			c.decay = !c.decay;
		}
		break;
	case AP_SMART: c.smart = !c.smart; break;
	case AP_VIA: c.via = (uint8_t)((c.via + 1u) % APRS_VIA_COUNT); break;
#if CONFIG_APRS_TUNE
	case AP_MY_FREQ: c.my_frequency = !c.my_frequency; break;
#endif
	case AP_KISS: c.kiss = !c.kiss; break;
#if CONFIG_APRS_RX
	case AP_OBJECTS: ui_push(SCR_APRS_OBJECTS, 0); return;
	case AP_BULLETINS: ui_push(SCR_APRS_BULLETINS, 0); return;
	case AP_RECENT: ui_push(SCR_APRS_PACKETS, 0); return;
#endif
#if CONFIG_APRS_WX
	case AP_WEATHER: ui_push(SCR_APRS_WEATHER, 0); return;
#endif
#if CONFIG_APRS_BEACON && !CONFIG_APRS_HOME
	case AP_BEACON:
		ui_push(SCR_APRS_BEACON, 0);
		return;
#endif
	case AP_HEARD:
		if (!CONFIG_APRS_RX) {
			ui_toast(TR("This radio can't receive APRS"));
		} else {
#if CONFIG_APRS_HOME
			stations_seen();
#endif
			ui_push(SCR_APRS_HEARD, 0);
		}
		return;
	default: return;
	}
	ui_svc_aprs_set_cfg(&c);
	ui_dirty();
}

#if CONFIG_APRS_RX && CONFIG_UI_COLOR
static void aprs_context(uint16_t i)
{
	(void)i;
	if (gfx_bpp() != 1)
		ui_push(SCR_APRS_RADAR, 0);
}
#endif


static const char *aprs_body(void)
{
#if CONFIG_APRS_HOME
	char *body = ui_aprs_text;
	ui_aprs_status(body, sizeof ui_aprs_text, false);
	sbuf_t b;
	sb_init(&b, body + strlen(body), sizeof ui_aprs_text - strlen(body));
	sb_chr(&b, '\n');
	size_t used = strlen(body);
	ui_aprs_status(body + used, sizeof ui_aprs_text - used, true);
	sb_init(&b, body + strlen(body), sizeof ui_aprs_text - strlen(body));
#if CONFIG_APRS_RX && CONFIG_UI_COLOR
	if (gfx_bpp() != 1) {
		sb_chr(&b, '\n');
		sb_str(&b, TR("# radar"));
	}
#endif
	return body;
#else
	return ui_aprs_beacon_status();
#endif
}

#if CONFIG_APRS_HOME && !CONFIG_APRS_BEACON
static uint16_t beacon_count(void) { return 1; }
static void beacon_item(uint16_t i, ui_item_t *it)
{
	(void)i;
	aprs_cfg_t c;
	ui_svc_aprs_cfg(&c);
	str_copy(it->label, sizeof it->label, TR("Symbol"));
	it->value[0] = c.sym_table;
	it->value[1] = c.sym_code;
	it->value[2] = 0;
}
const ui_scr_def_t ui_aprs_beacon_screen = {
	.name = "aprs-beacon", .flags = SF_LIST | SF_FIELD, .prefix = TRN("My beacon"), .hint = TRN("Back"),
	.body = ui_aprs_beacon_status, .count = beacon_count, .item = beacon_item,
};

#endif
const ui_scr_def_t ui_aprs_screen = {
#if CONFIG_APRS_RX && CONFIG_UI_COLOR
	.context = aprs_context,
#endif
	.name = "aprs", .flags = SF_LIST | SF_NUMBERED | SF_FIELD, .prefix = "APRS", .hint = TRN("0-9 pick  Back"),
	.count = aprs_count, .item = aprs_item, .select = aprs_select, .body = aprs_body,
};

// --- Stations heard (receive builds) ---

#if CONFIG_APRS_RX
static char station_call[AX25_CALL_LEN + 4];
void ui_aprs_station_open(const char *call)
{
	str_copy(station_call, sizeof station_call, call);
	ui_push(SCR_APRS_STATION, 0);
}
#else
static uint16_t heard_count(void) { return ui_svc_aprs_station_count(); }

static void heard_select(uint16_t i, bool hold)
{
	(void)hold;
#if CONFIG_APRS_RX
	aprs_station_t s;
	if (ui_svc_aprs_station((uint8_t)i, &s)) {
		ui_aprs_station_open(s.call);
	}
#else
	ui_push(SCR_APRS_STATION, i);
#endif
}

static void heard_item(uint16_t i, ui_item_t *it)
{
	aprs_station_t s;
	if (!ui_svc_aprs_station((uint8_t)i, &s)) {
		return;
	}
	sbuf_t b;
	uint32_t m, at = s.heard_ms;
	uint16_t deg;
	sb_init(&b, it->label, sizeof it->label);
	sb_str(&b, s.call);
	if (ui_svc_aprs_range(&s, &m, &deg)) {
		// How far and which way ("12.3 km NE"): an alternate, so a narrow row drops it before the call (font.h)
		sb_str(&b, TEXT_ALT_OPEN " ");
		sb_range(&b, m, deg);
		sb_str(&b, TEXT_ALT_NEXT TEXT_ALT_CLOSE);
		at = s.pos_ms;	// the age of the position the range is from, not of the station's last packet
	}
	ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - at);
	if (!s.direct) {
		it->flags |= UI_ITEM_TEMP;	// heard through a digipeater
	}
}

const ui_scr_def_t ui_aprs_heard_screen = {
	.name = "aprs-heard", .flags = SF_LIST | SF_FIELD, .prefix = TRN("Heard"), .hint = TRN("Back"),
	.empty = TRN("Nothing heard yet"),
	.count = heard_count, .item = heard_item, .select = heard_select,
};

#endif

#if CONFIG_APRS_RX
enum { ST_MESSAGE, ST_TUNE, ST_RANGE, ST_HEARD, ST_PATH, ST_POS_AGE, ST_SPEED, ST_COURSE, ST_SYMBOL, ST_TEXT, ST_PACKETS, ST_COUNT };

// Resolve optional rows from the current record; no copy of the engine's station table lives in the UI.
static uint8_t station_row(uint16_t i, const aprs_station_t *s)
{
	uint32_t m;
	uint16_t deg;
	for (uint8_t row = 0; row < ST_COUNT; row++) {
		if (row == ST_TUNE) {
#if CONFIG_APRS_TUNE
			if (!s->frequency.hz) continue;
#else
			continue;
#endif
		}
		if ((row == ST_RANGE && !ui_svc_aprs_range(s, &m, &deg)) ||
		    (row == ST_POS_AGE && (!s->has_pos || s->pos_ms == s->heard_ms)) ||
		    (row == ST_SPEED && (!s->has_pos || s->speed_kn == 0xFFFFu)) ||
		    (row == ST_COURSE && (!s->has_pos || s->course == 0xFFFFu)) ||
		    (row == ST_SYMBOL && (!s->sym_table || !s->sym_code)) ||
		    (row == ST_TEXT && !s->text[0])) {
			continue;
		}
		if (i-- == 0) {
			return row;
		}
	}
	return ST_COUNT;
}

static uint16_t station_count(void)
{
	aprs_station_t s;
	if (!ui_svc_aprs_station_find(station_call, &s)) {
		return 2;
	}
	uint16_t n = 0;
	while (station_row(n, &s) != ST_COUNT) {
		n++;
	}
	return n;
}

static void station_item(uint16_t i, ui_item_t *it)
{
	aprs_station_t s;
	bool found = ui_svc_aprs_station_find(station_call, &s);
	if (i == 0) {
		str_copy(it->label, sizeof it->label, TR("Message"));
		return;
	}
	if (!found) {
		str_copy(it->label, sizeof it->label, TR("No longer in list"));
		return;
	}
	sbuf_t b;
	sb_init(&b, it->value, sizeof it->value);
	const char *label = "";
	uint32_t m;
	uint16_t deg;
	switch (station_row(i, &s)) {
	case ST_TUNE:
#if CONFIG_APRS_TUNE
		ui_aprs_tune_item(&s.frequency, it);
		return;
#endif
		break;
	case ST_RANGE:
		label = TR(TEXT_ALT_OPEN "Distance" TEXT_ALT_NEXT "Dist" TEXT_ALT_NEXT "" TEXT_ALT_CLOSE);
		if (ui_svc_aprs_range(&s, &m, &deg)) {
			sb_range(&b, m, deg);
		}
		break;
	case ST_HEARD:
		label = TR("Heard");
		ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - s.heard_ms);
		break;
	case ST_PATH: label = s.direct ? TR("Direct") : TR("Via digipeater"); break;
	case ST_POS_AGE:
		label = TR(TEXT_ALT_OPEN "Position age" TEXT_ALT_NEXT "Pos age" TEXT_ALT_CLOSE);
		ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - s.pos_ms);
		break;
	case ST_SPEED:
		label = TR("Speed");
		sb_speed(&b, (uint32_t)s.speed_kn * 1852u);
		break;
	case ST_COURSE:
		label = TR("Course");
		sb_u32(&b, s.course, 1);
		sb_str(&b, GLYPH_DEGREE " ");
		sb_compass(&b, s.course);
		break;
	case ST_SYMBOL:
		label = TR("Symbol");
		sb_chr(&b, s.sym_table);
		sb_chr(&b, s.sym_code);
		break;
	case ST_TEXT:
		label = s.text;
		it->flags |= UI_ITEM_PREVIEW;
		break;
	case ST_PACKETS:
		label = TR("Packets");
		sb_u32(&b, s.count, 1);
		break;
	default: break;
	}
	str_copy(it->label, sizeof it->label, label);
}

static const char *station_title(void) { return station_call; }
static void station_select(uint16_t i, bool hold)
{
	(void)hold;
	if (i == 0) {
		aprs_station_t s;
		bool found = ui_svc_aprs_station_find(station_call, &s);
		const char *call = found ? s.call : station_call;
		if (ui_aprs_thread_exists(call))
			ui_aprs_thread_open(call);
		else
			ui_aprs_compose(call);
	}
#if CONFIG_APRS_TUNE
	else {
		aprs_station_t s;
		if (ui_svc_aprs_station_find(station_call, &s) && station_row(i, &s) == ST_TUNE)
			ui_aprs_tune(&s.frequency);
	}
#endif
}
#else
static uint16_t station_count(void) { return 1; }
static void station_item(uint16_t i, ui_item_t *it)
{
	(void)i;
	str_copy(it->label, sizeof it->label, TR("Message"));
}
static const char *station_title(void)
{
	static char call[AX25_CALL_LEN + 4];
	aprs_station_t s;
	if (!ui_svc_aprs_station((uint8_t)ui_frame()->arg, &s))
		return "";
	str_copy(call, sizeof call, s.call);
	return call;
}
static void station_select(uint16_t i, bool hold)
{
	(void)i; (void)hold;
	aprs_station_t s;
	if (ui_svc_aprs_station((uint8_t)ui_frame()->arg, &s))
		ui_aprs_compose(s.call);
}
#endif
const ui_scr_def_t ui_aprs_station_screen = {
	.name = "aprs-station", .flags = SF_LIST | SF_FIELD, .prefix = TRN("Heard"), .hint = TRN("OK choose  Back"),
	.count = station_count, .item = station_item, .select = station_select, .title = station_title,
};

#if CONFIG_APRS_RX
WEAK uint8_t ui_svc_aprs_packet_count(void) { return 0; }
WEAK bool ui_svc_aprs_packet(uint8_t i, aprs_packet_t *out) { (void)i; (void)out; return false; }
WEAK bool ui_svc_aprs_listening(void) { return false; }
WEAK bool ui_svc_aprs_can_receive(void) { return false; }

const char *ui_aprs_kind(uint8_t type, bool to_us)
{
	switch (type) {
	case APRS_T_POSITION: case APRS_T_MICE: return TR("position");
	case APRS_T_STATUS: return TR("status");
	case APRS_T_WEATHER: return TR("weather");
	case APRS_T_MESSAGE: return to_us ? TR("message to you") : TR("message");
	case APRS_T_OBJECT: return TR("object");
	case APRS_T_ITEM: return TR("item");
	case APRS_T_TELEMETRY: return TR("telemetry");
	case APRS_T_QUERY: return TR("query");
	case APRS_T_THIRD_PARTY: return TR("third party");
	default: return TR("packet");
	}
}

static aprs_packet_t packet_selected; // A ring overwrite cannot change the open packet.

static uint16_t packets_count(void) { return ui_svc_aprs_packet_count(); }
static void packets_item(uint16_t i, ui_item_t *it)
{
	aprs_packet_t p;
	if (!ui_svc_aprs_packet((uint8_t)i, &p))
		return;
	sbuf_t b;
	sb_init(&b, it->label, sizeof it->label);
	sb_str(&b, p.call);
	sb_str(&b, "  ");
	sb_str(&b, ui_aprs_kind(p.type, p.to_us));
	ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - p.ms);
}

static const char *packets_body(void)
{
	if (ui_svc_aprs_listening())
		return NULL;
	if (!ui_svc_aprs_can_receive())
		return TR("This radio can't receive APRS");
	aprs_cfg_t c;
	ui_svc_aprs_cfg(&c);
	if (!c.rx_on)
		return TR("Not listening: receive off");
	if (ui_view()->state == RS_TX)
		return TR("Not listening: transmitting");
	aprs_view_t v;
	ui_svc_aprs_view(&v);
	if (ui_view()->ch.mode != RADIO_MODE_FM || ui_view()->ch.rx_hz != v.freq_hz)
		return TR("Not listening: on voice channel");
	return TR("Not listening: audio unavailable");
}

static void packets_select(uint16_t i, bool hold)
{
	(void)hold;
	if (ui_svc_aprs_packet((uint8_t)i, &packet_selected))
		ui_push(SCR_APRS_PACKET, 0);
}

const ui_scr_def_t ui_aprs_packets_screen = {
	.name = "aprs-packets", .flags = SF_LIST | SF_FIELD, .prefix = TRN("Recent packets"),
	.hint = TRN("OK open  Back"), .empty = TRN("Nothing heard yet"),
	.count = packets_count, .item = packets_item, .select = packets_select, .body = packets_body,
};

// Up to sixteen displayed characters per raw row, including complete byte escapes.
static uint8_t packet_raw_end(uint8_t start)
{
	uint8_t width = 0;
	while (start < packet_selected.len) {
		uint8_t c = packet_selected.info[start];
		uint8_t n = c >= 32 && c <= 126 && c != '\\' ? 1 : 4;
		if (width + n > 16)
			break;
		width += n;
		start++;
	}
	return start;
}

static uint16_t packet_count(void)
{
	uint16_t n = 4u + (packet_selected.name[0] != 0) + (packet_selected.addressee[0] != 0) +
		(packet_selected.has_pos ? 2u : 0u) + packet_selected.truncated;
	if (packet_selected.has_pos) {
		n += packet_selected.course != APRS_COURSE_NONE;
		n += packet_selected.speed_kn != APRS_SPEED_NONE;
	}
	for (uint8_t at = 0; at < packet_selected.len; at = packet_raw_end(at))
		n++;
	return n;
}

static void packet_item(uint16_t i, ui_item_t *it)
{
	const aprs_packet_t *p = &packet_selected;
	const char *label = "";
	sbuf_t b;
	sb_init(&b, it->value, sizeof it->value);
	if (i == 0) {
		label = ui_aprs_kind(p->type, p->to_us);
	} else if (i == 1) {
		label = TR("Heard");
		ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - p->ms);
	} else if (i == 2) {
		label = p->direct ? TR("Direct") : TR("Via digipeater");
	} else {
		i -= 3;
		if (p->name[0]) {
			if (i-- == 0) { label = p->name; goto done; }
		}
		if (p->addressee[0]) {
			if (i-- == 0) { label = TR("To"); sb_str(&b, p->addressee); goto done; }
		}
		if (p->has_pos) {
			if (i < 2) {
				label = i == 0 ? TR("Latitude") : TR("Longitude");
				sb_degrees(&b, i == 0 ? p->lat : p->lon);
				goto done;
			}
			i -= 2;
		}
		if (p->has_pos && p->course != APRS_COURSE_NONE && i-- == 0) {
			label = TR("Course");
			sb_u32(&b, p->course, 1);
			sb_compass(&b, p->course);
			goto done;
		}
		if (p->has_pos && p->speed_kn != APRS_SPEED_NONE && i-- == 0) {
			label = TR("Speed");
			sb_u32(&b, ((uint32_t)p->speed_kn * 1852u + 500u) / 1000u, 1);
			sb_str(&b, " km/h");
			goto done;
		}
		if (i-- == 0) { label = TR("Raw"); goto done; }
		uint8_t at = 0;
		while (i-- != 0 && at < p->len)
			at = packet_raw_end(at);
		if (at >= p->len) {
			label = TR("[truncated]");
		} else {
			sb_init(&b, it->label, sizeof it->label);
			uint8_t end = packet_raw_end(at);
			for (; at < end; at++) {
				uint8_t c = p->info[at];
				if (c >= 32 && c <= 126 && c != '\\') {
					sb_chr(&b, (char)c);
				} else {
					static const char hex[] = "0123456789ABCDEF";
					sb_str(&b, "\\x");
					sb_chr(&b, hex[c >> 4]);
					sb_chr(&b, hex[c & 15]);
				}
			}
			return;
		}
	}
done:
	str_copy(it->label, sizeof it->label, label);
}
static const char *packet_title(void) { return packet_selected.call; }
const ui_scr_def_t ui_aprs_packet_screen = {
	.name = "aprs-packet", .flags = SF_LIST | SF_FIELD, .prefix = TRN("Packet"), .hint = TRN("Back"),
	.count = packet_count, .item = packet_item, .title = packet_title,
};
#endif
