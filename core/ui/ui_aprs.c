// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// APRS operating status and tasks; reporting policy lives on the Settings page.
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
#if CONFIG_APRS_BEACON
WEAK bool ui_svc_aprs_emergency(void) { return false; }
WEAK void ui_svc_aprs_stop_tx(bool stop) { (void)stop; }
WEAK bool ui_svc_aprs_tx_stopped(void) { return false; }
#endif
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
	AP_BEACON, AP_WEATHER, AP_SETTINGS, AP_POSITION, AP_LOCAL, AP_REPEAT,
#if CONFIG_APRS_TUNE
	AP_MY_FREQ,
#endif
#if CONFIG_APRS_BEACON
	AP_STOP, AP_DMR, AP_EMERGENCY,
#endif
	AP_LISTEN_VOICE, AP_COUNT };

// The rows this build shows, in order: the packet rows only with the AX.25 backend (CONFIG_APRS_AFSK), "Send via"
// only when there is a choice (both backends built)
static const uint8_t rows[] = {
	AP_SEND, AP_LOCAL, AP_REPEAT, AP_HEARD,
#if CONFIG_APRS_HOME
	AP_MESSAGES,
#endif
#if CONFIG_APRS_RX
	AP_BULLETINS,
#endif
#if CONFIG_APRS_WX
	AP_WEATHER,
#endif
#if CONFIG_APRS_RX
	AP_OBJECTS,
#endif
#if CONFIG_APRS_HOME || CONFIG_APRS_BEACON
	AP_BEACON,
#endif
#if CONFIG_APRS_RX
	AP_RECENT,
#endif
	AP_POSITION, AP_SETTINGS,
#if CONFIG_APRS_BEACON
	AP_STOP,
#endif
};
static const uint8_t settings_rows[] = {
	AP_AUTO,
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
	AP_KISS,
#if CONFIG_APRS_TUNE
	AP_MY_FREQ,
#endif
#if CONFIG_APRS_RX && CONFIG_DUAL_WATCH
	AP_LISTEN_VOICE,
#endif
#if CONFIG_APRS_BEACON
	AP_EMERGENCY,
#endif
#if CONFIG_APRS_BEACON && CONFIG_APRS_DMR
	AP_DMR,
#endif
};
#define N_ROWS (sizeof(rows) / sizeof(rows[0]))
#define N_SETTINGS (sizeof(settings_rows) / sizeof(settings_rows[0]))
static uint8_t aprs_row(uint16_t i)
{
	return ui_frame()->scr == SCR_APRS_SETTINGS ?
		(i < N_SETTINGS ? settings_rows[i] : AP_COUNT) : (i < N_ROWS ? rows[i] : AP_COUNT);
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

// A proposed setting stays here until the last review page is explicitly saved.
static aprs_cfg_t review_cfg;
static const char *review_comment;
static uint16_t review_first, review_last;
static uint8_t review_page;

void ui_aprs_review(const aprs_cfg_t *cfg, const char *comment)
{
	review_cfg = *cfg;
	review_comment = comment;
	review_page = 0;
	review_first = 0;
	ui_push(SCR_APRS_REVIEW, 0);
}

static void review_draw(void)
{
	aprs_view_t v;
	ui_svc_aprs_view(&v);
	sbuf_t b;
	sb_init(&b, ui_aprs_text, sizeof ui_aprs_text);
	const aprs_cfg_t *c = &review_cfg;
	switch (review_page) {
	case 0:
		sb_str(&b, TR("Precision: "));
		if (c->compressed) sb_str(&b, TR("Compressed position, 1-2 ft resolution. No ambiguity."));
		else {
			static const char *const minutes[] = { "0.01", "0.1", "1", "10", "60" };
			sb_str(&b, minutes[v.ambiguity < 5 ? v.ambiguity : 4]);
			sb_str(&b, TR(" minutes of position resolution."));
		}
		break;
	case 1:
		sb_str(&b, TR("Voice frequency: "));
#if CONFIG_APRS_TUNE
		if (c->my_frequency) {
			sb_str(&b, TR("Follows the voice channel. "));
			if (ui_view()->ch.mode == RADIO_MODE_FM && ui_view()->ch.rx_hz % 1000u == 0 &&
			    ui_view()->ch.rx_hz >= 1000000u && ui_view()->ch.rx_hz < 1000000000u &&
			    ui_view()->ch.tx_tone.type != TONE_DCS_INV) {
				sb_freq(&b, ui_view()->ch.rx_hz);
				sb_str(&b, " MHz");
			} else sb_str(&b, TR("Not included on this channel."));
		} else
#endif
			sb_str(&b, TR("Not included."));
		break;
	case 2:
		sb_str(&b, TR("Position source: "));
		sb_str(&b, v.pos_valid ? tr(pos_names[v.pos_src < 4 ? v.pos_src : 0]) : TR("No position"));
		if (v.pos_valid) {
			sb_chr(&b, '\n');
			sb_str(&b, TR("Position age: "));
			if (v.pos_src == 3) sb_str(&b, TR("Entry age unknown"));
			else { sb_u32(&b, v.pos_age_s, 1); sb_chr(&b, 's'); }
			sb_chr(&b, '\n');
			sb_degrees(&b, v.lat); sb_chr(&b, ' '); sb_degrees(&b, v.lon);
		}
		break;
	case 3:
		sb_str(&b, c->tx_on ? TR("Automatic beacons on. ") : TR("Automatic beacons off. "));
		if (c->smart) {
			sb_str(&b, TR("Smart: 30 min up to 5 km/h or unknown speed; 3 min from 90 km/h. Between: 270/speed min, at most 30 min. Above 5 km/h, turns over 28 + 400/speed degrees may send after 30s. Interval and decay do not apply."));
		} else {
			uint16_t seconds = CONFIG_APRS_PATHING && c->path == APRS_PATH_PROPORTIONAL ? 60 : c->interval_s;
			sb_str(&b, TR("Interval: ")); sb_u32(&b, seconds, 1); sb_chr(&b, 's');
#if CONFIG_APRS_PATHING
			if (c->decay) sb_str(&b, TR(". Decay doubles unchanged reports up to 30 min."));
#endif
		}
		break;
	default:
		sb_str(&b, TR("Save beacon settings?"));
		sb_chr(&b, '\n');
		sb_str(&b, c->tx_on ? TR("Automatic beacons on. ") : TR("Automatic beacons off. "));
		sb_chr(&b, '\n');
		sb_str(&b, TR("Path: "));
		sb_str(&b, tr(path_names[c->path < APRS_PATH_COUNT ? c->path : 0]));
		if (review_comment) { sb_chr(&b, '\n'); sb_str(&b, review_comment); }
		break;
	}
	review_last = (uint16_t)ui_draw_text_page(ui_view(), &ui_st, TR("Beacon review"), ui_aprs_text, &review_first, true);
	ui_draw_hint(review_page == 4 ? TR("OK save  Back") :
		review_first < review_last ? TR("Up/Dn more  OK next") : TR("OK next  Back"), false);
}

static bool review_key(const input_event_t *ev)
{
	if (ev->gesture != GESTURE_PRESS && ev->gesture != GESTURE_REPEAT && ev->action != ACT_ENC_STEP)
		return false;
	if (ev->action == ACT_SELECT) {
		if (review_page < 4) { review_page++; review_first = 0; }
		else {
#if CONFIG_APRS_BEACON
			if (review_comment) {
				if (!ui_svc_aprs_set_comment(review_comment)) {
					ui_toast(TR("Comment not saved"));
					return true;
				}
			} else
#endif
				ui_svc_aprs_set_cfg(&review_cfg);
			ui_pop();
		}
		ui_dirty();
		return true;
	}
	int dir = ev->action == ACT_NAV_UP ? -1 : ev->action == ACT_NAV_DOWN ? 1 :
		ev->action == ACT_ENC_STEP ? ev->value : 0;
	if (!dir) return false;
	int line = review_first + dir;
	review_first = (uint16_t)(line < 0 ? 0 : line > review_last ? review_last : line);
	ui_dirty();
	return true;
}
const ui_scr_def_t ui_aprs_review_screen = {
	.name = "aprs-review", .prefix = TRN("Beacon review"), .draw = review_draw, .key = review_key,
};

static uint16_t aprs_count(void) { return N_ROWS; }
static uint16_t settings_count(void) { return N_SETTINGS; }

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
		TRN("My beacon"), TRN("Weather"), TRN("Settings"), TRN("My position"), TRN("Local TX"), TRN("Repeat"),
#if CONFIG_APRS_TUNE
					     TRN(TEXT_ALT_OPEN "My frequency in my beacon" TEXT_ALT_NEXT "My frequency" TEXT_ALT_NEXT "My freq" TEXT_ALT_CLOSE),
#endif
#if CONFIG_APRS_BEACON
		TRN("Stop all APRS TX"), "DMR", TRN("Emergency"),
#endif
		TRN(TEXT_ALT_OPEN "Listen on voice channels" TEXT_ALT_NEXT "Listen on voice" TEXT_ALT_CLOSE),
	};
	aprs_view_t v;
	aprs_cfg_t c;
	bool built = ui_svc_aprs_view(&v);
	ui_svc_aprs_cfg(&c);
	bool settings = ui_frame()->scr == SCR_APRS_SETTINGS;
	it->num = settings && i < 10u ? (char)('0' + (i + 1u) % 10u) : 0;
	i = aprs_row(i);
	if (!settings) {
		switch (i) {
		case AP_HEARD: it->num = '1'; break;
#if CONFIG_APRS_HOME
		case AP_MESSAGES: it->num = '2'; break;
#endif
#if CONFIG_APRS_RX
		case AP_BULLETINS: it->num = '3'; break;
		case AP_OBJECTS: it->num = '5'; break;
		case AP_RECENT: it->num = '7'; break;
#endif
		case AP_WEATHER: it->num = '4'; break;
		case AP_BEACON: it->num = '6'; break;
		case AP_POSITION: it->num = '8'; break;
		case AP_SETTINGS: it->num = '0'; break;
		default: break;
		}
	}
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
	case AP_LOCAL:
	case AP_REPEAT: {
		it->flags |= UI_ITEM_DIM;
#if CONFIG_APRS_HOME
		aprs_home_t h;
		ui_svc_aprs_home(&h);
		if (i == AP_REPEAT) sb_str(&b, h.digi[0] ? h.digi : TR("No repeat heard"));
		else if (h.sent) sb_age_long(&b, ui_st.now_ms - h.sent_ms);
		else sb_str(&b, TR("Not sent"));
#else
		sb_str(&b, TR("Unknown"));
#endif
		break;
	}
#if CONFIG_APRS_HOME
	case AP_MESSAGES:
		sb_u32(&b, v.unread, 1);
		sb_str(&b, TR(" unread"));
		break;
#endif
#if CONFIG_APRS_BEACON
	case AP_STOP:
		if (ui_svc_aprs_tx_stopped()) str_copy(it->label, sizeof it->label, TR("Resume APRS TX"));
		break;
#endif
	case AP_SEND: break;
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
	case AP_LISTEN_VOICE: sb_str(&b, c.listen_voice ? TR("On") : TR("Off")); break;
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

#if CONFIG_APRS_BEACON
enum { TASK_RESULT, TASK_STOP, TASK_POSITION, TASK_DMR, TASK_EMERGENCY };
static uint16_t task_first, task_last;
static bool task_accepted, task_stop_done;

static void task_open(uint16_t task)
{
	task_first = 0;
	task_stop_done = false;
	ui_push(SCR_APRS_TASK, task);
}

static void task_draw(void)
{
	aprs_view_t v;
	aprs_cfg_t c;
	ui_svc_aprs_view(&v);
	ui_svc_aprs_cfg(&c);
	sbuf_t b;
	sb_init(&b, ui_aprs_text, sizeof ui_aprs_text);
	const char *title = TR("Send position now");
	const char *hint = TR("Up/Dn more  Back");
	switch (ui_frame()->arg) {
	case TASK_RESULT:
		sb_str(&b, !task_accepted ? (why_text(v.block) ? why_text(v.block) : TR("Can't send now")) :
			v.manual_result == APRS_SEND_SENT ? TR("Sent") :
			v.manual_result == APRS_SEND_STOPPED ? TR("Canceled") :
			v.manual_result == APRS_SEND_BLOCKED || v.manual_result == APRS_SEND_FAILED ? TR("Can't send now") : TR("Queued"));
		sb_chr(&b, '\n');
		sb_str(&b, v.pos_valid ? tr(pos_names[v.pos_src < 4 ? v.pos_src : 0]) : TR("No position"));
		sb_chr(&b, '\n');
		sb_str(&b, c.via == APRS_VIA_DMR ? "DMR" : tr(path_names[c.path < APRS_PATH_COUNT ? c.path : 0]));
#if CONFIG_APRS_HOME
		if (v.manual_result == APRS_SEND_SENT) {
			aprs_home_t h;
			ui_svc_aprs_home(&h);
			sb_chr(&b, '\n');
			sb_str(&b, h.digi[0] ? h.digi : TR("No repeat heard"));
		}
#endif
		break;
	case TASK_STOP:
		title = TR("Stop all APRS TX");
		sb_str(&b, ui_svc_aprs_tx_stopped() ? TR("TX off") : TR("Stop all APRS TX"));
		sb_chr(&b, '\n');
		sb_str(&b, TR("Beacons, messages, ACKs, relays"));
		hint = task_stop_done ? TR("Back") : ui_svc_aprs_tx_stopped() ? TR("OK resume  Back") : TR("OK confirm  Back");
		break;
	case TASK_POSITION:
		title = TR("My position");
		sb_str(&b, v.pos_valid ? tr(pos_names[v.pos_src < 4 ? v.pos_src : 0]) : TR("No position"));
		if (v.pos_valid) {
			sb_chr(&b, '\n'); sb_degrees(&b, v.lat);
			sb_chr(&b, '\n'); sb_degrees(&b, v.lon);
			sb_chr(&b, '\n');
			if (v.pos_src == 3) sb_str(&b, TR("Entry age unknown"));
			else { sb_u32(&b, v.pos_age_s, 1); sb_chr(&b, 's'); }
		}
		hint = TR("OK source  # fixed");
		break;
	case TASK_DMR:
		title = "DMR";
		sb_str(&b, TR("Send to")); sb_chr(&b, ' '); sb_u32(&b, c.dmr_dst, 1);
		sb_chr(&b, '\n'); sb_str(&b, c.dmr_udt ? "UDT" : "LRRP");
		sb_chr(&b, '\n'); sb_u32(&b, ui_view()->dmr.own_id, 1);
		sb_chr(&b, '\n'); sb_str(&b, ui_view()->ch.name);
		sb_chr(&b, '\n'); sb_str(&b, TR("Queued")); sb_chr(&b, ' '); sb_u32(&b, v.dmr_sent, 1);
		sb_chr(&b, '\n'); sb_str(&b, v.pos_valid ? TR("Position") : TR("No position"));
		break;
	default:
		title = TR("Emergency");
		sb_str(&b, TR("Send position now"));
		sb_chr(&b, '\n');
		sb_str(&b, TR("Emergency use only."));
		sb_chr(&b, '\n');
		sb_str(&b, v.pos_valid ? tr(pos_names[v.pos_src < 4 ? v.pos_src : 0]) : TR("No position"));
		hint = TR("OK confirm  Back");
		break;
	}
	task_last = (uint16_t)ui_draw_text_page(ui_view(), &ui_st, title, ui_aprs_text, &task_first, true);
	ui_draw_hint(hint, false);
}

static bool task_key(const input_event_t *ev)
{
	if (ev->gesture != GESTURE_PRESS && ev->gesture != GESTURE_REPEAT && ev->action != ACT_ENC_STEP) return false;
	uint16_t task = ui_frame()->arg;
	if (ev->action == ACT_SELECT && task == TASK_STOP) {
		if (!task_stop_done && ev->gesture == GESTURE_PRESS) ui_svc_aprs_stop_tx(!ui_svc_aprs_tx_stopped());
		task_stop_done = true;
		ui_dirty();
		return true;
	}
	if (task == TASK_EMERGENCY && ev->action == ACT_SELECT) {
		task_accepted = ui_svc_aprs_emergency();
		ui_frame()->arg = TASK_RESULT;
		task_first = 0;
		ui_dirty();
		return true;
	}
	if (task == TASK_POSITION) {
		if (ev->action == ACT_SELECT) { ui_push(SCR_GROUP, 4); return true; }
		if (ev->action == ACT_CONTEXT) { ui_push(SCR_FIXED_POS, 0); return true; }
	}
	int dir = ev->action == ACT_NAV_DOWN ? 1 : ev->action == ACT_NAV_UP ? -1 : ev->action == ACT_ENC_STEP ? ev->value : 0;
	if (!dir) return false;
	int line = task_first + dir;
	task_first = (uint16_t)(line < 0 ? 0 : line > task_last ? task_last : line);
	ui_dirty();
	return true;
}

const ui_scr_def_t ui_aprs_task_screen = {
	.name = "aprs-task", .flags = SF_FIELD, .draw = task_draw, .key = task_key,
};
#endif

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
	switch (aprs_row(i)) {
#if CONFIG_APRS_HOME
	case AP_MESSAGES:
		ui_push(SCR_MESSAGES, 0);
		return;
	case AP_BEACON:
		ui_push(SCR_APRS_BEACON, 0);
		return;
#endif
	case AP_SETTINGS: ui_push(SCR_APRS_SETTINGS, 0); return;
	case AP_POSITION:
#if CONFIG_APRS_BEACON
		task_open(TASK_POSITION);
#else
		ui_push(SCR_GROUP, 4);
#endif
		return;
#if CONFIG_APRS_BEACON
	case AP_STOP: task_open(TASK_STOP); return;
	case AP_DMR: task_open(TASK_DMR); return;
	case AP_EMERGENCY: task_open(TASK_EMERGENCY); return;
#endif
	case AP_SEND:
#if CONFIG_APRS_BEACON
		task_accepted = v.pos_valid && !ui_svc_aprs_tx_stopped() && ui_svc_aprs_send_now();
		task_open(TASK_RESULT);
#else
		if (ui_svc_aprs_send_now()) {
			ui_toast(TR("Sending position"));
		} else {
			ui_toast(v.block == APRS_NO_CALL ? TR("Set your callsign first") : TR("Can't send now"));
		}
#endif
		return;
	case AP_AUTO:
		c.tx_on = !c.tx_on;
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
		if (aprs_row(i) == AP_INTERVAL) {
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
	case AP_LISTEN_VOICE:
		c.listen_voice = !c.listen_voice;
		ui_svc_aprs_set_cfg(&c);
		ui_dirty();
		return;
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
	ui_aprs_review(&c, NULL);
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
	aprs_view_t v;
	aprs_cfg_t c;
	if (!ui_svc_aprs_view(&v)) return TR("APRS: not in this build");
	ui_svc_aprs_cfg(&c);
	sbuf_t b;
	sb_init(&b, ui_aprs_text, sizeof ui_aprs_text);
	sb_str(&b, v.call[0] ? v.call : TR("No callsign"));
	sb_chr(&b, ' ');
	sb_str(&b, v.pos_valid ? tr(pos_names[v.pos_src < 4 ? v.pos_src : 0]) : TR("No position"));
	sb_chr(&b, '\n');
	#if CONFIG_APRS_BEACON
	sb_str(&b, ui_svc_aprs_tx_stopped() ? TR("TX off") : c.tx_on ? TR("TX on") : TR("TX off"));
#else
	sb_str(&b, c.tx_on ? TR("TX on") : TR("TX off"));
#endif
	sb_chr(&b, ' ');
	sb_str(&b, c.via == APRS_VIA_DMR ? "DMR" : c.via == APRS_VIA_BOTH ? TR("FM + DMR") : "FM");
	if (c.via != APRS_VIA_DMR) {
		sb_chr(&b, '\n');
		sb_str(&b, tr(path_names[c.path < APRS_PATH_COUNT ? c.path : 0]));
	}
	return ui_aprs_text;
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
const ui_scr_def_t ui_aprs_settings_screen = {
	.name = "aprs-settings", .flags = SF_LIST | SF_NUMBERED, .prefix = TRN("APRS settings"),
	.hint = TRN("OK change  Back"), .count = settings_count, .item = aprs_item, .select = aprs_select,
};
const ui_scr_def_t ui_aprs_screen = {
#if CONFIG_APRS_RX && CONFIG_UI_COLOR
	.context = aprs_context,
#endif
	.name = "aprs", .flags = SF_LIST | SF_NUMBERED | SF_FIELD, .prefix = "APRS", .hint = TRN("OK choose  Back"),
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
enum { ST_MESSAGE, ST_TUNE, ST_RANGE, ST_HEARD, ST_PATH, ST_POS_AGE, ST_SPEED, ST_COURSE, ST_SYMBOL,
	ST_TEXT, ST_PACKETS, ST_LAT, ST_LON, ST_SOURCE, ST_POINT_AGE, ST_POINT_RANGE, ST_COUNT };

static uint16_t station_text_row(const char *text, uint16_t row, ui_item_t *it)
{
	char line[sizeof it->label];
	uint16_t count = 0;
	while (*text) {
		text = ui_wrap_line(ui_layout.small, text, gfx_width() - 12, line, sizeof line);
		if (it && count == row) str_copy(it->label, sizeof it->label, line);
		count++;
	}
	return count;
}

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
		    (row >= ST_LAT && !s->has_pos) ||
		    (row == ST_TEXT && !s->text[0])) {
			continue;
		}
		if (row == ST_TEXT) {
			uint16_t lines = station_text_row(s->text, 0, NULL);
			if (i < lines) return row;
			i -= lines;
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
		label = ui_st.now_ms - s.heard_ms > 1800000u ? TR("Old") : TR("Heard");
		ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - s.heard_ms);
		break;
	case ST_PATH: label = s.direct ? TR("Direct") : TR("Via digipeater"); break;
	case ST_POS_AGE:
		label = ui_st.now_ms - s.pos_ms > 1800000u ? TR("Old") : TR(TEXT_ALT_OPEN "Position age" TEXT_ALT_NEXT "Pos age" TEXT_ALT_CLOSE);
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
	case ST_TEXT: {
		uint16_t first = 0;
		while (station_row(first, &s) != ST_TEXT) first++;
		station_text_row(s.text, i - first, it);
		return;
	}
	case ST_POINT_AGE:
		label = ui_st.now_ms - s.pos_ms > 1800000u ? TR("Old") : TR(TEXT_ALT_OPEN "Position age" TEXT_ALT_NEXT "Pos age" TEXT_ALT_CLOSE);
		ui_sb_age(it->value, sizeof it->value, ui_st.now_ms - s.pos_ms);
		break;
	case ST_POINT_RANGE:
		label = TR("Distance");
		if (ui_svc_aprs_range(&s, &m, &deg)) sb_range(&b, m, deg);
		else sb_str(&b, TR("No own pos"));
		break;
	case ST_LAT: case ST_LON:
		label = station_row(i, &s) == ST_LAT ? TR("Latitude") : TR("Longitude");
		sb_degrees(&b, station_row(i, &s) == ST_LAT ? s.lat : s.lon);
		break;
	case ST_SOURCE: {
		aprs_view_t v;
		ui_svc_aprs_view(&v);
		label = TR("Own position");
		sb_str(&b, v.pos_valid ? tr(pos_names[v.pos_src < 4 ? v.pos_src : 0]) : TR("No position"));
		break;
	}
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
			ui_aprs_tune(&s.frequency, s.call, s.frequency_ms);
	}
#endif
}
const ui_scr_def_t ui_aprs_station_screen = {
	.name = "aprs-station", .flags = SF_LIST | SF_FIELD, .prefix = TRN("Heard"), .hint = TRN("OK choose  Back"),
	.count = station_count, .item = station_item, .select = station_select, .title = station_title,
};

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

static const char *packets_reason(void)
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

static const char *packets_body(void)
{
	static char text[128];
	aprs_view_t v;
	ui_svc_aprs_view(&v);
	sbuf_t b;
	sb_init(&b, text, sizeof text);
	sb_u32(&b, ui_svc_aprs_packet_count(), 1);
	sb_chr(&b, '/');
	sb_u32(&b, APRS_RECENT_PACKETS, 1);
	sb_chr(&b, ' ');
	sb_u32(&b, v.packets_dropped, 1);
	sb_str(&b, TR(" dropped"));
	const char *reason = packets_reason();
	if (reason) { sb_chr(&b, '\n'); sb_str(&b, reason); }
	return text;
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
			sb_speed(&b, (uint32_t)p->speed_kn * 1852u);
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
