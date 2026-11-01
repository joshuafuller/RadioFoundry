<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# Virtual radio checks

Run commands from the repository root after `make BOARD=vhost`. Use the Python environment from the
[quickstart](virtual-radio-quickstart.md#run-an-existing-radio-test).

## Fresh checkout prerequisites

The host tests and the Python conductor gate both build the virtual firmware and need the FreeRTOS submodule.
In a checkout made with `submodules: false`, `ci.sh` runs `tools/checkout-submodules.sh` when FreeRTOS is missing.
Direct host-test and coverage runs skip the virtual-board tests with a setup instruction when it is absent.
To include those tests, run the helper first. From the repository root:

```sh
tools/checkout-submodules.sh
uv venv build/venv
uv pip install --python build/venv/bin/python pytest pyserial
make -C tests/host
JOBS=8 tools/ci.sh --steps host,python
```

Default GCC 13.3 emits the same two baseline `origin/main` format-truncation warnings at `core/codeplug/zone_import.c:198` and `tests/host/zoneShareSvcTest.c:121`; they still fail CI's unchanged zero-warning gate.

## Focused checks

| Command | Checks |
|---|---|
| `make -C tests/host` | Existing host suite plus the virtual board's clock, ports, storage, display, modem and watchdog tests |
| `python3 tools/boards.py check` | The virtual board stays outside the physical-radio registry |
| `python3 tools/check_task_loops.py --self-test` | The checker rejects a harness that copies firmware task loops |
| `python3 tools/digital-twin/recording_lint.py --shareable tests/recordings` | Committed recordings contain permitted synthetic inputs |
| `build/venv/bin/python -m pytest -q tools/conductor/tests/test_airmodel.py tools/conductor/tests/test_lease.py tools/conductor/tests/test_recorder.py` | Air decisions, control leases and input recording |
| `build/venv/bin/python -m pytest -q -s tests/emu/test_keys.py tools/emu/test_hrc7000_soc.py tools/emu/test_hrc7000_modem.py -k 'not BootTest'` | GPIO/keypad behavior in Renode and the QEMU bridge's SoC and modem models |

The last command needs Renode at `$RENODE` or its documented default. The emulator tests use `renode_guard` to fail
and clean up if a process outlives its test. The selected QEMU bridge tests use fake CPU/debugger endpoints; they do
not boot a firmware image. Full boot checks need the corresponding compiler, locally built firmware and emulator.

Emulator descriptions live in `boards/<id>/emu/config.json`. That directory also owns its Renode platform and boot
scripts, flash template and board-specific checks. Shared tools discover these descriptions for engine selection,
keypad wiring, flash placement and boot settings; peripheral models stay in `tools/emu/`.

## Standalone emulator channels

`python3 tools/emu/emu_check.py --board <board>` runs the board ELF with a private debug-link channel.
Renode uses a PTY in the run's mode-0700 temporary directory; its monitor uses inherited pipes with the TCP
monitor disabled. No TCP port is selected or opened. Board scripts default to `$tcp=false`. The legacy explicit
`$tcp=true` option opens an unauthenticated terminal on all IPv4 interfaces; use it only in a protected environment.
The default standalone checks use PTYs instead.

QEMU's GDB endpoint is a Unix socket in a mode-0700 temporary directory in both standalone and conductor modes.
Standalone mode without lockstep binds its debug-link listener directly to `127.0.0.1:0` and retains that socket;
it reads the assigned port from the listener. Lockstep uses an in-process byte buffer. Startup waits are bounded
by the requested timeout. Neither main runner reserves a TCP port by closing a temporary socket before startup.
Both engines use the owner-death watcher to kill their process group when the Python owner exits, including SIGKILL.

## interaction_check

`python3 tools/conductor/checks/interaction_check.py` drives a voice call and an SMS between two radios, replays the run,
and checks that raw storage writes cannot enter shareable recordings. `make -C tests/host interaction-check` runs it too.
The SMS portion depends on upstream synchronous data-completion fix, which is absent from this branch. See the
[dependency note](virtual-radio.md#what-the-work-has-found); this command is not a passing gate yet.

## observe_check

`sh tools/conductor/checks/observe_check.sh` compares runs with different observers and checks the timing effect of
a controller's screenshot request. An observer should read cached state without changing the run.

## control_rules_check

`python3 tools/conductor/checks/control_rules_check.py` checks lease ownership, browser origin and host checks,
maintenance permission, console control permission and refusal auditing.

## browser_check

`python3 tools/conductor/checks/browser_check.py` drives the shipped front panel in two headless browser sessions.
It needs Playwright and Chromium. A cold browser start can take longer than a warm run.

## console_check

`python3 tools/conductor/checks/console_check.py --screenshot build/console.png` checks the shared-world console.
Add `--control` to exercise pause, step, speed and fault controls. Generated screenshots belong under `build/`.

## lookahead_check

`python3 tools/conductor/checks/lookahead_check.py` compares grants and pacing choices against trace hashes.
`--negative-control` deliberately uses an invalid larger grant and should fail.

## scale_probe

`python3 tools/conductor/checks/scale_probe.py --sizes 2,4 --no-grow --determinism-size 2 --virtual-s 5` measures a
small world and checks repeated traces. This is a local measurement, not a supported capacity claim.

## sms_restart_check

`python3 tools/conductor/checks/sms_restart_check.py` compares repeated and changed text across a sender restart.
Add `--mode reboot` to keep persisted flash. The verdict is `CONFIRMED` or `REFUTED`; either can exit zero if the
experiment completed consistently. It also needs the synchronous data-completion fix.

## parrot_check

`python3 tools/conductor/checks/parrot_check.py --part replay` checks repeatability of a repeater echo.
`--part echo` defaults to `--expect bug`: exit zero means the bystander played the echo and the caller did not.
Use `--expect fixed` to require the caller to play it. The demo uses this same verdict contract.
`--part mixed --fixed PATH` compares a separately supplied fixed build with the current one.

## Developer CI

`JOBS=8 tools/ci.sh --quick` runs the full conductor suite and all four retained recording/scenario suites under
an outer 45-minute timeout: full firmware-backed runs can exceed thirty minutes on shared hosts. Per-emulator
guards still apply. No workflow file is added. The Python step uses `build/venv/bin/python` when present,
otherwise `python3`; set `RFOUNDRY_VR_PYTHON` to select an existing interpreter. Missing pytest or pyserial fails
this gate. Install them as described in the quickstart. The equivalent standalone command is:

```sh
timeout -k 10 2700 build/venv/bin/python -m pytest -q tools/conductor/tests tools/digital-twin/scenario_test.py tools/digital-twin/convert_test.py tools/digital-twin/recording_lint_test.py tools/digital-twin/reduction_test.py
```

Emulator suites remain separate because they need optional Renode/QEMU installations and physical-board ELFs.
Their `renode_guard` fixtures check owned descendants only, so parallel test runs are not mistaken for leaks:

```sh
timeout -k 10 600 build/venv/bin/python -m pytest -q tools/emu tests/emu
```

Absent firmware or emulator prerequisites are explicit skips. Browser checks also need Playwright and Chromium.
The SMS acceptance command remains separate until the synchronous data-completion firmware fix is available.
Timing checks use synthetic clocks for pacing correctness; reported wall times are observations, not performance
gates. The stall gate has a 30-second outer timeout independent of its virtual clock and five-second stall budget.
Conductor-managed emulators use private command channels and reject any child TCP listener. A passing run does not
establish stability under every shared-machine load.

## Browser origin isolation

Both browser servers use fragment tokens, a session cookie and an exact-origin browser proof. The focused gates
check bootstrap parsing, credential binding, duplicate headers, lease separation and console capabilities:

```sh
build/venv/bin/python -m pytest -q tools/conductor/tests/test_webgw.py tools/conductor/tests/test_console.py
```

The two-port Chromium regressions are mandatory for browser-authentication changes. They collect cookies on an
unrelated loopback port, reject raw-client replay, check that no control was forwarded, and prove legitimate
control still works in the same run. Cookies still cross ports; the proof does not.

```sh
build/venv/bin/python tools/conductor/checks/browser_check.py
uv run --with playwright python tools/conductor/checks/console_check.py --control --screenshot build/console-security.png
```

The browser check also preserves direct-radio page behavior through the optional proof hook. These checks do not
cover LAN hosting, hostile same-origin code, root, or access to the owner's process or browser profile.
