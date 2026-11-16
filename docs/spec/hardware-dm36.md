<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->
# Baofeng DM-36 hardware

This page covers the DM-36 board. The board files are [pins.h](../../boards/dm36/pins.h), [board.c](../../boards/dm36/board.c),
[board.mk](../../boards/dm36/board.mk), and [memory.ld](../../boards/dm36/memory.ld). The DMR modem inside the
SoC is covered by `modem-hrc7000.md`.

Evidence labels have a narrow meaning. `(measured from dump)` means the value was established from the owner's
verified program- or data-flash dump. `(photo)` means an FCC exhibit photograph. `(reported)` means the FCC filing,
manual, or an owner report. `(unverified)` marks an electrical assumption or a value not confirmed on hardware.
No transmit or hardware test was performed for this page.

The SoC, clocks, core peripheral blocks, and most board wiring are the
[same as the DM-32UV](hardware-dm32.md). Differences are called out below.

| Section | Topic |
|---|---|
| 1, 2 | Board identity, SoC, clocks, and memory |
| 3, 4 | Peripheral assignments and pin map |
| 5, 6 | Controls and Bluetooth module |
| 7, 8 | Display and RF transceiver |
| 9, 10 | Audio, LEDs, power, ADC, DAC, and data flash |
| 11 | Programming and update paths |
| 12 | Open items |

---

## 1. Board and SoC

| Item | Value and evidence |
|---|---|
| Board | `DM36-C7000-V2.0`, dated `20251009` (photo) |
| SoC | HR_C7000 (photo) |
| CPU and baseband | C-SKY CK803S CPU, on-chip DMR/FM baseband, and on-chip audio codec; same as the [DM-32UV SoC](hardware-dm32.md#1-soc) (measured from dump) |
| System clock | 24 MHz crystal. The build assumes the same 168 MHz core / 84 MHz APB setup as the DM-32UV (photo), (unverified) |
| RF reference | 26 MHz crystal beside the FD6818B (photo) |
| RTC | Coin cell present; wiring is assumed to match the DM-32UV (photo), (unverified) |
| Program flash | 1 MiB GD25Q80-class device. The owner's part reports JEDEC ID `C8 40 14` (measured from dump) |
| Data flash | 16 MiB device. The owner's part reports JEDEC ID `85 20 18`; the photographed unit carries a Winbond 25Q128JVSQ (measured from dump), (photo) |
| Installed software | `DM36.NRFB.01.020`; bootloader identity `BFUV36-V1` (measured from dump) |
| GPS | No module is visible and no GPS capability is documented (photo), (reported) |
| USB-C | Battery charging only; it is not a radio data connector (reported) |

Use the [DM-32UV clock and core-peripheral setup](hardware-dm32.md#11-clocks-and-pll-at-boot-before-anything-else).
The dump shows the same HR_C7000 architecture but does not electrically verify every clock output on this board
(measured from dump), (unverified).

## 2. Memory map and application base

| Range | Size | Use and evidence |
|---|---:|---|
| `0x00010000–0x0002F2D7` | 127,704 bytes | Main RAM-resident application image copied from program flash at boot (measured from dump) |
| `0x0002F2D8–0x0004AC4F` | 113,016 bytes | Cleared application RAM, including data, BSS, heap, and stacks (measured from dump) |
| `0x03000000–0x030FFFFF` | 1 MiB | Memory-mapped program flash (measured from dump) |
| `0x03000000–0x0300BFFF` | 48 KiB | Stock bootloader. Preserve this range when using the stock update path (measured from dump) |
| `0x03001000` | — | Stock bootloader reset entry (measured from dump) |
| `0x0300C000` | — | Application entry and `APP_BASE` (measured from dump) |
| `0x030706A0–0x03074AA7` | 17,416 bytes | Modem/voice working image copied into shared SRAM (measured from dump) |
| `0x030BE8C4–0x030DDB9B` | 127,704 bytes | Load image copied to `0x00010000` (measured from dump) |
| `0x18000000–0x18007FFF` | 32 KiB | Shared modem SRAM; same block as the [DM-32UV memory map](hardware-dm32.md#2-memory-map) (measured from dump) |
| `0x18000800–0x18004C07` | 17,416 bytes | Destination of the modem/voice working image (measured from dump) |

The application sets vector base `0x00010000`. The memory-mapped peripheral ranges and shared PCM buffers are the
same as the [DM-32UV memory map](hardware-dm32.md#2-memory-map) (measured from dump).

### 2.1 Stock boot decision

The bootloader samples PTT on GPIO `0x2B` and a second boot key on GPIO `0x29`. Both inputs must be low to enter
the updater. If either input is high, execution transfers directly to `0x0300C000` (measured from dump). The
updater initially drives the red and green LEDs low, so both are off; it does not initialize the display
(measured from dump). The display state inherited from power-on is unverified.

There is no installed-application magic-word, header, length, checksum, CRC, program-flash flag, data-flash flag,
or signature check before that transfer (measured from dump). The CRC-16 check belongs only to blocks received
during an update (measured from dump). Therefore, the only bootloader-enforced image condition is executable
CK803S code at `0x0300C000` (measured from dump). A blank, corrupt, or incompatible application is still entered;
its subsequent fault, hang, or visible state is unverified, and the bootloader does not automatically remain in
update mode (measured from dump).

The dump does not separate proprietary codec code from the other copied modem/voice material. Do not reserve a
smaller codec subrange from this evidence alone (unverified).

## 3. Peripheral assignments

The HR_C7000 peripheral register layout is the
[same as the DM-32UV](hardware-dm32.md#3-peripheral-base-addresses) (measured from dump). Board-specific assignments
are below.

| Peripheral | Assignment and evidence |
|---|---|
| UART0, `0x14030000`, vector 44 | Programming jack, 115200 baud; same application programming parser as the DM-32UV (measured from dump) |
| UART1, `0x14040000` | No GPS module or established board use (photo), (unverified) |
| UART2, `0x14050000` | No established board use (unverified) |
| UART3, `0x14090000`, vector 45 | ET25SE Bluetooth module, 115200 baud (measured from dump) |
| I2C0, `0x14060000` | PTA5/PTA6; same assignment as the DM-32UV (measured from dump) |
| SPI0, `0x140A0000` | 16 MiB data flash; GPIO `0x12` is chip select (measured from dump) |
| PWM0, `0x140C0000` | LCD backlight on GPIO `0x0F`; same as the DM-32UV (measured from dump) |
| ADC, `0x140D0000` | Battery and other analog inputs. The volume knob is not an ADC input (measured from dump) |
| DAC, `0x140F0000` | PA/APC analog drive and one unresolved analog control; same arrangement as the DM-32UV (measured from dump) |
| GPIO B, vector 46 | Volume-encoder interrupts and other GPIO B inputs (measured from dump) |
| i8080 LCD, `0x12000000` | 8-bit display bus (measured from dump) |

UART register layout, divisor calculation, FIFO setup, and 8N1 configuration are described in the
[shared UART section](hardware-dm32.md#31-uarts-16550-style). UART3 uses the stock application's normal 8-bit line
setup. The parity and stop-bit interpretation was not independently checked against a capture (measured from dump),
(unverified).

## 4. Pin map

GPIO indices use the common HR_C7000 numbering: PTA0–31 are `0x00–0x1F`, PTB0–31 are `0x20–0x3F`, and PTC0–31
are `0x40–0x5F` (measured from dump).

| Index | Port pin | Function | Direction / active level | Evidence |
|---|---|---|---|---|
| `0x00–0x04` | PTA0–4 | JTAG | peripheral | same as the [DM-32UV pin map](hardware-dm32.md#5-pin-map) (unverified) |
| `0x05/0x06` | PTA5/6 | I2C0 SCL/SDA | peripheral | same as the DM-32UV (measured from dump) |
| `0x07` | PTA7 | FD6818 serial clock | output | same as the DM-32UV (measured from dump) |
| `0x08` | PTA8 | FD6818 serial data | bidirectional | same as the DM-32UV (measured from dump) |
| `0x09` | PTA9 | UART1 RX function | no established board net | SoC mux only (unverified) |
| `0x0D/0x0E` | PTA13/14 | UART3 RXD/TXD SoC functions | peripheral | ET25SE board traces not electrically proven (measured from dump), (unverified) |
| `0x0F` | PTA15 | LCD backlight / PWM0 | output, high on | same as the DM-32UV (measured from dump) |
| `0x10` | PTA16 | Power-switch monitor | input, high means off | same as the DM-32UV (measured from dump) |
| `0x11` | PTA17 | Power latch | output, high holds power | same as the DM-32UV (measured from dump) |
| `0x12` | PTA18 | Data-flash chip select | output, low selected | same as the DM-32UV (measured from dump) |
| `0x14–0x16` | PTA20–22 | SPI0 SCLK/MOSI/MISO | peripheral | same as the DM-32UV (measured from dump) |
| `0x18` | PTA24 | FD6818 chip select | output, low selected | same as the DM-32UV (measured from dump) |
| `0x19/0x1A` | PTA25/26 | Output-controlled pair | board function unknown | differs from the DM-32UV assignment (measured from dump), (unverified) |
| `0x1D` | PTA29 | Unknown control | output, initialized high | function unresolved (measured from dump), (unverified) |
| `0x20` | PTB0 | Green LED | output, high on | same as the DM-32UV (measured from dump) |
| `0x21` | PTB1 | Red LED | output, high on | same as the DM-32UV (measured from dump) |
| `0x22` | PTB2 | VHF LNA / RX supply | output, high on | same as the DM-32UV (measured from dump) |
| `0x23` | PTB3 | UHF PA select | output, high on | same as the DM-32UV (measured from dump) |
| `0x24` | PTB4 | UHF LNA / RX supply | output, high on | same as the DM-32UV (measured from dump) |
| `0x25` | PTB5 | Unknown input/control | unresolved | touched by stock logic (measured from dump), (unverified) |
| `0x26/0x27` | PTB6/7 | Volume encoder B/A | inputs with GPIO B interrupts | (measured from dump) |
| `0x28` | PTB8 | Keypad backlight | output, high on | same as the DM-32UV (measured from dump) |
| `0x29` | PTB9 | Unknown input | unresolved | touched by stock logic (measured from dump), (unverified) |
| `0x2B` | PTB11 | PTT | input, low pressed | same as the DM-32UV (measured from dump) |
| `0x2C` | PTB12 | No confirmed GPS-power function | unresolved | do not inherit the DM-32UV GPS assignment (unverified) |
| `0x2E` | PTB14 | Analog-FM versus codec microphone path | output; 1 analog, 0 codec | same as the DM-32UV (measured from dump) |
| `0x2F` | PTB15 | FD6818 versus codec speaker source | output; 1 FD6818, 0 codec | same as the DM-32UV (measured from dump) |
| `0x30` | PTB16 | Bluetooth module enable/power-or-reset | output, high enabled | DM-36-specific (measured from dump) |
| `0x31–0x34` | PTB17–20 | Keypad columns 1–4 | inputs, low closed | same as the DM-32UV (measured from dump) |
| `0x35` | PTB21 | Audio power-amplifier enable | output, high on | same as the DM-32UV (measured from dump) |
| `0x36` | PTB22 | Unknown control | unresolved | touched by stock logic (measured from dump), (unverified) |
| `0x37` | PTB23 | VHF PA select | output, high on | same as the DM-32UV (measured from dump) |
| `0x38` | PTB24 | Likely vibration motor control | output | static association only (measured from dump), (unverified) |
| `0x39/0x3D` | PTB25/29 | Paired unknown controls | outputs switched together | function unresolved (measured from dump), (unverified) |
| `0x3A/0x3B` | PTB26/27 | FD6818 GPIO1/GPIO0 | inputs / interrupts | same as the DM-32UV (measured from dump) |
| `0x42` | PTC2 | LCD reset | output, low reset | same as the DM-32UV (measured from dump) |
| `0x43–0x46` | PTC3–6 | LCD CS/RS/WR/RD | i8080; RD held high as GPIO | same as the DM-32UV (measured from dump) |
| `0x47–0x4B` | PTC7–11 | LCD DB0–DB4 and keypad rows 0–4 | shared peripheral/GPIO | same as the DM-32UV (measured from dump) |
| `0x4C–0x4E` | PTC12–14 | LCD DB5–DB7 | peripheral | same as the DM-32UV (measured from dump) |
| `0x52` | PTC18 | Unknown control | frequently switched output | function unresolved (measured from dump), (unverified) |

Pins not listed above have no board assignment established by the dump (unverified). The application uses the same
GPIO-bank register layout as the [DM-32UV](hardware-dm32.md#39-gpio-banks-and-the-pin-index-scheme) (measured from dump).

### 4.1 Boot mux, pulls, directions, and levels

The common register setup matches the DM-32UV except that UART3 selection clears bit 20 of `IO_DIPLEX1`
(measured from dump). The values below include the later per-pin direction changes made during application
initialization (measured from dump).

| Register | DM-36 final value | DM-32UV value | Evidence |
|---|---:|---:|---|
| `IO_DIPLEX0` | `0xFFFDFD80` | `0xFFFDFD80` | (measured from dump) |
| `IO_DIPLEX1` | `0xFFEFFE07` | `0xFFFFFE07` | (measured from dump) |
| `IO_DIPLEX2` | `0x0FC80100` | `0x0FC80100` | (measured from dump) |
| `IOMGR_REN_REG0` | `0xFEFBFFEF` | `0xFEFBFFEF` after masking | (measured from dump) |
| `IOMGR_REN_REG1` | `0x00FFFFFF` | `0x00FFFFFF` | (measured from dump) |
| `IOMGR_REN_REG2` | `0x0FFFFF70` | `0x0FFFFF70` after masking | (measured from dump) |
| final `GPIOA_DDR` | `0x270604E0` | `0x070604E0` before board-specific pin setup | (measured from dump) |
| final `GPIOB_DDR` | `0x23A0D01F` | `0x00A0D01F` before board-specific pin setup | (measured from dump) |
| final `GPIOC_DDR` | `0x00047FC4` | `0x00007FC4` before board-specific pin setup | (measured from dump) |

The requested DM-36-specific pins have these states after their observed initialization. “Input” means the
firmware does not drive an initial level; the external sampled level was not captured (unverified).

| GPIO | Stock direction | Initial driven level | DM-32UV comparison | Evidence |
|---|---|---|---|---|
| `0x19` | output | low | no corresponding DM-32UV initialization | (measured from dump) |
| `0x1A` | output | low | no corresponding DM-32UV initialization | (measured from dump) |
| `0x1D` | output | high | no corresponding DM-32UV initialization | (measured from dump) |
| `0x25` | input | not driven; sampled level unverified | no corresponding DM-32UV assignment | (measured from dump), (unverified) |
| `0x29` | input | not driven; low is the boot-key asserted state; sampled level unverified | no corresponding DM-32UV assignment | (measured from dump), (unverified) |
| `0x36` | input | not driven; sampled level unverified | no corresponding DM-32UV assignment | (measured from dump), (unverified) |
| `0x38` | output | low | no corresponding DM-32UV assignment | (measured from dump) |
| `0x39` | output | high | no corresponding DM-32UV assignment | (measured from dump) |
| `0x3D` | output | high | no corresponding DM-32UV assignment | (measured from dump) |
| `0x52` | output | low | no corresponding DM-32UV assignment | (measured from dump) |

The DM-32UV document has none of these ten board-specific assignments; its common mux, pull, and initial DDR
values otherwise match the values above (measured from dump).

## 5. Controls

### 5.1 Volume encoder

The volume knob is a quadrature encoder, not a potentiometer read by the ADC. A is GPIO `0x27`; B is GPIO `0x26`.
Both are GPIO B interrupt inputs on vector 46 (measured from dump).

The stock input path records transitions in the interrupt handler and consumes direction events in its foreground
input task. It does not sample the knob at a fixed frequency. Two complementary quadrature transitions produce
one direction event and one volume step, so the stock count is two transitions per detent (measured from dump).
With A on `0x27` and B on `0x26`, B leading A is volume up and A leading B is volume down (measured from dump).
Whether B-leading-A is physically clockwise on the fitted encoder is unverified. A 10-tick filter/countdown is
present, but the dump does not establish that tick's wall-clock duration (measured from dump), (unverified).

Normal speaker volume is an integer from 1 through 10. One accepted direction event changes it by one. The value
wraps at both ends and the same integer is sent to the on-screen volume display. The dB gain represented by each
level is unknown (measured from dump), (unverified).

RadioFoundry uses `CAP_VOLUME_ENCODER` and the shared [speaker volume](radio-features.md#42-speaker-volume)
path. It clamps at 1 and 10 instead of wrapping. The existing volume overlay shows the step. Hardware validation
is pending.

### 5.2 Keypad, side keys, and PTT

The matrix wiring and scan method are the same as the
[DM-32UV keypad](hardware-dm32.md#61-keypad-matrix) (measured from dump). LCD DB0–DB4 become GPIO rows during a
scan. Rows idle high. Each selected row is driven low and columns `0x31–0x34` are read low-active.

| Row | Column `0x31` | Column `0x32` | Column `0x33` | Column `0x34` | Evidence |
|---|---|---|---|---|---|
| `0x47` | green / OK | 3 | 2 | 1 | same as DM-32UV (measured from dump) |
| `0x48` | left | 6 | 5 | 4 | same as DM-32UV (measured from dump) |
| `0x49` | right | 9 | 8 | 7 | same as DM-32UV (measured from dump) |
| `0x4A` | red / back | # | 0 | * | same as DM-32UV (measured from dump) |
| `0x4B` | up | down | — | — | same as DM-32UV (measured from dump) |

PTT is GPIO `0x2B`, low when pressed. SK1 and SK2 use columns `0x33` and `0x34` with no matrix row driven, as on
the [DM-32UV side keys](hardware-dm32.md#62-side-keys-and-ptt) (measured from dump). The programming jack and PTT
electrical sharing has not been measured on a DM-36 (unverified).

## 6. Bluetooth module

The host-tested envelope driver and remaining host-stream integration are specified in [Bluetooth module host transport](bt-module-transport.md).

The shielded module is marked `ET25SE` and contains a Jieli chip. FCC reports cover BLE and BR/EDR. Nearby pads are
labeled `UART0_RXD`, `UART0_TXD`, `LINEOUT`, and `AF_BIAS` (photo), (reported).

`CONFIG_BT_MODULE_TRANSPORT=y` supplies `board.bt_module` and `CAP_BT_DATA`, UART3 at 115200,
and GPIO `0x30` active high. It remains off by default. The cable stays selected and no module is powered
just by building the switch. The descriptor uses a 20-byte TX profile, accepts a nonempty version string,
and has no data-profile token yet. Startup/off delays of 350 ms are provisional policy, not a conversion
of the measured stock delay ticks. UART3 mux selection and physical traces need `rf-cmh.7` verification.

The driver-lane build used 208,608 bytes of static RAM with the switch off and 210,688 with it on,
out of 211,456 bytes. The on build retains the descriptor and UART ring but discards the unused decoded
adapter. Its 768-byte headroom is not enough to add the current adapter state statically. Before selecting
Bluetooth, allocate its state within a verified RAM budget and implement host protocol/gateway teardown;
the successful descriptor-only build does not qualify an active transport image.

### 6.1 SoC link

| Item | Value and evidence |
|---|---|
| SoC peripheral | UART3 at `0x14090000`, interrupt vector 45 (measured from dump) |
| Baud | 115200 (measured from dump) |
| Pins | HR_C7000 UART3 functions are PTA13/PTA14; physical module traces are not proven (measured from dump), (unverified) |
| Format | 8-bit normal UART setup; 8N1 is the expected interpretation (measured from dump), (unverified) |
| Enable | GPIO `0x30` high after a startup delay; low on shutdown (measured from dump) |
| Separate reset | None identified (unverified) |

The module terminates Bluetooth. The SoC receives programming payloads over UART3, not through an HR_C7000 BLE
stack (measured from dump).

### 6.2 UART framing

Control uses ASCII lines terminated by CR/LF. Programming data uses a length-delimited envelope
(measured from dump).

| Direction | Frame and evidence |
|---|---|
| SoC to module, binary | `AT+DATA=00,<n>\r\n<payload>`; `n` is one to three ASCII decimal positions and the payload is exactly `n` bytes with no trailer (measured from dump) |
| Module to SoC, binary | `+DATA:00,<n>\r\n<payload>`; `n` is one to three ASCII decimal positions and the payload is exactly `n` bytes with no trailer (measured from dump) |
| SoC to module, alternate data | `AT+ADATA=01,<n>\r\n<payload>`; `n` is the original binary-byte count and the payload contains two uppercase hexadecimal characters per byte (measured from dump) |
| Module to SoC, alternate data | `+ADATA:00,<n>\r\n<payload>` with the same receive-header rules as `DATA` (measured from dump) |

The receive parser compares only the `+DATA:00` or `+ADATA:00` prefix. It then skips one byte, normally the comma,
and interprets one, two, or three following byte positions as base-10 digits according to where CR occurs. It does
not verify that the skipped byte is a comma or that the length positions contain `0` through `9` (measured from
dump). The largest well-formed declared length is therefore 999 bytes (measured from dump). A declared zero does
not complete as an empty frame: the parser enters payload state and cannot complete until its 16-bit byte counter
wraps, so zero-length envelopes are not usable (measured from dump).

The parser accumulates a control/header line only after `+` or `O`, requires LF immediately after CR to dispatch
it, and resets its line index after the 1,024th stored byte rather than reporting an error (measured from dump).
A malformed recognized DATA header is not rejected: arithmetic is performed on the unchecked byte positions and
the resulting 16-bit length controls payload collection (measured from dump). Other malformed or unknown complete
lines are ignored (measured from dump). There is no receive inactivity timeout or resynchronization search in this
path (measured from dump). While a declared payload is being collected, every byte—including CR/LF and event-looking
text—is payload; events are handled only after the payload count is satisfied (measured from dump).

The binary `DATA` receive path feeds the same programming parser used by UART0. Replies use `AT+DATA=00` when
Bluetooth is the selected transport. This proves an arbitrary-byte programming path; sustained PPP or KISS traffic
has not been tested (measured from dump), (unverified).

On transmit, the SoC builds the header and payload into one buffer and writes them as one polling UART operation;
it does not wait for a prompt or reply between the header and payload (measured from dump). It then waits for an
`OK\r\n` line, or a 25-counter timeout, before another command or envelope can be sent (measured from dump). The
counter's wall-clock period is unverified. The builder correctly renders decimal lengths through 999, but its
524-byte work buffer has no caller-length guard; 508 bytes is the largest three-digit payload that fits that buffer
with its 16-byte header (measured from dump). The largest length requested by a real stock session is unverified.
No deliberate inter-envelope delay exists beyond UART serialization and the `OK`/timeout gate (measured from dump).

### 6.3 Module command set

These are the complete printable commands emitted by the inspected SoC image (measured from dump).

| Function | Commands |
|---|---|
| Binary and alternate data | `AT+DATA=00,`, `AT+ADATA=01,` |
| Classic device control | `AT+SCAN=1`, `AT+SCAN=2`, `AT+CONN=`, `AT+DISC=000000000000`, `AT+DNAME?`, `AT+DNAME=` |
| LE device control | `AT+LESCAN=1`, `AT+LESCAN=2`, `AT+LEDISC=000000000000`, `AT+LENAME?`, `AT+LENAME=` |
| Identity and pairing | `AT+PIN?`, `AT+PIN=`, `AT+VER?`, `AT+AUTOCONN?` |
| Audio profiles | `AT+AGHF?`, `AT+AGHF=1`, `AT+AGHF=0`, `AT+AUDIO=1`, `AT+AUDIO=0` |
| Audio routing | `AT+MICEN=1`, `AT+MICEN=0`, `AT+SPKEN=1`, `AT+SPKEN=0` |
| Audio gain | `AT+MICGAIN=15`, `AT+SPKGAIN=25` |

Every module event below is a CR/LF-terminated line. Except for `OK`, the parser requires a leading `+`; token
comparisons are prefix comparisons, so trailing text is tolerated unless a handler examines it (measured from
dump).

| Event or reply | Syntax accepted and stock action |
|---|---|
| Ready | `+READY\r\n`; trailing bytes before CR/LF are ignored. It marks module startup complete (measured from dump). |
| Version | `+VER:<text>\r\n`; up to 20 bytes following the colon are retained (measured from dump). The actual identity text is unverified. |
| Connection | `+CONN<field>,<profile>\r\n`; the parser ignores the first field, locates the first comma, and tests the following profile token. `AG` selects the audio/headset state; the data-profile token is not recoverable with sufficient confidence from this dump (measured from dump), (unverified). |
| Host device | `+HOSTDEVICE<field>,<profile>\r\n`; field handling and audio/data profile tests mirror `CONN` (measured from dump). Exact field meanings and the data-profile token are unverified. |
| Disconnect | `+DISC\r\n`; trailing fields, if any, are ignored. It clears connection and pending-operation state (measured from dump). |
| Scan | `+SCAN:000000000000\r\n` is the no-device sentinel. Other accepted `+SCAN...\r\n` records contain at least three commas; the text after the third comma is retained as a result name, up to 49 bytes (measured from dump). The preceding field meanings are unverified. |
| LE name | `+LENAME:<text>\r\n`; up to 32 bytes after the colon are retained (measured from dump). |
| PIN | `+PIN:<text>\r\n`; up to 16 bytes after the colon are retained (measured from dump). |
| Headset-frequency state | `+HFREQ:<value>\r\n`; `0` and `1` clear and set the stored state respectively; other values are ignored (measured from dump). |
| Command acknowledgement | `OK\r\n`; it releases the pending-command gate. No `+` is present (measured from dump). |

The dump does not establish which connection field denotes classic versus LE. Classic scan/connect/disconnect uses
`AT+SCAN`, `AT+CONN`, and `AT+DISC`; LE scan/disconnect uses `AT+LESCAN` and `AT+LEDISC`. There is no LE-connect
command in the image (measured from dump). Therefore bearer identity still requires a real module (unverified).

### 6.4 Accessory PTT events

| Event | Syntax and action |
|---|---|
| PTT state | `+PTTKEY:<value>\r\n`. `1` asserts the accessory PTT state and `0` releases it. `3` and `6` set two separate accessory-status indications and clear an associated counter; their external meanings are unverified. Other values are ignored (measured from dump). |
| PTT battery | `+PTTBATT:<nn>\r\n` or `+PTTBATT:<nnn>\r\n`. Two or three decimal positions are converted to one byte and stored as the accessory battery level. Its use and 0–100 shape indicate percent, but the module's unit has not been observed (measured from dump), (unverified). |

These events update accessory/UI state. They are not inserted into the programming byte stream. The stock PTT press
is passed into the radio's normal keying path; release clears that state (measured from dump).

### 6.5 Startup, connection, and audio coexistence

When Bluetooth is enabled in settings, the SoC initializes UART receive state, waits 350 delay ticks, then drives
the module-enable pin high (measured from dump). The delay tick's wall-clock duration and any module-internal delay
after enable are unverified. The SoC waits for `+READY`; no bounded READY timeout or automatic enable-pin retry was
identified, so a silent module can leave startup pending (measured from dump). Command waits use a 30-counter gate;
transmit completion uses the 25-counter `OK` gate described above. Their wall-clock periods are unverified.

After READY, startup commands are serialized in this order: `AT+AGHF?`, microphone gain, speaker gain,
`AT+LENAME?`, `AT+PIN?`, then `AT+VER?` (measured from dump). The gain commands carry the saved two-digit values;
the image defaults are `AT+MICGAIN=15` and `AT+SPKGAIN=25` (measured from dump). Each command waits for the preceding
pending command to finish or time out (measured from dump).

Classic discovery and connection use `AT+SCAN=1`/`2`, `AT+CONN=<device>`, `AT+DNAME?`/`=<name>`, and
`AT+DISC=000000000000`; LE device management uses `AT+LESCAN=1`/`2`, `AT+LENAME?`/`=<name>`, and
`AT+LEDISC=000000000000` (measured from dump). The exact command sequence triggered by each UI connect operation is
unverified. Audio uses `AT+AGHF=1`/`0`, `AT+AUDIO=1`/`0`, `AT+MICEN=1`/`0`, `AT+SPKEN=1`/`0`, and the two gain
commands; data uses `AT+DATA=00` and the independent programming transport state (measured from dump). The SoC keeps
audio and data connection state independently and contains no mutual-exclusion teardown, so the stock application
allows them to be active together; whether the physical module can sustain both without loss is unverified.

Only UART RX, TX, and the enable pin are configured for this module. No RTS/CTS setup or separate flow-control pin
was found (measured from dump); board-level electrical behavior remains unverified.

### 6.6 Status of the transport specification's ten open measurements

| Item | Status from this dump |
|---|---|
| 1. RX DATA grammar and event ordering | Answered for the stock SoC parser, including zero-length behavior and events during payload (measured from dump). What the module emits still needs a real module (unverified). |
| 2. Envelope lengths and buffering | SoC RX grammar limit 999 and TX work-buffer limit 508 are answered (measured from dump). Module limits, phone fragmentation/coalescing, and buffering need a real module (unverified). |
| 3. Phone bearer | Needs a real module (unverified). |
| 4. BLE MTU or SPP write sizes | Needs a real module (unverified). |
| 5. Reply, credit, and flow control | SoC waits for `OK` only after a whole transmission and has no RTS/CTS (measured from dump). Phone-side credit and stall behavior need a real module (unverified). |
| 6. Throughput, duplex, latency, and pacing | Needs a real module (unverified). |
| 7. Pairing and security policy | Needs a real module (unverified). |
| 8. READY, VER, UART, and startup | Parser syntax, UART setup, enable order, and counter values are answered (measured from dump). Actual identity variants and wall-clock/restart behavior need a real module (unverified). |
| 9. Connection fields and association | Audio profile recognition and independent audio/data state are answered (measured from dump). Exact data token, field meanings, classic/LE association, and disconnect retention need a real module (unverified). |
| 10. PTT events and coexistence | Parser values and stock actions are answered (measured from dump). Battery units, peer association, and traffic-under-audio behavior need a real module (unverified). |

The phone application's DM-36 entry uses the DM-32UV programming protocol and a BLE GATT bridge
(reported). Service `FFE0`, characteristic `FFE1`, and baud characteristic `FF31` are app-side candidates, not a
verified DM-36 advertisement (unverified). No production GATT capture, SPP exposure, MTU, write mode, pacing,
bonding, or module OTA contract is established (unverified).

## 7. Display

The panel is a 240 by 320 RGB565 TFT with an ST7789-class command set. It uses the HR_C7000 8-bit i8080 controller
(measured from dump). The panel is described as 2.0 inches (reported).

Reset is GPIO `0x42`. CS, RS, WR, and RD are `0x43–0x46`; DB0–DB7 are `0x47–0x4E`. RD remains high as a GPIO.
DB0–DB4 are shared with keypad rows (measured from dump).

The stock init sequence is byte-for-byte the same register sequence documented for the
[DM-32UV LCD panel](hardware-dm32.md#7-lcd-panel): hardware reset; sleep out; porch, tearing, orientation, RGB565,
gate, VCOM, LCM, VDV/VRH, frame-rate, power, and gamma setup; a 240 by 320 window; inversion on; frame clear; and
display on (measured from dump). Use that shared table and its 180-degree orientation value. Bezel-safe insets have
not been measured on a DM-36 (unverified).

## 8. RF transceiver and transmitter controls

The RF chip is FD6818B. Its interface, tuning units, core register tables, and mode transitions match the
[DM-32UV FD6818 specification](hardware-dm32.md#8-fd6818-rf-transceiver) in the inspected paths
(photo), (measured from dump).

| Item | Value and evidence |
|---|---|
| Bus | Three-wire bit-banged serial; not I2C (measured from dump) |
| Pins | CS `0x18` active low, clock `0x07`, bidirectional data `0x08` (measured from dump) |
| Frame | 8-bit register address then 16-bit value, MSB first (measured from dump) |
| Frequency | 32-bit value in 10 Hz units; low half register `0x38`, high half `0x39` (measured from dump) |
| State | Register `0x30`: `0x0000` idle, `0xBFF1` RX; use the shared DM-32UV TX sequence (measured from dump), (unverified) |
| Mode/bandwidth | Register `0x43`; values and order are the same as the DM-32UV (measured from dump) |
| Receive paths | GPIO `0x22` VHF LNA and `0x24` UHF LNA (measured from dump) |
| PA selects | GPIO `0x37` VHF and `0x23` UHF (measured from dump) |
| PA drive | HR_C7000 DAC B / APC bias, separate from the PA-select GPIOs (measured from dump) |

No DM-36-specific incompatibility was found in the inspected init and tune paths. This does not verify PLL lock,
output power, spurious emissions, calibration, or safe transmission. Keep transmit disabled until bench and RF
validation are complete (unverified).

## 9. Audio, LEDs, power, ADC, and DAC

### 9.1 Audio

The board control path is the same as the [DM-32UV audio path](hardware-dm32.md#9-audio-path) for the functions
established by the dump (measured from dump).

| Element | DM-36 assignment and evidence |
|---|---|
| Microphone | HR_C7000 codec ADC; GPIO `0x2E` selects analog FM or codec path (measured from dump) |
| Speaker source | GPIO `0x2F` selects FD6818 audio or codec line out (measured from dump) |
| Speaker amplifier | GPIO `0x35`, high enabled (measured from dump) |
| Bluetooth audio | Module microphone, speaker, mode, and gains controlled by the UART commands in section 6 (measured from dump) |
| Module analog nets | Pads labeled `LINEOUT` and `AF_BIAS`; exact electrical routing unknown (photo), (unverified) |
| DMR PCM | Shared SRAM and codec interrupt model are the same as the DM-32UV (measured from dump) |
| Prompts | 59 RIFF/WAVE objects in data flash (measured from dump) |

The stock volume ladder has seven distinct hardware settings. User levels 8–10 saturate at the level-7 hardware
setting (measured from dump). For FM, the table byte becomes the low gain field in FD6818 register `0x48`; the
fixed upper bits produce the words below (measured from dump). For DMR, the table byte is converted to the codec
line-out byte written at codec offset `0x1F` (measured from dump).

| User level | FM table byte | FD6818 register `0x48` word | DMR table byte | Codec line-out byte | Evidence |
|---:|---:|---:|---:|---:|---|
| 1 | `0x02` | `0xB282` | `0x14` | `0x8B` | (measured from dump) |
| 2 | `0x04` | `0xB284` | `0x19` | `0x86` | (measured from dump) |
| 3 | `0x06` | `0xB286` | `0x1E` | `0x81` | (measured from dump) |
| 4 | `0x08` | `0xB288` | `0x28` | `0xB7` | (measured from dump) |
| 5 | `0x0C` | `0xB28C` | `0x2A` | `0xB5` | (measured from dump) |
| 6 | `0x0E` | `0xB28E` | `0x2C` | `0xB3` | (measured from dump) |
| 7 | `0x0F` | `0xB28F` | `0x2E` | `0xB1` | (measured from dump) |
| 8 | `0x0F` | `0xB28F` | `0x2E` | `0xB1` | (measured from dump) |
| 9 | `0x0F` | `0xB28F` | `0x2E` | `0xB1` | (measured from dump) |
| 10 | `0x0F` | `0xB28F` | `0x2E` | `0xB1` | (measured from dump) |

RadioFoundry applies this ladder for both FM and codec audio, including saturation at levels 8–10.
Level 0 remains the explicit software mute.

None of user levels 1–10 switches off the audio amplifier. GPIO `0x35` is gated by audio-path state, not by a
volume-level threshold (measured from dump). Electrical gain in dB remains unverified.

### 9.2 LEDs, backlights, vibration, and power

| Item | Value and evidence |
|---|---|
| Green LED | GPIO `0x20`, high on (measured from dump) |
| Red LED | GPIO `0x21`, high on (measured from dump) |
| LCD backlight | GPIO/PWM `0x0F`, high on (measured from dump) |
| Keypad backlight | GPIO `0x28`, high on (measured from dump) |
| Vibration | GPIO `0x38` is consistent with motor control, but the board net is not electrically proven (measured from dump), (unverified) |
| Power switch | GPIO `0x10` high means the knob is off (measured from dump) |
| Power latch | GPIO `0x11` high keeps the radio powered (measured from dump) |

### 9.3 ADC and DAC

The HR_C7000 ADC is 10-bit with eight channels, as described in the
[DM-32UV ADC section](hardware-dm32.md#37-adc-0x140d0000) (measured from dump). The volume control does not use an
ADC channel (measured from dump). Battery channel and voltage scaling were not independently established for the
DM-36; the DM-32UV channel C / ADC2 scale is a bring-up hypothesis only (unverified).

DAC B supplies PA/APC bias. PA-select GPIOs remain separate. The exact DAC calibration values and the purpose of
the other enabled analog output have not been measured on this board (measured from dump), (unverified).

## 10. Data flash and calibration

The owner's 16 MiB part reports JEDEC ID `85 20 18`. It uses SPI0 with chip select GPIO `0x12`. Command set and page
geometry are expected to follow the [DM-32UV data-flash interface](hardware-dm32.md#10-data-flash-16-mb)
(measured from dump), (unverified).

| Range | Content and rule | Evidence |
|---|---|---|
| `0x000000–0x000FFF` | Radio-owned data; never write | (measured from dump) |
| `0x001000–0x0C8FFF` | Native codeplug pool: 200 physical 4 KiB pages; byte `+0xFFF` is the logical block ID; never write from the open firmware | (measured from dump) |
| `0x0C9000–0x148FFF` | Dense radio-owned data; schema unknown | (measured from dump) |
| `0x149000–0x14FFFF` | Erased in this owner's radio | (measured from dump) |
| `0x150000–0x175FFF` | Boot-image region; preserve | (measured from dump) |
| `0x176000–0x17FFFF` | Erased in this owner's radio | (measured from dump) |
| `0x180000–0x1F9FFF` | Dense radio-owned table/font-like data; schema unknown | (measured from dump) |
| `0x1FA000–0x200FFF` | Erased in this owner's radio | (measured from dump) |
| `0x201000–0x201FFF` | Populated radio-owned data; schema unknown | (measured from dump) |
| `0x202000–0x27DFFF` | 59 voice-prompt RIFF/WAVE objects | (measured from dump) |
| `0x27E000–0x6DBFFF` | Erased in this owner's radio; no populated contact database | (measured from dump) |
| `0x6DC000–0x6E7FFF` | Sparse radio-owned sectors; schema unknown | (measured from dump) |
| `0x6E8000–0xFFFFFF` | Erased in this owner's radio | (measured from dump) |

### 10.1 Factory calibration page

Exactly one 4 KiB codeplug-pool page has logical block ID `2` in its last byte. It starts at data-flash offset
`0x00078000` (measured from dump). Its header and three fragments reconstruct the same `0x175`-byte calibration
image and field offsets specified by [DM-32UV section 11](hardware-dm32.md#11-factory-calibration) (measured from
dump).

| Table | Five decoded frequencies | Assessment | Evidence |
|---|---|---|---|
| UHF table 1 | 400.000, 417.500, 435.500, 452.500, 470.000 MHz | sane values inside the 400–480 MHz band | (measured from dump) |
| UHF table 2 | 400.000, 417.500, 435.500, 452.500, 470.000 MHz | sane values inside the 400–480 MHz band | (measured from dump) |
| VHF table 1 | 136.000, 145.500, 155.500, 164.500, 174.000 MHz | sane values spanning the 136–174 MHz band | (measured from dump) |
| VHF table 2 | 136.000, 145.500, 155.500, 164.500, 174.000 MHz | sane values spanning the 136–174 MHz band | (measured from dump) |

There is no second ID-2 page in the 200-page pool. The stock first-match scan therefore selects the sole page at
`0x00078000`; there is no duplicate-page ordering ambiguity in this dump (measured from dump).

RadioFoundry validates the reconstructed image before using it: header, decimal BCD digits, and four strictly
increasing frequency tables within the bands above. Invalid calibration uses the shared safe defaults and reports
`cal invalid` through the debug-link diagnostics. The build does not read a decoded copy at `0x710000`.

### 10.2 Stock write areas and first-write rule

The stock page manager can erase and rewrite physical pages anywhere in `0x001000–0x0C8FFF` while updating
codeplug-backed settings, including the current channel and volume (measured from dump). A logical page must not be
treated as fixed to the physical offset seen in one snapshot because the pool is wear-levelled (measured from
dump).

The dump contains sparse radio-owned data at `0x6DC000–0x6E7FFF`, but static analysis did not establish the complete
normal-use allocator or prove whether those sectors are logs, recordings, or another journal (unverified). No
normal-use erase/write path was proven for the currently erased ranges outside the page pool (unverified). In
particular, `0x6E8000–0xFFFFFF` being erased in this radio is not evidence that stock firmware never allocates it
(unverified). Consequently, this dump establishes no data-flash region safe for a first RadioFoundry write;
persistent writes must remain disabled until write-command traces or a bounded allocator prove a non-overlapping
range (unverified).

## 11. Programming and firmware update

### 11.1 Application programming

The cable path is UART0 at 115200 baud and uses the same application programming parser as the DM-32UV
(measured from dump). Use the shared `dm32-stock-cps.md` command set. Bluetooth feeds the same command bytes to that
parser through the UART3 `DATA` envelope in section 6 (measured from dump).

The app-side BLE service and characteristic selection, MTU, write response mode, and pacing remain unverified. A
successful codeplug transaction does not prove PPP or KISS suitability (unverified).

### 11.2 Stock bootloader update

Enter update mode by holding PTT and SK1 while powering on. The update uses a `.bin` file through the PC programming
software (reported).

| Item | DM-36 value and evidence |
|---|---|
| Link | Programming jack / UART0 at 115200; expected 8N1 (measured from dump), (unverified) |
| Identity | `BFUV36-V1` (measured from dump) |
| Application target | `0x0300C000` (measured from dump) |
| Transfer family | Same single-letter handshake and XMODEM-style block exchange as the [DM-32UV bootloader](hardware-dm32.md#132-baofeng-bootloader-update-mode) (measured from dump) |
| Header block | 128 bytes with block number and complement (measured from dump) |
| Application blocks | 1 KiB payloads with block number and complement (measured from dump) |
| Integrity | CRC-16/CCITT-XMODEM, polynomial `0x1021`, big-endian CRC bytes (measured from dump) |
| Signature | No public-key or cryptographic-signature verification path was identified (measured from dump) |
| Installed-image validation | None before the direct transfer to `0x0300C000` (measured from dump) |

The stock updater must preserve `0x03000000–0x0300BFFF` and write the application at `APP_BASE`. The model identity
differs from the DM-32UV, so a DM-32UV container must not be used (measured from dump).

No genuine DM-36 vendor update file was available. Its outer 256-byte file header, size rules, and any host-side
model checks beyond the returned identity remain unknown (unverified). The bootloader logic alone does not prove
that every correctly framed image is safe.

For a supervised first RadioFoundry write, use the SoC boot ROM stub, not the stock updater. Verify full backups
of both flashes and the recovery procedure first. Preserve program offsets `0x0000–0xBFFF`, write only the reviewed
application at `0xC000`, and compare its readback before booting. Data flash remains read-only, with no
open-firmware codeplug, journal, or settings allocation. No release image is offered.

## 12. Open items

1. Confirm UART3 PTA13/PTA14 board traces, voltage levels, 8N1 framing, and whether GPIO `0x30` is power, reset, or both.
2. Capture the module's production GATT database, SPP exposure, MTU, write mode, pacing, bonding, security, version, and OTA behavior.
3. Trace the module's `LINEOUT` and `AF_BIAS` pads and measure its microphone, speaker, and PTT audio behavior.
4. Identify GPIO `0x19`, `0x1A`, `0x1D`, `0x25`, `0x29`, `0x36`, `0x38`, `0x39`, `0x3D`, and `0x52` electrically,
   and establish the sampled reset levels of the input pins.
5. Measure speaker gain in dB for volume levels 1–10, confirm the encoder's physical clockwise direction, and
   establish the encoder filter tick duration.
6. Confirm battery ADC channel/scaling, DAC outputs, factory calibration compatibility, RF lock, PA/LNA switching, and safe power levels.
7. Trace every normal-use data-flash erase/program target and establish a non-overlapping writable area before enabling persistent writes.
8. Obtain a genuine DM-36 update file and verify its outer header, length, host-side identity checks, erase span, and recovery behavior.
9. Separate codec reservations from the copied modem/voice images before offering a release layout.

See the [feature matrix](../feature-matrix.md) for tested behavior. A successful build does not verify any hardware
function. Keep transmit and persistent data-flash writes disabled until the relevant open items are closed.
