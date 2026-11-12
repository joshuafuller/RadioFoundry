// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#ifndef RFOUNDRY_APRS_THREAD_H
#define RFOUNDRY_APRS_THREAD_H
#include "aprs/aprs_int.h"
#if CONFIG_APRS_RX
// Engine hooks: caller holds the APRS lock; storage lives in main RAM.
void aprs_thread_init(void);
void aprs_thread_stop(void);
void aprs_ax25_cancel_message(void);
bool aprs_thread_enqueue(const aprs_pending_t *message);
void aprs_thread_advance(void);
void aprs_thread_update(uint8_t state);
#endif
#endif
