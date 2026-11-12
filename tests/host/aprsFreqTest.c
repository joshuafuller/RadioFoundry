// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include <assert.h>
#include <stdio.h>
#include "aprs/aprs_freq.h"
#include "fuzz.h"

int main(void)
{
	static const struct { const char *text, *name; uint32_t hz; int offset; unsigned tone; char kind; } cases[] = {
		{"146.520MHz T100", NULL, 146520000, 0, 100, 'T'},
		{"146.52 MHz C107 -060 R25m", NULL, 146520000, -60, 107, 'T'},
		{"442.440mHz D256 +500 R25k", NULL, 442440000, 500, 0256, 'D'},
		{"T079 -060 R25m", "147.105md", 147105000, -60, 79, 'T'},
		{"Toff -000", "146.52-AB", 146520000, 0, 0, 0},
		{"145.50 MHz t077 Simplex", NULL, 145500000, 0, 77, 'T'},
		{"146.520MHz D999", NULL, 0, 0, 0, 0},
		{"146.520MHz +xyz", NULL, 0, 0, 0, 0},
		{"146.520MHz R2xm", NULL, 0, 0, 0, 0},
		{"146.520MHzjunk", NULL, 0, 0, 0, 0},
		{"146.520MHz", "442.440md", 0, 0, 0, 0},
		{"hello 146.520MHz", NULL, 0, 0, 0, 0},
		{"146.5 MHz", NULL, 0, 0, 0, 0},
	};
	aprs_freq_t f;
	for (unsigned i = 0; i < sizeof cases / sizeof cases[0]; i++) {
		bool ok = aprs_freq_parse(cases[i].text, strlen(cases[i].text), cases[i].name, &f);
		assert(ok == (cases[i].hz != 0));
		assert(f.hz == cases[i].hz && f.offset == cases[i].offset && f.tone == cases[i].tone && f.tone_kind == cases[i].kind);
	}
	assert(aprs_freq_parse("146.52 MHz C107 -060 R25m", 25, NULL, &f));
	assert(f.range == 25 && f.range_unit == 'm');
	subtone_t tone;
	assert(aprs_freq_tone(&f, &tone) && tone.type == TONE_CTCSS && tone.value == 1072);
	f.tone = 999;
	assert(!aprs_freq_tone(&f, &tone));
	char comment[44];
	aprs_freq_channel(146520000, (subtone_t){TONE_CTCSS, 1000}, true);
	aprs_freq_comment("Hello", comment, sizeof comment);
	assert(strcmp(comment, "146.520MHz T100 Hello") == 0);
	aprs_freq_channel(433500000, (subtone_t){TONE_DCS, 0256}, true);
	aprs_freq_comment("", comment, sizeof comment);
	assert(strcmp(comment, "433.500MHz D256") == 0);
	aprs_freq_channel(433500000, (subtone_t){TONE_NONE, 0}, false);
	aprs_freq_comment("Hello", comment, sizeof comment);
	assert(strcmp(comment, "Hello") == 0);
	fuzz_t rng;
	fuzz_init(&rng, 0xFEE);
	for (long i = 0; i < fuzz_iters(4000); i++) {
		uint8_t buf[80] = "146.520MHz T100 +060 R25m";
		int n = fuzz_mutate(&rng, buf, 24, sizeof buf);
		uint8_t *p = fuzz_exact(buf, n);
		aprs_freq_parse((char *)p, (size_t)n, NULL, &f);
		assert(f.hz < 1000000000u);
		free(p);
	}
	puts("aprsFreqTest: passed");
}
