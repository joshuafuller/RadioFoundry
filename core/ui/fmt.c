// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Small text builders for the UI
#include "fmt.h"
#include "font.h"
#include "i18n.h"
#include "config.h"
#if CONFIG_UNITS
#include "ui_svc.h"
#endif

void sb_init(sbuf_t *b, char *buf, size_t len)
{
	b->p = buf;
	b->end = buf + (len ? len - 1 : 0);
	if (len)
		*buf = 0;
}

void str_copy(char *dst, size_t cap, const char *src)
{
	sbuf_t b;
	sb_init(&b, dst, cap);
	sb_str(&b, src);
}

void sb_chr(sbuf_t *b, char c)
{
	if (b->p < b->end) {
		*b->p++ = c;
		*b->p = 0;
	}
}

void sb_str(sbuf_t *b, const char *s)
{
	while (*s)
		sb_chr(b, *s++);
}

void sb_u32(sbuf_t *b, uint32_t v, int min_digits)
{
	char tmp[10];
	int n = 0;
	do {
		tmp[n++] = (char)('0' + v % 10u);
		v /= 10u;
	} while (v && n < 10);
	while (n < min_digits && n < 10)
		tmp[n++] = '0';
	while (n)
		sb_chr(b, tmp[--n]);
}

void sb_i32(sbuf_t *b, int32_t v)
{
	if (v < 0) {
		sb_chr(b, '-');
		sb_u32(b, (uint32_t)(-(int64_t)v), 1);
	} else {
		sb_u32(b, (uint32_t)v, 1);
	}
}

void sb_freq(sbuf_t *b, uint32_t hz)
{
	sb_u32(b, hz / 1000000u, 1);
	sb_chr(b, '.');
	uint32_t frac = hz % 1000000u;	// Hz
	sb_u32(b, frac / 1000u, 3);
	uint32_t rest = (frac % 1000u) / 10u;	// 10 Hz units, two more decimals at most
	if (rest) {
		sb_chr(b, (char)('0' + rest / 10u));
		if (rest % 10u)
			sb_chr(b, (char)('0' + rest % 10u));
	}
}

void sb_tone(sbuf_t *b, const subtone_t *t)
{
	switch (t->type) {
	case TONE_CTCSS:
		sb_u32(b, t->value / 10u, 1);
		sb_chr(b, '.');
		sb_u32(b, t->value % 10u, 1);
		break;
	case TONE_DCS:
	case TONE_DCS_INV:
		sb_chr(b, 'D');
		sb_chr(b, (char)('0' + ((t->value >> 6) & 7u)));
		sb_chr(b, (char)('0' + ((t->value >> 3) & 7u)));
		sb_chr(b, (char)('0' + (t->value & 7u)));
		sb_chr(b, t->type == TONE_DCS_INV ? 'I' : 'N');
		break;
	default:
		break;
	}
}

void sb_split(sbuf_t *b, uint32_t rx_hz, uint32_t tx_hz)
{
	if (tx_hz == rx_hz) {
		sb_str(b, TR("Off"));
		return;
	}
	sb_chr(b, tx_hz > rx_hz ? '+' : '-');
	uint32_t d = tx_hz > rx_hz ? tx_hz - rx_hz : rx_hz - tx_hz;
	// Below 1 MHz in kHz, else in MHz; three decimals at most, trailing zeros dropped: "+600k", "+12.5k", "-7.6M"
	bool mhz = d >= 1000000u;
	uint32_t unit = mhz ? 1000000u : 1000u;
	sb_u32(b, d / unit, 1);
	uint32_t frac = d % unit / (unit / 1000u);
	if (frac != 0) {
		sb_chr(b, '.');
		for (uint32_t div = 100u; frac != 0; div /= 10u) {
			sb_chr(b, (char)('0' + frac / div));
			frac %= div;
		}
	}
	sb_chr(b, mhz ? 'M' : 'k');
}

void sb_mmss(sbuf_t *b, uint32_t seconds)
{
	sb_u32(b, seconds / 60u, 1);
	sb_chr(b, ':');
	sb_u32(b, seconds % 60u, 2);
}

// The age's number and unit letter: 45 's', 14 'm', 3 'h', 2 'd'
static uint32_t age_unit(uint32_t ms, char *unit)
{
	uint32_t s = ms / 1000u;
	if (s < 60u) {
		*unit = 's';
		return s;
	}
	if (s < 3600u) {
		*unit = 'm';
		return s / 60u;
	}
	if (s < 86400u) {
		*unit = 'h';
		return s / 3600u;
	}
	*unit = 'd';
	return s / 86400u;
}

void sb_age_long(sbuf_t *b, uint32_t ms)
{
	char unit;
	sb_u32(b, age_unit(ms, &unit), 1);
	sb_chr(b, unit);
	sb_str(b, TR(" ago"));
}

// "14m ago", "14m" as text alternates: the drawing keeps the longest that fits (font.h). One unit style everywhere,
// so two ages on one screen always read alike
void sb_age(sbuf_t *b, uint32_t ms)
{
	char unit;
	uint32_t v = age_unit(ms, &unit);
	sb_str(b, TEXT_ALT_OPEN);
	sb_u32(b, v, 1);
	sb_chr(b, unit);
	sb_str(b, TR(" ago"));
	sb_str(b, TEXT_ALT_NEXT);
	sb_u32(b, v, 1);
	sb_chr(b, unit);
	sb_str(b, TEXT_ALT_CLOSE);
}

void sb_degrees(sbuf_t *b, int32_t e7)
{
	uint32_t v = (uint32_t)(e7 < 0 ? -(int64_t)e7 : e7);
	v = (v + 500u) / 1000u;	// 1e-4 degree
	if (e7 < 0)
		sb_chr(b, '-');
	sb_u32(b, v / 10000u, 1);
	sb_chr(b, '.');
	sb_u32(b, v % 10000u, 4);
}

void sb_coord(sbuf_t *b, int32_t e7, char pos, char neg)
{
	uint32_t v = (uint32_t)(e7 < 0 ? -(int64_t)e7 : e7);
	v = (v + 5u) / 10u;	// 1e-6 degree
	sb_u32(b, v / 1000000u, 1);
	sb_chr(b, '.');
	sb_u32(b, v % 1000000u, 6);
	sb_chr(b, ' ');
	sb_chr(b, e7 < 0 ? neg : pos);
}

void sb_compass(sbuf_t *b, uint16_t bearing)
{
	static const char *const points[] = { "N", "NE", "E", "SE", "S", "SW", "W", "NW" };
	sb_str(b, points[((bearing % 360u) * 2u + 45u) / 90u % 8u]);
}

static bool metric(void)
{
#if CONFIG_UNITS
	return ui_svc_setting_get(UI_SET_UNITS) == 1;
#else
	return true;
#endif
}

static void decimal(sbuf_t *b, uint32_t v, uint32_t scale, int digits)
{
	sb_u32(b, v / scale, 1);
	sb_chr(b, '.');
	sb_u32(b, v % scale, digits);
}

void sb_distance(sbuf_t *b, uint32_t meters)
{
	bool si = metric();
	// Compare rounded tenths so a boundary never reads 100.0 km/mi.
	uint32_t tenths = si ? ((uint64_t)meters + 50u) / 100u :
		((uint64_t)meters * 10000u + 804672u) / 1609344u;
	if (si ? meters < 1000u : meters < 1610u) {
		sb_u32(b, si ? meters : ((uint64_t)meters * 1250u + 190u) / 381u, 1);
		sb_str(b, si ? " m" : " ft");
		return;
	}
	if (tenths < 1000u)
		decimal(b, tenths, 10u, 1);
	else
		sb_u32(b, si ? ((uint64_t)meters + 500u) / 1000u :
			((uint64_t)meters * 1000u + 804672u) / 1609344u, 1);
	sb_str(b, si ? " km" : " mi");
}

void sb_range(sbuf_t *b, uint32_t meters, uint16_t bearing)
{
	sb_distance(b, meters);
	sb_chr(b, ' ');
	sb_compass(b, bearing);
}

void sb_speed(sbuf_t *b, uint32_t meters_per_hour)
{
	bool si = metric();
	sb_u32(b, si ? ((uint64_t)meters_per_hour + 500u) / 1000u :
		((uint64_t)meters_per_hour * 1000u + 804672u) / 1609344u, 1);
	sb_str(b, si ? " km/h" : " mph");
}

static void signed_round(sbuf_t *b, int64_t numerator, uint32_t denominator)
{
	uint32_t v = ((uint64_t)(numerator < 0 ? -numerator : numerator) + denominator / 2u) / denominator;
	if (numerator < 0 && v)
		sb_chr(b, '-');
	sb_u32(b, v, 1);
}

void sb_altitude(sbuf_t *b, int32_t meters)
{
	bool si = metric();
	signed_round(b, si ? meters : (int64_t)meters * 1250, si ? 1u : 381u);
	sb_str(b, si ? " m" : " ft");
}

void sb_temperature(sbuf_t *b, int16_t celsius10)
{
	bool si = metric();
	signed_round(b, si ? celsius10 : (int32_t)celsius10 * 9 + 1600, si ? 10u : 50u);
	sb_str(b, si ? GLYPH_DEGREE "C" : GLYPH_DEGREE "F");
}

void sb_rain(sbuf_t *b, uint32_t hundredths_inch)
{
	bool si = metric();
	decimal(b, si ? ((uint64_t)hundredths_inch * 254u + 50u) / 100u : hundredths_inch,
		si ? 10u : 100u, si ? 1 : 2);
	sb_str(b, si ? " mm" : " in");
}

void sb_pressure(sbuf_t *b, uint16_t hpa10)
{
	bool si = metric();
	if (si)
		sb_u32(b, (hpa10 + 5u) / 10u, 1);
	else
		decimal(b, ((uint32_t)hpa10 * 10000u + 16932u) / 33864u, 100u, 2);
	sb_str(b, si ? " hPa" : " inHg");
}
