// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#include "bt/atmodule/atmodule.h"
#include "fuzz.h"
#include <assert.h>
#include <stdio.h>

static atmodule_t s;
static bool power, read_error;
static uint8_t output[8192], input[4096];
static size_t out_len, in_len, in_at, write_limit;
static uint32_t now, errors, ends;
static unsigned cases;
static bool start(void) { return true; }
static bool connected(void) { return true; }
static void enable(bool on) { power = on; }
static void flush(void) { in_len = in_at = 0; }
static uint32_t error_count(void) { return errors; }
static uint32_t clock_ms(void) { return now; }
static void end_session(void) { ends++; }
static size_t rd(uint8_t *b, size_t n)
{
	if (n > in_len - in_at) n = in_len - in_at;
	memcpy(b, input + in_at, n);
	in_at += n;
	if (read_error) errors++;
	return n;
}
static size_t wr(const uint8_t *b, size_t n)
{
	if (n > write_limit) n = write_limit;
	assert(n <= sizeof(output) - out_len);
	memcpy(output + out_len, b, n);
	out_len += n;
	return n;
}
static bool wait_rx(uint32_t ms) { now += ms; return in_at != in_len; }
static const hal_stream_t uart = { start, connected, rd, wait_rx, wr };
static atmodule_profile_t profile;
static bt_module_t module = { &uart, enable, flush, error_count, &profile, 10, 20 };
static void feed(const char *v) { atmodule_feed(&s, (const uint8_t *)v, strlen(v), now); }
static void tick(uint32_t ms) { now += ms; atmodule_step(&s, now); }
static void init(void)
{
	profile = (atmodule_profile_t){ "test", NULL, 20, 5000, 2000, 500 };
	out_len = in_len = in_at = errors = ends = now = 0;
	write_limit = sizeof(output);
	read_error = false;
	atmodule_init(&s, &module, now);
	assert(!power);
	assert(atmodule_enable(&s, now));
	tick(19); assert(!power);
	tick(1); assert(power);
}
static void identify(void)
{
	feed("+READY\r\n");
	assert(s.state == ATMODULE_IDENTIFYING);
	tick(0);
	assert(out_len == 9 && memcmp(output, "AT+VER?\r\n", 9) == 0);
	feed("+VER:test\r\n");
	assert(s.state == ATMODULE_IDENTIFYING);
	feed("OK\r\n");
	assert(s.state == ATMODULE_IDLE);
	out_len = 0;
}
static void setup(void) { init(); identify(); }
static size_t envelope(uint8_t *dst, const uint8_t *payload, size_t n)
{
	int h = sprintf((char *)dst, "+DATA:00,%u\r\n", (unsigned)n);
	memcpy(dst + h, payload, n);
	return (size_t)h + n;
}
static void connect_data(void)
{
	uint8_t b;
	feed("+DATA:00,1\r\nx");
	assert(s.state == ATMODULE_CONNECTED);
	assert(atmodule_read(&s, &b, 1) == 1 && b == 'x');
}
static void lifecycle(void)
{
	init(); tick(4999); assert(s.state == ATMODULE_STARTING);
	tick(1); assert(s.state == ATMODULE_FAULT && !power); cases++;
	init(); feed("+READY\r\n"); tick(2000); assert(s.state == ATMODULE_FAULT); cases++;
	init(); feed("+READY\r\n"); tick(0); feed("OK\r\n");
	assert(s.state == ATMODULE_IDENTIFYING); feed("+VER:test\r\n");
	assert(s.state == ATMODULE_IDLE); cases++;
	init(); feed("+READY\r\n"); tick(0); feed("+VER:other\r\n");
	assert(s.state == ATMODULE_FAULT); cases++;
	setup(); feed("+CONN:peer,AG\r\n+HOSTDEVICE:peer,AG\r\n");
	assert(s.state == ATMODULE_IDLE);
	profile.data_profile = "DATA";
	feed("+CONN:peer,DATAx\r\n"); assert(s.state == ATMODULE_IDLE);
	feed("+HOSTDEVICE:peer,DATA\r\n"); assert(s.state == ATMODULE_CONNECTED);
	feed("+DISC:any\r\n"); assert(s.state == ATMODULE_IDLE); cases++;
	profile.data_profile = NULL;
	connect_data(); feed("+PTTKEY:1\r\n+PTTKEY:0\r\n+PTTKEY:3\r\n+PTTKEY:6\r\n+PTTBATT:100\r\n");
	assert(s.counts.ptt == 5 && s.rx_len == s.rx_at && s.tx_len == 0 && out_len == 0); cases++;
	feed("+READY\r\n"); assert(s.state == ATMODULE_IDENTIFYING && s.rx_len == 0);
	tick(0); feed("OK\r\n+VER:test\r\n"); assert(s.state == ATMODULE_IDLE); cases++;
	atmodule_disable(&s, now); assert(!power && s.state == ATMODULE_DISABLED);
	assert(atmodule_enable(&s, now)); tick(19); assert(!power); tick(1); assert(power); cases++;
	atmodule_disable(&s, now); profile.tx_limit = 509; assert(!atmodule_enable(&s, now));
	profile.tx_limit = 0; assert(!atmodule_enable(&s, now)); cases++;
}
static void partitions(void)
{
	uint8_t payload[999], frame[1024], got[1200];
	for (unsigned i = 0; i < sizeof(payload); i++) payload[i] = (uint8_t)i;
	memcpy(payload + 256, "+DISC\r\n+READY\r\n+PTTKEY:1\r\nOK\r\n+DATA:00,1\r\n", 42);
	const unsigned lengths[] = {1,20,244,508,999};
	for (unsigned l = 0; l < sizeof(lengths)/sizeof(lengths[0]); l++) {
		size_t n = lengths[l], wire = envelope(frame, payload, n);
		for (size_t cut = 0; cut <= wire; cut++) {
			setup();
			atmodule_feed(&s, frame, cut, now);
			if (cut < wire) assert(s.rx_len == 0);
			atmodule_feed(&s, frame + cut, wire - cut, now);
			assert(atmodule_read(&s, got, sizeof(got)) == n);
			assert(memcmp(got, payload, n) == 0 && s.counts.ptt == 0);
			cases++;
		}
	}
	setup(); size_t wire = envelope(frame, payload, sizeof(payload));
	for (size_t i = 0; i < wire; i++) atmodule_feed(&s, frame+i, 1, now);
	assert(atmodule_read(&s, got, sizeof(got)) == sizeof(payload));
	assert(memcmp(got, payload, sizeof(payload)) == 0); cases++;
	setup(); feed("\r\n+DATA:01,3\r\nxyz+DATA:00,1\r\nx+DATA:00,1\r\ny");
	assert(atmodule_read(&s, got, sizeof(got)) == 2 && memcmp(got,"xy",2) == 0);
	assert(s.counts.wrong_channel == 1); cases++;
}
static void invalid(void)
{
	const char *bad[] = { "+DATA:00,0\r\n", "+DATA:00,1000\r\n", "+DATA:00,999999999999999999999999\r\n",
		"+DATA:00,\r\n", "+DATA:00,-1\r\n", "+DATA:00,1x\r\n", "+DATA:00;1\r\n", "+DATA:0,1\r\n",
		"+DATA:aa,1\r\n", "+DATA:00,1\nx", "+DATA:00,1\rx", "+DATAfoo\r\n" };
	for (unsigned i=0; i<sizeof(bad)/sizeof(bad[0]); i++) {
		setup(); feed(bad[i]); assert(s.state == ATMODULE_FAULT && !power); cases++;
	}
	setup(); uint8_t b[129]; memset(b, 'a', sizeof(b));
	atmodule_feed(&s, b, sizeof(b), now); assert(s.state == ATMODULE_FAULT); cases++;
	for (unsigned gap=499; gap<=501; gap++) {
		setup(); feed("+DAT"); tick(gap);
		assert((s.state == ATMODULE_FAULT) == (gap >= 500)); cases++;
	}
	setup(); feed("+DATA:00,20\r\na+DISC\r\n");
	assert(s.state == ATMODULE_IDLE && s.rx_len == 0);
	tick(500); assert(s.state == ATMODULE_FAULT && s.rx_len == 0); cases++;
	// A disconnect at every incomplete frame position cannot leak a partial payload.
	const char *frame = "+DATA:00,20\r\nabcdefghijklmnopqrst";
	for (size_t cut=1; cut<strlen(frame); cut++) {
		setup(); atmodule_feed(&s, (const uint8_t *)frame, cut, now);
		atmodule_disable(&s, now);
		assert(s.rx_len == 0 && s.tx_len == 0 && !power); cases++;
	}
	setup(); errors++; tick(0); assert(s.state == ATMODULE_FAULT && s.counts.overruns == 1); cases++;
	setup(); memcpy(input, "+DATA:00,1\r\nx", 13); in_len = 13; read_error = true;
	tick(0); assert(s.state == ATMODULE_FAULT && s.rx_len == 0); cases++;
	setup(); feed("+DATA:00,2\r\nx"); now += 500; feed("y"); assert(s.state == ATMODULE_FAULT); cases++;
}
static void transmit(void)
{
	uint8_t payload[ATMODULE_QUEUE+1]; memset(payload, 0xA5, sizeof(payload));
	setup(); connect_data(); write_limit = 1;
	assert(atmodule_write(&s, payload, 41) == 41);
	for (unsigned i=0; i<35; i++) tick(1);
	assert(out_len == 35 && memcmp(output,"AT+DATA=00,20\r\n",15) == 0);
	assert(memcmp(output+15,payload,20) == 0);
	for (unsigned i=0; i<50; i++) tick(1);
	assert(out_len == 35); feed("OK\r\n");
	for (unsigned i=0; i<35; i++) tick(1);
	assert(out_len == 70); feed("OK\r\n");
	for (unsigned i=0; i<15; i++) tick(1);
	assert(out_len == 85 && memcmp(output+70,"AT+DATA=00,1\r\n",14) == 0);
	feed("OK\r\n"); assert(!s.pending); cases++;
	setup(); connect_data(); assert(atmodule_write(&s,payload,sizeof(payload)) == ATMODULE_QUEUE);
	assert(atmodule_write(&s,payload,1) == 0); cases++;
	tick(0); size_t sent=out_len; tick(1999); assert(out_len==sent && s.state==ATMODULE_CONNECTED);
	tick(1); assert(s.state==ATMODULE_FAULT && s.tx_len==0); cases++;
	setup(); connect_data(); write_limit=0; atmodule_write(&s,payload,1); tick(0);
	feed("OK\r\n"); assert(s.pending); tick(2000); assert(s.state==ATMODULE_FAULT); cases++;
	setup(); connect_data(); profile.tx_limit=508; atmodule_write(&s,payload,508); tick(0);
	assert(out_len==524 && memcmp(output,"AT+DATA=00,508\r\n",16)==0); cases++;
	feed("+DISC\r\n"); assert(s.state==ATMODULE_FAULT && s.tx_len==0); cases++;
	setup(); connect_data(); atmodule_write(&s,payload,1); feed("+DISC\r\n");
	assert(s.state==ATMODULE_IDLE && s.tx_len==0); connect_data(); tick(0); assert(out_len==0); cases++;
	feed("+DISC\r\n+DATA:00,1\r\nz"); assert(s.state==ATMODULE_IDLE && s.rx_len==0); cases++;
	setup(); uint8_t frame[1024]; size_t n=envelope(frame,payload,999);
	atmodule_feed(&s,frame,n,now); atmodule_feed(&s,frame,n,now);
	assert(s.state==ATMODULE_FAULT && s.rx_len==0); cases++;
}
static void properties(void)
{
	fuzz_t f; fuzz_init(&f, 0xB700);
	uint8_t payload[999], frame[1024], got[999];
	long rounds=fuzz_iters(1000);
	for (long r=0; r<rounds; r++) {
		setup(); size_t n=1+fuzz_below(&f,999);
		for (size_t i=0;i<n;i++) payload[i]=(uint8_t)fuzz_next(&f);
		size_t len=envelope(frame,payload,n);
		for (size_t at=0;at<len;) {
			size_t chunk=1+fuzz_below(&f,31); if(chunk>len-at) chunk=len-at;
			atmodule_feed(&s,frame+at,chunk,now); at+=chunk; now+=fuzz_below(&f,3);
		}
		assert(atmodule_read(&s,got,sizeof(got))==n && memcmp(payload,got,n)==0);
		cases++;
	}
	printf("atmodule: %ld seeded byte-preservation/partition properties\n", rounds);
}
static void adapter(void)
{
	init(); atmodule_disable(&s, now);
	atmodule_stream_bind(&module, clock_ms, end_session);
	assert(atmodule_stream.start()); now+=20; atmodule_stream.wait_rx(0);
	memcpy(input,"+READY\r\n",8); in_len=8; in_at=0; atmodule_stream.wait_rx(0);
	assert(out_len==9 && ends==1);
	const char *reply="+VER:test\r\nOK\r\n+DATA:00,1\r\nx";
	memcpy(input,reply,strlen(reply)); in_len=strlen(reply); in_at=0;
	assert(atmodule_stream.wait_rx(0) && atmodule_stream.connected());
	uint8_t b; assert(atmodule_stream.read(&b,1)==1 && b=='x');
	assert(atmodule_stream.write(&b,1)==1);
	memcpy(input,"+DISC\r\n",7); in_len=7; in_at=0;
	atmodule_stream.wait_rx(0); assert(!atmodule_stream.connected() && ends==2);
	atmodule_stream_bind(NULL,NULL,NULL); assert(!power && !atmodule_stream.start()); cases++;
}
int main(void)
{
	lifecycle(); partitions(); invalid(); transmit(); properties(); adapter();
	printf("atmodule: %u cases passed (no RF callback linked)\n",cases);
	return 0;
}
