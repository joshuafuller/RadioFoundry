// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Start storage is generated in memory, before app_start, using the production editors. No image files.
// --storage blank (default), dmr-pair, dmr-group, or steps:<edit>;<edit>...; --radio-index selects the member:
// 1|2 for dmr-pair, 1..999 for dmr-group.
// Edits, in order (decimal numbers, no empty fields or escaping; names are 1..16 printable ASCII characters):
//   dmr_id=3100001                 test identities: 3100001 to 3100999 only
//   radio_name=N0SIM               the only fixture callsign
//   channel=1,Simplex,433500000,433500000,dmr,1,1,1
//           number,name,rx_hz,tx_hz,fm|dmr,color_code,timeslot,contact (0 for none)
//   contact=1,Peer,3100002,private,1  number,name,id,private|group,timeslot (0 for no override)
//                                 a private id is a test identity; a group id is talkgroup 9 only
//   zone=0,Pair,1                  index,name,channel[,channel...] (at least one)
//   setting=brightness,70         supported keys: vfo, squelch, zone_index, channel, zone_pos,
//                                 brightness, backlight_s, beeps (settings.h ranges; squelch also 255)
// Channel edits use narrow bandwidth, default power, no tones, and direct mode when RX equals TX.
// The pair selects one simplex channel in its own zone, with a private contact for the other member.
// The group is for N radios on one air: member k is 3100000+k, and every member has the same simplex channel
// (CC 1, TS 1) whose contact is talkgroup 9. A group call is heard when its talkgroup is the channel's talk target
// (docs/spec/dmr-voice.md, "Heard"), so PTT on any member is heard by all the others.
// Example: --storage dmr-group --radio-index 3 is DMR ID 3100003 talking on TG 9.
#ifndef STORAGE_VHOST_H
#define STORAGE_VHOST_H

#include <stdbool.h>

// SHA-256 of every byte, including erased space, through the virtual flash's own read path. False on read failure.
bool vstorage_digest(char out[65]);
// Emit STORE <sha256>. Used at boot and by the END trace hook, including reboot; a read failure ends with exit 2.
void vstorage_checkpoint(void);

#endif
