// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The real gfx and panel behind the link wrap, plus five-second firmware boots and a packet socket run.
#include <assert.h>
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>
#include "ui/gfx.h"
#include "display_profiles_vhost.h"
#include "vboard.h"
#include "vchan.h"
#include "vclock.h"
#include "vframe.h"
#include "vhost.h"
#include "vtrace.h"

static const vhost_option_t *options[8];
static unsigned n_options, frames, flushes, row_flushes, packets;
static uint64_t now;
static uint8_t panel[240 * 320 * 2], header[VFRAME_HEADER_SIZE];
static size_t received;
static unsigned complete;
static char frame_line[256];

void vhost_option_register(const vhost_option_t *option) { options[n_options++] = option; }
void vhost_finish(const char *why, int code)
{
	fprintf(stderr, "unexpected vhost_finish: %s (%d)\n", why, code);
	exit(1);
}
uint64_t vclock_now_us(void) { return now; }
uint64_t vtrace_hash(const void *data, size_t n) { (void)data; return n; }
void vtrace(const char *fmt, ...)
{
	char line[256];
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(line, sizeof line, fmt, ap);
	va_end(ap);
	if (strcmp(line, "GFX_FLUSH") == 0)
		flushes++;
	if (strncmp(line, "BOARD display.flush ", 20) == 0)
		row_flushes++;
	if (strncmp(line, "FRAME ", 6) == 0) {
		uint32_t id;
		assert(gfx_frame_idle(&id));
		frames++;
		strcpy(frame_line, line);
	}
}
static bool option(const char *name, const char *value)
{
	for (unsigned i = 0; i < n_options; i++)
		if (strcmp(options[i]->name, name) == 0)
			return options[i]->set(value);
	assert(!"missing option");
	return false;
}
static uint64_t get(const uint8_t *p, unsigned n)
{
	uint64_t value = 0;
	for (unsigned i = 0; i < n; i++)
		value |= (uint64_t)p[i] << (8 * i);
	return value;
}
static void put(uint8_t *p, uint64_t value, unsigned n)
{
	for (unsigned i = 0; i < n; i++)
		p[i] = (uint8_t)(value >> (8 * i));
}

static void chunk(const uint8_t *p, size_t n)
{
	assert(n > VFRAME_HEADER_SIZE && n <= VCHAN_PAYLOAD_MAX);
	size_t size = get(p + VFRAME_SIZE_OFF, 4), off = get(p + VFRAME_OFFSET_OFF, 4);
	assert(size > 0 && size <= sizeof panel);
	if (off == 0) {
		assert(received == 0 || received == get(header + VFRAME_SIZE_OFF, 4));
		memcpy(header, p, sizeof header);
		received = 0;
	}
	assert(off == received && n - VFRAME_HEADER_SIZE <= size - received);
	assert(memcmp(header, p, VFRAME_OFFSET_OFF) == 0);
	assert(memcmp(header + VFRAME_WIDTH_OFF, p + VFRAME_WIDTH_OFF,
		VFRAME_HEADER_SIZE - VFRAME_WIDTH_OFF) == 0);
	memcpy(panel + off, p + VFRAME_HEADER_SIZE, n - VFRAME_HEADER_SIZE);
	received += n - VFRAME_HEADER_SIZE;
	packets++;
	if (received == size) {
		vdisplay_snapshot_t s = { .data = panel, .len = size };
		assert(vdisplay_snapshot_crc(&s) == get(header + VFRAME_CRC_OFF, 4));
		complete++;
	}
}
void vchan_send(unsigned kind, const void *buf, size_t n)
{
	assert(kind == VCHAN_FRAME);
	chunk(buf, n);
}

static void draw(void)
{
	unsigned before = frames, before_complete = complete;
	uint32_t id;
	gfx_frame_begin();
	assert(!gfx_frame_idle(&id));
	gfx_flush();
	assert(frames == before + 1 && flushes == frames && complete == before_complete + 1);
	assert(gfx_frame_idle(&id) && get(header + VFRAME_ID_OFF, 4) == id);
	assert(get(header + VFRAME_TIME_OFF, 8) == now);
	assert(get(header + VFRAME_RADIO_OFF, 4) == 7 && get(header + VFRAME_BOOT_OFF, 4) == 3);
	vdisplay_snapshot_t s;
	vdisplay_snapshot(&s);
	assert(received == s.len && memcmp(panel, s.data, s.len) == 0);
	assert(get(header + VFRAME_WIDTH_OFF, 2) == s.width && get(header + VFRAME_HEIGHT_OFF, 2) == s.height);
	assert(header[VFRAME_BPP_OFF] == s.bpp && header[VFRAME_BACKLIGHT_OFF] == s.backlight);
	assert(header[VFRAME_HAS_PALETTE_OFF] == s.has_palette && header[39] == 0);
	for (unsigned i = 0; i < VDISPLAY_PALETTE_N; i++)
		assert(get(header + VFRAME_PALETTE_OFF + i * 2, 2) == (s.has_palette ? s.palette[i] : 0));
	char expected[256], pal[65] = "-";
	if (s.has_palette)
		for (unsigned i = 0; i < VDISPLAY_PALETTE_N; i++)
			snprintf(pal + 4 * i, 5, "%04x", s.palette[i]);
	snprintf(expected, sizeof expected, "FRAME radio=7 boot=3 id=%u us=%llu crc=%08x palette=%s backlight=%u",
		id, (unsigned long long)now, vdisplay_snapshot_crc(&s), pal, s.backlight);
	assert(strcmp(frame_line, expected) == 0);
}

static void unit(void)
{
	assert(option("--radio-id", "4294967295"));
	assert(option("--boot-id", "0"));
	const char *bad[] = { "", "-1", "+1", " 1", "1x", "4294967296", "18446744073709551616" };
	for (unsigned i = 0; i < sizeof bad / sizeof bad[0]; i++) {
		assert(!option("--radio-id", bad[i]));
		assert(!option("--boot-id", bad[i]));
	}
	assert(option("--radio-id", "7") && option("--boot-id", "3"));
	const char *profiles[] = { "rgb565-240x320", "indexed-160x128", "mono-128x64" };
	for (unsigned i = 0; i < sizeof profiles / sizeof profiles[0]; i++) {
		assert(vdisplay_select(profiles[i]));
		const hal_display_t *d = vhost_part_display();
		assert(gfx_init(d));
		now = (1ull << 40) + i;
		d->set_backlight(73);
		gfx_clear(0);
		draw();
		unsigned rows = row_flushes;
		uint32_t crc = get(header + VFRAME_CRC_OFF, 4);
		draw(); // unchanged pixels still complete a frame
		assert(row_flushes == rows && get(header + VFRAME_CRC_OFF, 4) == crc);
		gfx_pixel(0, 1, 0xffff);
		gfx_pixel(0, 3, 0xffff);
		draw();
		assert(row_flushes == rows + 2 && get(header + VFRAME_CRC_OFF, 4) != crc);
		d->set_backlight(19);
		gfx_clear(0xf800);
		draw();
		assert(now == (1ull << 40) + i);
	}
	printf("vframe unit: %u flushes, %u FRAME lines, %u complete panels, %u packets; PASSED\n",
		flushes, frames, complete, packets);
}

static void wait_ok(pid_t pid)
{
	int status;
	assert(waitpid(pid, &status, 0) == pid);
	assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
}

static void boot(unsigned run)
{
	char path[512], extracted[512];
	snprintf(path, sizeof path, "%s/vframe-boot-%u.log", VFRAME_OUT, run);
	snprintf(extracted, sizeof extracted, "%s/vframe-boot-%u.frames", VFRAME_OUT, run);
	fflush(stdout);
	pid_t pid = fork();
	assert(pid >= 0);
	if (pid == 0) {
		execl(VFRAME_ELF, VFRAME_ELF, "--until-ms", "5000", "--no-stall-watch", "--trace", path,
			"--radio-id", "7", "--boot-id", "3", (char *)NULL);
		perror("exec virtual radio");
		_exit(1);
	}
	wait_ok(pid);
	FILE *in = fopen(path, "r"), *out = fopen(extracted, "w");
	assert(in && out);
	char line[512];
	unsigned n_frames = 0, n_flushes = 0;
	bool pending = false;
	while (fgets(line, sizeof line, in)) {
		if (strstr(line, " GFX_FLUSH\n")) {
			assert(!pending);
			pending = true;
			n_flushes++;
		}
		char *p = strstr(line, " FRAME ");
		if (p) {
			unsigned id;
			assert(pending && sscanf(p, " FRAME radio=7 boot=3 id=%u", &id) == 1);
			assert(id == ++n_frames);
			pending = false;
			assert(fputs(line, out) >= 0);
		}
	}
	assert(!ferror(in) && !pending && n_frames > 1 && n_frames == n_flushes);
	assert(fclose(in) == 0 && fclose(out) == 0);
	printf("boot %u (5 s): gfx_flush=%u FRAME=%u, ids=1..%u; PASSED\n", run, n_flushes, n_frames, n_frames);
}

static void channel(void)
{
	int pair[2];
	if (socketpair(AF_UNIX, SOCK_SEQPACKET, 0, pair) != 0) {
		perror("vframeTest socketpair");
		exit(1);
	}
	fflush(stdout);
	pid_t pid = fork();
	assert(pid >= 0);
	if (pid == 0) {
		close(pair[0]);
		char fd[24];
		snprintf(fd, sizeof fd, "%d", pair[1]);
		execl(VFRAME_ELF, VFRAME_ELF, "--until-ms", "5000", "--no-stall-watch", "--trace", "/dev/null",
			"--radio-id", "7", "--boot-id", "3", "--chan", fd, (char *)NULL);
		perror("exec channel radio");
		_exit(1);
	}
	close(pair[1]);
	unsigned trace_frames = 0, last_id = 0;
	uint64_t trace_time = 0;
	uint32_t trace_crc = 0;
	for (;;) {
		uint8_t wire[VCHAN_HEADER_SIZE + VCHAN_PAYLOAD_MAX];
		ssize_t n = recv(pair[0], wire, sizeof wire, MSG_TRUNC);
		assert(n >= VCHAN_HEADER_SIZE && (size_t)n <= sizeof wire);
		assert(wire[0] == VCHAN_VERSION && get(wire + VCHAN_LENGTH_OFF, 2) == (size_t)n - VCHAN_HEADER_SIZE);
		const uint8_t *p = wire + VCHAN_HEADER_SIZE;
		size_t len = (size_t)n - VCHAN_HEADER_SIZE;
		if (wire[1] == VCHAN_DONE) {
			uint8_t grant[VCHAN_HEADER_SIZE] = { VCHAN_VERSION, VCHAN_GRANT };
			put(grant + VCHAN_TIME_OFF, 5000001, 8);
			assert(send(pair[0], grant, sizeof grant, MSG_NOSIGNAL) == sizeof grant);
		} else if (wire[1] == VCHAN_TRACE) {
			char line[VCHAN_PAYLOAD_MAX + 1];
			memcpy(line, p, len);
			line[len] = '\0';
			char *f = strstr(line, " FRAME ");
			if (f) {
				unsigned id, crc;
				unsigned long long time;
				assert(complete == trace_frames);
				assert(sscanf(f, " FRAME radio=7 boot=3 id=%u us=%llu crc=%x", &id, &time, &crc) == 3);
				assert(id == ++trace_frames);
				trace_time = time;
				trace_crc = crc;
			}
		} else if (wire[1] == VCHAN_FRAME) {
			chunk(p, len);
			assert(get(header + VFRAME_RADIO_OFF, 4) == 7 && get(header + VFRAME_BOOT_OFF, 4) == 3);
			assert(get(header + VFRAME_ID_OFF, 4) == trace_frames);
			assert(get(header + VFRAME_TIME_OFF, 8) == trace_time);
			assert(get(wire + VCHAN_TIME_OFF, 8) == trace_time);
			assert(get(header + VFRAME_CRC_OFF, 4) == trace_crc);
			if (complete > last_id)
				assert(complete == ++last_id);
		} else if (wire[1] == VCHAN_RESTART_REASON) {
			assert(len >= strlen("virtual time limit") && memcmp(p, "virtual time limit", strlen("virtual time limit")) == 0);
			break;
		}
	}
	close(pair[0]);
	wait_ok(pid);
	assert(complete > 1 && complete == trace_frames && received == get(header + VFRAME_SIZE_OFF, 4));
	printf("vframe channel: %u FRAME lines, %u complete panels, %u packets; PASSED\n", trace_frames, complete, packets);
}

int main(int argc, char **argv)
{
	alarm(30);
	if (argc == 2 && strcmp(argv[1], "--channel") == 0) {
		channel();
	} else {
		assert(argc == 1);
		unit();
		boot(1);
		boot(2);
		FILE *a = fopen(VFRAME_OUT "/vframe-boot-1.frames", "r");
		FILE *b = fopen(VFRAME_OUT "/vframe-boot-2.frames", "r");
		assert(a && b);
		int c;
		do { c = fgetc(a); assert(c == fgetc(b)); } while (c != EOF);
		assert(!ferror(a) && !ferror(b));
		fclose(a);
		fclose(b);
		puts("vframeTest: identical FRAME lists; PASSED");
	}
	return 0;
}
