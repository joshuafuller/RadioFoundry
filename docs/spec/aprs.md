<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# APRS

APRS (Automatic Packet Reporting System) on RadioFoundry: our position, status and messages as AX.25 UI frames in
Bell 202 AFSK on FM; positions over DMR to an APRS gateway ID; automatic beaconing from several trigger sources; a
KISS TNC on the host port (and on TCP port 8001 in USB network mode); the iGate rules, with the APRS-IS connection
held by a host helper or by the radio's own client (`tcp-services.md` 4, 7); and, on radios that can sample their
received audio, a demodulator, a station list, messages to us and a digipeater.

Code: `core/aprs` (portable, host tested), the RF part in `core/radio/radio_sm.c` (the APRS block), the board's tone
path behind `hal/hal_afsk.h`, the UI in `core/ui/ui_aprs.c`, the debug link in `core/debuglink/debuglink_aprs.c`, the
host helper `tools/aprs_gate.py`. Build switches: `CONFIG_APRS` (the engine), `CONFIG_APRS_AFSK` and
`CONFIG_APRS_DMR` (its two ways out, section 2), `CONFIG_APRS_RX`, `CONFIG_APRS_IGATE`, and on the USB network link
`CONFIG_NET_KISS` and `CONFIG_NET_APRSIS` (`docs/build-config.md`). Where our position comes from is the position
service's ([position.md](position.md)); DMR GPS Info and LRRP use it too and need no APRS.

Sources (all public, read for the formats only; no implementation was used as a source, `CLEANROOM.md`):

- AX.25 Link Access Protocol for Amateur Packet Radio, version 2.2 (TAPR, 1998): address field, UI frames, FCS,
  bit stuffing (sections 3, 6).
- APRS Protocol Reference 1.0 (APRS Working Group, 2000), "APRS101", and the APRS 1.1 and 1.2 addenda: data types,
  positions (plain and compressed), Mic-E, status, messages and acks, the third-party format, tocalls, paths,
  position ambiguity.
- The KISS TNC protocol (Chepponis and Karn, 1987): framing, escapes, the command bytes.
- aprs-is.net: "IGate Design", "q Construct", "Connecting to APRS-IS" (login, passcode, filters).
- SmartBeaconing as published by Tony Arnerich KD7TA and Steve Bragg KA9MVA (HamHUD), released for anyone to use.
- BrandMeister's APRS documentation: the location service IDs (the retired 5050-5059 and the MCC999 gateway) that turn Motorola (LRRP) and
  Hytera position reports into APRS positions ("Position reports of Hytera and Motorola radios can be forwarded to
  APRS", wiki.brandmeister.network/index.php/APRS, last edited 2020-07-03).
- Bell 202: 1200 baud, mark 1200 Hz, space 2200 Hz, NRZI (a 0 bit is a tone change).

Black-box oracles (never read for code): Direwolf decodes the WAV that `aprsTest --wav` writes
(`tests/host/aprs_wav_check.sh`) and the radio's transmission on the SDR bench (section 13); APRSdroid, Xastir or
aprx can stand in.

## 1. What each radio does

| Feature | Needs |
|---|---|
| FM AFSK transmit | `CONFIG_APRS_AFSK` and `board.afsk->tx_*` (5.1) |
| FM AFSK receive, station list, digipeater, messages to us | `CONFIG_APRS_RX` and receive audio: `board.afsk->rx_*`, or FM receive audio on the PCM stream (`AUDIO_TAP_FM_RX`) (5.3) |
| Positions over DMR (LRRP / UDT) | `CONFIG_APRS_DMR`, on a DMR channel |
| KISS TNC on the host port (UDP 3334 and TCP 8001 in network mode) | `CONFIG_APRS_AFSK` (TX path; RX frames only with RX) |
| iGate | RX for RF -> IS; IS -> RF messages only once RX exists |
| Position | the position service ([position.md](position.md)): the board GPS, host NMEA or the fixed position |

What a board provides:

| Path | AFSK transmit | AFSK receive | GPS |
|---|---|---|---|
| RF chip with a tone generator (AT1846S) | The tone generator retuned per symbol | FM receive audio on the PCM stream (`AUDIO_TAP_FM_RX`, 5.3) | `CAP_GPS` |
| HR_C7000 | 8 kHz PCM through the modem's prompt FIFO into the FM modulator ([modem-hrc7000.md](modem-hrc7000.md) 8.4) | The modem's FM receive audio, ungated (8.5) | `CAP_GPS` |
| No tone path written | None (DMR positions still work) | None | `CAP_GPS` |

[feature-matrix.md](../feature-matrix.md) shows which radios have AFSK transmit (`APRS`) and receive (`APRS_RX`).

Everything is host tested. On the SDR bench (section 13): positions over DMR (LRRP and UDT) decode, and FM AFSK
decodes with a valid FCS.

## 2. Model

The engine (`core/aprs/aprs.c`) runs in the radio task: `aprs_tick` from `radio_sm_poll` every 25 ms. The UI, the
debug link and the host link reach it through the "any task" calls, which take a lock the radio task sets up
(`aprs_set_lock`, the scheduler suspended for short copies). Settings and codeplug reads take the storage mutex and so
happen outside that lock.

The engine sends through two backends. Each has its own switch and works without the other:

| Part | Switch | File | Holds |
|---|---|---|---|
| Engine | `CONFIG_APRS` | `aprs.c` | the settings, the beacon triggers, the view, "Send position now" |
| AX.25 on FM | `CONFIG_APRS_AFSK` (needs `APRS` and `FM`) | `aprs_ax25.c` | the callsign, the frame queue and the AFSK jobs the radio core sends, status and messages, the KISS TNC and APRS-IS lines, receive |
| Positions over DMR | `CONFIG_APRS_DMR` (needs `APRS` and `DMR`) | `aprs_dmr.c` | the LRRP or UDT report to the gateway ID (section 8); turns on the DMR data core |

- `CONFIG_APRS=y` needs at least one backend; the build stops otherwise.
- `CONFIG_APRS_RX`, `CONFIG_APRS_IGATE`, `CONFIG_NET_KISS` and `CONFIG_NET_APRSIS` need `CONFIG_APRS_AFSK`.
- The halves share one state block (`aprs_int.h`). The engine calls the AX.25 half through `aprs_ax25_*`, which are
  empty inline functions when it isn't built. Calls other modules make into it (`aprs_send_message`,
  `aprs_rf_pending`, `aprs_host_frame`, ...) answer "nothing there" the same way, so callers need no switch.
- With one backend built, "Send via" is not asked: a beacon goes the way there is, whatever the setting stored by
  another build says. The settings record is the same in every build.
- Without the AX.25 half: no callsign, APRS frequency, path, messages or station list; the radio core has no APRS
  RF job; the host port takes NMEA lines only (`$...`, no KISS decoder).

Pure parts, each with host tests in `tests/host/aprsTest.c`: `ax25.c` (addresses, frames, TNC2 text), `hdlc.c` (FCS,
bit stuffing, NRZI symbols, the receive deframer), `afsk.c` (the tone plan for TX, a correlator demodulator with a PLL
for RX), `kiss.c`, `aprs_fmt.c` (APRS formats in and out), `aprs_beacon.c` (when to send),
`aprs_igate.c` (gating and digipeater rules). `tests/host/aprsRadioTest.c` runs the engine with the radio core on a
fake board whose AFSK output is demodulated back to frames, in a build without DMR; `radioCoreTestAprsDmr` runs it
with the DMR backend alone.

## 3. Rules

- Transmit is off by default. Automatic beacons need Tools > APRS > Settings > Automatic beacons; "Send position now" works
  without it.
- A callsign is required: the debug link's test call, else Settings > My radio's radio name, else the codeplug's
  radio name; its first word, upper case, 3-6 letters and digits with a digit after the first character, not N0CALL.
  Our SSID comes from the APRS settings (7 by default). No callsign: nothing goes out, the page says why.
- RF only on amateur frequencies: the APRS frequency must be in 144-148, 222-225, 420-450 or 902-928 MHz and in a
  band the board transmits in; else the reason is "not amateur" and nothing is keyed.
- A hard minimum of 30 s between automatic beacons (`AUTO_MIN_S`), 10 s for any configuration
  (`APRS_MIN_INTERVAL_FLOOR_S`), and "send now" pressed again within 5 s is ignored.
- Frames from the host (KISS) must carry our callsign (any SSID) as the source and are rate limited: a burst of 3,
  then one per 10 s.
- CSMA before every RF transmission (section 6); our own PTT always wins (an APRS transmission is aborted, the
  channel restored).
- iGate transmit (IS -> RF) only for messages to stations heard directly in the last 30 minutes (section 9.1).

## 4. Frames

### 4.1 What we send

- Position: `!` (or `=` when messaging is on), plain `DDMM.mmN/DDDMM.mmW` with the symbol, or compressed (base 91,
  the default), course / speed when known, altitude `/A=` when known, then the comment (the codeplug APRS record's
  comment if any). Position ambiguity 0-4 blanks digits as APRS101 chapter 8 says.
- Status: `>text` (40 characters).
- Messages: `:ADDRESSEE:text{NN` (67 characters), acks `:ADDRESSEE:ackNN`; ours retried at 30, 60 and 120 s
  (3 tries) until the ack. The composer can omit the message number (one transmission, no retry or acknowledgment)
  and append a position snapshot as ` @DD.DDDDN DDD.DDDDW` in the message text. Both are per-message choices; requesting
  an acknowledgment is on and including position is off when the composer opens. A position needs a current GPS, host
  or fixed source. Its text counts toward the 67-character limit; a message that cannot fit is refused.
  The send API accepts printable ASCII text and rejects control bytes. An incoming `rej` stops retries without
  reporting the message as acknowledged.
- Messages > New APRS message appears when APRS is built and our callsign is set. A station in Stations heard has a
  Message action that fills its callsign. The composer asks for an AX.25 callsign with optional SSID, then sends a
  localized preset or typed text through `aprs_send_message`. Presets fold accented letters to printable ASCII; typed
  text holds at most 67 characters. The callsign editor holds at most nine characters (six for the call, a hyphen and
  up to two for the SSID). Received APRS messages appear in Messages; replies use APRS. DMR replies use DMR, and
  Settings > Connections > Message format selects only a DMR encoding. Receive builds keep up to three unfinished outgoing messages in FIFO order; a full queue refuses the new send
  without replacing accepted work. Builds without receive keep one slot and refuse a send while it is active.
  `aprs_send_message` also refuses a message when the RF path is blocked.
- Path: none, WIDE1-1, WIDE1-1,WIDE2-1 (the default), WIDE2-1, WIDE2-2. Set on the APRS page (section 10), over the
  debug link, or in the codeplug's APRS record (section 11).

### Message threads (receive builds)

`aprs_thread.c` keeps `APRS_OUTBOX` outgoing records in main RAM (default six), including at most three queued or active messages.
Boards may reduce retained history to three records without reducing the send queue.
When full, only the oldest completed record can be evicted. The existing four-entry incoming inbox is unchanged.
Both stores are volatile. `CONFIG_APRS_RX=n` removes the thread storage, queue extension and screen.

Messages shows one entry per callsign, newest conversation first, merged with the DMR inbox by age. Selecting an
APRS entry opens `aprs-thread`. Station detail's Message action opens that thread when retained messages exist;
otherwise it opens the composer. Up/Down scrolls each full message, then moves to the next or previous message in
chronological order. The reader uses the display class body font with a persistent callsign title.
OK opens the existing composer addressed to the saved callsign. Context (#) cancels a queued or sending
message, or retries a No ack or Rejected message with the retained recipient and text. Cancel removes the
outgoing record and prevents further retries; an RF frame already on air finishes. Retry resets the attempt
count and requires a free outgoing queue slot. Stale action snapshots are refused. Opening or drawing the thread marks only that callsign's
incoming messages read, without adding messages or issuing a new alert. Duplicate reception retains the read flag.

Each outgoing record shows Queued, Sending with try count and next retry delay, Delivered (acked) with the ACK's
age, No ack after the final try, or Rejected. Sending counts attempts submitted to the RF queue; channel access
and lower-level RF retries may delay the next attempt beyond the displayed countdown. Delivered requires an ACK
from the addressed callsign with the matching message number. It does not establish that a person read the text.
No ack means no matching ACK was observed, not proof of nonreceipt. A message sent without requesting an ACK shows
Sent, no ack requested after the RF job succeeds. ACK and rejection retain the completed record and release the
next queued message after outstanding RF frames finish. The existing retry intervals are unchanged.

Received addressed-message banners show the sender callsign and inbox text; # opens that conversation.
Routine arrivals show the packet source and event kind. Bulletin and object prose wraps at spaces to the
class body width; all retained text remains reachable by scrolling. A token wider than a line is split
without dropping bytes.

### 4.1.1 Arithmetic

No libm: the 32-bit math library costs flash on every board and nothing here needs it.

| Value | Method | Accuracy |
|---|---|---|
| Compressed speed byte `round(ln(knots + 1) / ln 1.08)`, at most 89 | table of the lowest speed for each byte value | the formula's byte for all 65536 speeds (the nearest tie is 0.0007 of a step away) |
| Compressed speed read back `1.08^k - 1` | squared table, 32-bit mantissas | the formula's knots for every byte |
| Compressed altitude read back `1.002^n` feet, as meters | the same table method, Q24 | within 1 m |
| Distance (haversine, 6371 km) and bearing | float polynomials (sine and cosine to the 13th and 12th power, arctangent to 1e-5 rad); past a quarter of the globe the arc to the antipode, subtracted from pi | distance under 1 m below 1 km, 14 m to 100 km, 150 m beyond; bearing within the rounding to a whole degree for stations 20 m or more away |

The bearing uses `x = sin(dp) + 2 sin(p1) cos(p2) sin(dl / 2)^2`, which has no cancellation, so a station tens of
meters away gets a true bearing. The longitude difference is wrapped to the short way round first.

### 4.2 Tocall

Destination `APZRDF`: the APZ block is the experimental range anyone may use. A registered tocall (APxxxx from the
tocalls list) is an open decision.

### 4.3 Received (RX builds)

Positions (plain, compressed, with and without timestamps), Mic-E (the destination field's latitude and message bits,
longitude, speed and course from the information field), objects, items, status, messages, third-party packets,
queries and telemetry recognized by type. Weather is decoded when `CONFIG_APRS_WX` is enabled (4.4).

### 4.4 Weather

`CONFIG_APRS_WX` needs `CONFIG_APRS_RX`. `aprs_wx.c` decodes positioned reports with symbol `_`
(plain or compressed, with or without a timestamp) and positionless `_` reports with an eight-digit MDHM
timestamp. Field definitions and fixtures follow [APRS101 chapter 12](https://www.aprs.org/doc/APRS101.PDF)
and the [weather clarifications](https://www.aprs.org/aprs11/spec-wx.txt).

| Field | Stored unit |
|---|---|
| Wind direction | Degrees clockwise from north |
| Sustained wind and gust | mph; compressed position wind is converted from knots |
| Temperature | Signed degrees Fahrenheit |
| Rain last hour, last 24 hours, since midnight | Hundredths of an inch, independent values |
| Humidity | Percent; wire `h00` means 100% |
| Pressure | Tenths of hPa, displayed as inHg |
| Luminosity | W/m²; `L000`–`L999` and `l000`–`l999` cover 0–1999 |

Omitted fields, dots, spaces, invalid numbers and incomplete fields are unknown, never measured zero. A new
report replaces all weather fields; absent fields do not inherit old measurements. The parser stops at the
software/unit suffix. Weather wind never becomes station motion. Object weather is parsed but not stored under
the publishing station's identity.

`aprs_wx_store.c` holds the latest report per callsign/SSID in ordinary main RAM, separate from the engine's
CPU-only RAM section. `APRS_WX_STATIONS` is a board-overridable capacity (default four). Updates move a station to
the front; a full table replaces the oldest report. Ordinary station traffic neither evicts weather nor refreshes
its receipt time. A positionless report uses a position already retained in the station list, if available.
The table is cleared at startup. With the switch off, no weather table, decoder or screens are linked.

Tools > APRS > Weather opens a list ordered newest first. Sort toggles nearest; unknown distances follow known
ones, with newest first for ties or when our position is unavailable. Row identities are captured on entry or a
sort change so incoming packets cannot redirect a selection. Reopen or change Sort to refresh the list.
Details follow the selected callsign and show temperature, sustained wind, wind direction, gust and report age,
then the three rain periods, humidity, pressure and luminosity. Unknown measurements have no row; measured zero remains visible. Report age is time
since receipt on this radio, not an inferred observation timestamp. Values follow the shared Settings > Display > Units setting (US by default). All fields are scrollable on monochrome displays.

### 4.4 Objects, items and bulletins

Receive builds keep `APRS_OBJECTS` objects/items and `APRS_BULLETINS` bulletins (default eight each) in ordinary main-RAM `.bss`, outside the engine's
CPU-only state. `CONFIG_APRS_RX=n` removes both stores and their screens. Each list is newest first and replaces
its oldest entry when full; all entries are volatile.

- An object/item is keyed by its name. A new report replaces its owner, position, symbol, comment (up to 67 ASCII
  characters), live/removed state and receipt time. A publisher's own position and comment are unchanged.
  A kill remains visible as **Removed** for ten minutes after receipt; a live entry expires after 24 hours.
  A later live report revives it. This follows the named-entity replacement rule in the
  [APRS errata](https://www.aprs.org/errata.html).
- The object detail retains the selected name and owner while incoming reports reorder the list. Eviction or
  ownership change shows **No longer in list**. Distance and bearing use the existing position service and range
  formatter. A `ddd.dddMHz` token supplies the frequency row. With `CONFIG_APRS_TUNE`, valid frequency
  extensions in the full packet or object name offer the same Tune review as stations.
- Bulletin addresses are `BLN` plus a digit, announcement addresses `BLN` plus an uppercase letter, optionally
  followed by a group name. `NWS-*` messages share this list. Sender plus full addressee identifies a bulletin slot.
  Changed text replaces that slot and queues one banner. An identical repeat neither refreshes its receipt time
  nor alerts again while retained. Entries expire 24 hours after the last text change.
- A new bulletin waits for the existing banner to clear, then uses an informational banner. The **Bulletins**
  screen shows group, sender and age; OK opens its retained text. It offers no reply or acknowledgment action.
  Bulletin addresses bypass personal-message processing even if they match the configured callsign.
- One third-party envelope is decoded for these records; its inner sender owns the object or bulletin. Neither
  the gateway nor the publisher acquires the object's coordinates.

The formats follow [APRS101](https://www.aprs.org/doc/APRS101.PDF), chapters 11 and 14. The existing parser supplies
positions and message text. Long comments and bulletin text are split into scrollable rows using the current list
widget, including on monochrome displays. No storage writes or automatic response are introduced.

## 5. The radio side

### 5.1 hal_afsk

`hal/hal_afsk.h`: `tx_start(tones, nbits, baud, mark, space, level)` plays a symbol string (one bit per symbol,
1 = mark), `tx_busy`, `tx_stop`; `rx_rate_hz`, `rx_start`, `rx_read`, `rx_stop` deliver received audio samples.
`board.afsk` is NULL on boards without a tone path. A board that can only receive sets the `rx_*` members and leaves
`tx_start` NULL. Without a transmit path the reason is "no AFSK" and only DMR positions go out.

### 5.2 Transmit

The radio core keys the transmitter in FM, then the board switches the tone at each symbol boundary (a timer at the
baud rate), with phase continuity where the hardware allows, and keeps the RTOS out of its symbol interrupt. While
AFSK runs, the radio core leaves the transmitter alone (`status()` returns its cached reading). The level (0-100, 50
default) sets the deviation around the board's nominal (about 3 kHz peak at 25 kHz bandwidth). `tests/hil/test_aprs.py` measures on air the phase continuity across a tone change, the deviation the level
gives, the mark / space flatness and the TX delay (section 13). Each board's tone path is on its hardware page (the table in
section 1).

### 5.3 Receive

Accepted AX.25 packets also enter a fixed recent-packet ring in main RAM, separate from the CPU-only engine state.
Our own packets are excluded. Presentation deduplication uses the existing 16-slot source/destination/information
hash cache for 30 seconds, ignoring the digipeater path and trailing whitespace. It does not suppress message ACKs,
iGate processing or the station table's updates. The ring holds `APRS_RECENT_PACKETS` entries (default 8; a board
can override it in its build definitions). Each stores receipt time, source, parsed kind, direct/digipeated state,
parsed position/course/speed, object/item name, message addressee, and the first 40 information bytes with length
and a truncation flag. No pointer into a decode buffer is retained. It is cleared at initialization and never
written to flash. All storage and accessors are behind `CONFIG_APRS_RX`.

Received FM audio comes from one of two places (`aprs.c`, `rx_service`):

1. The board's own AFSK receive path: `hal_afsk_t.rx_start`, `rx_read`, `rx_stop` at `rx_rate_hz`. The HR_C7000 boards
   have one (shared `hrc_afsk`): the modem's FM receive audio at 8 kHz, not squelch gated
   ([modem-hrc7000.md](modem-hrc7000.md) 8.5). While it runs the
   speaker plays FM through the HR_C7000 instead of straight from the RF chip.
2. Otherwise FM receive audio on the board's PCM stream, when its audio tap has `AUDIO_TAP_FM_RX` (`hal_audio_tap.h`):
   8 kHz, de-emphasized, squelch gated. An HR_C6000 puts it on I2S; that stream exists only while the FM
   feed-through is on, so the volume knob at zero mutes the amp but leaves the feed-through and the RX gain alone. The
   shared stream (`core/usbaudio/audio_mux.c`) gives APRS a ring of its own (1024 samples, 128 ms) next to a USRP
   session's, keeps the stream running while APRS asks for it, and drops whole 20 ms blocks when the radio task reads
   too late.

The path runs while the receive setting is on, the radio receives FM on the APRS frequency (`aprs_cfg_freq`: the
region's, or the custom one) and no APRS transmission is under way. Anywhere else the stream is left alone: a
repeater channel's audio isn't APRS, and battery saving would be blocked for nothing. While it runs, battery saving
(`radio-features.md`) stays off: a sleeping receiver misses packets, and the squelch closes only after 300 ms of
noise readings over the line (`RADIO_SQL_CLOSE_POLLS_APRS`) instead of 50 ms. A start the board's path refuses (no
memory, its audio busy) is tried again 1 s later (`RX_RETRY_MS`), not on every tick, since each try routes the
receiver and back; `aprs.rx_start_fails` counts them. On air, a packet's tones
at full deviation pushed the AT1846S noise reading over the line for a poll or two, the squelch closed, and the
squelch gated audio cut the packet in pieces: the preamble in one opening, the frame in the next. `aprs_rx_listening()`
says whether the path runs.

The receive setting and messaging default to on when the radio can receive (`aprs_can_receive()`: the build has
`CONFIG_APRS_RX` and one of the two paths exists).

The demodulator (`afsk.c`): 8-48 kHz samples, a correlator per tone with hysteresis and a PLL that tightens once a
flag is seen. Integer arithmetic only: the float version's soft-float square roots and divisions took most of the
CPU of an HR_C7000 board (no FPU) at 8 kHz.

Two decision paths run side by side on the same samples, each with its own correlators, clock and HDLC decoder: one
on the audio as received, one after a high pass (two first-order stages at 700 Hz). A frame either path finds is
delivered once: the second copy (same length and FCS within a quarter second) is dropped. The correlators span one bit,
so their passbands are wide, and audio under the tones leaks into the mark correlator. A receiver on an HR_C7000 board getting a flat
transmitter at a strong level had 320-400 Hz 1-10 dB under the tones in its receive audio and decoded 50-80 % of
the frames with one path; with both, 12 of 12 (measured). The high pass alone costs weak signals (its phase
shift between the tones moves the tone edges), which is why the received path stays. Both paths together take about
6 % of that CPU at 8 kHz.

Host sweep (`aprsTest --sweep`, frames decoded of 100, noise over the full band):

| Audio | 10 dB SNR | 12 dB | 14 dB | 20 dB, 350 Hz 3 dB under the tones |
|---|---|---|---|---|
| Flat | 96 | 100 | 100 | 100 (one path: 7) |
| De-emphasized (flat transmitter, 750 us receiver) | 98 | 100 | 100 | 99 (one path: 57) |
| Pre-emphasized (pre-emphasizing transmitter, flat receiver) | 11 | 51 | 76 | 92 (one path: 0) |

Squelch gated audio loses the start of a transmission until the squelch opens; the sender's TX delay (flags before
the frame) covers that. The debug link's `aprs.rx_peak`, `rx_bits` and HDLC counters
tell missing audio from a demodulator that finds no frame.

A possible later step on an HR_C6000 board: its RX RAM carries the same audio ungated so packets
below the squelch could be decoded. That needs a board receive path (1 above) reading the RAM 31 times a second.

## 6. RF transmission

The radio core's APRS block (`radio_sm.c`, `aprs_*`): when a frame waits (`aprs_rf_pending`) and the radio is idle
(no PTT, no call heard or sent, no scan, no hotspot, no monitor, no TX inhibit, battery not critical), it takes the
job (`aprs_rf_take`: our queued frames, 1 flag between them, a TX delay of flags before, 3 after), and:

1. LISTEN: tunes the APRS frequency (25 kHz, or 12.5 kHz when narrow is set), 50 ms to settle, then 100 ms slots with
   p-persistence 64/256 while the squelch is closed; gives up after 10 s (`rf_busy`, retried later).
2. KEYUP: keys the FM transmitter on the APRS frequency at APRS's own power level when one is set (`aprs_power`:
   n = the board's level n - 1), else at the radio's current level (a temporary change included, as for PTT).
   `aprs.tx_power` on the debug link is the level it will use. 25 ms.
3. SEND: `board.afsk->tx_start`; at most 6 s.
4. Back: transmitter off, the user's channel retuned exactly as it was.

PTT, a channel change or any request other than beeps, DMR focus and settings aborts it (`rf_failed`, the frame
retried after 15 s, 3 tries). A failure counts in `rf_failed` and names its step in `rf_fail`: `aborted`, `no_key`
(the transmitter didn't key), `no_afsk` (`tx_start` refused), `timeout` (the tones didn't finish in 6 s). The settings save waits while a job is pending.

## 7. Position and triggers

The position is the position service's ([position.md](position.md)): its source setting and the fixed position
are in Settings > Location and serve DMR GPS Info and LRRP as well. The engine reads
`position_fix` every tick; GPS and host fixes carry speed and course. Host NMEA (`$GPRMC`, `$GPGGA` with valid
checksums) comes from KISS SetHardware 'N' or a bare `$` line on the host port.

Triggers (`aprs_beacon.c`), all under the hard minimum:

| Trigger | Default | |
|---|---|---|
| Interval | 10 min when beacons are on | 0 = none |
| SmartBeaconing | on | slow 5 km/h -> 30 min, fast 90 km/h -> 3 min, linear in between; corner pegging: a turn sharper than 28 degrees + 400 / speed sends after 30 s |
| Distance | off | after moving N meters |
| After PTT | off | after our own transmission (the user's PTT, not a DMR data burst list), if the last beacon is 2 min old |
| Manual | | "Send position now" (UI, debug link) |
| Boot | off | the first position after power-up |

SmartBeaconing always replaces the fixed interval and decay. Without speed it uses the slow rate (30 min),
with no turn trigger. Interval and Decay are dimmed with “Smart on”; selecting them explains that SmartBeaconing
sets timing. Stored values remain available when SmartBeaconing is turned off.

### 7.1 Decay and proportional pathing

`CONFIG_APRS_PATHING` adds `aprs_pathing.c`; its state is in main RAM. Decay defaults off. With SmartBeaconing off,
each accepted beacon doubles the next interval, capped at the 30-minute net cycle. An exact change to our latitude,
longitude or codeplug comment resets the interval to its base. The first beacon uses the base interval. Interval Off
stays off. A timing-policy change resets decay; re-enabling automatic beacons also starts the interval from now.
The minimum interval, position prerequisite and RF busy checks still apply. Acceptance means handed to the existing
transmit queue, as for the other triggers; a failed enqueue does not advance decay or the path sequence.

Path adds **Proportional**, after WIDE2-2. Its base interval is one minute, shown by a dimmed Interval row with
“Path: 1 min”. Automatic beacons cycle Direct, WIDE1-1, Direct, WIDE1-1,WIDE2-1: one packet per slot, not an
additional packet for each path. Decay stretches those slots when enabled; SmartBeaconing replaces their timing
while retaining the path cycle. Manual beacons use Direct and do not advance that cycle. Messages and status use
the two-hop path. The cycle resets on a policy change or restart, without catch-up bursts.

This follows the mobile sequence in [APRS proportional pathing guidance](https://www.aprs.org/fix14439.html).
The 30-minute cap and unchanged position/comment criterion are RadioFoundry's decay policy. The engine and its
view use the same effective interval, tested with a simulated clock including millisecond wrap.

Decay uses `SETTINGS_APRS2_DECAY` in the existing flags byte; proportional is an appended path enum value, so old
settings retain their meaning. A proportional policy is saved only in radio settings: codeplug paths remain literal.
A newly changed codeplug path still replaces the local choice. Builds without this feature keep stored choices but
use the ordinary interval and the two-hop path; they show no Decay row or selectable Proportional option.

## 8. Positions over DMR

Built with `CONFIG_APRS_DMR`. Send via (AFSK / DMR / Both; DMR always in a build without the AX.25 half): DMR
positions go out when the radio is on a DMR channel with the call stack running,
as a private data call to the destination ID (default 310999). BrandMeister's APRS gateway is a private ID
"MCC999", with MCC replaced by the master's MCC (310999 for US masters, 204999 for a Dutch one). 310999 works from any master; your own master's prefix
(`<MCC>999`) is recommended. The old 5057 was
retired on 2019-03-01 (<https://news.brandmeister.network/gps-5057-destination/>,
<https://wiki.brandmeister.network/index.php/APRS>). A radio that already saved its APRS settings keeps the stored
value: run `aprs set dmr_dst <MCC>999` (US: `aprs set dmr_dst 310999`).

- Motorola LRRP triggered location report (the default): UDP port 4001, built by `dmrLrrp.c`.
- ETSI UDT location (`dmr_udt` on): `dmrDataBuildUDTLocation`. BrandMeister forwards the position reports of Motorola
  (LRRP) and Hytera radios to APRS, so use LRRP, not UDT, for BrandMeister.
- The position in either report has the ambiguity setting applied (`position_blur`, [position.md](position.md) 3), as
  the AX.25 packets and the other DMR reports do.

### 8.1 LRRP details

An unsolicited report carries request ID 00000001 (no request came). BrandMeister's gateway accepts it with no ARS
registration. Checked on master 3102 through a hotspot: each report to 310999 appeared on APRS-IS as
`<callsign>-7>APBM1D,<callsign>,DMR*,qAR,<callsign>:=<lat>/<lon>[<name> D` (SSID and comment from the BrandMeister
profile, tocall APBM1D).
The retired 5050-5059 IDs chose the SSID and icon; whether MCC999 does is unverified (HIL step, section 13). The APRS settings
on BrandMeister's self care page set the comment.

### 8.2 Positions heard over DMR

Some radios of other makes send their DMR APRS position as an NMEA sentence over UDP port 10110 (compressed UDP/IP
header, SAP 3), as a private data call or to a talkgroup, usually to the APRS gateway's ID (BrandMeister's <MCC>999,
e.g. 310999). As on FM, where every station heard goes into the list, the data service hands each one heard on the
channel to the radio core whoever it is addressed to (only control station passthrough, our own repeated back and a
compressed header for a host behind the radio, DAID 1, are left out; `dmr-data.md` 3; the stock radio sends DAID 0).
The core parses it with `nmea_parse_unchecked` and puts a position with a fix into the station list through
`aprs_dmr_heard`, as a caller's GPS Info. The packet has no callsign and a data call no talker alias, so the station
is named by the DMR ID database (`dmr_iddb_lookup`: its callsign, the record's first word) when the ID is in it,
else by the DMR ID. BrandMeister names the same report on APRS-IS by the callsign registered for the ID, with the
SSID set in SelfCare; no SSID is made up here. What one stock radio sent on air:
`$GPRMC,...,3000.0000,N,8700.0000\0,W,...,A` with no `*hh` checksum, a NUL before `,W`, a NUL from the pad after the
sentence, and the longitude with 4 integer digits. The checksum is optional here (the packet's CRC-32 covered it,
but one that is given is checked), NULs are skipped, and the coordinates are read as numbers, so 8700.0000 is 87
degrees (anything past 90 / 180 degrees, or an integer part too long to scale, is refused, as in `nmea_parse`). Time
and date are not used. Like a caller's GPS Info, it does not depend on the APRS receive setting (`rx_on`, which is
the FM decoder's). Host tests: `aprsTest` (`test_nmea_unchecked`: input classes, the 90 / 180 degree, 60 minute,
32-bit and length edges, a seeded fuzz and a metamorphic check against the strict parse), `dmrDataServiceTest`
(`testNmeaPosition`: the packet heard on air rebuilt with placeholder IDs, its blocks and CRC-32 matching the ones
heard; the packet classes, ports and payload lengths) and `radioCoreTestAprs` (`test_dmr_aprs_position`: through the
radio core into the station list).

## 9. Host port: KISS TNC

With Tools > APRS > Settings > KISS on USB, the host link claims KISS frames (0xC0) and `$` lines on the USB serial port (and on
UDP port 3334 in USB network mode, where the host port runs over the network). The host link's TNC mode
(`HOSTLINK_TNC`) holds while frames flow and ends after 30 s idle, like the hotspot and CPS modes.

Port 0 only. DATA frames are checked (section 3) and queued for RF; TXDELAY, PERSIST and SLOTTIME are accepted and
ignored (the engine's own CSMA applies); SetHardware 'G' carries an APRS-IS line, 'N' an NMEA sentence. To the host:
received frames (RX builds) as KISS DATA, and lines for APRS-IS as SetHardware 'G'.

### 9.1 iGate

The rules are in firmware (`aprs_igate.c`, after aprs-is.net "IGate Design" and "q Construct"); the APRS-IS TCP
connection is in the host helper `tools/aprs_gate.py`, which logs in with the callsign and passcode, applies a
filter, and passes lines both ways as KISS SetHardware 'G' frames (serial port or UDP 3334), or, in USB network mode,
in the radio's own APRS-IS client (`CONFIG_NET_APRSIS`, `tcp-services.md` 7), which carries the same lines.

- RF -> IS: never a packet whose path holds TCPIP, TCPXX, NOGATE or RFONLY, nor a query; a third-party packet is
  gated as the packet it carries unless that one came from the Internet; the line gets `,qAR,<our call>`; each
  packet once per 30 s.
- IS -> RF (`igate_tx`, off): only messages and acks, to a station heard directly in the last 30 minutes, from a
  station not heard on RF in the last 10 minutes, not seen in the last 30 s; sent as `}SRC>DST,TCPIP,OURCALL*:info`.
- Our own beacons to APRS-IS (`to_is`): `CALL>APZRDF,TCPIP*:info` over the same channel.
- Digipeater (RX builds, off): fill-in (WIDE1-1 only) or wide (WIDE1-1 and WIDE2-N), also our own call in the path;
  our call inserted before the WIDE entry, marked used (traced), the hop count decremented; duplicates suppressed
  for 30 s.

## 10. UI

Tools > APRS is a status/task hub. Its header shows callsign-SSID, own position source, TX state and path.
Send position now is the initial selection on every display class. Local TX and matching-repeat evidence
precede secondary counts. The fixed task numbers are 1 Stations, 2 Messages, 3 Bulletins, 4 Weather,
5 Objects, 6 My beacon, 7 Recent packets, 8 My position (Location settings), and 0 APRS settings.
Unbuilt tasks are omitted without changing other numbers. A new packet from a station already viewed is not a
new station. The bounded viewed-call list is volatile.

Policy controls live on `aprs-settings`: automatic beacons, frequency, power, path, interval, SmartBeaconing,
output, decay, host port and voice-frequency inclusion, as supported by the build.
A changed configuration or comment is held in UI memory until `aprs-review` completes.
Its five pages show encoding precision/ambiguity, voice inclusion, current source/age, effective timing, and Save.
Up/Down scrolls whole-word wrapped body text on every display class. OK advances, then commits on the final page.
Back or Home cancels. Comment reviews save only the comment; other reviews save the proposed APRS configuration.
The position view exposes the position service's ambiguity even without a fix. Fixed-position entry age is unknown.
Smart timing describes the engine's 5/90 km/h thresholds, 1800/180 second rates, interpolation and turn rule;
interval and decay are overridden. The amateur-band block and engine send eligibility remain unchanged.

Settings > Display > APRS line enables APRS status on Home (default off, stored in `knob_flags` bit 1).
It occupies up to two existing activity rows below the channel and voice controls, never moving them. Voice
activity, monitor and banners take priority. APRS TX on/off replaces the Home hint while the APRS line is enabled.
A mono APRS arrival preserves the voice target; addressed messages combine sender and preview on one row,
truncated to fit. Other arrivals omit the preview. A short screen keeps only the first line;
large-text mode and dual watch keep their existing layouts. The hub always provides the full status. Listening reflects the actual receive
path: off, unavailable receiver, another channel, busy radio, or failed/busy receiver; position is not required.
A past send does not override a not-listening reason. Station rows without range say No own pos when the station
has a position, otherwise No position.

`aprs_home.c` retains the last successfully transmitted packet's source, destination and complete information
field in main RAM. A received own-source frame never enters the station table. A matching frame within ten
minutes, with a used digipeater callsign (not a WIDE/TRACE alias), publishes a retained repeat event: callsign,
receipt time and incrementing serial. The last used path address identifies the digipeater. A new successful
transmission clears the previous repeat. This is evidence of an RF repeat, not internet or recipient delivery.
The Home status feature and its retained repeat evidence compile out with `CONFIG_APRS_HOME=n`.

My beacon retains its callsign/frequency/position preview. Settings use the existing configuration fields.
Power and Path are dimmed without an RF transmitter; Frequency is dimmed without an AFSK or receive path.
The station list and range calculations are unchanged.

Settings rows depend on the build. Send via appears only with both backends. Without AFSK,
Frequency, Power and Path are omitted, and KISS on USB reads NMEA on USB. Settings digits follow its visible rows;
hub task numbers remain fixed. Position source, fixed coordinates and Send in DMR calls remain in
Settings > Location ([position.md](position.md) 6).

The station list (`ui_aprs_list.c`, `CONFIG_APRS_RX`) starts with Nearest when our position is valid, otherwise
Recent. `*` cycles Recent / Nearest / Callsign. An explicit sort and the filter last for the powered session.
Without a usable range, Nearest puts the station after known ranges; ties use receipt age, then callsign.
Recent compares unsigned elapsed times, including across the millisecond clock wrap. Callsign uses lexical order.
Each list refresh reapplies the sort and follows the focused callsign, including when returning from detail.
The UI stores only callsigns and ordering keys in main RAM and resolves rows through `ui_svc_aprs_station_find`.

`#` (or hold OK) opens the numbered `aprs-filter` menu: All, Weather, Mobiles, Objects, Digipeaters. Weather
matches the last packet's weather type or the retained weather symbol (`_`); Mobiles requires a retained position
with known speed above zero; Objects matches the last packet's object or item type; Digipeaters matches an advertised
digipeater symbol (`#`, including overlays), not a station merely heard via a digipeater. Objects still identifies
publishers in this station table, not separate named entities. These filters use only the retained fields.
The station and weather tables have separate capacity limits. A weather report can remain in Weather after its
sender is evicted from Stations; the station filter does not restore evicted senders.
They never remove table entries or change receive, acknowledgment or gating behavior. The title gives the filter
and a persistent hidden count when rows are filtered. The body shows used/capacity and the own-position source.
When the station table fills, a powered-session counter shows how many oldest records newer identities displaced;
updates to an existing identity do not increment it. Reset to All takes `#`, `1`.
The selected callsign and right-aligned age precede its range and comment preview; remaining identities use the
same age column. Ages over 30 minutes are explicitly marked Old, using position age when available.
Nothing heard yet means an empty table; No matches means a filter hid all retained entries.
If the focused station is hidden or evicted, the list selects its first remaining station; an already open detail
keeps its original callsign. With no matches, the counts remain and the filter menu is still reachable.

Selecting a heard station opens `aprs-station`, a scrollable detail list. The UI retains its callsign in main RAM,
not its row index, and resolves the current station through `ui_svc_aprs_station_find` on each draw. New packets
can reorder the heard list without changing the open station or Message recipient. Message is the first row and
opens its thread if messages are retained, otherwise the composer. The remaining rows show distance and compass
direction when both positions
exist, heard age, Direct or Via digipeater, position age when different from heard age, speed in the selected units and course
when the retained position has them, symbol table and code, the retained comment/status text, and packet count.
Unknown speed and course have no row. Text is limited to the engine's 24-byte field; no digipeater identity or
additional comment text is retained. The retained text wraps into complete scrollable rows. Latitude, longitude
and own-position source follow the packet count for positioned stations. Position age and range repeat alongside
these coordinates so they remain visible together on the smallest display. Detail rows scroll with Up / Down and do not perform actions. If the station
is evicted, the screen says "No longer in list" and Message still addresses the saved callsign. This detail code
and the saved callsign are guarded by `CONFIG_APRS_RX`; the engine storage is unchanged.

On color displays, # on the APRS page opens `aprs-radar` (`ui_aprs_radar.c`, guarded by
`CONFIG_APRS_RX && CONFIG_UI_COLOR`). North is up and the center cross is our position. Three equal range rings use a
1/2/5 times power-of-ten interval of the display unit (Settings > Display > Units: miles or kilometers), rounded up to
include every positioned station. A body taller than it is wide puts the info area under the radar and writes each
ring's range inside the ring beside the north-south axis, in the first of four places no marker is in, the unit on
the outermost one shown; a wide body puts the info area beside the radar and names the outer ring's range in it,
after a ring mark.

| Symbol class | Marker | Color |
|---|---|---|
| Moving stations (primary table `> [ b k v j < ^ s`) | Dot | RX |
| Digipeaters and gateways (`/#`, `/&`) | Diamond | Warning |
| Weather (`_`) | Triangle | Privacy |
| Others | Square | Foreground |

Markers use existing theme colors and have a background rim. Positions older than 30 minutes are dim outlines. The
selected station is drawn last inside a foreground ring. The info area shows its callsign, distance, bearing as a
compass point and degrees, position age ("Old 45m" for an old position), and speed with the compass point it is
heading to when the position has them and the row has room. Its last row names the source of our position after a
cross mark. Up / Down or the encoder cycles positioned stations, wrapping at each end. Selection stays by callsign
through packet reorder and OK opens the same station detail. Eviction shows "No longer in list" without changing the
subject; with no positioned station the area says "No positions yet" and the hint is Back alone. Without our
position, the page says "Set your position to see the radar" and directs the user to Settings > Location. The page
writes no settings and allocates only the selected callsign in main RAM. Mono displays retain the station list.
A new recent packet raises an eight-second non-modal banner with sender, plain packet kind, and distance/direction
when both station and local positions exist. `# Open` opens that sender through the same callsign-based station
detail. New traffic replaces an earlier traffic banner, but never another kind of alert. Addressed messages retain the existing message path. Arrival
banners are dropped during PTT/TX, text or frequency entry, confirmations and emergency screens; starting one of
those operations hides an existing traffic banner. Packets remain in the ring. If several packets arrive between
UI polls, only the newest can raise a banner.

Object comments and bulletin text use the shared UI word wrapper, as message threads do. Lines break at spaces;
a word longer than a line splits only when necessary. Scrolling reaches all retained text.

Objects, Bulletins and Recent packets are operating tasks on the hub.
`Recent packets` opens `aprs-packets`: newest first, callsign, plain kind and receipt age.
Digit picking uses the fixed task numbers above. The empty text is "Nothing heard
yet". When `aprs_rx_listening()` is false the body explains the unavailable receive path, receive setting off, TX,
voice channel, or unavailable audio. Selecting a packet copies its snapshot into main RAM and opens `aprs-packet`;
ring eviction cannot change it. The detail lists kind, age, direct/digipeated state, retained name/addressee and
position fields, with speed in the selected display units, then Raw text. Non-printable bytes and backslash
appear as `\xHH`; rows never split an escape.
A separate `[truncated]` row marks a shortened information field. Up/Down scroll; Back returns to the list.
These screens, banners and their new translations disappear with `CONFIG_APRS_RX=n`. Catalog entries used only
by this feature carry `#. requires: CONFIG_APRS_RX`; the build excludes them from translation tables when off.

With `CONFIG_APRS_BEACON`, My beacon (`ui_aprs_beacon.c`) adds a callsign, symbol name and comment preview,
then five rows: Symbol, Comment, Beacons, Pace and Reach. Symbol offers 40 named choices, with table/code
assignments from the [APRS symbol specification](https://www.aprs.org/symbols/symbolsX.txt); no graphics are stored.
A symbol outside this subset reads Custom and is preserved until a choice is saved. Comment uses the existing
text editor, accepts up to 43 printable ASCII characters and shows the remaining count. OK reviews, including an
empty comment; Back deletes, then cancels when empty. Hold Back cancels and returns Home.
Beacons selects Off or Automatic. Pace selects Smart or a fixed interval of 1, 2, 5, 10 or 30 minutes; choosing
Smart preserves the fallback interval. Reach selects Direct, Local relay, Local then wide, Wide one hop or
Wide two hops or Proportional, in `aprs_path_t` order. These use the existing settings and path-to-codeplug storage job.
Only Beacons changes `tx_on`; editing a symbol, comment, pace, reach or fixed position never enables beacons.
The feature needs `CONFIG_APRS_AFSK`; off, the screens, symbol table and comment editing code are absent.
The common settings record still grows by 44 bytes in every build (132 bytes across its three main-RAM copies),
and the shared translation catalogs retain the new strings when their language is built. The switch therefore
does not remove all shared metadata storage.
New UI buffers and the longer engine comment buffer live in main RAM, without enlarging CPU-only RAM.

The rest (digital destination, ambiguity, iGate, digipeater, fixed altitude) is set over the
debug link (`rfoundry_debug.py aprs set`) and later by qdmr through the native codeplug.

## 11. Settings

`settings_t` (`core/settings/settings.h`), taken only when `aprs_stored` is 1, else the defaults: `aprs_flags`
(TX, SmartBeaconing, compressed, after PTT, boot, KISS, narrow, RX), `aprs_flags2` (iGate, iGate TX, messaging, UDT,
to APRS-IS), `aprs_region`, `aprs_ssid`, `aprs_freq_hz`, `aprs_dmr_dst`, `aprs_interval_s`, `aprs_distance_m`,
`aprs_symbol`, `aprs_path`, `aprs_level`, `aprs_power` (0 = the radio's current level, n = the board's level n - 1),
`aprs_txdelay`, `aprs_misc` bits 0-3 (via, digipeater). The position fields in the same record (`aprs_lat`,
`aprs_lon`, `aprs_alt_m`, `aprs_pos_src`, the DMR GPS Info flag, `aprs_misc` bits 4-7) are the position service's
([position.md](position.md) 5): APRS never changes them, and its save keeps them. The codeplug's APRS record 1 (EEPROM 0x1588,
64 bytes each) gives the SSID, path, symbol, comment and ambiguity when present; its fixed position and frequency
encodings are unknown and not used. Of these the radio takes the comment and the path.

The appended `aprs_comment[44]` and `SETTINGS_APRS2_COMMENT` flag (bit 7, separate from decay in bit 6) hold an on-radio comment override.
Without the flag the codeplug comment remains in use. Saving an empty string sets the flag too, so a cleared
comment does not return after restart. Other APRS saves preserve the flag. The settings layout is identical
across feature builds; builds without My beacon retain the field but use the codeplug comment.

The path lives in both places, and the one changed last wins:

- Codeplug to radio: when the record's path differs from `aprs_cp_path` (the codeplug path last taken, `aprs_path_t` +
  1), it replaces `aprs_path` and becomes the new `aprs_cp_path`. Checked at start-up and whenever the codeplug
  serial changes (a CPS write). A path that isn't one of the five (another callsign, a second entry without a first) is
  left alone.
- Radio to codeplug: a change on the APRS page posts a storage job (`aprs_path_to_codeplug`) that rewrites the record's
  two path entries (callsign space padded, SSID byte) and sets `aprs_cp_path` to match. No record is made when there is
  none: the radio then keeps the path in the settings only. A change over the debug link stays in the settings.

Defaults: TX off, region N America, SSID 7, symbol `/[`, path WIDE1-1,WIDE2-1, compressed, interval 10 min,
SmartBeaconing on, level 50, TX delay 300 ms, via Radio, destination 310999; messaging and RX only on boards with a
receive path.

## 12. Size

Boards may bound volatile history with `APRS_STATIONS` (default 12), `APRS_RECENT_PACKETS` (8),
`APRS_WX_STATIONS` (4), `APRS_OBJECTS` (8), `APRS_BULLETINS` (8), and `APRS_OUTBOX` (6, minimum 3).
These limits change retention, not the available screens or the three-message outgoing queue. The four-message
incoming inbox is unchanged. Board overrides live in `board.mk`; per-radio limits are on the hardware pages.
No extra history is placed in CCM or `RFOUNDRY_CPU_RAM`.

The radar draws its empty state separately from the plot so station snapshots and plotting locals do not stay
on the stack while the list renderer runs. It keeps only the selected station's timestamp while scanning other
stations. Firmware CI checks the resulting task call chains; host UI tests cover both radar paths.

The flash and RAM cost of `CONFIG_APRS` and `CONFIG_APRS_RX` per radio is in [feature-matrix.md](../feature-matrix.md).
`CONFIG_DMR_GPS` costs about 2.2 KB flash. A build with `CONFIG_APRS_AFSK=n` (positions over DMR only) leaves out
AX.25, the tone plan, KISS, messages and the receive path.

## 13. Tests

Host: `make -C tests/host` runs `aprsTest` (spec vectors: AX.25, FCS, stuffing, KISS, every APRS format, NMEA, the
triggers, the iGate rules, a modulate / demodulate loop; `--sweep` prints the decode rate against SNR, `--wav DIR`
writes `aprs_test_44k1.wav` with three frames) and `aprsRadioTest` (the engine with the radio core: beacons on the
air decoded back, the minimum interval, CSMA on a busy channel, PTT cutting a transmission, the not-amateur block,
manual and after-PTT triggers, KISS and NMEA from the host, messages with acks, the station list, digipeating and
the iGate both ways, the DMR position source, a packet as AFSK audio through the PCM stream into the station list
only on the APRS frequency). `usrpNetTest` checks the stream's APRS ring. `tests/host/aprs_wav_check.sh` feeds the WAV to Direwolf's `atest`
when it is installed (a black-box check).

My beacon host coverage: `uiFlowTest` and `uiFlowTestNoColor` open each screen, select and cancel choices,
edit and clear comments, and check the 43-character limit at every display geometry. `aprsRadioTest` checks
validation, settings reload, no automatic TX after position edits and the decoded comment. `settingsTest`
checks comment persistence and clearing across simulated power cycles. The navigation-only HIL test
`test_aprs_beacon_ui.py` needs `RFOUNDRY_HIL_APRS_BEACON=1`; it opens and cancels editors without saving or
requesting RF. It has not been run on a radio.

HIL (opt-in, `tests/hil/test_aprs.py`, README there): `--allow-rf --allow-service-stop --aprs-call CALL` (a licensed
operator's callsign, set as the RAM-only test call; SSID `--aprs-ssid`, default 15).

Where the AFSK tests transmit, so nothing reaches the APRS network: the custom region on `--aprs-test-mhz`
(a simplex frequency the operator may use, away from the APRS network: 144.390 would be digipeated and gated to
APRS-IS, and every regional APRS frequency is refused), no path, the radio's lowest power (checked through `aprs.tx_power` before keying;
6 RF says APRS keys at the radio's current level). Each transmission is about 0.6 s. Every APRS setting touched is
saved and put back once the queued frames and message retries have gone out on the test frequency.

1. Position, status, message: a bench board decodes Bell 202 / AX.25 itself while the radio sends (an FM receive
   job with APRS decode: frames with a good FCS and their tone measurements, no I/Q). Asserted: a valid FCS,
   `CALL-SSID>APZRDF` with no digipeaters, the payload (position within 1e-4 degree, `>status`,
   `:ADDRESSEE:text{N`), `aprs.rf_sent` +1, the radio back on its channel, deviation peak 2.0-4.5 kHz, twist under
   6 dB. Printed: mark and space deviation, TX delay (flags before the frame). The position is sent from the DMR
   channel and from the FM VFO.
2. Digital: on the DMR test channel the position goes to `--aprs-dmr-dst` as an LRRP report and as a UDT location;
   the bench modem's listener hears the header and blocks, the test checks the CRCs, the IPv4 / UDP 4001 / LRRP or
   UDT NMEA layers and the position (within 2e-5 degree). Passed on air (a 70 cm DMR channel, color code 2, 50 mW).
3. Still manual: BrandMeister (`aprs set via dmr` through a BrandMeister repeater or hotspot; the position on aprs.fi
   with the SSID BrandMeister assigns, and the LRRP request ID question, 8.1) and KISS (`aprs set kiss 1`,
   `tools/aprs_gate.py --port PORT --kiss-tcp 8001`, or in USB network mode the radio's own KISS port 8001; a position
   from APRSdroid / Xastir goes out on RF).

Results (SDR bench): DMR LRRP and UDT pass. AFSK decodes off air with a valid FCS on each kind of board with a tone
path, with twist under 1.5 dB and continuous phase (about 6 to 15 degrees residual); the tone level is set per board so
the peak deviation stays near 3 kHz (a transmitter limits at 5.1 kHz peak). `test_aprs.py` on the 2 m amateur band
is still to run on the HR_C7000 boards. Per-radio levels and results are in the hardware specs.

## 14. Open decisions

- Default triggers: SmartBeaconing on with a 10 min interval fallback, after-PTT and boot off.
- A registered tocall instead of APZRDF.
- The LRRP request ID and token for unsolicited reports to BrandMeister; whether LRRP answers should use the APRS
  position when there is no GPS; whether Settings > Location > Send position should drive APRS.
- The codeplug APRS record's fixed position and frequency encodings.
- Receive from the HR_C6000's ungated RX RAM (5.3).
- Settings and debug link section / command numbers may need renumbering when merged with other branches.

Future, not done:

- 9600 baud APRS (G3RUH FSK, scrambled NRZI direct FM): needs a flat (discriminator / modulator direct) audio path
  and an FSK modem; a tone generator path can't do it. Not planned for now.
- Settings > Location tied to APRS; GPS Info in the hotspot's own transmissions.

## Display units

Settings > Display > Units selects **US** (default) or **Metric** for the whole radio. It is saved with the
other settings and applies immediately to APRS, GPS and caller distances. Internal values and transmitted
packets do not change. Builds without `CONFIG_UNITS` retain metric displays and omit the setting.

| Quantity | US | Metric |
|---|---|---|
| Distance | ft below one mile, then mi | m below one kilometer, then km |
| Speed | mph | km/h |
| Altitude | ft | m |
| Temperature | °F | °C |
| Rain | in, two decimals | mm, one decimal |
| Pressure | inHg, two decimals | hPa |

Distance uses one decimal below 100 mi/km and whole units above, switching to whole units when rounding
would reach 100. Other values round to the nearest displayed unit; exact halves round away from zero.
Received weather uses these same display units.

## 14. Advertised voice frequency

`CONFIG_APRS_TUNE` adds the frequency parser, temporary tuning and the optional beacon prefix. It needs
`CONFIG_APRS_AFSK`; constrained builds may leave it off. New station data and tune undo state use main RAM.
The grammar comes from [APRS frequency specification](https://www.aprs.org/info/freqspec.txt).

The comment must start with `FFF.FFFMHz` or `FFF.FF MHz` (MHz is case insensitive). Optional space-separated
fields are `Tnnn` or `Cnnn` (whole Hz of a standard CTCSS tone), `Dnnn` (octal DCS), signed `+nnn`/`-nnn`
(offset in 10 kHz), and `Rnnm`/`Rnnk` (range in miles/km). Lowercase tone letters request narrow FM.
`Toff` clears the tone. Missing offset means simplex; no regional repeater offset is guessed.
The parser also accepts a frequency object name with a two- or three-digit fractional MHz prefix.
Different frequencies in the object name and comment are refused rather than guessing a cross-band split.

Position and status comments update the sender's frequency before display-text truncation. A comment without a
frequency clears it. Objects, items and messages do not change the sender's tuning target. Objects and items retain their own parsed frequency, tone and offset before comment truncation.

Station and object/item detail offer Tune to. Selecting it opens a review of frequency in MHz, tone, offset,
source callsign and frequency-packet age. The review snapshots those values; later packets cannot retarget it.
Up/Down scrolls the review, OK tunes and Back cancels without changing the radio. Missing tone means none;
missing offset means simplex. It refuses an unsupported receive or transmit
frequency, an unusable tone, or a busy radio with a reason. The radio task checks the request again and applies
frequency, explicit offset, FM bandwidth and transmit tone in one transaction. Receive tone is cleared.
Tuning does not key the transmitter or write the codeplug. The temporary-tune screen shows the current receive
frequency and live APRS listening state. Back restores the previous frequency (also in the VFO) and returns Home.
Hold Back reaches Home without undoing; Back on Home still restores the previous frequency. Selecting a channel
drops the temporary tune.

My frequency in my beacon defaults off, stored in the appended `aprs_my_frequency` setting. When enabled, position comments start
with the current voice channel's receive frequency and transmit tone. The radio task refreshes this prefix before
beacon generation, including after a channel change. It preserves the configured comment after a space, within
`APRS_COMMENT_MAX`. Digital channels, inverted DCS, or frequencies not representable in whole kHz omit the prefix.
The setting changes content only; it does not trigger transmission.

Recent traffic shows used/capacity and the powered-session count of oldest packets displaced by a full ring.
Duplicate packets suppressed by the existing deduplication rule do not increment the counter.

### Manual task coverage

The composer, shared Location settings, fixed-coordinate entry and DMR gateway configuration existed before
these review flows. The manual result, all-TX stop, fixed-entry review, editable phrases, emergency review and
unsaved-editor exit complete the corresponding operating paths. Decorative mockup controls are not implemented.

| Task | Behavior |
|---|---|
| S04 Send now | A retained paged result tracks the manual beacon, separately from other RF traffic. DMR submission says queued, not sent. Missing position and blocked RF do not report success |
| S05 Stop | The hub has one Stop all APRS TX action. Confirm inhibits manual/automatic beacons, messages, ACKs, relays and host RF injection; the next radio poll aborts active APRS RF and owned DMR data. Received messages remain available |
| S08/S09 Position | My position shows the current source, full coordinates and age. OK opens shared source settings; # opens fixed entry. Each edited coordinate is reviewed with the other coordinate before saving; saving does not arm TX |
| S18/S19 Compose/phrases | Selecting a phrase inserts text for editing. OK opens a full paged review with recipient and count; another OK sends. # on a phrase edits its reusable session value (31 characters). Message drafts still allow 67 characters. Failed sends retain the review |
| S26 DMR | APRS settings expose the existing channel, own DMR ID, destination, UDT/LRRP format, position readiness and queue count. The existing Send via and Automatic beacons reviews control reporting |
| S27 Emergency | APRS settings offer a warning and separate confirmation for one FM position with the literal comment EMERGENCY. It does not arm periodic emergency reports, alter the saved comment, or claim a standardized remote alarm/dispatch. Back sends nothing. Stop cancels queued emergency work |
| S31 Unsaved work | Leaving a modified text editor or fixed-entry review with held Back asks to keep editing or discard. The prompt defaults to keep. Back from message review returns to its retained body |

Stop is a session inhibit, independent of stored automatic-beacon settings. Reopen the same action to resume;
repeated OK on its result cannot resume. Canceled outgoing messages keep their text and require an explicit Retry.
Resume permits future scheduled work but never requeues canceled frames. Traffic already handed to an external
network host cannot be recalled. Unexpected power loss still loses session drafts, phrases and message history.
