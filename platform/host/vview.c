// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// ADR 0008: observe only calls the firmware already makes, with no UI request or redraw.
#include <stdio.h>
#include <string.h>
#include "ui/ui_app.h"
#include "vchan.h"
#include "vclock.h"
#include "vhost.h"
#include "vtrace.h"
#include "vview.h"

static uint32_t radio_id, boot_id, frame_id;
static bool have_frame;
static uint8_t previous[VCHAN_PAYLOAD_MAX];
static size_t previous_len;

static void frame_line(const char *line, size_t n)
{
	(void)n;
	unsigned radio, boot, frame;
	// shortcut: FRAME supplies identity, use a shared accessor if status ever precedes the first frame.
	if (sscanf(line, "%*s %*s FRAME radio=%u boot=%u id=%u", &radio, &boot, &frame) == 3) {
		radio_id = radio;
		boot_id = boot;
		frame_id = frame;
		have_frame = true;
	}
}
VTRACE_LINE_HOOK(frame_line)

static void put_le(uint8_t *p, uint64_t value, unsigned n)
{
	for (unsigned i = 0; i < n; i++)
		p[i] = (uint8_t)(value >> (8 * i));
}

void __real_ui_app_status(ui_app_status_t *status);
void __wrap_ui_app_status(ui_app_status_t *status)
{
	__real_ui_app_status(status);
	if (!have_frame)
		vhost_finish("VIEW_NO_FRAME", 3);

	uint8_t wire[VCHAN_PAYLOAD_MAX] = { 0 };
	wire[VVIEW_DEPTH_OFF] = status->depth;
	wire[VVIEW_LOCKED_OFF] = status->locked;
	put_le(wire + VVIEW_FOCUS_OFF, status->focus, 2);
	put_le(wire + VVIEW_COUNT_OFF, status->count, 2);
	const char *strings[] = { status->screen, status->banner, status->toast, status->query };
	size_t len = VVIEW_HEADER_SIZE;
	for (unsigned i = 0; i < sizeof strings / sizeof strings[0]; i++) {
		const char *s = strings[i] != NULL ? strings[i] : "";
		size_t n = strnlen(s, sizeof wire - len);
		if (n == sizeof wire - len)
			vhost_finish("VIEW_TOO_LARGE", 3);
		memcpy(wire + len, s, n + 1);
		len += n + 1;
	}
	// Frame and time alone are not changes to the eight semantic fields.
	if (len == previous_len && memcmp(wire + VVIEW_DEPTH_OFF, previous + VVIEW_DEPTH_OFF,
		len - VVIEW_DEPTH_OFF) == 0)
		return;
	uint64_t now = vclock_now_us();
	put_le(wire + VVIEW_RADIO_OFF, radio_id, 4);
	put_le(wire + VVIEW_BOOT_OFF, boot_id, 4);
	put_le(wire + VVIEW_ID_OFF, frame_id, 4);
	put_le(wire + VVIEW_TIME_OFF, now, 8);
	memcpy(previous, wire, len);
	previous_len = len;
	// Keep the trace bounded; the channel carries full text, and the hash covers every status byte.
	vtrace("VIEW radio=%u boot=%u id=%u us=%llu screen=%.64s depth=%u focus=%u count=%u locked=%u status=%016llx",
		radio_id, boot_id, frame_id, (unsigned long long)now, (const char *)wire + VVIEW_HEADER_SIZE,
		status->depth, status->focus, status->count, status->locked,
		(unsigned long long)vtrace_hash(wire + VVIEW_DEPTH_OFF, len - VVIEW_DEPTH_OFF));
	vchan_send(VCHAN_VIEW, wire, len);
}
