// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Export the actual vhost fixture bytes, including the production settings and journal records.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "vboard.h"
#include "vhost.h"
#include "vtrace.h"

static const band_t bands[] = { { 400000000u, 480000000u, true } };
const board_t board = { .name = "Virtual", .flash = &vboard_flash, .bands = bands, .band_count = 1, .power_count = 2 };
void storage_lock(void) {}
void storage_unlock(void) {}
void storage_quick_lock(void) {}
void storage_quick_unlock(void) {}
void vtrace(const char *fmt, ...) { (void)fmt; }
uint64_t vtrace_hash(const void *data, size_t n) { (void)data; (void)n; return 0; }
void vhost_finish_hook_register(vhost_finish_fn fn) { (void)fn; }

static const vhost_option_t *options[4];
static unsigned n_options;
void vhost_option_register(const vhost_option_t *opt)
{
	if (n_options == sizeof options / sizeof options[0])
		abort();
	options[n_options++] = opt;
}

void vhost_finish(const char *why, int code)
{
	fprintf(stderr, "%s\n", why);
	exit(code);
}

static bool option(const char *name, const char *value)
{
	for (unsigned i = 0; i < n_options; i++)
		if (strcmp(options[i]->name, name) == 0)
			return options[i]->set(value);
	return false;
}

int main(int argc, char **argv)
{
	if (argc != 3 || !option("--storage", argv[1]) || !option("--radio-index", argv[2]))
		return 2;
	vboard_flash_boot();
	uint8_t sector[4096];
	for (uint32_t at = 0; at < vboard_flash.size; at += sizeof sector) {
		if (!vboard_flash.read(at, sector, sizeof sector) ||
		    fwrite(sector, 1, sizeof sector, stdout) != sizeof sector)
			return 1;
	}
	return fflush(stdout) == 0 ? 0 : 1;
}
