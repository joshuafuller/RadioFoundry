// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Silent virtual PCM: class-40 blocks on the radio's clock, delivered by a deferred task (hal_audio.h).
// The phase stays at multiples of 20000 us from power-on, including across stop/start. Samples never leave the trace.
#include <string.h>
#include "vboard.h"
#include "vclock.h"
#include "vhost.h"
#include "vport.h"
#include "vtrace.h"
#include "FreeRTOS.h"
#include "task.h"

#define BLOCK_US 20000u

static TaskHandle_t task;
static audio_block_cb_t block_cb;
static void *block_ctx;
static uint32_t pending;
static int16_t mic[AUDIO_BLOCK_SAMPLES], spk[AUDIO_BLOCK_SAMPLES];

static bool set_input(const char *value)
{
	bool pattern = strcmp(value, "pattern") == 0;
	if (!pattern && strcmp(value, "silence") != 0)
		return false;
	for (unsigned i = 0; i < AUDIO_BLOCK_SAMPLES; i++)
		mic[i] = pattern ? (int16_t)((int)i * 64 - 5088) : 0;
	return true;
}
VHOST_OPTION("--audio-input", "silence|pattern", set_input, "virtual microphone samples (default silence)")

static void audio_task(void *arg)
{
	(void)arg;
	for (;;) {
		ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
		for (;;) {
			taskENTER_CRITICAL();
			audio_block_cb_t cb = pending ? block_cb : NULL;
			void *ctx = block_ctx;
			if (pending)
				pending--;
			taskEXIT_CRITICAL();
			if (cb == NULL)
				break;
			memset(spk, 0, sizeof spk);
			cb(mic, spk, ctx);
			vtrace("BOARD audio.block mic=%016llx spk=%016llx",
				(unsigned long long)vtrace_hash(mic, sizeof mic),
				(unsigned long long)vtrace_hash(spk, sizeof spk));
		}
	}
}

static void audio_isr(void)
{
	BaseType_t woken = pdFALSE;
	vTaskNotifyGiveFromISR(task, &woken);
	portYIELD_FROM_ISR(woken);
}

static void block_event(uint32_t arg)
{
	(void)arg;
	// Queue first: raising the interrupt can switch to the audio task.
	vclock_at(vclock_now_us() + BLOCK_US, VCLOCK_CLASS_OWN, block_event, 0);
	if (block_cb != NULL) {
		pending++;
		vhost_irq_raise(VHOST_IRQ_AUDIO);
	}
}

static bool audio_start(audio_block_cb_t cb, void *ctx)
{
	if (cb == NULL)
		return false;
	if (task == NULL) {
		if (xTaskCreate(audio_task, "audio", 1280, NULL, 5, &task) != pdPASS) {
			task = NULL;
			vtrace("BOARD audio.start refused");
			return false;
		}
		vhost_irq_register(VHOST_IRQ_AUDIO, "audio", audio_isr);
		uint64_t now = vclock_now_us();
		vclock_at(now - now % BLOCK_US + BLOCK_US, VCLOCK_CLASS_OWN, block_event, 0);
	}
	taskENTER_CRITICAL();
	block_ctx = ctx;
	block_cb = cb;
	taskEXIT_CRITICAL();
	vtrace("BOARD audio.start");
	return true;
}

static void audio_stop(void)
{
	taskENTER_CRITICAL();
	block_cb = NULL;
	block_ctx = NULL;
	pending = 0;
	taskEXIT_CRITICAL();
	vtrace("BOARD audio.stop");
}

static void speaker_pcm(bool on)
{
	vtrace("BOARD audio.speaker_pcm %u", on);
}

static const hal_audio_t audio = { .start = audio_start, .stop = audio_stop, .speaker_pcm = speaker_pcm };

const hal_audio_t *vhost_part_audio(void)
{
	return &audio;
}
