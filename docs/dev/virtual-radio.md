<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# Virtual radio

**Status:** an unmerged development preview on this fork, refreshed with upstream `ad79ab67` on November 7, 2026.

The virtual radio runs RadioFoundry's firmware tasks on Linux with a virtual board in place of hardware.
Each radio is a separate process. A conductor supplies the clock, connects radios through a shared simulated air,
and records inputs and checkpoints. The firmware tasks and protocol engines are unchanged. The browser front panel
adds a session check when accessed through the virtual-radio gateway.

Use it to test firmware without a radio, reproduce interactions between several radios, and replay a run in fresh
processes. A browser shows the firmware's front panel. Scripts and existing hardware-in-the-loop tests can use the
debug link through the conductor.

## Better tools to move faster

The aim is to shorten the path from a user's bug report to a reproducible failure, a verified fix, and a regression
test. As more radios and DMR features are supported, contributors need ways to check interactions without owning
every radio involved. Repeatable software tests can reduce that setup time and leave more hardware test time for
the behavior that needs a real radio.

- Run several radios against one controlled clock and simulated air, with repeatable inputs and fault conditions.
- Record a failure and replay it in fresh processes so another contributor can investigate it.
- Exercise the firmware's front panel and debug link through a browser or script.
- Reuse suitable hardware-in-the-loop test scripts against virtual radios, then verify hardware behavior on a bench.

The host virtual radio is **software-in-the-loop simulation**: firmware tasks run natively against modeled hardware.
The Renode and QEMU adapters provide **firmware emulation**: compiled firmware runs against modeled processors and
peripherals. **Hardware-in-the-loop testing** uses physical radios. Running a HIL test script against a virtual radio
is still a software test. These approaches complement each other; none of the modeled results establishes RF,
analog, audio quality or physical timing performance.

This preview is a place to try the approach and discuss useful pieces. The extraction sequence below is a proposal
for smaller contributions, not an agreed upstream roadmap or a request to merge the whole branch.

## Five-minute quickstart

You need Linux (including WSL2), Git, GCC, GNU make and binutils, and Python 3.11 or newer. These commands clone this
preview, initialize its FreeRTOS dependency, build the firmware, run two radios for two virtual seconds, and exit:

```sh
git clone --branch virtual-radio-preview https://github.com/joshuafuller/RadioFoundry.git
cd RadioFoundry
git submodule update --init -- third_party/FreeRTOS-Kernel
make BOARD=vhost
python3 tools/vradio.py up --radios 2 --ratio max --until-ms 2000 --no-browser
```

If you already checked out this branch, start at the submodule command from the repository root.
The launcher prints a world folder and two loopback URLs, then `ended: virtual time limit`. No hardware is opened.
For a browser session, key presses, a hardware-in-the-loop test and replay, see the
[quickstart](virtual-radio-quickstart.md).

## What the work has found

These are historical observations from the source work. A reproduction's outcome depends on the firmware version;
it may report that a bug is fixed after an upstream update.

| Observation | Reproduction |
|---|---|
| After a sender restarts, an identical new SMS can be treated as a duplicate at the receiver | `tools/conductor/checks/sms_restart_check.py` compares repeated and changed text after restart or reboot |
| A repeater's parrot echo can be ignored by the original caller while another radio receives it | `tools/conductor/checks/parrot_check.py --part echo`, for caller echo bug |
| A dual-watch restart immediately after stopping could be lost by the UI | The debug-link sequence led to a separate upstream UI correction, now inherited through the upstream update |
| Codeplug transfers needed a visible busy screen | CPS busy-screen work is separate from the virtual-radio infrastructure |
| A DMR VFO could lose its color code when switching to a channel and back | `tools/emu/vfo_channel_check.py` follows the VFO channel-switch bug steps in an emulator |

The upstream synchronous data-completion fix ([PR #305](https://github.com/RadioFoundry/RadioFoundry/pull/305)) is
included through the upstream update. SMS and data-dependent replay checks no longer need a separate firmware patch.
See [the checks page](virtual-radio-checks.md) for commands and what their results mean.

## Limits and clean room

Board-owned emulator descriptions, wiring and boot fixtures live in `boards/<id>/emu/`. Shared tools discover
those descriptions; the generic-code and board-registry checks cover that layout.

- The virtual vocoder and `tools/emu/codec_stub.py` are stubs. There is no real codec or decoded speech validation.
- The air model carries bursts and applies timing, loss and collision rules. It does not calibrate RF propagation,
  analog performance, or regulatory behavior.
- Renode and QEMU run firmware images against peripheral models written from the project's specifications and
  datasheets. Those models cover the paths implemented here; a passing run does not establish hardware equivalence.
- Replay checks the recorded inputs and checkpoints against a particular build. Changing a build requires comparison
  mode or explicit build selection. Live browser activity must first be recorded to become repeatable.
- Local recordings can contain typed text. They start private. Use the recording lint and promotion commands before
  sharing one. No codec, vendor firmware, flash dump or codeplug is supplied.

The implementation follows [the clean-room rules](../CLEANROOM.md): project specifications, public standards,
datasheets and permissively licensed code are the inputs. Other radio firmware source, restrictive vendor SDKs and
disassembly are not implementation references. Emulator executables are separate tools, not source copied here.

## Engine capabilities

| Operation | Host firmware | Renode adapter | QEMU adapter |
|---|---|---|---|
| Time grants and shared burst air | Yes | Yes | Yes |
| Common KEY / ENC input | Yes | Yes | Unsupported |
| Common debug-link bytes / Ethernet input | Yes | Unsupported | Unsupported |
| VIEW / STORE checkpoints | Yes | Unsupported | Unsupported |
| Panel frames | Yes | Modeled LCD snapshots | Unsupported |
| Browser front panel and virtual HIL | Yes | Unsupported | Unsupported |
| Full recording/replay contract | Yes | Unsupported | Unsupported |

Unsupported channel operations return a named capability error before they enter the input queue. Emulator-only
checks use the separate `debug()` probe explicitly. These probes do not provide browser, HIL or replay equivalence.

## Proposed upstream sequence

A possible extraction order follows; each future PR needs its working command and focused check.
Sizes below are **source slices measured on November 1, 2026**, not forecasts of extracted patches or current counts.
They count tracked source lines, including comments and tests. Globs include tracked files only;
earlier rows own any overlapping file. Extraction glue and future installation docs are not counted.

| Order | PR contents | Measured lines | Counted paths (repository-relative) |
|---|---|---:|---|
| 1 | Task-loop safeguard, trace/map fixtures and check integration | 296 | `tools/check_task_loops.py`, `tools/testdata/check_task_loops/*` |
| 2 | Deterministic host FreeRTOS port, virtual clock and scheduler tests | 1,806 | `platform/host/freertos/*`, `platform/host/FreeRTOSConfig.h`, `platform/host/vclock.*`, `platform/host/vport.h`, `platform/host/vtick.c`, `boards/vhost/tests/portTest.c`, `boards/vhost/tests/vclockTest.c`, `boards/vhost/tests/mk/port.mk`, `boards/vhost/tests/mk/vclock.mk` |
| 3 | Single-radio virtual board, required HALs and bounded headless boot | 3,013 | `boards/vhost/*`, `platform/host/main.c`, `platform/host/platform.mk`, `platform/host/config.mk` |
| 4 | Channel protocol, bounded world control, restart and cleanup | 2,026 | `tools/conductor/channel.py`, `tools/conductor/world.py`, `tools/conductor/worldapi.py`, `tools/conductor/tests/test_channel.py`, `tools/conductor/tests/test_world.py`, `tools/conductor/tests/test_worldapi.py` |
| 5 | Shared burst air, fixtures and interaction regressions | 1,788 | `tools/conductor/airmodel.py`, `tools/conductor/parts/air.py`, `tools/conductor/parts/airctl.py`, `tools/conductor/tests/test_airmodel.py`, `tools/conductor/tests/test_air.py`, `tools/conductor/tests/test_airctl.py`, `tools/conductor/tests/test_group_talk.py` |
| 6 | Recording/replay, launch identity, disk histories and privacy lint | 3,299 | `tools/conductor/history.py`, `tools/conductor/replay.py`, `tools/conductor/parts/recorder.py`, `tools/conductor/parts/resolve.py`, `tools/conductor/tests/test_recorder.py`, `tools/conductor/tests/test_replay.py`, `tools/conductor/tests/test_resolve.py`, `tools/digital-twin/recording_lint.py`, `tools/digital-twin/recording_lint_test.py`, `tests/recordings/ping.json` |
| 7 | Leases, debug-link multiplexing, virtual HIL and Python check integration | 3,025 | `tools/conductor/parts/lease.py`, `tools/conductor/parts/holds.py`, `tools/conductor/parts/audit.py`, `tools/conductor/parts/dlmux.py`, `tools/conductor/parts/dlcache.py`, `tools/conductor/parts/hil_endpoint.py`, `tools/conductor/run_hil.py`, `tools/conductor/serial_handlers/*.py`, `tools/conductor/tests/test_lease*.py`, `tools/conductor/tests/test_dlmux.py`, `tools/conductor/tests/test_hil_endpoint.py`, `tools/conductor/tests/test_run_hil.py` |
| 8 | Loopback front-panel gateway and browser authorization | 2,834 | `tools/conductor/parts/webgw.py`, `tools/conductor/parts/tcppeer.py`, `tools/conductor/parts/frames.py`, `tools/conductor/tests/test_webgw.py`, `tools/conductor/tests/test_frames.py`, `tools/conductor/web/*` |
| 9 | Optional console and behavior-named demos with contract tests | 5,594 | `tools/console.py`, `tools/demo.py`, `tools/conductor/tests/test_console.py`, `tools/conductor/tests/test_demo.py`, `tools/conductor/checks/*` |
| 10 | Renode isolated boot, minimal peripherals, codec and lifecycle tests | 1,036 | `tools/emu/emu_check.py`, `tools/emu/hostport.py`, `tools/emu/stm32_stubs.cs`, `tools/emu/codec_stub.py`, `tools/emu/renode_guard.py`, `tools/emu/test_emu_check.py`, `tools/emu/test_codec_stub.py`, `tools/emu/test_renode_guard.py` |
| 11 | Renode LCD/storage models and board-owned fixtures | 2,066 | `tools/emu/peripherals/lcd*`, `tools/emu/peripherals/spinor*`, `tools/emu/flashimg.py`, `boards/*/emu/*.resc`, `boards/*/emu/*.repl`, `boards/*/emu/config.json` |
| 12 | Renode modem/air support and common chip mechanics | 4,922 | `tools/emu/peripherals/c5000*`, `tools/emu/peripherals/c6000*`, `tools/emu/peripherals/at1846s*`, `tools/conductor/parts/emu.py`, `tools/conductor/tests/test_emu.py` |
| 13 | External QEMU bridge, SoC models, capabilities and lifecycle tests | 3,751 | `tools/emu/hrc7000*`, `tools/emu/test_hrc7000*`, `tools/conductor/tests/test_emu_hrc7000.py` |

Reproduce a row by expanding its path globs, keeping tracked files not counted by earlier rows, then summing
`len(path.read_text().splitlines())`. These slices do not inventory every preview file. The first independently
useful slice is the task-loop checker and trace fixture; integration hunks are extracted separately.

The first runnable radio arrives in the third PR. Firmware corrections stay separate. Reduce the large synthetic
recording and common modem-model duplication during extraction. The existing `two-radio-dmr.json` is 6,217 lines;
it is excluded from the small recording slice above. The two modem models share 762 identical lines in matching
blocks of at least 15 lines (`difflib.SequenceMatcher(autojunk=False)`). Neither is reduced by this sequence.

## Layout

| Path | Purpose |
|---|---|
| `platform/host`, `boards/vhost` | Host execution, virtual clock, peripheral interfaces and focused host tests |
| `tools/conductor` | Worlds, time grants, leases, debug link multiplexing, air, recording and replay |
| `tools/vradio.py`, `tools/console.py`, `tools/console` | Launcher, front-panel gateway and shared-world console |
| `tools/emu`, `tests/emu` | Renode and QEMU bridges, peripheral models and tests |
| `tools/digital-twin` | Scenario schema, conversion, recording validation and their required helpers and tests |
| `tests/recordings`, `tools/testdata` | Synthetic recording examples and task-loop checker fixtures |

See [architecture](virtual-radio-architecture.md), [host implementation](virtual-radio-host.md),
[control channel](virtual-radio-channel.md), and [checks and CI](virtual-radio-checks.md).
