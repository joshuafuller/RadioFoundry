// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// ADR 0006: KEY is bit u8, down u8; ENC is signed i32 little endian. The channel supplies virtual time.
#include <limits.h>
#include <string.h>
#include "vboard.h"
#include "vchan.h"
#include "vhost.h"
#include "vport.h"
#include "vtrace.h"

static bool has_encoder;
static int64_t detents;

static bool set_encoder(const char *value)
{
	if (strcmp(value, "on") != 0 && strcmp(value, "off") != 0)
		return false;
	has_encoder = strcmp(value, "on") == 0;
	return true;
}
VHOST_OPTION("--encoder", "on|off", set_encoder, "the world profile has an encoder (default: off)")

static int8_t encoder_read(void)
{
	int8_t step = (int8_t)(detents > 127 ? 127 : detents < -127 ? -127 : detents);
	detents -= step;
	if (step != 0)
		vtrace("BOARD encoder.read detents=%d remaining=%lld", step, (long long)detents);
	return step;
}

vhost_encoder_fn vhost_part_encoder(void)
{
	return has_encoder ? encoder_read : NULL;
}

static void key_input(const vchan_message_t *message)
{
	if (message->length != 2 || message->payload[0] > 31 || message->payload[1] > 1)
		vhost_finish("VINPUT_BAD_KEY", 4);
	if (!keypad_vhost_set_now(message->payload[0], message->payload[1] != 0))
		vhost_finish("VINPUT_BAD_KEY", 4);
}

static void encoder_input(const vchan_message_t *message)
{
	if (message->length != 4)
		vhost_finish("VINPUT_BAD_ENC", 4);
	if (!has_encoder)
		vhost_finish("VINPUT_NO_ENCODER", 4);
	uint32_t bits = 0;
	for (unsigned i = 0; i < 4; i++)
		bits |= (uint32_t)message->payload[i] << (8 * i);
	int64_t count = bits <= INT32_MAX ? (int64_t)bits : (int64_t)bits - (INT64_C(1) << 32);
	if ((count > 0 && detents > INT64_MAX - count) || (count < 0 && detents < INT64_MIN - count))
		vhost_finish("VINPUT_ENCODER_OVERFLOW", 4);
	detents += count;
	vhost_irq_raise(VHOST_IRQ_KEY);
}

__attribute__((constructor)) static void register_inputs(void)
{
	vchan_register(VCHAN_KEY, key_input);
	vchan_register(VCHAN_ENC, encoder_input);
}
