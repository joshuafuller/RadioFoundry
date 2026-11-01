<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# Running a virtual radio on the host

The virtual radio runs the firmware's own startup and FreeRTOS tasks in one Linux process. FreeRTOS is the kernel
that chooses which ready task runs next. The host port gives each task a thread, with only one running at a time.
The virtual board supplies simulated hardware through the same interfaces as a physical board. See
[ADR 0002](adr/0002-execution-model.md) for the decision and [the architecture](virtual-radio-architecture.md) for
the planned stages. Physical board capabilities are in the [feature matrix](../feature-matrix.md).

## Build and run

Use Linux with GCC, GNU make, GNU binutils, Python 3.11 or later and the MIT FreeRTOS kernel submodule. The stall test
also needs `bc`; the host port tests use `timeout` and, when installed, `taskset`.

```sh
tools/checkout-submodules.sh
make -j8 BOARD=vhost
build/vhost/rfoundry_vhost.elf --until-ms 100 --trace build/vhost/boot.log
build/vhost/rfoundry_vhost.elf --help
```

The build writes an executable, image and link map under `build/vhost/`. Run the `.elf` on the host. It needs no
connected radio and emits no RF. `--until-ms` is a limit in virtual milliseconds, not elapsed wall time. A successful
bounded run exits 0 and ends its trace with `END virtual time limit`. Options belong to the part that uses them;
`--help` lists the options built into this executable (`platform/host/main.c:82`).

For example, this run enables the simulated host port and holds the PTT key for 500 ms, starting at virtual 1500 ms:

```sh
build/vhost/rfoundry_vhost.elf --until-ms 3000 --host-port on --key 1500:4:500 --trace build/vhost/key.log
```

The event goes through the virtual input and the firmware's input task. It does not call the radio's step function
directly. The repeat scenario has a longer example (`platform/host/repeat_check.scenario:25`).

## Reading the trace

Each line begins with virtual microseconds and the FreeRTOS tick count. For example:

```text
000000000000 0000000000 CREATE start prio=5
000000000000 0000000000 SCHEDULER start
000000000000 0000000000 CREATE IDLE prio=0
000000000000 0000000000 CREATE Tmr Svc prio=6
000000000000 0000000000 IN Tmr Svc
000000000000 0000000000 IN start
```

| Record | Meaning |
|---|---|
| `BOOT`, `SCHEDULER` | Host entry and scheduler startup |
| `CREATE`, `DELETE`, `IN` | Task creation, deletion and every switch into a task, including selecting it again |
| `ISR` | A simulated interrupt; `--no-tick-log` omits tick interrupt lines |
| `BOARD` | A simulated hardware action, such as a key edge, host bytes, a flash write or a display flush |
| `CRASH`, `END` | Crash information, when present, and why the run ended |

Flash writes and display flushes include content hashes, so a changed setting or screen can change the trace.
The trace contains no host pointer, thread ID or wall time (`platform/host/vtrace.c:3`). The speed report goes to
standard error. Keep it separate from the trace when comparing runs.

## Checks and CI

```sh
python3 tools/check_task_loops.py --self-test
python3 tools/check_task_loops.py --map build/vhost/rfoundry_vhost.map --trace build/vhost/boot.log
make -C tests/host vhost-repeat
make -C tests/host vhost-stall
make -C tests/host vhost-watchdog
JOBS=8 tools/ci.sh --board vhost --steps firmware
```

The normal CI host step runs the virtual board's unit tests and boot checks through
`boards/vhost/tests/host.mk:7`. The firmware step rebuilds the virtual board from scratch, rejects compiler or linker
warnings, checks its boot and map, and runs the repeat, stall and watchdog gates. The Python step discovers
`check_task_loops.py --self-test` in its existing tool sweep. Remove `build/ci` after saving any needed results.

The link map's cross reference table lists the defining object and referring objects for each symbol. The checker
rejects references to `radio_sm_poll`, `radio_sm_tick`, `ui_app_tick`, `ui_app_key`, `settings_poll` or
`storage_job_poll` outside `core/`, `hal/`, `drivers/` and `third_party/`. It requires the firmware's startup
references and a completed trace in which `start` creates `radio`, `input`, `ui` and `host`, in that order. For
example, `core/app/app.c:133` defines `app_start`, which creates `start`; that task calls `radio_core_start` before
`input_start` (`core/app/app.c:101`). A map records object references, not the instruction-level call stack. The trace
adds the running task at creation time. Focused unit tests may call step functions; this check applies to the linked
virtual radio.

The repeat gate runs one six-second scenario twice, on one loaded CPU, and as eight processes at once. Every run
must exit successfully with a byte-identical trace. Two controls change one input each: one must move the transmitter
off time; the other must change flash and screen hashes. Its self-test deliberately breaks traces, exits, reads and
controls to show that the gate rejects them (`platform/host/repeat_check.py:4`).

The stall check deliberately spins, including with interrupts disabled, and requires a bounded failure naming the
task. The watchdog check stops feeding the simulated watchdog and requires a watchdog crash record. These gates
exercise deliberate failures; the gate itself exits successfully only when the expected failure is observed
(`boards/vhost/tests/mk/stall.mk:30`, `boards/vhost/tests/mk/crash.mk:24`).

## Guarantee and limits

For the same build, starting state and timed inputs, the virtual radio repeats the firmware task order selected by
its FreeRTOS kernel byte for byte. Code takes no virtual time. Time advances when tasks wait, and interrupts run
only at fixed points. This guarantee has the limits stated in [ADR 0002](adr/0002-execution-model.md#consequences):

| Limit | What needs another test |
|---|---|
| No interrupt inside plain C code or a critical section | Interrupt races on physical hardware |
| No time slicing between equal-priority tasks | Behavior that depends on equal-priority time slices |
| Code takes no virtual time | Deadlines missed because a function runs slowly |
| A loop that never sleeps stops virtual time | Real hardware behavior during a spin |
| Host stacks and heap differ from hardware | Stack fit and hardware memory limits |

For example, the radio task's late-pass branch cannot model a slow CPU here (`core/radio/radio_task.c:150`). Use
the bench and HIL for these limits. A repeated trace does not prove physical RF timing or unimplemented board parts.

**No bare spins outside test code.** A loop must block or sleep when it waits. Repeatedly reading `hal_millis` or
`hal_micros` does not advance virtual time. The stall watch ends a stuck run and names its task; it does not make a
busy loop a valid wait. Deliberate spin tests are the exception (`boards/vhost/tests/mk/stall.mk:19`).
