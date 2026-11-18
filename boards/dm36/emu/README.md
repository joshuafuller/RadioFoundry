<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# DM-36 firmware in QEMU

Build the ELF in a checkout of [the DM-36 bring-up PR](https://github.com/RadioFoundry/RadioFoundry/pull/607)
using the C-SKY toolchain on your PATH. The ELF target builds the project firmware without packaging a codec.

```sh
git submodule update --init --depth 1 -- third_party/FreeRTOS-Kernel
make -j4 BOARD=dm36 build/dm36/rfoundry_dm36.elf
export DM36_ELF="$PWD/build/dm36/rfoundry_dm36.elf"
```

From the root of the virtual radio preview, use Python with pyserial. Set `CSKY_QEMU` to the path of
XuanTie’s `qemu-system-cskyv2` and `CSKY_CROSS` to the path prefix ending in `csky-elf-` if they are outside
the runner’s defaults. Keep the results in a fresh temporary directory.

```sh
dm36_run=$(mktemp -d)
python3 tools/emu/emu_check.py --board dm36 --elf "$DM36_ELF" --screen "$dm36_run/home.png" --scale 1
```

The debug link should name `Baofeng DM-36` and the Git revision you built. On PR revision `0c29c4a3`
the firmware reported `ui.screen=home` and rendered a 240 × 320 frame. The panel model captures the pixels
the firmware draws. Bezel alignment and backlight brightness need the handset.

## Receive a DMR Call Alert

The following synthetic modem input is the Call Alert fixture from `tools/emu/test_hrc7000_modem.py`,
addressed from radio 3100001 to radio 3100002. The second member of the synthetic codeplug pair has that
recipient ID. The command runs the firmware's modem interrupt handler and DMR data service.

```sh
echo '{"wire":"000310a8290000000000010001010000000000000000000c9f0000002f4d622f4d61a50a"}' > "$dm36_run/alert.jsonl"
python3 tools/emu/hrc7000_emu.py --board dm36 --elf "$DM36_ELF" --boot --get-version \
    --flash dmr-pair --radio-index 2 --modem --air-in "$dm36_run/alert.jsonl"
```

Look for `deframed` and `'data.call_alerts_received': '1'`. The same PR revision also counted one
CSBK answer, but the model produced no outgoing ACK burst. A separate outbound Call Alert request
counted one send and produced no burst. Its radio view reported `tx_deny=no_codec`.
The physical bring-up check still needs the ACK heard by a second receiver. RF power, sensitivity and
decoded voice require separate measurements on the radio.
