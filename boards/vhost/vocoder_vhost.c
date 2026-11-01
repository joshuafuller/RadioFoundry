// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Null vocoder: a stand-in, not an AMBE codec. Encode returns 49 zero bits; decode returns 160 silent samples.
// It exercises the firmware's voice and FEC path without stock firmware or any sound device.
#include <string.h>
#include "vboard.h"
#include "vtrace.h"

static bool present(void)
{
	vtrace("BOARD vocoder.present null");
	return true;
}

static void reset(void)
{
	vtrace("BOARD vocoder.reset");
}

static void encode(const int16_t pcm[VOCODER_FRAME_SAMPLES], uint8_t bits[VOCODER_BITS])
{
	memset(bits, 0, VOCODER_BITS);
	vtrace("BOARD vocoder.encode pcm=%016llx bits=%016llx",
		(unsigned long long)vtrace_hash(pcm, VOCODER_FRAME_SAMPLES * sizeof *pcm),
		(unsigned long long)vtrace_hash(bits, VOCODER_BITS));
}

static void decode(const uint8_t bits[VOCODER_BITS], int16_t pcm[VOCODER_FRAME_SAMPLES])
{
	memset(pcm, 0, VOCODER_FRAME_SAMPLES * sizeof *pcm);
	vtrace("BOARD vocoder.decode bits=%016llx pcm=%016llx",
		(unsigned long long)vtrace_hash(bits, VOCODER_BITS),
		(unsigned long long)vtrace_hash(pcm, VOCODER_FRAME_SAMPLES * sizeof *pcm));
}

static const char *profile(void)
{
	vtrace("BOARD vocoder.profile null");
	return "null";
}

static const hal_vocoder_t vocoder = {
	.present = present, .reset = reset, .encode = encode, .decode = decode, .profile = profile,
};

const hal_vocoder_t *vhost_part_vocoder(void)
{
	return &vocoder;
}
