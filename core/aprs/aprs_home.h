// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#ifndef RFOUNDRY_APRS_HOME_H
#define RFOUNDRY_APRS_HOME_H
#include "aprs/aprs.h"

typedef enum { APRS_LISTEN_OFF, APRS_LISTEN_UNAVAILABLE, APRS_LISTEN_CHANNEL,
	APRS_LISTEN_BUSY, APRS_LISTEN_FAILED, APRS_LISTEN_ON, APRS_LISTEN_SHARED } aprs_listen_t;
// The retained repeat event belongs to the last successfully transmitted RF packet.
typedef struct {
	bool sent;
	uint32_t sent_ms;
	uint8_t listening;
	char digi[AX25_CALL_LEN + 4];
	uint32_t repeated_ms, repeat_serial;
} aprs_home_t;
#if CONFIG_APRS_HOME
void aprs_home_get(aprs_home_t *out); // any task
// Engine only, under its lock (reset runs at initialization).
void aprs_home_reset(void);
void aprs_home_sent(const uint8_t *raw, size_t n, uint32_t now);
void aprs_home_repeat(const ax25_frame_t *f, uint32_t now);
void aprs_home_listening(uint8_t state);
#else
static inline void aprs_home_reset(void) {}
static inline void aprs_home_sent(const uint8_t *raw, size_t n, uint32_t now) { (void)raw; (void)n; (void)now; }
static inline void aprs_home_repeat(const ax25_frame_t *f, uint32_t now) { (void)f; (void)now; }
static inline void aprs_home_listening(uint8_t state) { (void)state; }
#endif
#endif
