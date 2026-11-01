// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The real audio parts and AMBE voice glue. Only the clock, interrupt controller, RTOS calls and trace are faked.
#include <assert.h>
#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "vboard.h"
#include "vclock.h"
#include "vhost.h"
#include "vport.h"
#include "vtrace.h"
#include "FreeRTOS.h"
#include "task.h"
#include "dmr/ambe/ambeVoice.h"

static const vhost_option_t *input_option;
void vhost_option_register(const vhost_option_t *opt)
{
	assert(strcmp(opt->name, "--audio-input") == 0);
	input_option = opt;
}

static uint64_t now, due;
static vclock_event_fn event;
static uint32_t event_arg;
static bool in_event, in_isr, masked, irq_pending;
static vhost_isr_t audio_irq;
uint64_t vclock_now_us(void) { return now; }
void vclock_at(uint64_t t, unsigned cls, vclock_event_fn fn, uint32_t arg)
{
	assert(event == NULL && t > now && cls == 40);
	due = t;
	event = fn;
	event_arg = arg;
}
void vclock_advance_to(uint64_t t)
{
	assert(t >= now);
	while (event != NULL && due <= t) {
		vclock_event_fn fn = event;
		event = NULL;
		now = due;
		in_event = true;
		fn(event_arg);
		in_event = false;
	}
	now = t;
}

static void deliver_irq(void)
{
	if (irq_pending && !masked) {
		irq_pending = false;
		in_isr = true;
		audio_irq();
		in_isr = false;
	}
}
void vhost_irq_register(unsigned irq, const char *name, vhost_isr_t isr)
{
	assert(irq == VHOST_IRQ_AUDIO && strcmp(name, "audio") == 0);
	audio_irq = isr;
}
void vhost_irq_raise(unsigned irq)
{
	assert(irq == VHOST_IRQ_AUDIO);
	irq_pending = true;
	deliver_irq();
}
bool vhost_in_isr(void) { return in_isr; }
void vPortEnterCritical(void) { assert(!masked); masked = true; }
void vPortExitCritical(void) { masked = false; deliver_irq(); }
void vPortYield(void) { assert(in_isr); }

static TaskFunction_t worker;
static unsigned creates, notifications;
static bool fail_create;
static int task_token;
static jmp_buf blocked;
BaseType_t xTaskCreate(TaskFunction_t fn, const char *const name, const configSTACK_DEPTH_TYPE depth,
	void *const arg, UBaseType_t priority, TaskHandle_t *const handle)
{
	assert(strcmp(name, "audio") == 0 && depth >= 128 && priority == 5 && arg == NULL);
	creates++;
	if (fail_create)
		return pdFAIL;
	worker = fn;
	*handle = (TaskHandle_t)&task_token;
	return pdPASS;
}
void vTaskGenericNotifyGiveFromISR(TaskHandle_t handle, UBaseType_t index, BaseType_t *woken)
{
	assert(in_isr && handle == (TaskHandle_t)&task_token && index == 0);
	notifications++;
	*woken = pdTRUE;
}
uint32_t ulTaskGenericNotifyTake(UBaseType_t index, BaseType_t clear, TickType_t ticks)
{
	assert(!in_isr && !in_event && index == 0 && clear == pdTRUE && ticks == portMAX_DELAY);
	if (!notifications)
		longjmp(blocked, 1);
	uint32_t n = notifications;
	notifications = 0;
	return n;
}
static void run_worker(void)
{
	if (setjmp(blocked) == 0)
		worker(NULL);
}

static unsigned block_lines, encode_lines, decode_lines;
static char last_block[256];
void vtrace(const char *fmt, ...)
{
	char line[256];
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(line, sizeof line, fmt, ap);
	va_end(ap);
	if (strstr(line, "audio.block")) {
		block_lines++;
		strcpy(last_block, line);
	}
	if (strstr(line, "vocoder.encode"))
		encode_lines++;
	if (strstr(line, "vocoder.decode"))
		decode_lines++;
}
uint64_t vtrace_hash(const void *data, size_t n)
{
	const uint8_t *p = data;
	uint64_t h = 0xcbf29ce484222325ull;
	while (n--) {
		h ^= *p++;
		h *= 0x100000001b3ull;
	}
	return h;
}

static unsigned callbacks;
static bool want_pattern, stop_in_callback;
static int context;
static void block(const int16_t *mic, int16_t *spk, void *ctx)
{
	assert(!in_event && !vhost_in_isr() && ctx == &context);
	for (unsigned i = 0; i < AUDIO_BLOCK_SAMPLES; i++) {
		assert(mic[i] == (want_pattern ? (int)i * 64 - 5088 : 0));
		assert(spk[i] == 0);
		spk[i] = (int16_t)((int)i - 80);
	}
	callbacks++;
	if (stop_in_callback)
		vhost_part_audio()->stop();
}

static void check_audio(void)
{
	const hal_audio_t *a = vhost_part_audio();
	assert(input_option && !input_option->set("/tmp/audio.wav"));
	a->stop();
	assert(!a->start(NULL, NULL) && creates == 0);
	fail_create = true;
	assert(!a->start(block, &context) && event == NULL);
	fail_create = false;
	vclock_advance_to(12345);
	assert(a->start(block, &context) && due == 20000);
	assert(a->start(block, &context) && creates == 2);
	vclock_advance_to(19999);
	run_worker();
	assert(callbacks == 0);
	for (unsigned i = 1; i <= 50; i++) {
		vclock_advance_to(i * 20000);
		assert(callbacks == i - 1);
		run_worker();
		assert(callbacks == i && block_lines == i && due == (i + 1) * 20000);
	}
	int16_t silence[AUDIO_BLOCK_SAMPLES] = { 0 }, speaker[AUDIO_BLOCK_SAMPLES];
	for (unsigned i = 0; i < AUDIO_BLOCK_SAMPLES; i++)
		speaker[i] = (int16_t)((int)i - 80);
	char expected[256];
	snprintf(expected, sizeof expected, "BOARD audio.block mic=%016llx spk=%016llx",
		(unsigned long long)vtrace_hash(silence, sizeof silence),
		(unsigned long long)vtrace_hash(speaker, sizeof speaker));
	assert(strcmp(last_block, expected) == 0);
	puts("PASS: class 40, 20000 us cadence, 160 samples, deferred callback, speaker hash");

	// A masked interrupt can cover two blocks; neither is lost when the deferred task catches up.
	masked = true;
	vclock_advance_to(1040000);
	assert(callbacks == 50);
	masked = false;
	deliver_irq();
	run_worker();
	assert(callbacks == 52);
	vclock_advance_to(1060000);
	a->stop();
	vclock_advance_to(1061234);
	assert(input_option->set("pattern"));
	want_pattern = true;
	assert(a->start(block, &context) && due == 1080000);
	run_worker();
	assert(callbacks == 52);
	stop_in_callback = true;
	vclock_advance_to(1080000);
	run_worker();
	assert(callbacks == 53);
	vclock_advance_to(1120000);
	run_worker();
	assert(callbacks == 53 && creates == 2);
	a->speaker_pcm(true);
	a->speaker_pcm(false);
	assert(input_option->set("silence"));
	puts("PASS: input selection, start failure, repeated start, stop/restart phase, pending blocks");
}

static void check_vocoder(void)
{
	const hal_vocoder_t *v = vhost_part_vocoder();
	assert(v->present() && strcmp(v->profile(), "null") == 0);
	assert(v->options == NULL && v->set_options == NULL && v->encode_flags == NULL);
	v->reset();
	uint8_t bits[VOCODER_BITS + 2];
	int16_t pcm[VOCODER_FRAME_SAMPLES + 2];
	memset(bits, 0xa5, sizeof bits);
	for (unsigned i = 0; i < VOCODER_FRAME_SAMPLES + 2; i++)
		pcm[i] = 1234;
	v->encode(pcm + 1, bits + 1);
	assert(bits[0] == 0xa5 && bits[VOCODER_BITS + 1] == 0xa5);
	for (unsigned i = 1; i <= VOCODER_BITS; i++)
		assert(bits[i] == 0);
	for (unsigned i = 0; i < VOCODER_FRAME_SAMPLES + 2; i++)
		assert(pcm[i] == 1234);
	memset(bits + 1, 1, VOCODER_BITS);
	v->decode(bits + 1, pcm + 1);
	assert(pcm[0] == 1234 && pcm[VOCODER_FRAME_SAMPLES + 1] == 1234);
	for (unsigned i = 1; i <= VOCODER_FRAME_SAMPLES; i++)
		assert(pcm[i] == 0);
	for (unsigned i = 1; i <= VOCODER_BITS; i++)
		assert(bits[i] == 1);
	assert(encode_lines == 1 && decode_lines == 1);
	puts("PASS: null vocoder, 49 zero bits, 160 silent samples, buffer bounds");
}

static void check_voice(void)
{
	ambeVoiceInit(vhost_part_audio(), vhost_part_vocoder());
	const dmr_voice_source_t *source = ambeVoiceSource();
	unsigned before_blocks = block_lines, before_encodes = encode_lines;
	uint64_t start = now;
	source->begin(source->ctx);
	for (unsigned i = 1; i <= 50; i++) {
		vclock_advance_to(start + i * 20000);
		run_worker();
		uint8_t frame[DMR_AMBE_FRAME_BYTES];
		assert(source->next(frame, source->ctx));
		assert(!source->next(frame, source->ctx));
	}
	source->end(source->ctx);
	ambeVoiceStop();
	assert(block_lines - before_blocks == 50 && encode_lines - before_encodes == 50);
	ambe_voice_stats_t stats;
	ambeVoiceGetStats(&stats);
	assert(stats.tx_frames == 50 && stats.tx_overflows == 0);
	puts("PASS: 1 s AMBE voice source, audio.block=50, vocoder.encode=50 (no modem)");
}

int main(void)
{
	check_audio();
	check_vocoder();
	check_voice();
	puts("vaudioTest: all checks passed");
	return 0;
}
