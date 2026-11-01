// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Ethernet frames over the control channel. The channel calls eth_in at the stamped virtual time, even with
// interrupts off. Frames stay in device state until the host link task polls receive(); no interrupt is needed.
#include "config.h"
#if CONFIG_USB_NETWORK
#include <string.h>
#include "vboard.h"
#include "vchan.h"
#include "vhost.h"
#include "vtrace.h"
#include "FreeRTOS.h"
#include "task.h"

#define FRAME_MAX	600u	// IPGW_MAX_FRAME (core/dmr/ipGateway.h)
#define RX_FRAMES	64u	// one full control-channel input batch; overflow ends the run, never silently drops

static struct {
	uint8_t data[FRAME_MAX];
	uint16_t length;
} rx[RX_FRAMES];
static unsigned rx_head, rx_tail;
static bool net_selected;
static hal_netif_stats_t net_stats;

static void eth_in(const vchan_message_t *message)
{
	if (message->length < 14 || message->length > FRAME_MAX)
		vhost_finish("VNETIF_BAD_LENGTH", 4);
	if (rx_head - rx_tail == RX_FRAMES)
		vhost_finish("VNETIF_RX_FULL", 4);
	unsigned slot = rx_head % RX_FRAMES;
	memcpy(rx[slot].data, message->payload, message->length);
	rx[slot].length = message->length;
	rx_head++;
	vtrace("BOARD net.rx %u type=%02x%02x", message->length, message->payload[12], message->payload[13]);
}

__attribute__((constructor)) static void net_register(void)
{
	vchan_register(VCHAN_ETH_IN, eth_in);
}

static bool n_select(bool network)
{
	net_selected = network;
	vtrace("BOARD net.select %d", network);
	return true;
}

static bool n_selected(void) { return net_selected; }
static bool n_up(void) { return net_selected; }	// the host configures the adapter at once

static bool n_send(const uint8_t *frame, int length)
{
	if (!n_up() || frame == NULL || length < 14 || length > (int)FRAME_MAX)
		return false;
	vchan_send(VCHAN_ETH_OUT, frame, (size_t)length);
	net_stats.tx_frames++;
	vtrace("BOARD net.tx %d type=%02x%02x", length, frame[12], frame[13]);
	return true;
}

static int n_receive(uint8_t *buf, int max)
{
	int length = 0;
	taskENTER_CRITICAL();
	if (n_up() && buf != NULL && rx_head != rx_tail) {
		unsigned slot = rx_tail % RX_FRAMES;
		if (max >= rx[slot].length) {
			length = rx[slot].length;
			memcpy(buf, rx[slot].data, (size_t)length);
			rx_tail++;
			net_stats.rx_frames++;
		}
	}
	taskEXIT_CRITICAL();
	return length;
}

static void n_tick(uint32_t now_ms) { (void)now_ms; }

static void n_macs(uint8_t host[6], uint8_t radio_mac[6])
{
	static const uint8_t h[6] = { 0x02, 0x56, 0x48, 0x00, 0x00, 0x02 };
	static const uint8_t r[6] = { 0x02, 0x56, 0x48, 0x00, 0x00, 0x01 };
	memcpy(host, h, 6);
	memcpy(radio_mac, r, 6);
}

static void n_stats(hal_netif_stats_t *stats) { *stats = net_stats; }

static const hal_netif_t netif = {
	.select = n_select, .selected = n_selected, .up = n_up, .send = n_send, .receive = n_receive, .tick = n_tick,
	.macs = n_macs, .stats = n_stats, .frame_max = FRAME_MAX,
};

const hal_netif_t *vhost_part_netif(void)
{
	return &netif;
}
#endif
