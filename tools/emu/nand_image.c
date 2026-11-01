// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Host tool for tools/emu/hrc7000_flash.py: a board's NAND region with a
// NOR image written into it through the firmware's own sector layer (drivers/flash/nand_sectors).
//
// BLOCKS and MAX_SECTORS are supplied by the board emulator description at compile time.
// Usage: nand_image <nor.bin> <sectors> > region.bin
// The first <sectors> x 4 KB of nor.bin go in, sector by sector, skipping all-0xFF sectors (they read 0xFF anyway).
// Out: the region's raw pages, block by block, each page 2048 data bytes and 64 spare bytes, as the chip holds them.
// Example: 32 blocks x 64 pages x 2112 bytes = 4,325,376 bytes.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "flash/nand_sectors/nand_sectors.h"

#define PPB		64u
#define DATA		2048u
#define SPARE		64u
#define PAGE		(DATA + SPARE)

static uint8_t chip[BLOCKS * PPB * PAGE];

static bool dev_read(void *ctx, uint32_t page, uint16_t column, void *buf, uint32_t len)
{
	(void)ctx;
	if (page >= BLOCKS * PPB || column + len > PAGE)
		return false;
	memcpy(buf, chip + page * PAGE + column, len);
	return true;
}

static nand_result_t dev_program(void *ctx, uint32_t page, uint32_t src, const nand_chunk_t *chunks, unsigned n)
{
	(void)ctx;
	uint8_t buf[PAGE];
	if (page >= BLOCKS * PPB)
		return NAND_ERROR;
	if (src == NAND_SECTORS_NO_PAGE)
		memset(buf, 0xFF, sizeof buf);
	else
		memcpy(buf, chip + src * PAGE, PAGE);
	for (unsigned i = 0; i < n; i++)
		memcpy(buf + chunks[i].column, chunks[i].data, chunks[i].len);
	for (uint32_t i = 0; i < PAGE; i++)
		chip[page * PAGE + i] &= buf[i];
	return NAND_OK;
}

static nand_result_t dev_erase(void *ctx, uint16_t block)
{
	(void)ctx;
	if (block >= BLOCKS)
		return NAND_ERROR;
	memset(chip + (uint32_t)block * PPB * PAGE, 0xFF, PPB * PAGE);
	return NAND_OK;
}

static bool dev_block_bad(void *ctx, uint16_t block, bool *bad)
{
	(void)ctx;
	*bad = chip[(uint32_t)block * PPB * PAGE + DATA] != 0xFF;
	return true;
}

static void dev_mark_bad(void *ctx, uint16_t block)
{
	(void)ctx;
	chip[(uint32_t)block * PPB * PAGE + DATA] = 0;
}

static const nand_dev_t dev = {
	.pages_per_block = PPB, .blocks = BLOCKS, .spare_size = SPARE,
	.read = dev_read, .program = dev_program, .erase = dev_erase, .block_bad = dev_block_bad, .mark_bad = dev_mark_bad,
};

static uint16_t sector_map[MAX_SECTORS];
static uint8_t block_live[BLOCKS], block_flags[BLOCKS];
static uint32_t block_order[BLOCKS];

int main(int argc, char **argv)
{
	if (argc != 3) {
		fprintf(stderr, "usage: nand_image <nor.bin> <sectors>\n");
		return 2;
	}
	unsigned sectors = (unsigned)strtoul(argv[2], NULL, 0);
	if (sectors == 0 || sectors > MAX_SECTORS)
		return 2;
	nand_sectors_t layer = { .dev = &dev, .sectors = (uint16_t)sectors, .map = sector_map, .live = block_live,
				 .flags = block_flags, .order = block_order };
	memset(chip, 0xFF, sizeof chip);
	if (!nand_sectors_mount(&layer)) {
		fprintf(stderr, "nand_image: mount failed\n");
		return 1;
	}
	FILE *in = fopen(argv[1], "rb");
	if (in == NULL)
		return 1;
	static uint8_t sector[NAND_SECTORS_SECTOR];
	unsigned written = 0;
	for (unsigned s = 0; s < sectors; s++) {
		size_t got = fread(sector, 1, sizeof sector, in);
		if (got < sizeof sector)
			memset(sector + got, 0xFF, sizeof sector - got);
		bool blank = true;
		for (size_t i = 0; i < sizeof sector && blank; i++)
			blank = sector[i] == 0xFF;
		if (blank)
			continue;
		if (!nand_sectors_write(&layer, s * NAND_SECTORS_SECTOR, sector, sizeof sector)) {
			fprintf(stderr, "nand_image: sector %u write failed\n", s);
			return 1;
		}
		written++;
	}
	fclose(in);
	fwrite(chip, 1, sizeof chip, stdout);
	fprintf(stderr, "nand_image: %u sectors written\n", written);
	return 0;
}
