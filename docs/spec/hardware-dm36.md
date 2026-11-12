<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->
# Baofeng DM-36 hardware

Build-only bring-up for `boards/dm36`: built, never run on a radio. Every band has transmit disabled because PA
select and DAC pins are unconfirmed. This does not make the inherited initialization safe to run on hardware.
No release image is offered. There is no GPS capability in this build.

The facts below are the supplied public evidence from FCC ID **2AJGM-DM36**, granted **2026-04-13**, and owner
reports. `(photo)` identifies exhibit photographs, `(manual)` the filing's manual, `(reported)` the other filing
statements or owner reports named in the row, and `(unverified)` a build hypothesis. No stock firmware, CPS package,
third-party protocol description or radio was inspected for this page. Upstream tracking: RadioFoundry/RadioFoundry #452.

## 1. SoC, clocks and memory

| Item | Value and evidence |
|---|---|
| Board | `DM36-C7000-V2.0`, dated `20251009` (photo) |
| SoC | HR_C7000 (photo) |
| System crystal | 24 MHz (photo) |
| RTC | Coin cell present; wiring unknown (photo), (unverified) |
| Program flash | GD25Q80E (photo) |
| Data flash | Winbond 25Q128JVSQ (photo) |
| Application base | Build uses `0x0300C000`; bootloader identity and application boundary unknown (unverified) |

## 2. Peripherals, controls and RF

| Item | Value and evidence |
|---|---|
| RF chip | FD6818B, 26 MHz reference crystal (photo) |
| Bluetooth | Shielded `ET25SE` module with a Jieli chip and a PCB antenna in the case (photo); BLE and BR/EDR test reports in the filing (reported) |
| Module-adjacent pads | `UART0_RXD`, `UART0_TXD`, `LINEOUT`, `AF_BIAS` (photo); SoC routing and commands unknown (unverified) |
| Display | 2.0 inch color TFT, separate keypad board joined by flex (photo), (manual); resolution and controller unknown (unverified) |
| Keys | Full keypad (photo), (manual); wiring unknown (unverified) |
| GPS | No module visible and none documented (photo), (manual) |
| Connectors | USB-C on the battery, charge only; 2-pin programming jack (photo), (manual) |
| Rated transmit | 136-174 and 400-470 MHz; about 4.4 W VHF and 4.1 W UHF, FCC grant (reported) |
| Receive | VHF/UHF, air band and NOAA (manual) |
| Alert | Vibration (manual); drive pin unknown (unverified) |
| Other names | DM36, DM36UV, DM36PRO, DM36 PLUS, DM36Ultra, MD36UV, AT36D, GT36D; identical except name per the FCC model difference letter (reported) |

## 3. Programming and update reports

| Item | Value and evidence |
|---|---|
| CPS compatibility | Owners report DM-32UV CPS v1.60 programs it and accepts a DM-32 codeplug (reported) |
| Update mode | Owners report PTT + SK1 and a `.bin` through CPS, as on DM-32UV (reported) |
| Loader identity | Unknown; the registry uses a deliberately unverified placeholder (unverified) |

These reports establish neither the flash layout nor a safe update path.

## Assumed same as the DM-32UV until the firmware is read

Section numbers below refer to `hardware-dm32.md`. They identify inherited build choices, not confirmed DM-36 facts.
Any future firmware reading belongs to the separate spec side of the [clean-room process](../CLEANROOM.md).

| Sections | Assumption used by the build (unverified) |
|---|---|
| 1-4 | CPU setup, clocks beyond the visible crystal, memory map, interrupts and peripheral assignments |
| 5-6 | Every pin value, IO mux, reset level, power control, ADC, encoder, LEDs and keypad wiring |
| 7 | ST7789, 240x320, i8080 bus, orientation and display insets; placeholders despite the smaller panel |
| 8-9 | RF sequences, receive ranges, broadcast receiver, audio routing and PA/DAC assignments; all TX bands disabled |
| 10-11 | SPI wiring, storage map, write floor and calibration format/locations |
| 13 | Application base only; update compatibility is reported, loader identity is not assumed verified |
| 15, 17 | Codec reservations, SRAM/flash layout, shared drivers, RTOS heap and runtime stub assumptions |

Section 12 (GPS) is not inherited as a capability. Section 14's other chips are not confirmed; the build still
inherits the broadcast receiver driver. Bluetooth and vibration have no working board glue. The registry's gallery
and codec profile are placeholders, with no supported codec source. The inherited UHF receive limit of 480 MHz
is a hypothesis; the FCC transmit grant ends at 470 MHz.

## Open items

- Complete pin map, IO mux and reset levels, including the inherited GPS-power output on a board without GPS.
- LCD controller, resolution, orientation, bus and timing.
- Keypad wiring, side keys and encoder.
- Bluetooth module UART routing, baud rate, commands and audio connections.
- PA select and DAC pins, RF switching, calibration and safe power levels.
- Bootloader identity string and `APP_BASE`; flash protection, storage and codec layout.

See the [feature matrix](../feature-matrix.md) for build switches. A successful build does not verify any hardware function.
