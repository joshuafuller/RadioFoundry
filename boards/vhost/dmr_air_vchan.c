// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// AIR_OUT and AIR_IN share an explicit little-endian record, also in tools/conductor/parts/air.py:
// ts u8, kind u8, air-start us u64, cc i8 (-1 any), voice index u8, sync u8, flags u8,
// embedded LC[9], length u8, payload[length]. Flags: 1 host CRC, 2 embedded LC valid, 4 corrupted.
// The channel header is the delivery stamp, not the slot start. Keeping the start in the payload lets a burst
// cross a grant barrier without moving its receive event (start + VDMR_SLOT_US). Times are radio-relative.
#include <string.h>
#include "dmr_vhost.h"
#include "vchan.h"
#include "vclock.h"
#include "vhost.h"

#define AIR_PREFIX 24u

static void air_fail(const char *reason) __attribute__((noreturn));
static void air_fail(const char *reason)
{
	vchan_send(VCHAN_RESTART_REASON, reason, strlen(reason));
	vhost_finish(reason, 4);
}

static void air_out(void)
{
	vdmr_tx_t r;
	while (vdmr_outbox_take(&r)) {
		uint8_t wire[AIR_PREFIX + DMR_PAYLOAD_MAX] = { 0 };
		wire[0] = (uint8_t)r.ts;
		wire[1] = r.kind;
		for (unsigned i = 0; i < 8; i++)
			wire[2 + i] = (uint8_t)(r.t_us >> (8 * i));
		wire[10] = (uint8_t)r.cc;
		wire[11] = r.voice_index;
		wire[12] = r.sync;
		wire[13] = (r.host_crc ? 1u : 0u) | (r.emb_valid ? 2u : 0u);
		memcpy(wire + 14, r.emb_lc, sizeof r.emb_lc);
		if (r.len > DMR_PAYLOAD_MAX)
			air_fail("VCHAN_BAD_AIR");
		wire[23] = r.len;
		memcpy(wire + AIR_PREFIX, r.payload, r.len);
		vchan_send(VCHAN_AIR_OUT, wire, AIR_PREFIX + r.len);
	}
}

static void air_in(const vchan_message_t *message)
{
	const uint8_t *p = message->payload;
	if (message->length < AIR_PREFIX || p[23] > DMR_PAYLOAD_MAX || message->length != AIR_PREFIX + p[23] ||
	    p[0] > 2 || (p[1] > DMR_BURST_USBD && p[1] != DMR_BURST_VOICE) ||
	    (p[10] > 15 && p[10] != 255) || p[11] > 5 || p[12] > DMR_SYNC_TS2 || (p[13] & ~7u))
		air_fail("VCHAN_BAD_AIR");
	uint64_t start = 0;
	for (unsigned i = 0; i < 8; i++)
		start |= (uint64_t)p[2 + i] << (8 * i);
	if (start >= UINT64_MAX - VDMR_SLOT_US || start + VDMR_SLOT_US <= vclock_now_us())
		air_fail("VCHAN_BAD_AIR_TIME");
	dmr_burst_t b = { 0 };
	b.ts = (int8_t)p[0];
	b.kind = p[1];
	b.cc = p[10] == 255 ? 0 : p[10];
	b.voice_index = p[11];
	b.sync = p[12];
	b.crc_ok = !(p[13] & 4u);
	b.len = p[23];
	memcpy(b.payload, p + AIR_PREFIX, b.len);
	if (b.crc_ok && b.kind == DMR_BURST_VOICE && b.voice_index == 5 && (p[13] & 2u)) {
		b.lc_valid = true;
		memcpy(b.lc, p + 14, sizeof b.lc);
	} else if (b.crc_ok && b.len >= sizeof b.lc &&
		   (b.kind == DMR_BURST_VOICE_LC_HEADER || b.kind == DMR_BURST_TERMINATOR_LC)) {
		b.lc_valid = true;
		memcpy(b.lc, b.payload, sizeof b.lc);
	}
	if (!vdmr_inbox_put(start, &b))
		air_fail("VCHAN_AIR_INBOX_FULL");
}

__attribute__((constructor)) static void air_register(void)
{
	vchan_register(VCHAN_AIR_IN, air_in);
	vchan_on_done(air_out);
}
