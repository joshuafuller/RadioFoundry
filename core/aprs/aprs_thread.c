// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include "config.h"
#if CONFIG_APRS_RX
#include <string.h>
#include "aprs/aprs_thread.h"

static struct {
	aprs_pending_t pending;
	uint32_t ms, done_ms;
	uint8_t state;
} outgoing[APRS_OUTBOX];
static uint8_t count;
static int8_t active = -1;

void aprs_thread_init(void)
{
	memset(outgoing, 0, sizeof outgoing);
	count = 0;
	active = -1;
}

bool aprs_thread_enqueue(const aprs_pending_t *message)
{
	uint8_t waiting = 0;
	for (uint8_t i = 0; i < count; i++)
		waiting += outgoing[i].state <= APRS_MSG_SENDING;
	if (aprs_s.tx_stopped || waiting >= APRS_OUT_QUEUE)
		return false;
	if (count == APRS_OUTBOX) {
		uint8_t i = 0;
		while (i < count && outgoing[i].state <= APRS_MSG_SENDING)
			i++;
		if (i == count)
			return false;
		memmove(&outgoing[i], &outgoing[i + 1], (count - i - 1u) * sizeof outgoing[0]);
		if (active > (int8_t)i)
			active--;
		count--;
	}
	outgoing[count].pending = *message;
	outgoing[count].ms = aprs_s.now;
	outgoing[count].done_ms = 0;
	outgoing[count++].state = APRS_MSG_QUEUED;
	aprs_changed();
	return true;
}

void aprs_thread_stop(void)
{
	for (uint8_t i = 0; i < count; i++) {
		if (outgoing[i].state <= APRS_MSG_SENDING) {
			outgoing[i].state = APRS_MSG_CANCELED;
			outgoing[i].done_ms = aprs_s.now;
		}
	}
	active = -1;
}

void aprs_thread_advance(void)
{
	if (aprs_s.tx_stopped || aprs_s.out.active || active >= 0)
		return;
	for (uint8_t i = 0; i < count; i++) {
		if (outgoing[i].state == APRS_MSG_QUEUED) {
			active = (int8_t)i;
			aprs_s.out = outgoing[i].pending;
			aprs_s.out.next_ms = aprs_s.now;
			return;
		}
	}
}

void aprs_thread_update(uint8_t state)
{
	if (active < 0)
		return;
	outgoing[active].pending = aprs_s.out;
	outgoing[active].state = state;
	if (state > APRS_MSG_SENDING) {
		outgoing[active].done_ms = aprs_s.now;
		active = -1;
	}
	aprs_changed();
}

bool aprs_message_action(const aprs_thread_msg_t *message, bool retry)
{
	LOCK();
	bool ok = false;
	for (uint8_t i = 0; i < count; i++) {
		aprs_pending_t *p = &outgoing[i].pending;
		if (outgoing[i].ms != message->ms || outgoing[i].state != message->state ||
		    strcmp(p->to, message->call) || strcmp(p->text, message->text))
			continue;
		if (retry) {
			if (aprs_s.tx_stopped) break;
			if (outgoing[i].state != APRS_MSG_NO_ACK && outgoing[i].state != APRS_MSG_REJECTED && outgoing[i].state != APRS_MSG_CANCELED)
				break;
			uint8_t waiting = 0;
			for (uint8_t j = 0; j < count; j++)
				waiting += outgoing[j].state <= APRS_MSG_SENDING;
			if (waiting >= APRS_OUT_QUEUE)
				break;
			p->active = true;
			p->acked = false;
			p->tries = 0;
			p->next_ms = aprs_s.now;
			outgoing[i].state = APRS_MSG_QUEUED;
			outgoing[i].done_ms = 0;
		} else {
			if (outgoing[i].state != APRS_MSG_QUEUED && outgoing[i].state != APRS_MSG_SENDING)
				break;
			if (active == (int8_t)i) {
				aprs_ax25_cancel_message();
				active = -1;
			} else if (active > (int8_t)i) {
				active--;
			}
			memmove(&outgoing[i], &outgoing[i + 1], (count - i - 1u) * sizeof outgoing[0]);
			count--;
		}
		ok = true;
		aprs_changed();
		break;
	}
	UNLOCK();
	return ok;
}

uint8_t aprs_thread(const char *call, aprs_thread_msg_t out[APRS_THREAD_MAX], bool read)
{
	LOCK();
	uint8_t n = 0;
	for (uint8_t i = 0; i < aprs_s.nmsg; i++) {
		aprs_msg_t *m = &aprs_s.msgs[i];
		if (call != NULL && strcmp(call, m->from) != 0)
			continue;
		if (read && call != NULL && !m->read) {
			m->read = true;
			aprs_changed();
		}
		memset(&out[n], 0, sizeof out[n]);
		memcpy(out[n].call, m->from, sizeof m->from);
		memcpy(out[n].text, m->text, sizeof m->text);
		out[n].ms = m->ms;
		out[n++].read = m->read;
	}
	for (uint8_t i = 0; i < count; i++) {
		const aprs_pending_t *m = &outgoing[i].pending;
		if (call != NULL && strcmp(call, m->to) != 0)
			continue;
		memset(&out[n], 0, sizeof out[n]);
		memcpy(out[n].call, m->to, sizeof m->to);
		memcpy(out[n].text, m->text, sizeof m->text);
		out[n].ms = outgoing[i].ms;
		out[n].done_ms = outgoing[i].done_ms;
		out[n].next_ms = m->next_ms;
		out[n].tries = m->tries;
		out[n].read = true;
		out[n++].state = outgoing[i].state;
	}
	// Age comparisons also order records across the millisecond clock wrapping.
	for (uint8_t i = 1; i < n; i++) {
		aprs_thread_msg_t m = out[i];
		uint8_t j = i;
		while (j && aprs_s.now - out[j - 1].ms < aprs_s.now - m.ms) {
			out[j] = out[j - 1];
			j--;
		}
		out[j] = m;
	}
	UNLOCK();
	return n;
}
#endif
