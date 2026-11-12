// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Color-only relative positions. Keep the subject by callsign as the heard list moves.
// Drawing runs on the UI task's deepest chains (docs/dev/stack-check.md): one station at a time, and the engine's
// view, the list and the text buffers stay in small functions of their own.
#include "config.h"
#if CONFIG_APRS_RX && CONFIG_UI_COLOR
#include <string.h>
#include "ui_nav.h"
#include "ui_svc_aprs.h"
#include "fmt.h"

#define STALE_MS	1800000u

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

static void ring(int cx, int cy, int r, color_t c)
{
	int x = r, y = 0, e = 1 - r;
	while (x >= y) {
		gfx_pixel(cx + x, cy + y, c); gfx_pixel(cx - x, cy + y, c);
		gfx_pixel(cx + x, cy - y, c); gfx_pixel(cx - x, cy - y, c);
		gfx_pixel(cx + y, cy + x, c); gfx_pixel(cx - y, cy + x, c);
		gfx_pixel(cx + y, cy - x, c); gfx_pixel(cx - y, cy - x, c);
		y++;
		if (e < 0) {
			e += 2 * y + 1;
		} else {
			x--;
			e += 2 * (y - x) + 1;
		}
	}
}

// A shape per symbol class, so the color is never the only cue: moving stations a dot, digipeaters and gateways a
// diamond, weather a triangle, the rest a square
enum { MARK_DOT, MARK_DIAMOND, MARK_TRIANGLE, MARK_SQUARE };

static int symbol_mark(const aprs_station_t *s)
{
	if (s->sym_code == '_')
		return MARK_TRIANGLE;
	if (s->sym_table == '/') {
		if (strchr(">[bkvj<^s", s->sym_code) && s->sym_code)
			return MARK_DOT;
		if (s->sym_code == '#' || s->sym_code == '&')
			return MARK_DIAMOND;
	}
	return MARK_SQUARE;
}

// Reuse theme colors: no extra palette entries on indexed displays.
static color_t mark_color(int mark)
{
	return mark == MARK_DOT ? ui_col.rx : mark == MARK_DIAMOND ? ui_col.warn :
		mark == MARK_TRIANGLE ? ui_col.priv : ui_col.fg;
}

static bool in_mark(int mark, int dx, int dy, int k)
{
	int ax = dx < 0 ? -dx : dx, ay = dy < 0 ? -dy : dy;
	if (mark == MARK_DOT)
		return dx * dx + dy * dy <= k * k + k / 2;
	if (mark == MARK_DIAMOND)
		return ax + ay <= k;
	if (mark == MARK_TRIANGLE)
		return ay <= k && ax <= (dy + k + 1) / 2;
	return ax < k && ay < k;
}

// A marker of half size k on a rim of background, so a ring, a label or a neighbor never runs into it. Hollow: its
// outline only (an old position)
static void marker(int x, int y, int mark, int k, color_t c, bool hollow)
{
	for (int dy = -k - 1; dy <= k + 1; dy++) {
		for (int dx = -k - 1; dx <= k + 1; dx++) {
			bool in = in_mark(mark, dx, dy, k);
			bool ink = in && !(hollow && in_mark(mark, dx - 1, dy, k) && in_mark(mark, dx + 1, dy, k) &&
				in_mark(mark, dx, dy - 1, k) && in_mark(mark, dx, dy + 1, k));
			if (in || in_mark(mark, dx, dy, k + 1))
				gfx_pixel(x + dx, y + dy, ink ? c : ui_col.bg);
		}
	}
}

// Our own position: a cross of arm a, cut out of whatever is under it (a station where we are)
static void cross(int x, int y, int a, color_t c)
{
	gfx_fill(x - a - 1, y - 1, 2 * a + 3, 3, ui_col.bg);
	gfx_fill(x - 1, y - a - 1, 3, 2 * a + 3, ui_col.bg);
	gfx_hline(x - a, y, 2 * a + 1, c);
	gfx_vline(x, y - a, 2 * a + 1, c);
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
				ui_aprs_station_open(selected);
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

// Where our position comes from, or false without one. noinline, as the rest below: the engine's view, the list and
// the text buffers each stay out of the frames above and beside them
static bool __attribute__((noinline)) own_position(uint8_t *src)
{
	aprs_view_t v;
	if (!ui_svc_aprs_view(&v) || !v.pos_valid)
		return false;
	*src = v.pos_src;
	return true;
}

static void __attribute__((noinline)) no_position(void)
{
	// Use the list's word wrapping so translations keep the complete instruction.
	uint16_t top = 0;
	ui_list_t l = { .prefix = TR("Radar"), .top = &top,
		.body = TR("Set your position to see the radar"),
		.empty = TR("Settings > Location"), .hint = TR("Back") };
	ui_draw_list(ui_view(), &ui_st, &l);
}

// Text in a box w wide at x, y: word-wrapped over at most rows rows, a row that is still too long shortened; against
// the right edge when asked. Returns the y under it
static int __attribute__((noinline)) put(const font_t *f, int x, int y, int w, int rows, bool right, const char *s,
	color_t c)
{
	char words[24], row[24];
	while (*s && rows-- > 0) {
		size_t fit = 0, n = 0;
		do {	// whole words while they fit; the last row takes what is left
			n++;
			while (s[n] && s[n] != ' ')
				n++;
			if (rows > 0 && fit && (int)n * f->w > w)
				break;
			fit = n;
		} while (s[n]);
		str_copy(words, fit < sizeof words ? fit + 1 : sizeof words, s);
		gfx_text_fit(f, words, w, row, sizeof row);
		gfx_text(f, right ? x + w - gfx_text_width(f, row) : x, y, row, c);
		s += fit;
		while (*s == ' ')
			s++;
		y += f->h + 1;
	}
	return y;
}

// What the selected station's position says, one piece a row (the info area arranges them)
enum { INFO_DISTANCE, INFO_BEARING, INFO_RANGE, INFO_AGE, INFO_MOVING };

typedef struct {
	uint32_t m, age_ms;
	uint16_t deg, course, speed_kn;
} radar_fix_t;

static int __attribute__((noinline)) info(const font_t *f, int x, int y, int w, int rows, bool right, int what,
	const radar_fix_t *p, color_t c)
{
	char text[24];
	sbuf_t b;
	sb_init(&b, text, sizeof text);
	switch (what) {
	case INFO_DISTANCE:
		sb_distance(&b, p->m);
		break;
	case INFO_RANGE:
		sb_distance(&b, p->m);
		sb_chr(&b, ' ');
		// fall through
	case INFO_BEARING:
		sb_compass(&b, p->deg);
		sb_chr(&b, ' ');
		sb_u32(&b, p->deg, 1);
		sb_str(&b, GLYPH_DEGREE);
		break;
	case INFO_AGE:
		// An old position says so in words: its hollow marker is not the only cue
		if (p->age_ms > STALE_MS) {
			char age[24];
			bool more;
			sb_init(&b, age, sizeof age);
			sb_age(&b, p->age_ms);
			sb_init(&b, text, sizeof text);
			sb_str(&b, TR("Old"));
			sb_chr(&b, ' ');
			gfx_text_alt(age, 1, b.p, (size_t)(b.end - b.p) + 1, &more);	// the short age: "Old 45m"
		} else {
			sb_age_long(&b, p->age_ms);
		}
		break;
	default:
		if (p->speed_kn == 0xFFFFu)
			return y;
		sb_speed(&b, (uint32_t)p->speed_kn * 1852u);
		if (p->speed_kn && p->course != 0xFFFFu) {
			size_t n = strlen(text);
			sb_str(&b, " " GLYPH_ARROW " ");
			sb_compass(&b, p->course);
			if (rows < 2 && gfx_text_width(f, text) > w)
				text[n] = 0;	// no room for where it is heading: the speed alone
		}
		break;
	}
	return put(f, x, y, w, rows, right, text, c);
}

// A ring's label goes inside it beside the north-south axis: under the center to the right, to the left, then above
// it to the right, to the left. Place p of ring i (1..3) is bit 4 * (i - 1) + p of the places a marker is in
#define LABEL_CHARS	6
static void label_at(int cx, int cy, int rr, int p, const font_t *f, int *x, int *y)
{
	*x = p & 1 ? cx - 2 - LABEL_CHARS * f->w : cx + 3;
	*y = p < 2 ? cy + rr - f->h : cy - rr + 1;
}

// A ring's range, n of the display unit: "15", "15 mi"
static void range_text(char *out, size_t len, uint32_t n, bool unit)
{
	sbuf_t b;
	sb_init(&b, out, len);
	sb_u32(&b, n, 1);
	if (unit)
		sb_str(&b, fmt_metric() ? " km" : " mi");
}

// The rings' ranges (n, 2n, 3n; the unit on the outermost one shown), each in its first place without a marker, or
// left out
static void __attribute__((noinline)) ring_labels(int cx, int cy, int r, uint32_t n, uint16_t hit)
{
	const font_t *f = ui_layout.small;
	bool unit = true;
	for (int i = 3; i >= 1; i--) {
		int p = 0, x, y;
		while (p < 4 && (hit >> (4 * (i - 1) + p) & 1u))
			p++;
		if (p == 4)
			continue;
		char label[12];
		range_text(label, sizeof label, n * (uint32_t)i, unit);
		unit = false;
		label_at(cx, cy, r * i / 3, p, f, &x, &y);
		gfx_text_bg(f, p & 1 ? x + LABEL_CHARS * f->w - gfx_text_width(f, label) : x, y, label, ui_col.dim, ui_col.bg);
	}
}

// The outer ring's range as a row of the info area, where the rings are too close for text between them
static void __attribute__((noinline)) range_row(int x, int y, int w, int k, uint32_t n)
{
	const font_t *f = ui_layout.small;
	char label[12];
	range_text(label, sizeof label, n, true);
	ring(x + k, y + f->h / 2, k, ui_col.dim);
	put(f, x + 2 * k + 5, y, w - 2 * k - 5, 1, false, label, ui_col.dim);
}

// noinline: its frame stays off the list's chain (no_position), the UI task's deepest
static void __attribute__((noinline)) radar(uint8_t src)
{
	int top = ui_draw_top(ui_view(), &ui_st, TR("Radar"), NULL) + 2, bottom = ui_layout.hint_y - 1;
	const font_t *f = ui_layout.small, *fc = f;
	int w = gfx_width(), pitch = f->h + 1;
	// The 240 px class: a wider gutter and larger markers. A tall body puts the info area under the radar, a wide
	// one beside it
	int gutter = w >= 220 ? 6 : 4, k = w >= 220 ? 4 : 3, rim = k + 4;
	bool below = bottom - top >= w;
	int ix = gutter, iy, iw = w - 2 * gutter, aw = iw, ah;
	if (below) {
		fc = ui_layout.medium;
		iy = bottom - (fc->h + 1) - 2 * pitch;
		ah = iy - 4 - top;
	} else {
		iw = 10 * f->w;
		ix = w - gutter - iw;
		iy = top;
		aw = ix - 2 * gutter;
		ah = bottom - top;
	}
	// North's letter stands clear of a marker on the outer ring, and so does every edge of the radar's area
	int r = (aw - 2 * rim) / 2, rv = (ah - f->h - 2 * rim) / 2;
	if (rv < r) r = rv;
	if (r < 9) r = 9;
	int cx = gutter + aw / 2, cy = top + (ah - f->h - 2 * rim - 2 * r) / 2 + f->h + rim + r;

	aprs_station_t s;
	radar_fix_t sel = { 0 };
	uint32_t m, far = 0;
	uint16_t deg;
	bool found = false;
	for (uint8_t i = 0; i < ui_svc_aprs_station_count(); i++) {
		if (!plotted(i, &s, &m, &deg)) continue;
		if (m > far) far = m;
		if (strcmp(s.call, selected)) continue;
		found = true;
		sel = (radar_fix_t){ .m = m, .age_ms = ui_st.now_ms - s.pos_ms, .deg = deg, .course = s.course,
			.speed_kn = s.speed_kn };
	}
	// Three equal rings, with a 1/2/5 x power-of-ten interval in the display unit (Settings > Display > Units)
	const bool si = fmt_metric();
	const uint32_t unit = si ? 1000u : 1609u;
	uint32_t step = unit;
	while ((uint64_t)step * 3 < far) {
		uint32_t base = unit;
		while (base <= step / 10) base *= 10;
		step = step / base == 1 ? base * 2 : step / base == 2 ? base * 5 : base * 10;
	}
	gfx_hline(cx - r - 2, cy, 2 * r + 5, ui_col.line);
	gfx_vline(cx, cy - r - rim + 2, 2 * r + rim + 1, ui_col.line);
	gfx_text(f, cx - f->w / 2, cy - r - rim - f->h + 1, "N", ui_col.fg);
	for (int i = 1; i <= 3; i++)
		ring(cx, cy, r * i / 3, ui_col.line);
	// hit: the label places a marker is in (label_at), so a ring's label never covers a station
	int sx = 0, sy = 0, smark = 0;
	uint16_t hit = 0;
	for (uint8_t i = 0; i < ui_svc_aprs_station_count(); i++) {
		if (!plotted(i, &s, &m, &deg)) continue;
		int radius = (int)((uint64_t)m * (uint32_t)r / ((uint64_t)step * 3));
		if (radius > r) radius = r;
		int x = cx + radius * sine(deg) / 256, y = cy - radius * sine((deg + 90) % 360) / 256;
		int mark = symbol_mark(&s);
		for (int j = 0; j < 12; j++) {
			int lx, ly;
			label_at(cx, cy, r * (j / 4 + 1) / 3, j % 4, f, &lx, &ly);
			if (x + k + 2 > lx && x - k - 2 < lx + LABEL_CHARS * f->w && y + k + 2 > ly && y - k - 2 < ly + f->h)
				hit |= (uint16_t)(1u << j);
		}
		if (!strcmp(s.call, selected)) {	// drawn last, over its neighbors
			sx = x; sy = y; smark = mark;
			continue;
		}
		bool stale = ui_st.now_ms - s.pos_ms > STALE_MS;
		marker(x, y, mark, k, stale ? ui_col.dim : mark_color(mark), stale);
	}
	if (found) {
		bool stale = sel.age_ms > STALE_MS;
		ring(sx, sy, k + 2, ui_col.bg);
		ring(sx, sy, k + 4, ui_col.bg);
		ring(sx, sy, k + 3, ui_col.fg);
		marker(sx, sy, smark, k, stale ? ui_col.dim : mark_color(smark), stale);
	}
	bool on_rings = r >= 3 * (f->h + 4);
	if (on_rings)
		ring_labels(cx, cy, r, step / unit, hit);
	cross(cx, cy, k + 1, ui_col.fg);

	// Where our own position (the cross) comes from: the info area's last row, on the right when the area is wide
	static const char *const sources[] = { TRN("Auto"), "GPS", TRN("Computer"), TRN("Fixed") };
	const char *text = tr(sources[src < 4 ? src : 0]);
	int y = below ? iy + fc->h + 1 + pitch : bottom - f->h, tw = gfx_text_width(f, text) + 2 * k + 5;
	if (tw > iw / 2 && below) tw = iw / 2;
	int x = below ? ix + iw - tw : ix;
	cross(x + k, y + f->h / 2, k, ui_col.dim);
	put(f, x + 2 * k + 5, y, (below ? tw : iw) - 2 * k - 5, 1, false, text, ui_col.dim);
	if (!below && !on_rings)
		range_row(ix, y - pitch, iw, k, step / unit * 3);

	// The selected station: who, how far and which way, how old, and how it moves when it says so
	text = selected[0] ? TR("No longer in list") : TR("No positions yet");
	y = iy;
	if (below) {
		gfx_hline(0, iy - 3, w, ui_col.line);
		if (selected[0])
			put(fc, ix, y, found ? iw - 8 * f->w : iw, 1, false, selected, ui_col.fg);
		if (found)
			info(f, ix, y + fc->ascent - f->ascent, iw, 1, true, INFO_AGE, &sel, ui_col.dim);
		y += selected[0] ? fc->h + 1 : 0;
		if (found) {
			info(f, ix, y, iw, 1, false, INFO_RANGE, &sel, ui_col.fg);
			info(f, ix, y + pitch, iw - tw - f->w, 1, false, INFO_MOVING, &sel, ui_col.dim);
		} else {
			put(f, ix, y, iw, 1, false, text, ui_col.dim);
		}
	} else {
		if (selected[0])
			y = put(f, ix, y, iw, 1, false, selected, ui_col.fg);
		if (found) {
			y = info(f, ix, y, iw, 1, false, INFO_DISTANCE, &sel, ui_col.fg);
			y = info(f, ix, y, iw, 1, false, INFO_BEARING, &sel, ui_col.fg);
			y = info(f, ix, y, iw, 1, false, INFO_AGE, &sel, ui_col.dim);
			info(f, ix, y, iw, (bottom - 2 * pitch - y) / pitch, false, INFO_MOVING, &sel, ui_col.dim);
		} else {
			put(f, ix, y, iw, 3, false, text, ui_col.dim);
		}
	}
	ui_draw_bottom(&ui_st, selected[0] ? TR("OK open  Back") : TR("Back"));
}

static void radar_draw(void)
{
	uint8_t src;
	gfx_clear(ui_col.bg);
	if (own_position(&src))
		radar(src);
	else
		no_position();
	ui_draw_banner(&ui_st);
}

const ui_scr_def_t ui_aprs_radar_screen = {
	.name = "aprs-radar", .flags = SF_FIELD, .enter = radar_enter, .key = radar_key, .draw = radar_draw,
};
#endif
