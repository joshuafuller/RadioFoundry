// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#ifndef RFOUNDRY_APRS_OBJECTS_H
#define RFOUNDRY_APRS_OBJECTS_H
#include "config.h"
#include "aprs_fmt.h"

#ifndef APRS_OBJECTS
#define APRS_OBJECTS 8
#endif
#ifndef APRS_BULLETINS
#define APRS_BULLETINS 8
#endif
#define APRS_REMOVED_MS 600000u
#define APRS_RETAIN_MS 86400000u

typedef struct {
	char name[APRS_NAME_MAX + 1], owner[AX25_CALL_LEN + 4];
	aprs_pos_t pos;
	char comment[APRS_MSG_TEXT_MAX + 1];
	uint32_t ms, frequency_hz;
	bool killed;
} aprs_object_t;

typedef struct {
	char group[APRS_ADDRESSEE_LEN + 1], from[AX25_CALL_LEN + 4];
	char text[APRS_MSG_TEXT_MAX + 1];
	uint32_t ms;
	bool pending;
} aprs_bulletin_t;

#if CONFIG_APRS_RX
// Radio task, APRS lock held.
void aprs_objects_init(void);
void aprs_objects_tick(uint32_t now_ms);
void aprs_objects_receive(const ax25_frame_t *f, const aprs_parsed_t *p, uint32_t now_ms);
bool aprs_is_bulletin(const aprs_parsed_t *p);
// Any task: copies under the APRS lock, newest first.
uint8_t aprs_object_count(void);
bool aprs_object(uint8_t i, aprs_object_t *out);
uint8_t aprs_bulletin_count(void);
bool aprs_bulletin(uint8_t i, aprs_bulletin_t *out);
bool aprs_bulletin_alert(aprs_bulletin_t *out);
#endif
#endif
