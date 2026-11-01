// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Host unit test of the stall watch's counting rules (platform/host/vstall.c). One call of vstall_tick is one wall
// second, so the test needs no sleep. Example: with a budget of 3, three ticks in a row with the radio running and no
// progress make the third one answer "stuck".
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>
#include <string.h>
#include "vhost.h"
#include "vstall.h"

static int failures;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c); failures++; } } while (0)

// The registry and the end of the run belong to main.c; the test only needs them to link
void vhost_option_register(const vhost_option_t *opt) { (void)opt; }
void vhost_finish(const char *why, int code)
{
	printf("FAIL vhost_finish(%s, %d) called\n", why, code);
	_exit(1);
}

// Another thread publishes progress and the task name while this one counts: ThreadSanitizer must see no race
static void *publisher(void *arg)
{
	(void)arg;
	for (int i = 0; i < 20000; i++) {
		vstall_task(i % 2 ? "host" : "UI");
		vstall_progress();
	}
	return NULL;
}

int main(void)
{
	// No stall while waiting: the radio is not running firmware, so nothing counts, however long it takes
	vstall_configure(3, true);
	vstall_running(false);
	for (int i = 0; i < 100; i++)
		CHECK(!vstall_tick());

	// Stall after the budget while running: ticks 1 and 2 pass, tick 3 says stuck
	vstall_running(true);
	CHECK(!vstall_tick());
	CHECK(!vstall_tick());
	CHECK(vstall_tick());

	// Progress starts the count again
	vstall_configure(3, true);
	CHECK(!vstall_tick());
	CHECK(!vstall_tick());
	vstall_progress();
	CHECK(!vstall_tick());	// moved: count restarts
	CHECK(!vstall_tick());
	CHECK(!vstall_tick());
	CHECK(vstall_tick());	// the third still tick after the move

	// Waiting in the middle resets the count (a grant gate closing is not a stall)
	vstall_configure(3, true);
	CHECK(!vstall_tick());
	CHECK(!vstall_tick());
	vstall_running(false);
	CHECK(!vstall_tick());
	vstall_running(true);
	CHECK(!vstall_tick());
	CHECK(!vstall_tick());
	CHECK(vstall_tick());

	// Off with the debugger option (and --no-stall-watch): never stuck
	vstall_configure(3, false);
	for (int i = 0; i < 100; i++)
		CHECK(!vstall_tick());

	// The name is the last one published, copied whole
	vstall_task("host");
	char name[32];
	vstall_task_name(name, sizeof name);
	CHECK(strcmp(name, "host") == 0);

	// Threads: one publishes while this one counts and reads the name
	vstall_configure(1000, true);
	pthread_t t;
	pthread_create(&t, NULL, publisher, NULL);
	for (int i = 0; i < 2000; i++) {
		(void)vstall_tick();
		vstall_task_name(name, sizeof name);
	}
	pthread_join(t, NULL);
	CHECK(strcmp(name, "host") == 0 || strcmp(name, "UI") == 0);

	if (failures == 0)
		printf("vstallTest: all cases passed\n");
	return failures != 0;
}
