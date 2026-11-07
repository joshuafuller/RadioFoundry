<!-- SPDX-License-Identifier: MIT -->
<!-- Copyright (c) 2026 The RadioFoundry Authors -->

# Virtual radio quickstart

This guide builds the preview and runs its first checks. For the purpose and scope, read the
[overview](virtual-radio.md). Nothing here opens a connected radio or transmits RF.

## Build and start


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

A successful startup checks that the radios boot. To exercise communication and repeatability, run this next from
the repository root:

```sh
python3 tools/conductor/checks/interaction_check.py
```

It checks call setup, an acknowledged text message, repeatable replay, and recording privacy. A passing run ends
with `interaction_check: PASS --part all`. The modeled vocoder does not validate decoded speech.
All remaining commands also run from the repository root.

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

[Checks and proposed CI](virtual-radio-checks.md) lists focused commands and explains the reproduction verdicts.

## Common setup problems

| Symptom | What to check |
|---|---|
| Build reports missing FreeRTOS headers | Run `git submodule update --init -- third_party/FreeRTOS-Kernel` from the repository root |
| A world name already exists | Choose a new `--name`; use the same name in the replay path |
| The completed world folder is gone | Add `--keep` when starting the run; successful launcher runs otherwise remove their folder |
| A printed browser URL no longer opens a session | The bootstrap token is single-use and the world must still be running; a new browser session needs a fresh URL |
| Replay refuses a build or VIEW version | Use the recorded build, or make a new recording. Compare mode still enforces compatible schemas and VIEW versions |
| `pytest` or `serial` cannot be imported | Use the Python interpreter where `pytest` and `pyserial` were installed |

Keep builds and firmware-backed checks sequential within one checkout. A test can rebuild the shared executable;
starting a second build while another test launches it can cause a transient execution failure.
