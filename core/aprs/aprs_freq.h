// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#ifndef RFOUNDRY_APRS_FREQ_H
#define RFOUNDRY_APRS_FREQ_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "config.h"
#include "hal_radio.h"

typedef struct {
	uint32_t hz;
	int16_t offset; // 10 kHz units, zero when not supplied; no regional offset guessing
	uint16_t tone; // whole Hz for T/C, octal value for D
	uint8_t range;
	char tone_kind, range_unit;
	bool narrow;
} aprs_freq_t;
#if CONFIG_APRS_TUNE
// Length-delimited comment; object name is NUL terminated, or NULL. False clears out.
bool aprs_freq_parse(const char *text, size_t len, const char *object, aprs_freq_t *out);
bool aprs_freq_tone(const aprs_freq_t *f, subtone_t *out);
// Current voice channel's prefix, refreshed by the radio task before beacon generation.
void aprs_freq_channel(uint32_t hz, subtone_t tone, bool fm);
void aprs_freq_comment(const char *comment, char *out, size_t max);
#endif
#endif
