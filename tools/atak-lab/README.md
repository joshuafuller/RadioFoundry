<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# No-radio CoT multicast path

This host integration check transfers representative CoT XML payloads between two isolated processes running
RadioFoundry's actual IP gateway, DMR packet builder/receiver and 33-byte frame FEC conversion. It adds no firmware
feature, Android plugin, network listener or production API. No radio, USB device, Bluetooth connection or RF is used.

From this worktree's root:

```sh
make -C tools/atak-lab check
```

Requirements are the installed host C compiler, make, `uv` and system Python 3. The runner uses `uv` with the
existing interpreter and standard library; there is no dependency installation. `PYTHON=/path/to/python` can select
another installed interpreter. This is a Linux/WSL host tool using Unix sequenced-packet sockets and subprocesses.

## Agreed test boundary and flow

The test boundary is the host-side UDP destination mapping and firmware Ethernet/DMR packet interfaces. The
test first failed at the missing multicast mapping, then passed after the mapping and two-process fixture were
implemented. The focused command is the only runtime check for this slice.

1. Independently authored position and chat-shaped CoT XML fixtures enter the host mapping as byte payloads.
2. The mapping translates the two supported application destination/port pairs into lab radio groups:

   | Application destination | Radio destination | DMR group |
   |---|---|---:|
   | `239.2.3.1:6969` | `225.0.0.201:6969` | 201 |
   | `224.10.10.1:17012` | `225.0.0.202:17012` | 202 |

3. A constructed Ethernet/IPv4/UDP frame enters `ipGatewayEthernetIn` in the sending process. The two process
   identities are 1001 and 1002, with separate global gateway and packet-reassembly state.
4. The gateway's air callback uses `dmrDataEncodeIP`, `dmrDataBuildPacket` and `dmrDataBurstToFrame`. Full IPv4
   headers, unconfirmed Rate 1/2 data and two preambles are selected; header compression is disabled in this fixture.
5. The memory link carries the burst type and each actual coded 33-byte frame to the other process. Its receiver
   uses `dmrDataFrameToBurst`, `dmrDataRxBurst`, `dmrDataGetIP` and `ipGatewayDeliverIP`.
6. The host consumer checks reconstructed multicast MAC, radio-derived source address, destination, UDP ports,
   TTL 1, IPv4/UDP checksums and byte-for-byte payload identity. The reverse mapping recovers the application
   destination. Both directions are checked for both message types.

No application socket joins or emits multicast in this test. The host adapter operates on datagram arguments;
it is not yet a running ATAK companion app. The addresses are the documented conventions in the
gateway addressing in the [USB network specification](../../docs/spec/usb-network.md), not a promise about every ATAK build.
The chat-shaped fixture is not captured ATAK output, and successful XML parsing does not prove ATAK GeoChat behavior.

## Results and artifacts

| Payload | Bytes | DMR bursts | Modeled air time | Plus one default separate-TX guard |
|---|---:|---:|---:|---:|
| Position fixture | 262 | 28 | 1.68 s | 1.93 s |
| Chat-shaped fixture | 302 | 31 | 1.86 s | 2.11 s |

The virtual clock advances 60 ms per burst. Guard time is reported separately and is not executed by a service
scheduler in this fixture. These are source-derived timing estimates, not observed RF throughput. They correspond
to approximately 1.1 kbit/s of useful payload for these separate transmissions under ideal access conditions.

- `build/atak-lab/results.json`: machine-readable passed result, mappings, packet sizes, burst
  counts and modeled timing for the four successful message/direction combinations.
- `build/atak-lab/ethernet.pcap`: generated sender-input and receiver-output Ethernet frames,
  timestamped by the virtual burst clock, for independent packet inspection. Coordinates are synthetic zeroes.

Additional checks prove that conventional application groups are refused without translation; TCP, IGMP and
marked IP fragments do not cross the gateway; a dropped group-data burst prevents delivery; and the next complete
transmission reassembles correctly. A 558-byte UDP payload reaches the receiving host. The mapping refuses a
559-byte payload and unsupported destination/port pairs without truncation. Mapping is intentionally limited
to the two explicit pairs; this does not forward arbitrary phone multicast.

The receiving gateway emits no new RF frames in response to the decoded group payload. This proves absence of
an echo at that gateway boundary, not loop prevention in a future live app bridge. That bridge will need explicit
direction handling and duplicate suppression when it injects events back into local multicast sockets.

## The serial host link (PPP)

Boards without a USB data port reach the gateway through PPP on the serial host port
([PPP specification](../../docs/spec/ppp.md)). The same check then runs both nodes that way: each "phone" is a
minimal PPP client in `lab.py`, written from RFC 1661, 1662 and 1332 only and sharing no code with the firmware's
engine. Its bytes enter the node's host port in writes of at most 20 bytes, the size of a default BLE write, so a
frame is always split across many writes.

| Step | Checked |
|---|---|
| Link | A request that starts with a flag enters PPP. LCP and IPCP open. The phone asks for `0.0.0.0` and is given `12.<DMR ID>`; the radio is that address XOR 1 and announces MRU 584 |
| CoT both ways | Both fixtures, both directions: phone, PPP, gateway, DMR bursts, gateway, PPP, phone. Payload, ports, source address, TTL 1 and checksums match. Burst counts equal the Ethernet runs |
| Limits | A 556-byte UDP payload (MRU 584 less 28) arrives. A 557-byte one and a frame with a wrong FCS produce no burst |

The mapping takes the link's UDP limit as an argument: 558 on the USB network adapter, 556 on PPP.

Sabotage check: with the node's PPP delivery removed, or with PPP never entered, the run fails at the first PPP case.

## Original implementation versus this lab

| Provided by RadioFoundry | Added for this proof |
|---|---|
| IPv4 routing/filtering, multicast CAI mapping and host delivery | Two isolated host processes and the private IPC link |
| DMR packet headers, CRCs, reassembly and frame FEC | Explicit application/radio destination translation |
| Existing protocol and simulator test interfaces | CoT fixtures, host producer/consumer, boundary checks and capture output |
| The PPP engine and its netif to the gateway | The phone's PPP client and the chunked byte pipe |

The original project already supports host/SIL testing. Our contribution connects selected real modules through
a repeatable harness. This is not a full virtual handset: it does not boot the MCU image, run all FreeRTOS tasks,
model a physical modem or execute the radio's complete host-link/data-service path.

## Evidence limits

The gateway, packet/FEC code, PPP engine and PPP netif are real; the USB driver, the host link's port dispatch,
PPP timers (the clock stays at 0), DHCP negotiation, the air queue, data-service timers,
channel access, voice/PTT scheduling, modem admission, RF loss/interference and repeater distribution are not
executed here. Loss injection drops one coded data burst. It is not a physical RF noise or bit-error model, and
there is no group ACK or automatic retry. The later successful packet is an explicit new transmission.

Android NCM binding, USB routing, multicast socket selection, actual ATAK event/chat consumption, application
segmentation, queue saturation and stale-event policy remain untested. No Bluetooth adapter is modeled: the byte
pipe is ordered and lossless, with no latency, flow control, pairing or reconnect, and nothing shows that a given
radio's Bluetooth module passes these bytes through. Firmware
replay security is not added, and the host PQC/OTAR lab does not encrypt this production gateway path.

The radio needs no ATAK-specific map screen to perform this gateway role. Generic link, group, queue, drop and
error status are the useful radio-side diagnostics; the map and messages belong on the ATAK device. No new radio
screens or settings are implemented by this tool.
