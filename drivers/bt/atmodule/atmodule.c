// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include "atmodule.h"
#include <stdio.h>
#include <string.h>

static bool active(const atmodule_t *s)
{
	return s->state != ATMODULE_DISABLED && s->state != ATMODULE_FAULT;
}

static void clear(atmodule_t *s)
{
	s->counts.drops += (uint32_t)(s->rx_len - s->rx_at + s->tx_len + s->payload_at);
	s->rx_len = s->rx_at = s->tx_len = s->line_len = s->payload_len = s->payload_at = 0;
	s->wire_len = s->wire_at = 0;
	s->pending = s->version_seen = s->discard = false;
	s->version[0] = 0;
	s->generation++;
	s->module->flush();
}

void atmodule_init(atmodule_t *s, const bt_module_t *module, uint32_t now)
{
	memset(s, 0, sizeof(*s));
	s->module = module;
	s->off_at = now;
	module->enable(false);
}

void atmodule_disable(atmodule_t *s, uint32_t now)
{
	s->module->enable(false);
	s->powered = false;
	s->off_at = now;
	clear(s);
	s->state = ATMODULE_DISABLED;
}

void atmodule_fault(atmodule_t *s, uint32_t now)
{
	atmodule_disable(s, now);
	s->state = ATMODULE_FAULT;
}

bool atmodule_enable(atmodule_t *s, uint32_t now)
{
	const atmodule_profile_t *p = s->module->profile;
	if (active(s) || p == NULL || p->tx_limit == 0 || p->tx_limit > ATMODULE_TX_MAX ||
	    p->ready_ms == 0 || p->command_ms == 0 || p->rx_ms == 0) {
		return false;
	}
	if (!s->module->uart->start()) {
		atmodule_fault(s, now);
		return false;
	}
	clear(s);
	s->errors = s->module->errors();
	s->since = now;
	s->state = ATMODULE_STARTING;
	return true;
}

static void command(atmodule_t *s, uint32_t now)
{
	s->wire_at = 0;
	s->pending = true;
	s->command_at = now;
}

static void bad(atmodule_t *s, uint32_t now)
{
	s->counts.malformed++;
	atmodule_fault(s, now);
}

static void line(atmodule_t *s, uint32_t now)
{
	char *l = s->line;
	const atmodule_profile_t *p = s->module->profile;
	if (strncmp(l, "+DATA", 5) == 0) {
		size_t n = strlen(l);
		if (n < 10 || n > 12 || strncmp(l, "+DATA:", 6) != 0 || l[8] != ',' ||
		    l[6] < '0' || l[6] > '9' || l[7] < '0' || l[7] > '9') {
			bad(s, now);
			return;
		}
		size_t count = 0;
		for (size_t i = 9; i < n; i++) {
			if (l[i] < '0' || l[i] > '9') {
				bad(s, now);
				return;
			}
			count = count * 10u + (unsigned)(l[i] - '0');
		}
		if (count == 0 || count > ATMODULE_RX_MAX ||
		    (s->state != ATMODULE_IDLE && s->state != ATMODULE_CONNECTED)) {
			bad(s, now);
			return;
		}
		s->discard = l[6] != '0' || l[7] != '0';
		s->counts.wrong_channel += s->discard;
		s->payload_len = count;
		s->payload_at = 0;
	} else if (strcmp(l, "+READY") == 0) {
		clear(s);
		s->state = ATMODULE_IDENTIFYING;
		s->since = now;
		memcpy(s->wire, "AT+VER?\r\n", 9);
		s->wire_len = 9;
		command(s, now);
	} else if (strncmp(l, "+VER:", 5) == 0 && s->state == ATMODULE_IDENTIFYING) {
		size_t n = strlen(l + 5);
		if (n == 0 || n > 20 || (p->version != NULL && strcmp(l + 5, p->version) != 0)) {
			bad(s, now);
			return;
		}
		memcpy(s->version, l + 5, n + 1);
		s->version_seen = true;
		if (!s->pending) {
			s->state = ATMODULE_IDLE;
		}
	} else if (strcmp(l, "OK") == 0) {
		if (s->pending && s->wire_at == s->wire_len) {
			s->pending = false;
			s->wire_at = s->wire_len = 0;
			if (s->state == ATMODULE_IDENTIFYING && s->version_seen) {
				s->state = ATMODULE_IDLE;
			}
		}
	} else if (strncmp(l, "+DISC", 5) == 0) {
		// A partially sent command cannot safely be followed by a fresh command.
		if (s->pending) {
			atmodule_fault(s, now);
		} else if (s->state == ATMODULE_IDLE || s->state == ATMODULE_CONNECTED) {
			clear(s);
			s->state = ATMODULE_IDLE;
		}
	} else if (strncmp(l, "+CONN", 5) == 0 || strncmp(l, "+HOSTDEVICE", 11) == 0) {
		const char *token = strchr(l, ',');
		if (s->state == ATMODULE_IDLE && token != NULL && p->data_profile != NULL &&
		    strcmp(token + 1, p->data_profile) == 0) {
			s->state = ATMODULE_CONNECTED;
		}
	} else if (strncmp(l, "+PTTKEY:", 8) == 0 || strncmp(l, "+PTTBATT:", 9) == 0) {
		s->counts.ptt++; // Accessory events never reach the host or any RF input path.
	} else if (*l != 0) {
		s->counts.unknown++;
	}
}

void atmodule_feed(atmodule_t *s, const uint8_t *buf, size_t len, uint32_t now)
{
	if (active(s) && (s->line_len || s->payload_len) && now - s->last_rx >= s->module->profile->rx_ms) {
		s->counts.timeouts++;
		atmodule_fault(s, now);
	}
	for (size_t i = 0; i < len && active(s) && s->powered; i++) {
		uint8_t b = buf[i];
		s->last_rx = now;
		if (s->payload_len != 0) {
			s->payload[s->payload_at++] = b;
			if (s->payload_at == s->payload_len) {
				if (!s->discard) {
					// Until the data-profile token is measured, complete DATA starts the session.
					if (s->module->profile->data_profile == NULL) {
						s->state = ATMODULE_CONNECTED;
					}
					if (s->state != ATMODULE_CONNECTED || s->payload_len > ATMODULE_QUEUE - (s->rx_len - s->rx_at)) {
						s->counts.refused++;
						atmodule_fault(s, now);
						return;
					}
					memmove(s->rx, s->rx + s->rx_at, s->rx_len - s->rx_at);
					s->rx_len -= s->rx_at;
					s->rx_at = 0;
					memcpy(s->rx + s->rx_len, s->payload, s->payload_len);
					s->rx_len += s->payload_len;
				}
				s->payload_at = s->payload_len = 0;
			}
		} else if (b == '\n' && s->line_len && s->line[s->line_len - 1] == '\r') {
			s->line[--s->line_len] = 0;
			uint32_t gen = s->generation;
			line(s, now);
			s->line_len = 0;
			if (s->generation != gen) {
				return; // Discard the rest of this UART batch from the old session.
			}
		} else if (b == 0 || b == '\n' || s->line_len == 128 ||
			   (s->line_len && s->line[s->line_len - 1] == '\r')) {
			bad(s, now);
		} else {
			s->line[s->line_len++] = (char)b;
		}
	}
}

void atmodule_step(atmodule_t *s, uint32_t now)
{
	if (!active(s)) {
		return;
	}
	const bt_module_t *m = s->module;
	const atmodule_profile_t *p = m->profile;
	if (!s->powered) {
		if (now - s->since < m->startup_ms || now - s->off_at < m->off_ms) {
			return;
		}
		m->flush();
		s->errors = m->errors();
		m->enable(true);
		s->powered = true;
		s->since = now;
	}
	uint32_t errors = m->errors();
	if (errors != s->errors) {
		s->counts.overruns += errors - s->errors;
		atmodule_fault(s, now);
		return;
	}
	if (((s->line_len || s->payload_len) && now - s->last_rx >= p->rx_ms) ||
	    (s->pending && now - s->command_at >= p->command_ms) ||
	    (s->state == ATMODULE_STARTING && now - s->since >= p->ready_ms) ||
	    (s->state == ATMODULE_IDENTIFYING && now - s->since >= p->command_ms)) {
		s->counts.timeouts++;
		atmodule_fault(s, now);
		return;
	}
	uint8_t buf[64];
	// Bound each service pass even if an untrusted peer never stops sending.
	for (unsigned i = 0; i < 32 && active(s); i++) {
		size_t n = m->uart->read(buf, sizeof(buf));
		errors = m->errors();
		if (errors != s->errors) {
			s->counts.overruns += errors - s->errors;
			atmodule_fault(s, now);
			return;
		}
		if (!n) {
			break;
		}
		uint32_t gen = s->generation;
		atmodule_feed(s, buf, n, now);
		if (gen != s->generation) {
			break;
		}
	}
	if (!active(s)) {
		return;
	}
	if (!s->pending && s->state == ATMODULE_CONNECTED && s->tx_len) {
		size_t n = s->tx_len < p->tx_limit ? s->tx_len : p->tx_limit;
		int h = snprintf((char *)s->wire, sizeof(s->wire), "AT+DATA=00,%u\r\n", (unsigned)n);
		memcpy(s->wire + h, s->tx, n);
		s->wire_len = (size_t)h + n;
		memmove(s->tx, s->tx + n, s->tx_len - n);
		s->tx_len -= n;
		command(s, now);
	}
	if (s->pending && s->wire_at < s->wire_len) {
		s->wire_at += m->uart->write(s->wire + s->wire_at, s->wire_len - s->wire_at);
	}
}

size_t atmodule_read(atmodule_t *s, uint8_t *buf, size_t max)
{
	if (s->state != ATMODULE_CONNECTED) {
		return 0;
	}
	size_t n = s->rx_len - s->rx_at;
	if (n > max) {
		n = max;
	}
	memcpy(buf, s->rx + s->rx_at, n);
	s->rx_at += n;
	return n;
}

size_t atmodule_write(atmodule_t *s, const uint8_t *buf, size_t len)
{
	if (s->state != ATMODULE_CONNECTED) {
		return 0;
	}
	size_t n = ATMODULE_QUEUE - s->tx_len;
	if (n > len) {
		n = len;
	}
	s->counts.refused += n != len;
	memcpy(s->tx + s->tx_len, buf, n);
	s->tx_len += n;
	return n;
}
