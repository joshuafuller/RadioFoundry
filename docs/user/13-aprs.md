<!-- SPDX-License-Identifier: MIT -->

# APRS

APRS (Automatic Packet Reporting System) shares your position with other amateur stations and maps such as aprs.fi.
The radio sends APRS two ways:

- **On FM**: as 1200 baud AFSK packets on the APRS frequency of your region. The radio leaves your channel for about
  a second, sends, and comes back.
- **Over DMR**: as a position report to an APRS gateway ID (BrandMeister's 310999 by default; it works from any master, but your own master's MCC plus 999 is recommended, for example 204999). This works on DMR
  channels. BrandMeister forwards Motorola (LRRP) and Hytera position reports to APRS, so keep the default LRRP format
  (not UDT). A radio that already saved APRS settings keeps its stored ID: run `aprs set dmr_dst 310999` (US) or
  `aprs set dmr_dst <MCC>999`. The position appears on APRS-IS as your callsign with the SSID from your BrandMeister
  profile (aprs.fi may take a minute to show it).

Not every radio does both. Some send only, some can't send APRS on FM yet, and a build can leave APRS out for flash
room. The [Radio reference](23-radios.md) lists them and says where APRS is verified.

## Before you start

- You need an amateur radio license. APRS sends only on amateur frequencies, and the radio refuses others.
- Your callsign is the first word of the radio name (Settings > My radio, or the codeplug's radio name). Without a
  valid callsign nothing is sent, and the APRS page says why.
- Sending is off until you turn it on. "Send position now" works at any time.

## The APRS page

Press **OK**, **6** (Tools), **8** (APRS). The top of the page shows your callsign, the APRS
frequency, your position and its source, and when the next beacon goes out. If the radio can't send, it says why.

| Row | What it does |
|---|---|
| Send position now | One position at once |
| Automatic beacons | On: positions go out by themselves (see Triggers) |
| Frequency | Your region: N America 144.390, Europe 144.800, Australia 145.175, New Zealand 144.575, Japan 144.660, China 144.640 MHz, or Custom (`tools/rfoundry_debug.py aprs set region custom`, then `aprs set freq_hz 144390000`) |
| Power | As channel: APRS transmits at the radio's current power level. Or pick a level of its own, for example a low one for a nearby digipeater. Dimmed on radios that can't send APRS over the air |
| Path | How far digipeaters pass your packets on: None, WIDE1-1, WIDE1-1,WIDE2-1 (the default), WIDE2-1 or WIDE2-2. See Path below. Dimmed on radios that can't send APRS over the air |
| Interval | How often: Off, 1, 2, 5, 10 or 30 minutes. Dimmed when SmartBeaconing or Proportional sets timing |
| Decay | Off by default. Double the interval for unchanged position and comment, up to 30 minutes. Available in builds with beacon pathing; dimmed while SmartBeaconing is on |
| SmartBeaconing | More often when you move fast, at once after a turn, rarely when you stand still |
| Send via | AFSK (on FM), DMR, or both |
| My beacon | Preview and edit symbol, comment, automatic beacons, pace and reach (when built) |
| KISS on USB | The radio works as a KISS TNC for APRS programs on the computer |
| Objects | Named objects and items; OK opens position, owner, comment and advertised frequency |
| Bulletins | Announcements and group bulletins; OK opens the complete text |
| Recent packets | The latest decoded packets, newest first; OK opens one |
| Stations heard | Stations the radio heard (radios that can receive APRS), sorted as described below. A station that sent its position shows how far away and which way it was from yours, when the radio has a position too; the age on that row is then the age of that position. Other rows show when the station was last heard. Objects and items a station sends don't count as its position, and a position more than a day old is dropped. Select one and choose Message to fill its callsign |

A firmware built with only one way to send APRS shows only the rows for it. With APRS over DMR only there is no
Frequency, Power, Path, Send via or Stations heard, and KISS on USB is called NMEA on USB (the computer can still give
the radio its position).

## My beacon

Open **Tools > APRS > My beacon** when included in your build. The preview shows your callsign, frequency, position/source, symbol name
and comment for FM packets. DMR gateways use their own profile for symbol and comment.

| Row | Choice | Default |
|---|---|---|
| Symbol | Pick from 40 plain names with Up / Down, then OK. Back keeps the old symbol | On foot |
| Comment | Edit up to 43 characters; the editor shows how many remain | Codeplug comment, until edited here |
| Beacons | Off or Automatic | Off |
| Pace | Smart, or a fixed interval of 1, 2, 5, 10 or 30 minutes | Smart, with a 10-minute fallback |
| Reach | Direct, Local relay, Local then wide, Wide one hop, Wide two hops or Proportional | Local then wide |

In Comment, **OK** saves. **Back** deletes a character; when empty, Back cancels. To clear the saved comment,
delete its text and press OK. Hold Back cancels and returns Home. A saved comment, including an empty one,
overrides the codeplug comment and survives restart. Symbols outside the list show **Custom** until you replace them.

Reach names correspond to None, WIDE1-1, WIDE1-1,WIDE2-1, WIDE2-1, WIDE2-2 and Proportional, respectively; see Path below.
Changing Pace leaves Beacons as it was. Setting a position, symbol, comment or reach never turns beacons on.
The DMR destination remains available through `tools/rfoundry_debug.py aprs set ...`.

## Sort and filter stations

**Stations heard** starts with **Nearest** when your position is known, otherwise **Recent**. Press **\*** to
cycle **Recent**, **Nearest**, and **Callsign**. New packets keep that order, and the selection follows the same
callsign. Nearest puts stations without a known range last and uses receipt time when no ranges are available.
Your chosen sort and filter last until power off.

Press **#** (or hold **OK**) for **All / Weather / Mobiles / Objects / Digipeaters**. Select with OK or a numbered
key. **#**, **1** returns to All. The title shows the view and count, such as **Weather only, 3 of 14**; a dim row
shows the sort and how many stations are hidden. Filtering does not delete stations or stop reception.

Weather uses a weather packet or weather symbol. Mobiles means a retained position with a known speed above zero.
Objects shows stations whose last packet was an object or item, under the publisher's callsign. Digipeaters uses
the advertised digipeater symbol; hearing a station through a digipeater does not put it in that filter.
If the selected station leaves the view, the list selects the first remaining station.

## Objects and bulletins

In **Tools > APRS**, open **Objects** for places and items published by other stations. Select a name to see its
publisher (**From**), age, distance and direction, comment, and a frequency advertised as `ddd.dddMHz`.
Distance needs a position of your own. The frequency is information only; opening it does not tune or transmit.
An object's location never moves its publisher in **Stations heard**.

A withdrawn object reads **Removed** for ten minutes, then leaves the list. A new live report brings it back.
Live objects expire after a day. The list holds up to eight recently updated names; boards with less RAM retain fewer.

Open **Bulletins** for announcements, group bulletins and NWS bulletins, newest first. Select a row to read its
sender, age and full retained text; scroll with **Up / Down**. A new or changed bulletin shows a banner once,
after any current banner clears. Repeated identical packets do not alert again while in the list. Bulletins have
no Reply action and the radio does not acknowledge them.

The bulletin list holds up to eight sender/address pairs (fewer on boards with less RAM) and drops entries a day after their text last changed.
Both lists clear at power-off. If an open entry leaves the list, its detail says **No longer in list**.

## Station details

Open **Stations heard**, select a callsign, and press **OK**. **Message** is first; press OK again to open a message
addressed to that station, or its conversation when messages are retained. The selection stays on the same callsign when incoming packets reorder the heard list.

Scroll with **Up / Down** to see distance and direction (when both positions are known), when the station was heard,
**Direct** or **Via digipeater**, and the position's age if it differs from the last packet's age. Speed in km/h and
course appear only when known for the retained position. The remaining rows show the symbol's table and code,
the retained comment or status, and the packet count. The radio retains at most 23 text characters plus the end
marker; longer comments are incomplete. No digipeater name is retained.

If the station leaves the heard list, the page says **No longer in list**. You can still choose Message to address
that callsign.

## Message conversations

In **Messages**, select an APRS callsign to read its conversation. Incoming and outgoing messages appear in time
order. **Up / Down** scrolls each full message, then moves to the next or previous one. **OK** or **#** opens the existing
reply composer for that callsign.
Opening the conversation marks its incoming messages read. New traffic does not change the reply recipient.

| Outgoing state | Meaning |
|---|---|
| Queued | Waiting behind another message or for its first attempt |
| Sending | Attempt number, maximum attempts and seconds until the next retry is due |
| Delivered (acked) | The addressed station acknowledged the message; the age is when that ACK arrived |
| No ack after 3 tries | The retry limit was reached without a matching acknowledgment |
| Rejected | The addressed station rejected the message |
| Sent, no ack requested | The radio transmitted a message with acknowledgment turned off |

Three outgoing messages can wait or retry at once. When full, a new send is refused and the composer stays open;
the accepted messages are preserved. Up to six outgoing records are retained (at least three on boards with less RAM), replacing only completed records as needed.
The incoming inbox retains four messages. These records and queued work are lost at power-off. Builds without APRS
receive have one outgoing slot and refuse a second send while it is occupied.

An acknowledgment comes from the station's software; it does not prove a person read the message. No acknowledgment
does not prove the message was not received. A busy channel can delay a retry beyond its displayed countdown.

## Path

The path says how many digipeater hops a packet may take on FM. Each **WIDEn-N** entry allows N more hops. Press
**OK** on Path to step to the next choice.

| Path | Hops | Use it |
|---|---|---|
| None | 0 | Only stations that hear you directly (an iGate nearby) |
| WIDE1-1 | 1 | A fill-in digipeater or one wide digipeater |
| WIDE1-1,WIDE2-1 | 2 | Mobile and handheld use in most of North America and Europe (the default) |
| WIDE2-1 | 1 | Fixed stations that reach a wide digipeater directly |
| WIDE2-2 | 2 | Sparse areas; skips fill-in digipeaters |
| Proportional | 0, 1, 0, 2 | One-minute slots: Direct, WIDE1-1, Direct, WIDE1-1,WIDE2-1, then repeat |

More hops mean more copies on a busy frequency. Use the fewest that reach an iGate. The path doesn't apply to
positions sent over DMR.

The path is also in the codeplug's first APRS configuration, which qdmr edits. A change on the radio is written
there too, and a codeplug written with a different path changes the radio's. Whichever changed last is used.

Proportional is available in builds with beacon pathing. It is saved on the radio; the codeplug keeps its literal
path. A newly changed codeplug path replaces Proportional too. Manual beacons go direct without advancing the cycle;
messages and status use two hops. With Decay on, unchanged position and comment stretch the slots up to 30 minutes.
SmartBeaconing keeps the path cycle but chooses timing by movement.

## Receiving

On radios that can receive APRS, the radio decodes packets while you listen on the APRS frequency. Make a channel on
the frequency shown in **Frequency** on the APRS page (your region's, 144.390 MHz in North America) in FM, or tune
the VFO there, and leave the radio on it. If the packets are sent on another frequency, set **Frequency** to Custom
and enter it first. Heard
stations go into **Stations heard**, messages to your callsign appear in **Messages** and are acknowledged, and the digipeater (if you
turned it on) passes packets on.

- It listens only on the APRS frequency, in FM, and the tuned frequency must equal the APRS **Frequency** exactly.
  On other FM channels nothing is decoded.
- The volume knob doesn't matter: a radio turned all the way down still decodes.
- On a radio that decodes only what opens the squelch ([Radio reference](23-radios.md)), a packet must be strong enough to open it. Keep the squelch at its usual level, not tight.
- Battery saving pauses while the radio listens for APRS: a sleeping receiver misses packets.
- Receiving is on by default. Turn it off with `tools/rfoundry_debug.py aprs set rx 0` (FM only).

Each new packet briefly shows its sender and kind, such as position, status or weather. When your radio and the
sender have positions, the banner also shows distance and direction. Press the hinted **# Open** key to open that
station's detail. It disappears after eight seconds. Your own packets and repeats of the same packet within about
30 seconds do not raise another banner. Messages addressed to you keep their normal message handling.

Traffic banners stay out of the way while you transmit, type or confirm an action, and do not replace another
alert. To see packets you missed, open **Tools > APRS > Recent packets**. Each row shows sender, kind and age.
**OK** opens the packet's decoded fields, whether it was heard directly or through a digipeater, and its retained
raw text. **Up/Down** scroll and **Back** returns. Non-printable bytes appear as `\xHH`; `[truncated]` means the
information field exceeded the retained 40 bytes. The list holds the latest 8 packets by default (a board can configure
a larger ring), drops the oldest when full, and clears at power-off. An open packet stays the
same when new packets arrive. An empty list says **Nothing heard yet**; when reception is inactive it also says
why. The list records local decoding, not delivery to an internet service.

On a DMR channel, a DMR APRS position another radio sends (some makes send it as an NMEA position) also goes into
**Stations heard**, whoever it is addressed to: usually the APRS gateway, as on FM every station heard is listed. It
is listed under the sender's callsign when its DMR ID is in the radio's DMR ID database, else under the DMR ID. This
works whatever the receive setting. **Stations heard** shows the station and when it was heard, not its position.

## Radar (color displays)

On **Tools > APRS**, press **#** to open Radar. Your position is the center cross; north is at the top.
Three labeled rings in kilometers automatically scale to include the stations with positions. Small markers show
the stations; positions older than 30 minutes have dim hollow markers.

Press **Up / Down** or turn the knob to select a station. Its outlined marker and the bottom area show which one
is selected, with its callsign, distance, bearing and position age. **OK** opens that station's details; **Back**
returns. New packets do not change the selected callsign. Stations without positions remain in **Stations heard**.

If Radar asks you to set your position, use **Settings > Location** to choose GPS, Computer or a fixed position
(see [Position](#position)). Radar is available in color builds with APRS receive enabled.

## Triggers

Automatic beacons go out at the interval, or with SmartBeaconing by speed and turns. Never more often than every
30 seconds. The radio waits for a clear APRS frequency before it sends, and gives up after 10 seconds. Pressing PTT
always wins: an APRS transmission in progress stops and your channel comes back.

With **Decay** on and SmartBeaconing off, the first beacon uses the base interval; each beacon doubles the next
wait, up to 30 minutes. Changing your position or comment resets the wait to the base interval. Interval Off stays
off. Proportional uses a one-minute base instead of the saved interval.

With **SmartBeaconing** on, Interval and Decay say **Smart on** and are dimmed. Selecting them explains why; their
saved values stay unchanged. Without a known speed, SmartBeaconing uses its 30-minute slow rate.

## Position

The position comes from:

- **GPS**: on radios that have one ([Radio reference](23-radios.md)).
- **Computer**: NMEA from a GPS on the computer, sent over USB by `tools/aprs_gate.py --nmea <GPS port>` (KISS on USB
  must be on).
- **Fixed**: a position you enter.

Auto takes a fresh GPS position, else the computer's, else the fixed one.

The position is not only APRS's: DMR calls and location requests (LRRP) use the same one. Its settings are under
**Settings > Location**: **Position source** (Auto, GPS, Computer, Fixed) and **Fixed position**, a page with
**Latitude** and **Longitude**. To set the fixed position:

1. Open Settings > Location > Fixed position, select Latitude and press OK. The value opens for editing.
2. Type the degrees with the digits, decimal places included: `3`,`3`,`7`,`4`,`9`,`0` is 33.7490. The decimal point is
   already there. Longitude has three places before the point: 84.3880 is `8`,`4`,`3`,`8`,`8`,`0` (the radio puts the
   leading 0 in for you), and 122.4194 is `1`,`2`,`2`,`4`,`1`,`9`,`4`. For 10 to 19 degrees, type the 0 yourself.
3. Press Up or Down (or turn the knob) to switch between N and S, or E and W. West longitudes are W.
4. Press OK to save. Places you didn't type count as zeros. BACK removes the last digit; with nothing typed it closes
   the value unchanged.

Four decimal places put you within about 11 m. You can also type "latitude" or "fixed" on Home to find the page.

## GPS status

**Tools > Position** shows what the radio's GPS is doing, on four pages. Press Up or Down (or turn the knob) for the
next page, or press 1 to 4. On a radio with a tall screen, Status also shows the grid locator and degrees, minutes and
seconds, so there are three pages.

| Page | What it shows |
|---|---|
| 1 Status | 3D Fix, 2D Fix (Fix when the receiver doesn't say which) or Searching; satellites used and seen; position, altitude, speed, HDOP (accuracy: good, fair, poor) and the UTC time of the fix |
| 2 Sky | where each satellite is: the outer ring is the horizon, the middle ring 45 degrees up, north at the top. A filled dot is used in the fix. The colors and the legend show the system: GPS, GLONASS, Galileo, BeiDou, QZSS |
| 3 Signal | each satellite's signal strength as a bar; filled bars are used in the fix |
| 4 Grid | your Maidenhead grid locator (FN42li), and the position in degrees, minutes and seconds |

"No GPS, or it is off" means the radio has no GPS or **Settings > Location > GPS** is off. A first fix outdoors takes
from a few seconds to a few minutes; indoors there may be none.

## With a computer (KISS and iGate)

Turn on **KISS on USB**, then run `tools/aprs_gate.py --port <radio port>`:

- `--kiss-tcp 8001`: APRS programs (APRSdroid, Xastir, YAAC) connect to TCP port 8001 and send through the radio. The
  radio sends only packets from your own callsign, at most one every 10 seconds after a burst of three.
- `--call <your call>`: connects to APRS-IS. A radio that can receive APRS is then an iGate. It passes what it hears
  to APRS-IS. If you allow it, it also passes messages from APRS-IS to local stations back on the air.
- In USB network mode use `--port udp:<radio address>`.

The radio can't be in hotspot mode at the same time.

In USB network mode the radio does both without the helper. APRS programs connect to the radio's address on TCP
port 8001 directly ([Services on the radio](15-usb-network.md#services-on-the-radio)). The radio logs in to an
APRS-IS server itself ([APRS-IS from the radio](15-usb-network.md#aprs-is-from-the-radio)). KISS on USB must be on
for both.

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

## Home status and the APRS hub

On builds with the APRS Home feature, enable **Settings > Display > APRS line** (off by default).
Below the channel controls, Home shows when APRS last sent and when the next timed beacon is due, or why it
cannot send or listen. Receiving does not need your position. A second line, when there is room, shows
**Repeated by** and the digipeater's callsign, or the station count and nearest known distance in the selected display units.
A repeat means the radio heard its last packet repeated; it does not confirm delivery to the internet.
Voice activity and notifications take priority. Large-text mode and dual watch keep their existing layouts.

Open **Tools > APRS** for the hub:

| Key | Row |
|---|---|
| 1 | Stations: count and new stations since your last visit |
| 2 | Messages: unread count; opens the shared inbox |
| 3 | Weather: received reports (when enabled) |
| 4 | Send position now |
| 5 | My beacon: review identity, frequency, position and symbol |

The existing beacon settings follow these rows. Keys 1-9 and 0 pick the first ten rows; use Up / Down and OK for
later rows. Builds without this feature keep the compact APRS settings page.

## Weather

Open **Tools > APRS > Weather** to see received weather stations. The list starts with the newest report.
Select **Sort** to switch to nearest first. Nearest needs your position and the station's position; reports
without a known distance follow the others. With no position of your own, the list stays newest first.
Reopen Weather or change Sort to refresh its station list.

Select a station to read temperature, wind, gust and report age, then scroll to rain for the last hour,
last 24 hours and since midnight, humidity, pressure and luminosity. Units follow Settings > Display > Units; US is the default. Luminosity is shown in W/m².
**Unknown** means the report did not provide that measurement. A reported zero is shown as zero.
Report age is how long ago your radio received the weather, not the age of the station's observation.

Reports stay in a small memory table until replaced or the radio restarts. A new report replaces that station's
old measurements; missing fields do not keep older values. Weather appears only in builds with weather receive
support enabled.

### Tune to a station's voice frequency

When a station advertises a voice frequency, its detail list offers **Tune to**, followed by the frequency and
tone. Select it to listen there in FM. Any advertised offset and transmit tone are applied together. Missing
an offset means simplex. An unsupported frequency or tone shows a reason and leaves the channel unchanged.

Tuning returns to Home and is temporary. Press Back on Home to undo it, or select another channel. It does not
transmit or save a channel. On a single receiver you leave the APRS frequency while listening to voice.

### My frequency in my beacon

This APRS setting is **Off** by default. Turn it on to put your current voice frequency and tone at the start of
position beacon comments. Changing channels updates the next beacon; it does not send one immediately. Your
existing comment follows if it fits. Digital channels and inverted DCS do not advertise an FM frequency.
These controls require a build with `CONFIG_APRS_TUNE`.
