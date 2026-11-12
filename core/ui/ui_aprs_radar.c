// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Color-only relative positions. Keep the subject by callsign as the heard list moves.
#include "config.h"
#if CONFIG_APRS_RX && CONFIG_UI_COLOR
#include <string.h>
#include "ui_nav.h"
#include "ui_svc_aprs.h"
#include "fmt.h"

static char selected[AX25_CALL_LEN + 4];

static int sine(int deg)
{
	static const uint16_t q[] = { 0, 66, 128, 181, 222, 247, 256 };
	deg %= 360;
	int sign = deg >= 180 ? -1 : 1;
	deg %= 180;
	if (deg > 90)
		deg = 180 - deg;
	int i = deg / 15;
	return sign * (q[i] + (i < 6 ? (q[i + 1] - q[i]) * (deg % 15) / 15 : 0));
}

static void ring(int cx, int cy, int r)
{
	int x = r, y = 0, e = 1 - r;
	while (x >= y) {
		gfx_pixel(cx + x, cy + y, ui_col.line); gfx_pixel(cx - x, cy + y, ui_col.line);
		gfx_pixel(cx + x, cy - y, ui_col.line); gfx_pixel(cx - x, cy - y, ui_col.line);
		gfx_pixel(cx + y, cy + x, ui_col.line); gfx_pixel(cx - y, cy + x, ui_col.line);
		gfx_pixel(cx + y, cy - x, ui_col.line); gfx_pixel(cx - y, cy - x, ui_col.line);
		y++;
		if (e < 0) {
			e += 2 * y + 1;
		} else {
			x--;
			e += 2 * (y - x) + 1;
		}
	}
}

// Reuse theme colors: no extra palette entries on indexed displays.
static color_t symbol_color(const aprs_station_t *s)
{
	if (s->sym_code == '_')
		return ui_col.priv;
	if (s->sym_table == '/') {
		if (strchr(">[bkvj<^s", s->sym_code) && s->sym_code)
			return ui_col.rx;
		if (s->sym_code == '#' || s->sym_code == '&')
			return ui_col.warn;
	}
	return ui_col.fg;
}

static bool plotted(uint8_t i, aprs_station_t *s, uint32_t *m, uint16_t *deg)
{
	return ui_svc_aprs_station(i, s) && ui_svc_aprs_range(s, m, deg);
}

static void radar_enter(void)
{
	aprs_station_t s;
	uint32_t m;
	uint16_t deg;
	selected[0] = 0;
	if (plotted((uint8_t)ui_frame()->arg, &s, &m, &deg)) {
		str_copy(selected, sizeof selected, s.call);
		return;
	}
	for (uint8_t i = 0; i < ui_svc_aprs_station_count(); i++) {
		if (plotted(i, &s, &m, &deg)) {
			str_copy(selected, sizeof selected, s.call);
			return;
		}
	}
}

static bool radar_key(const input_event_t *ev)
{
	int dir = 0;
	if (ev->action == ACT_ENC_STEP)
		dir = ev->value > 0 ? 1 : ev->value < 0 ? -1 : 0;
	else if (ev->gesture == GESTURE_PRESS || ev->gesture == GESTURE_REPEAT) {
		if (ev->action == ACT_NAV_DOWN) dir = 1;
		if (ev->action == ACT_NAV_UP) dir = -1;
		if (ev->action == ACT_SELECT && ev->gesture == GESTURE_PRESS) {
			if (selected[0])
				ui_aprs_open_station(selected);
			return true;
		}
	}
	if (!dir)
		return false;
	int n = ui_svc_aprs_station_count(), current = dir > 0 ? -1 : 0;
	aprs_station_t s;
	uint32_t m;
	uint16_t deg;
	for (int i = 0; i < n; i++) {
		if (ui_svc_aprs_station((uint8_t)i, &s) && !strcmp(s.call, selected))
			current = i;
	}
	for (int k = 1; k <= n; k++) {
		int i = (current + dir * k + n) % n;
		if (plotted((uint8_t)i, &s, &m, &deg)) {
			str_copy(selected, sizeof selected, s.call);
			break;
		}
	}
	ui_dirty();
	return true;
}

static void radar_draw(void)
{
	aprs_view_t v;
	gfx_clear(ui_col.bg);
	int y = ui_draw_top(ui_view(), &ui_st, TR("Radar"), NULL) + 2;
	const font_t *f = ui_layout.tiny;
	int w = gfx_width(), pitch = f->h + 1;
	if (!ui_svc_aprs_view(&v) || !v.pos_valid) {
		// Use the list's word wrapping so translations keep the complete instruction.
		uint16_t top = 0;
		ui_list_t l = { .prefix = TR("Radar"), .top = &top,
			.body = TR("Set your position to see the radar"),
			.empty = TR("Settings > Location"), .hint = TR("Back") };
		ui_draw_list(ui_view(), &ui_st, &l);
		ui_draw_banner(&ui_st);
		return;
	}
	aprs_station_t s, focus;
	uint32_t m, far = 0, fm = 0;
	uint16_t deg, fd = 0;
	bool found = selected[0] && ui_svc_aprs_station_find(selected, &focus) &&
		ui_svc_aprs_range(&focus, &fm, &fd);
	for (uint8_t i = 0; i < ui_svc_aprs_station_count(); i++) {
		if (plotted(i, &s, &m, &deg) && m > far)
			far = m;
	}
	// Three equal rings, with a 1/2/5 x power-of-ten kilometer interval.
	uint32_t step = 1000;
	while ((uint64_t)step * 3 < far) {
		uint32_t base = 1000;
		while (base <= step / 10) base *= 10;
		step = step / base == 1 ? base * 2 : step / base == 2 ? base * 5 : base * 10;
	}
	int bottom = ui_layout.hint_y - 2 * pitch - 2;
	int r = (bottom - y - pitch - 6) / 2;
	if (r > (w - 8) / 2) r = (w - 8) / 2;
	if (r < 3) r = 3;
	int cx = w / 2, cy = y + pitch + r + 2;
	static const char *const sources[] = { TRN("Auto"), "GPS", TRN("Computer"), TRN("Fixed") };
	gfx_text(f, 0, y, tr(sources[v.pos_src < 4 ? v.pos_src : 0]), ui_col.dim);
	gfx_text(f, cx - f->w / 2, y, "N", ui_col.fg);
	for (int i = 1; i <= 3; i++) {
		int rr = r * i / 3;
		ring(cx, cy, rr);
		char label[20];
		sbuf_t b;
		sb_init(&b, label, sizeof label);
		sb_u32(&b, step / 1000 * (uint32_t)i, 1);
		sb_str(&b, " km");
		gfx_text_bg(f, cx + 2, cy + rr - f->h, label, ui_col.dim, ui_col.bg);
	}
	for (uint8_t i = 0; i < ui_svc_aprs_station_count(); i++) {
		if (!plotted(i, &s, &m, &deg)) continue;
		int radius = (int)((uint64_t)m * (uint32_t)r / ((uint64_t)step * 3));
		if (radius > r) radius = r;
		int x = cx + radius * sine(deg) / 256, sy = cy - radius * sine((deg + 90) % 360) / 256;
		bool stale = ui_st.now_ms - s.pos_ms > 1800000u;
		color_t c = stale ? ui_col.dim : symbol_color(&s);
		if (stale) {
			gfx_fill(x - 2, sy - 2, 5, 5, ui_col.bg);
			gfx_rect(x - 2, sy - 2, 5, 5, c);
		} else {
			gfx_fill(x - 1, sy - 1, 3, 3, c);
		}
		if (!strcmp(s.call, selected)) gfx_rect(x - 4, sy - 4, 9, 9, ui_col.focus);
	}
	gfx_hline(cx - 3, cy, 7, ui_col.fg);
	gfx_vline(cx, cy - 3, 7, ui_col.fg);
	if (found) {
		char text[80];
		sbuf_t b;
		sb_init(&b, text, sizeof text);
		sb_str(&b, focus.call);
		sb_str(&b, "  ");
		sb_age(&b, ui_st.now_ms - focus.pos_ms);
		gfx_text(f, 0, bottom, text, ui_col.fg);
		sb_init(&b, text, sizeof text);
		sb_range(&b, fm, fd);
		sb_str(&b, "  ");
		sb_u32(&b, fd, 1);
		sb_str(&b, GLYPH_DEGREE);
		gfx_text(f, 0, bottom + pitch, text, ui_col.fg);
	} else {
		gfx_text(f, 0, bottom, selected[0] ? TR("No longer in list") : TR("No positions yet"), ui_col.dim);
	}
	ui_draw_bottom(&ui_st, TR("OK open  Back"));
	ui_draw_banner(&ui_st);
}

const ui_scr_def_t ui_aprs_radar_screen = {
	.name = "aprs-radar", .flags = SF_FIELD, .enter = radar_enter, .key = radar_key, .draw = radar_draw,
};
#endif
