// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The host platform's run (platform/host/main.c): the command line options and the end of the run.
//
// Options. Each module declares its own options in its own file, so a new option never edits main.c or a shared
// table. Example, in vtrace.c:
//
//	static bool set_trace(const char *value) { path = value; return true; }
//	VHOST_OPTION("--trace", "FILE", set_trace, "the trace file (default: stdout)");
//
// VHOST_OPTION registers the option before main runs (a constructor, which --gc-sections keeps). main.c hands each
// command line option to its handler in command line order, so options that queue events (--key, --host) queue them
// in a fixed order. A flag has no value (arg NULL): its handler gets NULL. A handler returns false for a bad value; an
// unknown option, a missing value or a bad one ends the run with exit code 2. Two modules that register one name stop
// the run at start, with both names in the message.
#ifndef VHOST_H
#define VHOST_H

#include <stdbool.h>
#include <stddef.h>

typedef struct {
	const char *name;			// "--until-ms"
	const char *arg;			// the value's name for --help ("N"), or NULL for a flag with no value
	bool (*set)(const char *value);		// false: a bad value
	const char *help;			// one line for --help
} vhost_option_t;

void vhost_option_register(const vhost_option_t *opt);

#define VHOST_OPTION_CAT2(a, b) a##b
#define VHOST_OPTION_CAT(a, b) VHOST_OPTION_CAT2(a, b)
#define VHOST_OPTION(name_, arg_, set_, help_)								\
	static const vhost_option_t VHOST_OPTION_CAT(vhost_opt_, __LINE__) = { name_, arg_, set_, help_ };	\
	__attribute__((constructor)) static void VHOST_OPTION_CAT(vhost_opt_reg_, __LINE__)(void)		\
	{												\
		vhost_option_register(&VHOST_OPTION_CAT(vhost_opt_, __LINE__));				\
	}

// End the run with a named reason: the END trace line, the trace flushed, a speed line on stderr, then exit with code.
// Any part may call it (the virtual time limit, a power cut, an assert, a stall). 0 is a run that ended as planned
void vhost_finish(const char *why, int code) __attribute__((noreturn));

// Work a part does just before the END line, so its last trace lines (a final checkpoint) come before END and END
// stays the last line. Hooks run once, in registration order; a hook that itself calls vhost_finish ends the run
// with its own reason and the remaining hooks are skipped
typedef void (*vhost_finish_fn)(void);
void vhost_finish_hook_register(vhost_finish_fn fn);
#define VHOST_FINISH_HOOK(fn)								\
	__attribute__((constructor)) static void VHOST_OPTION_CAT(vhost_fin_reg_, __LINE__)(void)	\
	{										\
		vhost_finish_hook_register(fn);						\
	}

#endif
