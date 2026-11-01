// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Real codeplug/settings editors and virtual NOR; generated flash only, no test copies of the encoders.
#include <setjmp.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include "vboard.h"
#include "vhost.h"
#include "vtrace.h"
#include "storage_vhost.h"
#include "codeplug/codeplug.h"
#include "settings/settings.h"
#include "storage/storage.h"

static int failures;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); failures++; } } while (0)

static const band_t bands[] = { { 400000000u, 480000000u, true } };
const board_t board = { .name = "Virtual", .flash = &vboard_flash, .bands = bands, .band_count = 1, .power_count = 2 };
void storage_lock(void) {}
void storage_unlock(void) {}
void storage_quick_lock(void) {}
void storage_quick_unlock(void) {}

static const vhost_option_t *options[4];
static unsigned n_options, stores;
static char last_store[80];
static jmp_buf ended;
static int end_code;

void vhost_option_register(const vhost_option_t *opt)
{
	if (n_options < 4)
		options[n_options++] = opt;
}

static bool option(const char *name, const char *value)
{
	for (unsigned i = 0; i < n_options; i++)
		if (strcmp(options[i]->name, name) == 0)
			return options[i]->set(value);
	return false;
}

void vtrace(const char *fmt, ...)
{
	char line[512] = "000000000000 0000000000 ";
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(line + 24, sizeof line - 24, fmt, ap);
	va_end(ap);
	if (strncmp(line + 24, "STORE ", 6) == 0) {
		stores++;
		snprintf(last_store, sizeof last_store, "%.79s", line + 24);
	}
}

static void (*finish_hook)(void);	// storage_vhost.c registers its closing STORE here (VHOST_FINISH_HOOK)
void vhost_finish_hook_register(void (*fn)(void)) { finish_hook = fn; }

uint64_t vtrace_hash(const void *data, size_t n)
{
	(void)data;
	(void)n;
	return 0;
}

void vhost_finish(const char *why, int code)
{
	(void)why;
	end_code = code;
	longjmp(ended, 1);
}

static void boot(const char *fixture)
{
	CHECK(option("--storage", fixture));
	unsigned before = stores;
	vboard_flash_boot();
	CHECK(stores == before + 1);
	CHECK(strlen(last_store) == 70);
	CHECK(storage_init());
	settings_init();
	codeplug_init();
}

static void pair(void)
{
	CHECK(option("--radio-index", "2"));
	boot("dmr-pair");
	CHECK(codeplug_dmr_id() == 3100002);
	char name[CODEPLUG_NAME_LEN + 1];
	codeplug_radio_name(name);
	CHECK(strcmp(name, "N0SIM") == 0);
	channel_t ch;
	CHECK(codeplug_channel(1, &ch));
	CHECK(ch.rx_hz == 433500000 && ch.tx_hz == ch.rx_hz && ch.mode == RADIO_MODE_DMR);
	CHECK(ch.color_code == 1 && ch.timeslot == 1 && ch.contact == 1 && ch.dmo);
	zone_t z;
	CHECK(codeplug_zone(0, &z));
	CHECK(z.count == 1 && z.channels[0] == 1);
	contact_t c;
	CHECK(codeplug_contact(1, &c));
	CHECK(c.id == 3100001 && c.type == CONTACT_PRIVATE && c.timeslot == 1);
	settings_t s;
	settings_read(&s);
	CHECK(s.channel == 1 && s.vfo == 0);
	char first[65], second[65];
	CHECK(vstorage_digest(first));
	boot("dmr-pair");
	CHECK(vstorage_digest(second));
	CHECK(strcmp(first, second) == 0);
	CHECK(option("--radio-index", "1"));
	boot("dmr-pair");
	CHECK(codeplug_dmr_id() == 3100001);
	CHECK(codeplug_contact(1, &c) && c.id == 3100002);
	CHECK(vstorage_digest(second));
	CHECK(strcmp(first, second) != 0);
	printf("pair: radio 2 ID=3100002, private peer=3100001, CC=1 TS=1; repeat STORE %s\n", first);
}

// N radios on one air: member k is 3100000+k with the same channel and talkgroup 9 contact
static void group(void)
{
	CHECK(option("--radio-index", "4"));
	boot("dmr-group");
	CHECK(codeplug_dmr_id() == 3100004);
	char name[CODEPLUG_NAME_LEN + 1];
	codeplug_radio_name(name);
	CHECK(strcmp(name, "N0SIM") == 0);
	channel_t ch;
	CHECK(codeplug_channel(1, &ch));
	CHECK(ch.rx_hz == 433500000 && ch.tx_hz == ch.rx_hz && ch.mode == RADIO_MODE_DMR);
	CHECK(ch.color_code == 1 && ch.timeslot == 1 && ch.contact == 1 && ch.dmo);
	zone_t z;
	CHECK(codeplug_zone(0, &z) && z.count == 1 && z.channels[0] == 1);
	contact_t c;
	CHECK(codeplug_contact(1, &c));
	CHECK(c.id == 9 && c.type == CONTACT_GROUP && c.timeslot == 1);
	settings_t s;
	settings_read(&s);
	CHECK(s.channel == 1 && s.vfo == 0);
	char first[65], second[65];
	CHECK(vstorage_digest(first));
	boot("dmr-group");
	CHECK(vstorage_digest(second) && strcmp(first, second) == 0);
	CHECK(option("--radio-index", "999"));
	boot("dmr-group");
	CHECK(codeplug_dmr_id() == 3100999);
	CHECK(vstorage_digest(second) && strcmp(first, second) != 0);
	CHECK(option("--radio-index", "1"));
	printf("group: radio 4 ID=3100004, talkgroup 9, CC=1 TS=1; repeat STORE %s\n", first);
}

static void blank(void)
{
	CHECK(option("--storage", "blank"));
	vboard_flash_boot();
	char erased[65], changed[65];
	CHECK(vstorage_digest(erased));
	// Independent oracle: hashlib.sha256(b'\xff' * 0x1000000).hexdigest().
	CHECK(strcmp(erased, "dffab0dd410657cb30c7b2fd7f2586a4792e8472e58882b3532581f8111a646d") == 0);
	uint8_t last = 0;
	CHECK(vboard_flash.write(vboard_flash.size - 1, &last, 1));
	CHECK(vstorage_digest(changed));
	CHECK(strcmp(erased, changed) != 0);
	printf("blank SHA-256 matches independent oracle: STORE %s\n", erased);
}

static void step_list(void)
{
	static const char steps[] = "steps:channel=1,Simplex,433500000,433500000,dmr,1,1,1;"
		"zone=0,Pair,1;contact=1,Peer,3100001,private,1;dmr_id=3100002;radio_name=N0SIM;"
		"setting=channel,1;setting=brightness,70";
	char first[65], again[65], changed[65];
	boot(steps);
	CHECK(vstorage_digest(first));
	settings_t s;
	settings_read(&s);
	CHECK(s.brightness == 70 && s.channel == 1);
	boot(steps);
	CHECK(vstorage_digest(again));
	CHECK(strcmp(first, again) == 0);
	char different[sizeof steps];
	strcpy(different, steps);
	different[sizeof steps - 2] = '1';
	boot(different);
	CHECK(vstorage_digest(changed));
	CHECK(strcmp(first, changed) != 0);
	printf("same steps twice: STORE %s\n", first);
	printf("brightness 70 -> 71: STORE %s\n", changed);
	unsigned before = stores;
	CHECK(finish_hook != NULL);
	finish_hook();	// the run ends (virtual time limit): the closing STORE comes before END
	CHECK(stores == before + 1 && strcmp(last_store + 6, changed) == 0);
	finish_hook();	// the run ends again (reboot)
	CHECK(stores == before + 2 && strcmp(last_store + 6, changed) == 0);
	vtrace("END a trace line alone");	// an END line by itself no longer writes STORE
	CHECK(stores == before + 2);
	puts("STORE at each boot, run end and reboot: PASSED");
}

static void power_cut(void)
{
	const char *path = "vstorageTest.powercut.tmpfile";
	CHECK(option("--flash-file", path));
	boot("steps:setting=brightness,70");
	settings_t *live = settings_begin();
	live->brightness = 71;
	settings_end(10);
	CHECK(settings_flush());
	char before[65], after[65];
	CHECK(vstorage_digest(before));
	CHECK(option("--restore-flash", path));
	boot("steps:setting=brightness,70");	// The fixture must not overwrite saved 71 during restore.
	settings_t s;
	settings_read(&s);
	CHECK(s.brightness == 71);
	CHECK(vstorage_digest(after) && strcmp(before, after) == 0);
	live = settings_begin();
	live->brightness = 72;
	settings_end(20);	// Still only in RAM at the cut.
	boot("steps:setting=brightness,70");
	settings_read(&s);
	CHECK(s.brightness == 71);
	CHECK(option("--restore-flash", NULL));
	CHECK(option("--flash-file", NULL));
	CHECK(unlink(path) == 0);
	puts("power cut: saved brightness 71 survives fixture 70; unsaved 72 is lost: PASSED");
}

static void invalid(void)
{
	static const char *const bad[] = {
		"image.bin", "steps:", "steps:dmr_id=3100001;", "steps:dmr_id=3100001;;radio_name=N0SIM",
		"steps:dmr_id=3100000", "steps:dmr_id=3101000", "steps:dmr_id=-1", "steps:dmr_id=4298067297",
		"steps:radio_name=REALCALL", "steps:setting=brightness,9", "steps:setting=beeps,2",
		"steps:setting=privacy_key,1", "steps:zone=0,Pair,0", "steps:zone=0,,1",
		"steps:zone=0,Pair,1,", "steps:contact=0,Peer,3100002,private,1",
		"steps:contact=1,Peer,9,private,1", "steps:contact=1,TG,91,group,1",
		"steps:contact=1,TG,3100002,group,1", "steps:contact=1,Peer,3101000,private,1",
		"steps:channel=1,Simplex,433500000,433500000,dmr,16,1,1",
		"steps:channel=1,Simplex,433500000,433500000,dmr,1,3,1",
		"steps:channel=1,Name is too long!!,433500000,433500000,dmr,1,1,1",
	};
	for (unsigned i = 0; i < sizeof bad / sizeof bad[0]; i++)
		CHECK(!option("--storage", bad[i]));
	CHECK(!option("--radio-index", "0"));
	CHECK(!option("--radio-index", "1000"));
	CHECK(!option("--radio-index", "4294967297"));
	CHECK(option("--storage", "steps:zone=0,Missing,1024"));
	if (setjmp(ended) == 0) {
		vboard_flash_boot();
		CHECK(false);
	} else {
		CHECK(end_code == 2);
	}
	// dmr-pair has only two members: index 3 ends the boot with exit 2
	CHECK(option("--radio-index", "3"));
	CHECK(option("--storage", "dmr-pair"));
	end_code = 0;
	if (setjmp(ended) == 0) {
		vboard_flash_boot();
		CHECK(false);
	} else {
		CHECK(end_code == 2);
	}
	puts("invalid syntax, identities, ranges and editor failure: PASSED");
}

int main(void)
{
	CHECK(n_options == 4);
	if (setjmp(ended) != 0) {
		printf("FAIL unexpected finish, exit %d\n", end_code);
		return 1;
	}
	blank();
	pair();
	group();
	step_list();
	power_cut();
	invalid();
	printf("# vstorageTest: %s\n", failures ? "FAILED" : "PASSED");
	return failures != 0;
}
