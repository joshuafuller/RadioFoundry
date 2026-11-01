# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The data flash on SPI0 for tools/emu/hrc7000_emu.py, behind the firmware's own SSI calls, with a codeplug image from
tools/emu/flashimg.py: a GD25Q128 (16 MB SPI NOR) or a SPI NAND (SpiNand, at the end of this file).

Why: without a codeplug the radio has no DMR ID (core/radio/radio_sm.c:4104, the default is 0) and refuses every DMR
transmission (TXDENY_NO_ID). The image gives it a DMR channel, a color code and an ID.

How it hooks in. The firmware talks to the chip through two functions in platform/hrc7000/ssi.c:
`hrc_ssi_select(base, active)` (chip select) and `hrc_ssi_xfer(base, tx, rx, len)` (clock bytes both ways). The bridge
stops at the entry of both. When r0 is SPI0_BASE this model does the transfer: it reads `len` bytes at `tx`, feeds them
to the chip model and writes the answer to `rx`. For any other SPI base the old stub answer stands (return 0, "failed").
One stop per call: a 4 KB read is one stop and one memory write.

The chip: the command subset drivers/flash/spi_nor/spi_nor.c sends (JEDEC ID 9F, read 03, status 05/35/15, write
enable 06, page program 02, 4 KB erase 20, security read 48, unique ID 4B), from the public GD25Q128 / W25Q128
datasheets. A command starts when chip select goes active and ends when it goes inactive.

The image. flashimg.py writes the virtual radio's layout (boards/vhost/flash_vhost.c: EEPROM image at 0, settings at
0x11000, journal at 0x19000, flash image at 0x20000). The board description gives the shift to its flash map, so the image is copied there unchanged.
Example: the `dmr-pair` fixture, member 1: channel 1 "Simplex" on 433.5 MHz, DMR, color code 1, timeslot 1, own ID
3100001, contact 3100002.
"""
import os
import tempfile

from board_config import BOARDS, default_board

SPI0_BASE = 0x140A0000          # platform/hrc7000/platform.h
SIZE = 0x1000000                # GD25Q128: 16 MB
JEDEC = bytes((0xC8, 0x40, 0x18))   # GigaDevice, GD25Q128 (capacity code 0x18 = 2^24 bytes)
UID = bytes(range(0x50, 0x60))  # a fixed synthetic unique ID (16 bytes; the driver reads SPI_NOR_UID_LEN of them)
PAGE, SECTOR = 256, 4096


class SpiNor:
    """The chip at the byte level. select(True) starts a command, feed(bytes) returns the bytes the chip drives back,
    select(False) ends it (and runs a page program or erase).

    Example: select(True); feed(b"\\x9f\\0\\0\\0") gives b"\\xff\\xc8\\x40\\x18"; select(False)."""

    def __init__(self, image=None):
        self.mem = bytearray(image) if image is not None else bytearray(b"\xff" * SIZE)
        if len(self.mem) != SIZE:
            raise ValueError("image must be 16 MB")
        self.security = bytearray(b"\xff" * 768)
        self.wel = False
        self.cmd = bytearray()      # every byte clocked in since chip select went active
        self.active = False
        self.reads = self.programs = self.erases = self.commands = 0

    def select(self, active):
        if active and not self.active:
            self.cmd = bytearray()
        elif not active and self.active:
            self._finish()
        self.active = active

    def _addr(self):
        c = self.cmd
        return (c[1] << 16 | c[2] << 8 | c[3]) if len(c) >= 4 else None

    def feed(self, data):
        out = bytearray()
        for b in data:
            i = len(self.cmd)
            self.cmd.append(b)
            out.append(self._out(i))
        return bytes(out)

    def _out(self, i):
        """The byte the chip drives during byte i of the command (byte 0 is the opcode)."""
        op = self.cmd[0]
        if op == 0x9F:
            return JEDEC[i - 1] if 1 <= i <= 3 else 0xFF
        if op == 0x05:
            return 0x02 if self.wel else 0x00     # never busy: program and erase finish at once
        if op in (0x35, 0x15):
            return 0x00 if op == 0x35 else 0x60
        if op in (0x03, 0x0B):
            first = 4 if op == 0x03 else 5
            if i < first:
                return 0xFF
            if i == first:
                self.reads += 1
            return self.mem[(self._addr() + i - first) % SIZE]
        if op == 0x48:
            if i < 5:
                return 0xFF
            a = self._addr()
            reg, off = (a >> 12) & 3, a & 0xFF
            return self.security[((reg - 1) % 3) * 256 + (off + i - 5) % 256]
        if op == 0x4B:
            return UID[(i - 5) % len(UID)] if i >= 5 else 0xFF
        return 0xFF

    def _finish(self):
        c = self.cmd
        if not c:
            return
        self.commands += 1
        op = c[0]
        if op == 0x06:
            self.wel = True
        elif op == 0x04:
            self.wel = False
        elif op == 0x02 and self.wel and len(c) > 4:
            a = self._addr()
            base = a & ~(PAGE - 1)
            for k, b in enumerate(c[4:]):
                p = base + (a + k) % PAGE
                self.mem[p] &= b            # programming only clears bits
            self.programs += 1
            self.wel = False
        elif op == 0x20 and self.wel and len(c) >= 4:
            a = self._addr() & ~(SECTOR - 1)
            self.mem[a:a + SECTOR] = b"\xff" * SECTOR
            self.erases += 1
            self.wel = False


def codeplug_image(fixture="dmr-pair", radio_index=1, board=default_board("qemu")):
    """A 16 MB fixture shifted to the board's flash map, with an optional synthetic calibration page.
    The stock area otherwise stays erased; calibration values are never suitable for hardware."""
    import flashimg    # tools/emu/flashimg.py: compiles the vhost fixture loader with the host C compiler
    with tempfile.TemporaryDirectory(prefix="-flash-") as work:
        path = os.path.join(work, "flash.bin")
        flashimg.build_image(path, fixture, radio_index)
        with open(path, "rb") as f:
            vhost = f.read()
    config = BOARDS[board]["flash"]
    shift = config["vhost_shift"]
    img = bytearray(b"\xff" * SIZE)
    img[shift:] = vhost[:SIZE - shift]
    if "calibration_page" in config:
        page = config["calibration_page"]
        img[page:page + SECTOR] = bytes(SECTOR)
        img[page:page + 8] = bytes((0x00, 0x40, 0x00, 0x47, 0xFF, 0xFF, 0xFF, 0xFF))
        img[page + SECTOR - 1] = 2
    return bytes(img)


class FlashHook:
    """The bridge side: handle(stop) for the stops at hrc_ssi_select and hrc_ssi_xfer. `r` is the RSP link."""

    def __init__(self, chip):
        self.chip = chip
        self.xfers = 0
        self.other = 0          # calls on another SPI base, answered as the old stub (0)

    def select(self, g):
        if g[0] == SPI0_BASE:
            self.chip.select(bool(g[1] & 0xFF))

    def xfer(self, r, g):
        """r0 base, r1 tx (NULL: send 0xFF), r2 rx (NULL: drop), r3 len. Returns the value for r0."""
        if g[0] != SPI0_BASE:
            self.other += 1
            return 0
        tx, rx, n = g[1], g[2], g[3]
        if n == 0:
            return 1
        data = r.read(tx, n) if tx else b"\xff" * n
        out = self.chip.feed(data)
        if rx:
            r.write(rx, out)
        self.xfers += 1
        return 1

    def report(self):
        c = self.chip
        return (f"flash: {self.xfers} SPI0 transfers, {c.commands} commands, {c.reads} reads, {c.programs} page "
                f"programs, {c.erases} erases; {self.other} calls on other SPI buses answered 'failed'")


# --- SPI NAND ------------------------------------------------------------------------------------------------
# The Kioxia TC58CVG1S3HRAIJ as drivers/flash/spi_nand/spi_nand.c drives it: 2048 blocks of 64 pages, each page 2048
# data bytes and 64 spare bytes (ECC on). One page buffer (the cache) between the array and the bus.
NAND_PPB, NAND_BLOCKS, NAND_DATA, NAND_SPARE = 64, 2048, 2048, 64
NAND_PAGE = NAND_DATA + NAND_SPARE
NAND_ID = bytes((0x98, 0xCB))           # Kioxia, TC58CVG1S3HRAIJ (the driver only checks it is not 00 00 or FF FF)
CONFIG_IDR_E = 0x40                     # feature 0xB0 bit 6: page reads give the unique ID page


def synthetic_tune_page(pa=1000):
    """A synthetic TUNE calibration page (boards/db25d/calibration_db25d.c): "TUNE", save counter 1, and
    PA drive `pa` at all 8 points of both PA tables (0x045 high, 0x056 low: a byte, then 8 u16 LE) in both halves
    (UHF at 0, VHF at 0x400). Without it the board refuses every transmission. Every other field is 0: the
    page only lets the radio key up here, its values are not a radio's."""
    page = bytearray(NAND_PAGE)
    page[NAND_DATA:] = b"\xff" * NAND_SPARE
    page[0:8] = b"TUNE" + (1).to_bytes(4, "little")
    for half in (0, 0x400):
        for table in (0x045, 0x056):
            for k in range(8):
                at = half + table + 1 + 2 * k
                page[at:at + 2] = pa.to_bytes(2, "little")
    return bytes(page)


class SpiNand:
    """The chip at the byte level, the same surface as SpiNor: select(True), feed(bytes), select(False).

    Pages live in a dict (page number: 2112 bytes); a page not in it reads erased. Every operation ends at once, so the
    status (feature 0xC0) never shows busy and never shows an ECC or program error.
    Example: select(True); feed(b"\\x9f\\0\\0\\0") gives b"\\xff\\xff\\x98\\xcb"; select(False)."""

    def __init__(self, pages=None):
        self.pages = dict(pages or {})
        self.cache = bytearray(b"\xff" * NAND_PAGE)
        self.features = {0xA0: 0x38, 0xB0: 0x10, 0xC0: 0x00}
        self.wel = False
        self.cmd = bytearray()
        self.active = False
        self.reads = self.programs = self.erases = self.commands = 0

    def select(self, active):
        if active and not self.active:
            self.cmd = bytearray()
        elif not active and self.active:
            self._finish()
        self.active = active

    def feed(self, data):
        out = bytearray()
        for b in data:
            i = len(self.cmd)
            self.cmd.append(b)
            out.append(self._out(i))
        return bytes(out)

    def _out(self, i):
        op = self.cmd[0]
        if op == 0x9F:
            return NAND_ID[i - 2] if 2 <= i <= 3 else 0xFF
        if op == 0x0F:
            if i < 2:
                return 0xFF
            if self.cmd[1] == 0xC0:
                return 0x02 if self.wel else 0x00
            return self.features.get(self.cmd[1], 0)
        if op == 0x03:
            if i < 4:
                return 0xFF
            col = self.cmd[1] << 8 | self.cmd[2]
            return self.cache[(col + i - 4) % NAND_PAGE]
        if op in (0x02, 0x84) and i >= 3:
            col = self.cmd[1] << 8 | self.cmd[2]
            if i == 3 and op == 0x02:
                self.cache[:] = b"\xff" * NAND_PAGE
            p = col + i - 3
            if p < NAND_PAGE:
                self.cache[p] = self.cmd[i]
        return 0xFF

    def _page(self):
        c = self.cmd
        return c[1] << 16 | c[2] << 8 | c[3]

    def _finish(self):
        c = self.cmd
        if not c:
            return
        self.commands += 1
        op = c[0]
        if op == 0x06:
            self.wel = True
        elif op == 0x04:
            self.wel = False
        elif op == 0x1F and len(c) >= 3:
            self.features[c[1]] = c[2]
        elif op == 0x13 and len(c) >= 4:
            self.reads += 1
            if self.features.get(0xB0, 0) & CONFIG_IDR_E:
                copy = UID + bytes(b ^ 0xFF for b in UID)
                self.cache[:] = (copy * 16).ljust(NAND_PAGE, b"\xff")
            else:
                self.cache[:] = self.pages.get(self._page(), b"\xff" * NAND_PAGE)
        elif op == 0x10 and len(c) >= 4 and self.wel:
            page = self._page()
            old = self.pages.get(page, b"\xff" * NAND_PAGE)
            self.pages[page] = bytes(a & b for a, b in zip(old, self.cache))
            self.programs += 1
            self.wel = False
        elif op == 0xD8 and len(c) >= 4 and self.wel:
            first = self._page() & ~(NAND_PPB - 1)
            for p in range(first, first + NAND_PPB):
                self.pages.pop(p, None)
            self.erases += 1
            self.wel = False


def nand_pages(fixture="dmr-pair", radio_index=1, board=None):
    """NAND pages for a fixture, placed in the board's logical map and block range through the firmware's
    sector layer (tools/emu/nand_image.c). Returns {page number: 2112 bytes}."""
    if board is None:
        board = next(b for b, c in BOARDS.items() if c.get("flash", {}).get("kind") == "nand")
    config = BOARDS[board]["flash"]
    import shlex
    import subprocess
    import flashimg
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with tempfile.TemporaryDirectory(prefix="rf-nand-") as work:
        path = os.path.join(work, "flash.bin")
        flashimg.build_image(path, fixture, radio_index)
        with open(path, "rb") as f:
            vhost = f.read()
        logical = bytearray(vhost[:config["sectors"] * SECTOR])
        logical[config["native_address"]:config["native_address"] + 0x100000] = vhost[0xE00000:0xF00000]
        nor = os.path.join(work, "logical.bin")
        with open(nor, "wb") as f:
            f.write(logical)
        tool = os.path.join(work, "nand_image")
        subprocess.run(shlex.split(os.environ.get("CC", "cc")) + ["-std=gnu11", "-O2", "-Wall", "-Werror",
                       f"-I{os.path.join(root, 'drivers')}", "-o", tool,
                       f"-DBLOCKS={config['blocks']}", f"-DMAX_SECTORS={config['sectors']}",
                       os.path.join(root, "tools/emu/nand_image.c"),
                       os.path.join(root, "drivers/flash/nand_sectors/nand_sectors.c")], check=True)
        region = subprocess.run([tool, nor, str(config["sectors"])], capture_output=True, check=True).stdout
    pages = {config["calibration_address"] // NAND_DATA: synthetic_tune_page()}
    first = config["first_block"] * NAND_PPB
    for k in range(len(region) // NAND_PAGE):
        page = region[k * NAND_PAGE:(k + 1) * NAND_PAGE]
        if page.count(0xFF) != NAND_PAGE:
            pages[first + k] = page
    return pages
