<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# Virtual radio quickstart

Start with the build and two-second run in [Virtual radio](virtual-radio.md#five-minute-quickstart).
All commands below run from the repository root on Linux. Nothing opens a connected radio or transmits RF.

## Browser controls

Run a bounded browser session:

```sh
python3 tools/vradio.py up --radios 2 --until-ms 15000 --no-browser
```

Open the printed URLs while it runs. Each URL has a one-time token. Click the front-panel keys to operate the firmware.
After 15 virtual seconds the processes exit. Omit `--no-browser` to let the launcher open the pages automatically.
For an ongoing session omit `--until-ms`; Ctrl-C stops it. The gateway binds only to loopback.

## Record and replay

A world records itself from startup. `--keep` preserves its folder after a successful run. Give this run a new name; if `quickstart` already exists, use a different name
in both commands. It runs at full speed and ends on its own:

```sh
python3 tools/vradio.py up --radios 2 --name quickstart --keep --ratio max --until-ms 2000 --no-browser
python3 tools/conductor/replay.py replay "$(python3 -c 'from tools import vradio; print(vradio.runtime())')/rfoundry-vr/quickstart"
```

Replay starts fresh radio processes and compares their trace and checkpoints with the recording. It prints
`replay: SAME run` when they match. The world folder contains `events.jsonl`, `audit.jsonl`, `report.md` and the
`recording/` directory. Keep local recordings private: they may contain what you typed.

## Run an existing radio test

Install the Python test dependencies in a local environment. If you already have pytest and pyserial, use that
interpreter instead. With `uv` installed:

```sh
uv venv build/venv
uv pip install --python build/venv/bin/python pytest pyserial
build/venv/bin/python tools/conductor/run_hil.py tests/hil/test_boot.py::test_version_and_features tests/hil/test_ui.py::test_hub_and_back
```

The runner starts a virtual radio, waits for its first screen, grants the test a lease, and supplies a Unix socket
as its serial port. Both tests should pass. The Hub test accounts for the virtual board's extra Last caller entry.

## Console and other checks

`python3 tools/console.py --help` explains how to attach the console to a world. It shows radio screens and the air
timeline. World control is opt-in and uses a separate one-time control code; watching does not acquire a radio lease.
Use `python3 tools/vradio.py status` to list worlds and `python3 tools/vradio.py down --name NAME` to stop one.

[Checks and proposed CI](virtual-radio-checks.md) lists focused commands. Read the
[synchronous data-completion fix dependency](virtual-radio.md#what-the-work-has-found) before running SMS checks.
