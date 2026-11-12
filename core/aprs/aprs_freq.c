// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Wire grammar: https://www.aprs.org/info/freqspec.txt (2013-03-26).
#include "aprs_freq.h"
#if CONFIG_APRS_TUNE
#include <string.h>
#include "radio/tones.h"
#include "debuglink/dl_fmt.h"

static bool digits(const char *s, size_t n, unsigned base, uint16_t *v)
{
	*v = 0;
	for (size_t i = 0; i < n; i++) {
		if (s[i] < '0' || (unsigned)(s[i] - '0') >= base)
			return false;
		*v = (uint16_t)(*v * base + s[i] - '0');
	}
	return true;
}

static size_t frequency(const char *s, size_t n, uint32_t *hz)
{
	uint16_t mhz, frac;
	if (n < 6 || s[3] != '.' || !digits(s, 3, 10, &mhz) || !digits(s + 4, 2, 10, &frac))
		return 0;
	size_t k = 6;
	frac *= 10;
	if (n > 6 && s[6] >= '0' && s[6] <= '9') {
		frac += s[6] - '0';
		k++;
	}
	*hz = (uint32_t)mhz * 1000000u + (uint32_t)frac * 1000u;
	return *hz ? k : 0;
}

bool aprs_freq_parse(const char *text, size_t len, const char *object, aprs_freq_t *out)
{
	aprs_freq_t f = {0};
	memset(out, 0, sizeof(*out));
	if (text == NULL)
		len = 0;
	bool named = object && frequency(object, strlen(object), &f.hz);
	uint32_t hz;
	size_t k = len ? frequency(text, len, &hz) : 0;
	if (k && len >= 10 && (k == 7 || (k == 6 && text[6] == ' ')) &&
	    (text[7] == 'M' || text[7] == 'm') && (text[8] == 'H' || text[8] == 'h') &&
	    (text[9] == 'Z' || text[9] == 'z') && (len == 10 || text[10] == ' ')) {
		// A second frequency on a frequency object is a split, not an offset we can guess.
		if (named && hz != f.hz)
			return false;
		f.hz = hz;
		text += 10;
		len -= 10;
	} else if (!named) {
		return false;
	}
	while (len) {
		if (*text == ' ') { text++; len--; continue; }
		size_t n = 0;
		while (n < len && text[n] != ' ') n++;
		uint16_t v;
		char c = text[0];
		if (c == 'T' || c == 'C' || c == 'D' || c == 't' || c == 'c' || c == 'd') {
			if (n == 4 && memcmp(text + 1, "off", 3) == 0) {
				f.tone_kind = 0;
				f.tone = 0;
			} else if (n == 4 && digits(text + 1, 3, c == 'D' || c == 'd' ? 8 : 10, &v)) {
				f.tone_kind = c == 'D' || c == 'd' ? 'D' : 'T';
				f.tone = v;
			} else {
				// Ordinary prose ends the extension; malformed four-byte fields do not tune.
				if (n == 4 || (n > 1 && text[1] >= '0' && text[1] <= '9')) return false;
				break;
			}
			f.narrow = c >= 'a';
		} else if (c == '+' || c == '-') {
			if (n != 4 || !digits(text + 1, 3, 10, &v)) return false;
			f.offset = c == '-' ? -(int16_t)v : (int16_t)v;
		} else if (c == 'R' && n == 4) {
			if (!digits(text + 1, 2, 10, &v) || (text[3] != 'm' && text[3] != 'k')) return false;
			f.range = (uint8_t)v;
			f.range_unit = text[3];
		} else {
			break;
		}
		text += n;
		len -= n;
	}
	*out = f;
	return true;
}

bool aprs_freq_tone(const aprs_freq_t *f, subtone_t *out)
{
	*out = (subtone_t){TONE_NONE, 0};
	if (!f->tone_kind) return true;
#if CONFIG_SUBTONE
	if (f->tone_kind == 'D') {
		*out = (subtone_t){TONE_DCS, f->tone};
		return tones_valid(out);
	}
	for (unsigned i = 0; i < TONES_CTCSS_COUNT; i++) {
		if (tones_ctcss[i] / 10u == f->tone) {
			*out = (subtone_t){TONE_CTCSS, tones_ctcss[i]};
			return true;
		}
	}
#endif
	return false;
}

static char voice[16]; // MAIN RAM, not the APRS engine's CCM block
void aprs_freq_channel(uint32_t hz, subtone_t tone, bool fm)
{
	voice[0] = '\0';
	if (!fm || hz < 1000000u || hz >= 1000000000u || hz % 1000u || tone.type == TONE_DCS_INV)
		return;
	unsigned v = tone.type == TONE_CTCSS ? tone.value / 10u :
		(tone.value >> 6) * 100u + ((tone.value >> 3) & 7u) * 10u + (tone.value & 7u);
	if (tone.type == TONE_NONE)
		dl_fmt(voice, sizeof voice, "%03u.%03uMHz", (unsigned)(hz / 1000000u), (unsigned)(hz / 1000u % 1000u));
	else
		dl_fmt(voice, sizeof voice, "%03u.%03uMHz %c%03u", (unsigned)(hz / 1000000u),
			 (unsigned)(hz / 1000u % 1000u), tone.type == TONE_CTCSS ? 'T' : 'D', v % 1000u);
}

void aprs_freq_comment(const char *comment, char *out, size_t max)
{
	dl_fmt(out, max, "%s%s%s", voice, voice[0] && comment[0] ? " " : "", comment);
}
#endif
