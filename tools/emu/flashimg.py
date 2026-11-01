#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Build synthetic SPI NOR images. See peripherals/spinor.md for the file format and Renode setup."""

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SIZE = 0x1000000
SOURCES = (
    "tools/emu/peripherals/spinor_image.c",
    "boards/vhost/storage_vhost.c",
    "boards/vhost/flash_vhost.c",
    "core/codeplug/codeplug.c",
    "core/settings/settings.c",
    "core/storage/storage.c",
)


def compile_builder(output):
    subprocess.run(
        shlex.split(os.environ.get("CC", "cc"))
        + ["-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wshadow", "-O2"]
        + [f"-I{ROOT / d}" for d in ("boards/vhost", "platform/host", "core", "hal")]
        + [str(ROOT / p) for p in SOURCES]
        + ["-o", str(output)],
        check=True,
    )


def security_registers():
    """Synthetic UHF calibration, docs/spec/hardware-md380.md 11.2; never use on hardware."""
    data = bytearray(b"\xff" * 768)
    data[0x009] = 0x40
    for offset, value in ((0x010, 80), (0x020, 24), (0x030, 128),
                          (0x130, 64), (0x140, 64), (0x170, 64), (0x180, 64)):
        data[offset:offset + 9] = bytes([value]) * 9
    for i in range(9):
        # Nine points, 400..480 MHz in 10 Hz units, little-endian packed BCD.
        data[0x1B0 + 4 * i:0x1B4 + 4 * i] = bytes.fromhex(f"{40000000 + i * 1000000:08d}")[::-1]
    return bytes(data)


def build_image(output, fixture="blank", radio_index=1):
    if fixture not in ("blank", "dmr-pair", "dmr-group"):
        raise ValueError("unknown storage fixture")
    if not 1 <= radio_index <= (2 if fixture == "dmr-pair" else 999):
        raise ValueError("radio index must be 1..2 for dmr-pair or 1..999 otherwise")
    output = Path(output)
    security = Path(str(output) + ".security")
    created = []
    try:
        # Exclusive creation protects an earlier run's persistent storage, including its sidecar.
        with output.open("xb") as image:
            created.append(output)
            with security.open("xb") as registers:
                created.append(security)
                with tempfile.TemporaryDirectory(prefix="rf-flashimg-") as work:
                    builder = Path(work) / "fixture"
                    compile_builder(builder)
                    subprocess.run([str(builder), fixture, str(radio_index)], stdout=image, check=True)
                if image.tell() != SIZE:
                    raise ValueError("fixture did not produce a 16 MiB image")
                registers.write(security_registers())
    except BaseException:
        for path in reversed(created):
            path.unlink()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new raw 16 MiB image; also creates OUTPUT.security")
    parser.add_argument("--storage", choices=("blank", "dmr-pair", "dmr-group"), default="blank")
    parser.add_argument("--radio-index", type=int, default=1)
    args = parser.parse_args()
    try:
        build_image(args.output, args.storage, args.radio_index)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"flashimg: {error}\n")
    print(f"{args.output}: {SIZE} bytes, {args.storage}, radio {args.radio_index}; synthetic calibration in .security")


if __name__ == "__main__":
    main()
