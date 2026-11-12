// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include "board.h"
#include "dmr/ipGateway.h"
#include "dmr/dmrData.h"
#include "hostlink/hostlink_ppp.h"
#include "hotspot/dmrDataFrame.h"
#include "net/ppp.h"

static int link_fd;
static uint32_t radio_id;

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
	if (send(link_fd, message, length + 1, MSG_NOSIGNAL) != (ssize_t)(length + 1))
		fail("IPC send failed");
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
	for (int b = 0; b < count; b++) {
		uint8_t coded[34];
		coded[0] = bursts[b].dataType;
		if (!dmrDataBurstToFrame(&bursts[b], 1, coded + 1))
			fail("DMR frame encoding failed");
		emit('B', coded, sizeof coded);
	}
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
	if (argc != 3)
		fail("usage: radio-node RADIO_ID IPC_FD");
	char *end;
	errno = 0;
	unsigned long id = strtoul(argv[1], &end, 10);
	if (errno != 0 || *end != 0 || id == 0 || id > 0xFFFFFF)
		fail("invalid radio ID");
	radio_id = (uint32_t)id;
	errno = 0;
	long fd = strtol(argv[2], &end, 10);
	if (errno != 0 || *end != 0 || fd < 0 || fd > INT_MAX)
		fail("invalid IPC descriptor");
	link_fd = (int)fd;
	const uint8_t gateway_mac[6] = { 0x02, 0x52, 0x46, 0, 0, 1 };
	const uint8_t host_mac[6] = { 0x02, 0x52, 0x46, 0, 0, 2 };
	ipGatewayInit(gateway_mac, host_mac);
	dmrDataRxReset();
	for (;;) {
		uint8_t message[2048];
		ssize_t length = recv(link_fd, message, sizeof message, MSG_TRUNC);
		if (length == 0)
			return 0;
		if (length < 0) {
			if (errno == EINTR)
				continue;
			fail("IPC receive failed");
		}
		if ((size_t)length > sizeof message)
			fail("oversized IPC request");
		if (message[0] == 'Q' && length == 1)
			return 0;
		if (message[0] == 'H' && length > 1)
			ipGatewayEthernetIn(message + 1, (int)length - 1);
		else if (message[0] == 'B' && length == 35)
			receive_frame(message + 1);
		else if (message[0] == 'P' && length > 1) {
			// As the host link does: a request that starts with a flag in idle mode enters PPP, passive
			if (!ppp_running() && message[1] == 0x7E)
				hostlink_ppp_begin(false, 0);
			if (ppp_running())
				ppp_input(message + 1, (size_t)length - 1, 0);
		}
		else
			fail("invalid IPC request");
		emit('Z', NULL, 0);
	}
}
