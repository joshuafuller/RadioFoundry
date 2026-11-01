#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Build and run a local world: python3 tools/vradio.py up --radios 2.

World folders hold the conductor's config, events and audit log. A clean end
removes the folder unless up --keep is given; failed runs keep their logs. Browser URLs
contain one-time tokens; opening one grants that browser the printed lease.
Use status to find running worlds and down --name NAME to end one.
"""

import argparse
from contextlib import closing
from dataclasses import replace
import errno
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.conductor.channel import NEVER
from tools.conductor.conductor import install_parts, scenario_config
from tools.conductor.parts import webgw
from tools.conductor.run_hil import WorldClient
from tools.conductor.world import Config, World
from tools.conductor.worldapi import private_directory


class LauncherError(Exception):
    """An actionable, single-sentence error for the command line."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"vradio: {' '.join(message.split())}; run {self.prog} --help for usage.\n")


def positive(value):
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("choose a positive whole number")


def ratio(value):
    if value == "max":
        return value
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("choose a positive ratio or max")


def name(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise argparse.ArgumentTypeError("use 1 to 64 letters, digits, underscores or hyphens for the name")
    return value


def arguments(argv):
    parser = Parser(prog="vradio.py", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up", help="build and start a world until Ctrl-C")
    up.add_argument("--radios", type=positive, help="radio count (default: scenario's radios, otherwise 1)")
    up.add_argument("--ratio", type=ratio, default=1)
    up.add_argument("--scenario", type=Path)
    up.add_argument("--name", type=name, default=f"vr-{uuid.uuid4().hex[:12]}")
    up.add_argument("--no-build", action="store_true")
    up.add_argument("--build", action="append", default=[], metavar="RADIO=PATH",
                    help="use an existing executable for this radio (repeat for more radios)")
    up.add_argument("--until-ms", type=positive)
    up.add_argument("--no-browser", action="store_true", help="print URLs without opening a browser")
    up.add_argument("--keep", action="store_true", help="keep the world folder after a clean end for logs and replay")
    commands.add_parser("status", help="list running worlds")
    down = commands.add_parser("down", help="end a named world")
    down.add_argument("--name", type=name, required=True)
    args = parser.parse_args(argv)
    if args.command == "up":
        if args.radios is None:
            try:
                args.radios = json.loads(args.scenario.read_text()).get("radios", 1) if args.scenario else 1
            except (OSError, ValueError, AttributeError):
                up.error("the scenario is invalid or unreadable; provide a version 1 conductor scenario file")
            if type(args.radios) is not int or args.radios < 1:
                up.error("the scenario's radios must be a positive whole number")
        if args.radios > webgw.TOKENS_MAX:
            up.error(f"the gateway supports {webgw.TOKENS_MAX} pending browser tokens; use fewer radios")
        if args.until_ms is not None and args.until_ms * 1000 >= NEVER:
            up.error("--until-ms is too large; choose a smaller time limit")
        builds = {}
        for selection in args.build:
            radio, separator, path = selection.partition("=")
            if (not separator or not radio.isdecimal() or not 1 <= int(radio) <= args.radios
                    or not path or "\x00" in path):
                up.error("--build requires RADIO=PATH with a radio number from 1 to --radios")
            if int(radio) in builds:
                up.error("--build names the same radio more than once")
            builds[int(radio)] = path
        args.build = builds
    return args


def build(no_build):
    target = "build/vhost/rfoundry_vhost.elf"
    executable = ROOT / target
    if no_build:
        if not executable.is_file():
            raise LauncherError("the virtual radio is missing; run up without --no-build to build it.")
        return executable
    if shutil.which("make") is None:
        raise LauncherError("make is missing; install GNU make and retry up.")
    # Make owns the dependency graph, including generated assets and compiler flags.
    env = {key: value for key, value in os.environ.items() if key not in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES")}
    command = ["make", "BOARD=vhost", target]
    if executable.is_file():
        query_command = [*command, "-q"]
        stamp = executable.parent / "version"
        # Dirty builds use wall time; querying must preserve the last build's time.
        if stamp.is_file():
            fields = stamp.read_text().split()
            if fields and fields[-1].isdigit():
                query_command.append(f"RFOUNDRY_GIT_TIME={fields[-1]}")
        query = subprocess.run(query_command, cwd=ROOT, env=env, capture_output=True, text=True)
        if query.returncode == 0:
            return executable
        if query.returncode != 1:
            raise LauncherError("build dependencies could not be checked; run make BOARD=vhost to see the error.")
    if shutil.which("gcc") is None:
        raise LauncherError("gcc is missing; install GCC and retry up.")
    print("building virtual radio...", flush=True)
    executable.parent.mkdir(parents=True, exist_ok=True)
    log = executable.parent / "launcher-build.log"
    with log.open("w") as output:
        result = subprocess.run([*command, "-j8"], cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        raise LauncherError(f"the virtual radio build failed; fix the error in {log} and retry up.")
    return executable


def runtime():
    path = Path(os.environ.get("XDG_RUNTIME_DIR") or f"{tempfile.gettempdir()}/rfoundry-runtime-{os.getuid()}")
    private_directory(path)
    return path


def configuration(args, executable):
    options = {"executable": str(executable), "radios": args.radios}
    if args.build:
        options["radio_executables"] = tuple(args.build.get(radio) for radio in range(1, args.radios + 1))
    try:
        config = scenario_config(args.scenario, **options) if args.scenario else Config(**options)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise LauncherError("the scenario is invalid or unreadable; provide a version 1 conductor scenario file.") from error
    if args.until_ms is not None:
        end_us = args.until_ms * 1000
        config = replace(config, end_us=min(config.end_us, end_us) if config.end_us is not None else end_us)
    return config


def group_air(radios):
    """Two or more radios with no scenario share one air as a talkgroup (the dmr-group start storage in
    boards/vhost/storage_vhost.h): radio k is DMR ID 3100000+k, and all have the same channel, color code, slot and
    talkgroup 9 contact, so PTT on one is heard on all the others. Example: 3 radios are 3100001..3100003."""
    return {"groups": [list(range(1, radios + 1))], "seed": 0, "radios": [
        {"radio": r, "storage": "dmr-group", "radio_index": r, "identity": 3100000 + r, "channel": 1, "cc": 1, "ts": 1}
        for r in range(1, radios + 1)]}


def air_settings(args):
    """The air_config for this world: the scenario's own `air` section, else one talkgroup for two or more radios,
    else none."""
    if args.scenario:
        try:
            data = json.loads(Path(args.scenario).read_text())
        except (OSError, ValueError) as error:
            raise LauncherError("the scenario is invalid or unreadable; provide a version 1 conductor scenario file.") from error
        return data.get("air")
    return group_air(args.radios) if args.radios >= 2 else None


def up(args):
    config = configuration(args, build(args.no_build))
    air = air_settings(args)
    world = World(config)
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: signal.default_int_handler(signum, frame))
    try:
        install_parts(world)
        if air is not None:
            response = world.api.dispatch({**air, "version": 1, "command": "air_config"})
            if not response["ok"]:
                raise LauncherError(f"the air settings were refused: {response['error']}")
        gateway = webgw.get(world, enabled=True)
        root = runtime()
        try:
            path = world.api.bind(args.name, root)
        except ValueError as error:
            if str(error) != "WORLD_ALREADY_EXISTS":
                raise
            folder = root / "rfoundry-vr" / args.name
            raise LauncherError(f"world {args.name} already exists at {folder}; choose another --name, or use "
                                f"status to confirm it is stopped, then clear it with: rm -rf -- "
                                f"{shlex.quote(str(folder))}") from error
        print(f"world: {args.name}\nworld folder: {path.parent}", flush=True)
        world.start()
        world.run(args.ratio)
        # Each radio's address is printed (and its browser opened) only once the gateway has the radio's own page,
        # so the first visit never meets 503
        pending = {}
        for radio in sorted(world.radios):
            session = gateway.token({"radio": radio})
            world.leases.grant(radio, session["principal"])
            pending[radio] = session
        while world.state != "Ended":
            world.api.poll(0.05 if world.state == "Paused" else 0)
            world.advance()
            for radio in list(pending):
                if gateway.status().get(str(radio), {}).get("state") == "up":
                    session = pending.pop(radio)
                    print(f"radio {radio}: {session['url']} lease holder: {session['principal']}", flush=True)
                    if not args.no_browser and not webbrowser.open(session["url"]):
                        print(f"browser unavailable; open the printed URL for radio {radio}.", file=sys.stderr)
        # Flush the reply to a world client's end command before closing its socket.
        world.api.poll(0)
    except KeyboardInterrupt:
        world.end()
    finally:
        try:
            world.close()
        finally:
            signal.signal(signal.SIGTERM, previous)
    reasons = {"WORLD_END_TIME": "virtual time limit", "WORLD_STOPPED": "stopped"}
    if world.reason not in reasons:
        raise LauncherError(f"the world ended with {world.reason}; inspect the world folder and retry up.")
    if not args.keep and world.api.directory is not None:
        shutil.rmtree(world.api.directory)
    print(f"ended: {reasons[world.reason]}", flush=True)
    return 0


def status():
    parent = runtime() / "rfoundry-vr"
    found = False
    if parent.exists():
        private_directory(parent)
        for directory in sorted(parent.iterdir()):
            private_directory(directory)
            try:
                with closing(WorldClient(directory / "world.sock")) as client:
                    client.sock.settimeout(2)
                    state = client.call("status")
            except OSError as error:
                if error.errno in (errno.ENOENT, errno.ECONNREFUSED):
                    continue
                raise
            if state["state"] != "Ended":
                found = True
                print(f"{directory.name}: {state['state']}, {len(state['radios'])} radios, "
                      f"{state['time_us'] / 1000:g} ms; world folder: {directory}")
    if not found:
        print("no running worlds")
    return 0


def down(args):
    parent = runtime() / "rfoundry-vr"
    directory = parent / args.name
    if not (directory / "world.sock").exists():
        raise LauncherError(f"world {args.name} is not running; use status to find a running world.")
    private_directory(parent)
    private_directory(directory)
    with closing(WorldClient(directory / "world.sock")) as client:
        client.sock.settimeout(5)
        client.call("end")
        while client.sock.recv(65536):
            pass
    print(f"ended: {args.name}")
    return 0


def main(argv=None):
    args = arguments(argv)
    try:
        if args.command == "up":
            return up(args)
        return status() if args.command == "status" else down(args)
    except LauncherError as error:
        message = str(error)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            message = "a gateway port is busy; stop the process using that port and retry up."
        elif error.errno in (errno.EPERM, errno.EACCES, errno.EROFS):
            message = "runtime access was denied; use a writable private XDG_RUNTIME_DIR and allow local sockets."
        else:
            message = f"{error}; check the world folder and retry the command."
    except (ValueError, RuntimeError) as error:
        if str(error) == "WORLD_UNSAFE_RUNTIME_DIRECTORY":
            message = "the runtime folder is unsafe; set XDG_RUNTIME_DIR to your own non-symlink folder with mode 0700."
        else:
            message = f"{error}; inspect the world folder and retry the command."
    print(f"vradio: {' '.join(message.split())}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
