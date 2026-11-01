// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The stall watch (platform/host/vstall.c): diagnostics, never part of the run. A host thread that wakes once a wall
// second. If no progress was published for the stall budget, the firmware spins in plain C with no call into the
// virtual radio (nothing can preempt it on the host), so the watch names the running task and ends the run.
//
// The task threads publish through the writers below; the watch thread reads only what they publish (atomics or a
// lock), never the kernel's task state.
#ifndef VSTALL_H
#define VSTALL_H

#include <stdbool.h>
#include <stddef.h>

// The task switch hook (vtrace.c): the task now running, by name (copied)
void vstall_task(const char *name);
// Progress: virtual time moved (vclock.c), a trace line was written (vtrace.c) or an interrupt ran (port.c)
void vstall_progress(void);
// The radio runs firmware (true) or not (false): the watch counts only while true. main.c sets it around the run;
// world control's grant gate sets it around each grant
void vstall_running(bool on);
// The scheduler starts (port.c): the watch thread starts, unless --no-stall-watch or --debugger
void vstall_start(void);


// For the watch thread and the unit test. One wall second passed: true when the budget of seconds with no progress,
// all while running, is used up. Waiting (running false), progress or a watch that is off start the count again
bool vstall_tick(void);
// The running task's name, copied under the lock
void vstall_task_name(char *out, size_t cap);
// Set the budget (seconds) and the on/off state, and restart the count. The options do this on the command line
void vstall_configure(unsigned budget, bool on);

#endif
