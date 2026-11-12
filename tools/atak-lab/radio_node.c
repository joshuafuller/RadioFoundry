// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "board.h"
#include "dmr/ipGateway.h"
#include "dmr/dmrData.h"
#include "dmr/dmrDataService.h"
#include "dmr/dmrAirQueue.h"
#include "hostlink/hostlink_ppp.h"
#include "hotspot/dmrDataFrame.h"
#include "net/ppp.h"

static uint32_t radio_id, clock_ms;
static bool scheduled, channel_clear = true;
static dmrDataChannel_t channel;
static const dmrBurst_t *air_bursts;
static int air_count, air_at;
static uint32_t air_next;

static void fail(const char *message)
{
	fprintf(stderr, "radio-node: %s\n", message);
	exit(1);
}

static void emit(uint8_t kind, const void *payload, size_t length)
{
	uint8_t message[IPGW_MAX_FRAME + 1];
	if (length > IPGW_MAX_FRAME)
		fail("oversized IPC reply");
	message[0] = kind;
	if (length != 0)
		memcpy(message + 1, payload, length);
	uint8_t header[2] = { (length + 1) >> 8, (length + 1) & 255 };
	if (fwrite(header, 1, 2, stdout) != 2 || fwrite(message, 1, length + 1, stdout) != length + 1 || fflush(stdout))
		fail("IPC send failed");
}

static void emit_burst(const dmrBurst_t *burst)
{
	uint8_t coded[34];
	coded[0] = burst->dataType;
	if (!dmrDataBurstToFrame(burst, 1, coded + 1))
		fail("DMR frame encoding failed");
	emit('B', coded, sizeof coded);
}

static bool tx_start(const dmrBurst_t *bursts, int count, void *ctx)
{
	air_bursts = bursts;
	air_count = count;
	air_at = 0;
	air_next = clock_ms + 60;
	return true;
}

static bool clear_channel(uint32_t nowMs, void *ctx) { return channel_clear; }

static void tick(uint32_t target)
{
	if (target < clock_ms)
		fail("clock moved backward");
	// Millisecond steps preserve completion and guard boundaries even when the runner jumps ahead.
	while (clock_ms < target) {
		clock_ms++;
		ppp_poll(clock_ms);
		if (!scheduled)
			continue;
		if (air_count && clock_ms >= air_next) {
			emit_burst(&air_bursts[air_at++]);
			air_next += 60;
			if (air_at == air_count) {
				air_count = 0;
				dmrDataServiceTxDone(0);
			}
		}
		dmrDataServiceTick(clock_ms, &channel);
		airqTick(radio_id, clock_ms);
	}
}

static void status(void)
{
	ppp_stats_t p;
	airqStats_t q;
	dmrDataStats_t d;
	ppp_stats(&p);
	airqGetStats(&q);
	dmrDataServiceGetStats(&d);
	char json[600];
	int n = snprintf(json, sizeof json,
		"{\"up\":%d,\"running\":%d,\"rx_errors\":%u,\"rx_frames\":%u,"
		"\"depth\":%u,\"bytes\":%u,\"capacity\":%u,\"refused\":%u,"
		"\"aged\":%u,\"gave_up\":%u,\"sent\":%u,\"chained\":%u,"
		"\"tx_lists\":%u,\"tx_guarded\":%u,\"tx_last_ms\":%u}",
		ppp_ip_up(), ppp_running(), p.rx_errors, p.rx_frames, q.depth, q.bytes, q.capacity,
		q.refusedFull, q.aged, q.gaveUp, q.sent, q.chained, d.txLists, d.txGuarded, d.txLastMs);
	if (n < 0 || (size_t)n >= sizeof json)
		fail("status overflow");
	emit('J', json, (size_t)n);
}

// ---- the serial host port: PPP's bytes to the phone go out as 'S' messages ----

static size_t port_write(const uint8_t *buf, size_t len)
{
	for (size_t at = 0; at < len; at += 512)
		emit('S', buf + at, (len - at < 512) ? len - at : 512);
	return len;
}

static const hal_stream_t port = { .write = port_write };
const board_t board = { .name = "atak-lab", .host_port = &port, .netif = &hostlink_ppp_netif };

static uint8_t frame_buf[IPGW_MAX_FRAME];
uint8_t *hostlink_net_frame(void) { return frame_buf; }
bool ppp_foreign_start(uint8_t b) { return false; } // No other protocol shares the port in this fixture.

bool ipGatewaySendFrame(const uint8_t *frame, int length)
{
	if (length < 0)
		fail("invalid Ethernet frame length");
	if (ppp_running())
		return board.netif->send(frame, length);
	emit('F', frame, (size_t)length);
	return true;
}

uint32_t ipGatewayRadioId(void) { return radio_id; }
void ipGatewaySerialIn(const uint8_t *data, int length) {} // No serial service in this host fixture.
bool ipGatewayRawToAir(bool group, uint32_t dst, const uint8_t *data, int length) { return false; }

bool ipGatewayIPToAir(bool group, uint32_t dst, uint32_t srcIp, uint32_t dstIp, uint8_t ttl, uint8_t protocol,
	const uint8_t *l4, int length)
{
	if (scheduled) {
		airqItem_t item = { .kind = AIRQ_IP, .owner = AIRQ_OWNER_GATEWAY, .group = group,
			.dst = dst, .srcIp = srcIp, .dstIp = dstIp, .ttl = ttl, .protocol = protocol,
			.data = l4, .length = length, .waitMs = 2000, .maxAgeMs = 10000 };
		return airqPut(&item);
	}
	const uint8_t *pdu;
	uint8_t sap;
	if (srcIp == IPGW_AIR_SRC_OURS)
		srcIp = dmrDataIdAddress(false, radio_id);
	int count = dmrDataEncodeIP(group, dst, radio_id, dstIp, srcIp, ttl, protocol, l4, length,
		false, dmrDataNextIpId(), &pdu, &sap);
	if (count <= 0)
		return false;
	dmrBurst_t bursts[DMR_DATA_MAX_BURSTS];
	count = dmrDataBuildPacket(DMR_DPF_UNCONFIRMED, sap, group, dst, radio_id, pdu, count, DMR_BURST_RATE_12_DATA,
		DMR_DATA_IP_PREAMBLES, bursts, DMR_DATA_MAX_BURSTS);
	if (count <= 0)
		return false;
	for (int b = 0; b < count; b++)
		emit_burst(&bursts[b]);
	return true;
}

static void receive_frame(const uint8_t *coded)
{
	dmrBurst_t burst;
	if (!dmrDataFrameToBurst(coded[0], coded + 1, &burst)) {
		emit('E', NULL, 0);
		return;
	}
	dmrDataRxResult_t result = dmrDataRxBurst(&burst);
	if (result == DMR_DATA_RX_ERROR) {
		emit('E', NULL, 0);
	} else if (result == DMR_DATA_RX_PACKET) {
		dmrDataIP_t ip;
		if (!dmrDataGetIP(&dmrDataRxPacket, &ip) ||
			!ipGatewayDeliverIP(dmrDataRxPacket.group, dmrDataRxPacket.dst, dmrDataRxPacket.src,
				ip.srcIp, ip.dstIp, ip.ttl, ip.protocol, ip.payload, ip.length))
			fail("decoded IP could not reach the host");
	}
}

int main(int argc, char **argv)
{
	if (argc != 2)
		fail("usage: radio-node RADIO_ID");
	char *end;
	errno = 0;
	unsigned long id = strtoul(argv[1], &end, 10);
	if (errno != 0 || *end != 0 || id == 0 || id > 0xFFFFFF)
		fail("invalid radio ID");
	radio_id = (uint32_t)id;
	const uint8_t gateway_mac[6] = { 0x02, 0x52, 0x46, 0, 0, 1 };
	const uint8_t host_mac[6] = { 0x02, 0x52, 0x46, 0, 0, 2 };
	ipGatewayInit(gateway_mac, host_mac);
	dmrDataRxReset();
	for (;;) {
		uint8_t message[2048];
		uint8_t header[2];
		size_t got = fread(header, 1, 2, stdin);
		if (got == 0 && feof(stdin))
			return 0;
		if (got != 2)
			fail("truncated IPC header");
		size_t length = ((size_t)header[0] << 8) | header[1];
		if (length == 0 || length > sizeof message)
			fail("invalid IPC length");
		if (fread(message, 1, length, stdin) != length)
			fail("truncated IPC request");
		if (message[0] == 'Q' && length == 1)
			return 0;
		if (message[0] == 'H' && length > 1)
			ipGatewayEthernetIn(message + 1, (int)length - 1);
		else if (message[0] == 'B' && length == 35)
			receive_frame(message + 1);
		else if (message[0] == 'P' && length > 1) {
			// As the host link does: a request that starts with a flag in idle mode enters PPP, passive
			if (!ppp_running() && message[1] == 0x7E)
				hostlink_ppp_begin(false, clock_ms);
			if (ppp_running())
				ppp_input(message + 1, (size_t)length - 1, clock_ms);
		}
		else if (message[0] == 'T' && length == 5) {
			uint32_t target = ((uint32_t)message[1] << 24) | ((uint32_t)message[2] << 16) |
				((uint32_t)message[3] << 8) | message[4];
			tick(target);
		} else if (message[0] == 'J' && length == 1)
			status();
		else if (message[0] == 'M' && length == 1 && !scheduled) {
			dmrDataEnv_t env = { .txStart = tx_start, .channelClear = clear_channel };
			dmrDataSettings_t settings = { .udpCompress = DMR_UDPC_OFF };
			channel = (dmrDataChannel_t){ .ownId = radio_id, .canTransmit = true };
			dmrDataServiceInit(&env);
			dmrDataServiceSetSettings(&settings);
			airqInit();
			dmrDataServiceTick(clock_ms, &channel);
			scheduled = true;
		} else if (message[0] == 'C' && length == 3) {
			channel_clear = message[1] != 0;
			channel.canTransmit = message[2] != 0;
		} else
			fail("invalid IPC request");
		emit('Z', NULL, 0);
	}
}
