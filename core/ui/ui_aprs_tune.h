// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#ifndef UI_APRS_TUNE_H
#define UI_APRS_TUNE_H
#include "aprs/aprs_freq.h"
#include "ui_nav.h"
#if CONFIG_APRS_TUNE
void ui_aprs_tune_item(const aprs_freq_t *f, ui_item_t *it);
void ui_aprs_tune(const aprs_freq_t *f);
#endif
#endif
