<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# SPI NOR emulation

`spinor.cs` provides `SPI.SpiNor` for Renode 1.17.0. Load the source with
`include @tools/emu/peripherals/spinor.cs` before loading the platform description.
Attach it to the board's SPI controller and connect the chip-select GPIO to input 0:

```text
flash: SPI.SpiNor @ spi
    imageFile: "/absolute/path/to/flash.bin"
```

Input 0 is active-low. A rising edge finishes the transaction. `Reset` clears the
write-enable latch and discards an unfinished transaction. It preserves stored bytes.

Build a new image from the repository root:

```sh
python3 tools/emu/flashimg.py /tmp/flash.bin --storage dmr-pair --radio-index 2
```

The builder requires a host C compiler (`CC`, default `cc`). It calls the actual
[vhost fixture loader](../../../boards/vhost/storage_vhost.c), storage journal,
codeplug editors, and settings encoder. It writes a raw 16 MiB main array and
`flash.bin.security`, three concatenated 256-byte security registers. Existing
files are refused. No stock firmware, dumps, or codec files are inputs.

| Fixture | Main array |
|---|---|
| `blank` | All `0xFF`; firmware chooses its defaults on boot |
| `dmr-pair` | Members 1 and 2; each calls the other's private ID |
| `dmr-group` | Members 1..999; all use talkgroup 9 |

The security sidecar contains synthetic UHF calibration values for the layout in
[the hardware spec, section 11](../../../docs/spec/hardware-md380.md#11-calibration).
The firmware reader accepts the power, tuning and modulation rows. The retained fixture lacks the last band
endpoint, so the reader uses its default frequency grid and reports `factory/grid` (hardware spec 11.2).
These values are emulator fixtures, never measured calibration for a physical radio.
Register 3 stays erased, including the optional display-orientation byte.

The model implements the standard SPI subset described in the
[Winbond W25Q128FV datasheet, revision L](https://files.waveshare.com/upload/4/46/W25Q128fv.pdf):

| Commands | Behavior |
|---|---|
| `9F` | JEDEC ID `EF 40 18`, 16 MiB |
| `03`, `0B` | Read; fast read consumes one dummy byte; wraps at array end |
| `06`, `04`, `05` | Write enable, disable, and status 1 (write-enable latch) |
| `35`, `15` | Status 2 = 0, status 3 = `60` (power-on drive strength) |
| `02` | Page program, wraps within 256 bytes, clears bits only |
| `20`, `52`, `D8` | Aligned 4, 32, and 64 KiB erase |
| `48` | Security-register read, one dummy byte, wraps within that register |
| `4B` | Four dummy bytes, then a fixed synthetic 8-byte UID |

Completed program and erase commands write through to the main-array file.
Each emulator needs its own image. Do not edit or replace its files while it runs.
The sidecar is read-only. Malformed writes and commands outside this subset have
no effect. Security program/erase, status writes, protection, quad mode, chip erase,
and power-down are outside the current driver's needs.

Program and erase complete at chip-select release, with BUSY always clear.
This model proves storage behavior, not flash timing, wear, or interruption during
an internal program/erase cycle. It uses no wall-clock timers. The UID is fixed
across instances and must not be used as an emulated radio's unique identity.

Run only the focused checks:

```sh
python3 -m unittest discover -s tests/emu -p 'test_spinor.py' -v
```

Set `RENODE` if the executable is not at `~/tools/renode/renode`. The test puts
Renode configuration and generated images in a temporary directory. It requires
no sockets and never opens a radio or RF service. It checks Renode SPI attachment,
the byte protocol, persistence, fixture decoding, and the actual calibration reader.
