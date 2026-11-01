// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// VIEW payload (ADR 0008), one VCHAN_VIEW packet. Integers are little endian.
// The header is followed by screen, banner, toast and query, each NUL terminated.
// Strings are copied in full; an oversized record ends the run rather than truncating.
#ifndef VVIEW_H
#define VVIEW_H

#define VVIEW_HEADER_SIZE 26u
enum {
	VVIEW_RADIO_OFF = 0, VVIEW_BOOT_OFF = 4, VVIEW_ID_OFF = 8, VVIEW_TIME_OFF = 12,
	VVIEW_DEPTH_OFF = 20, VVIEW_LOCKED_OFF = 21, VVIEW_FOCUS_OFF = 22, VVIEW_COUNT_OFF = 24,
};

#endif
