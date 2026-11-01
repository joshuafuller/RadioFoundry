# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Run with python3 -m tools.conductor.conductor --name WORLD [--scenario FILE]."""

import argparse
import importlib
import json
from pathlib import Path

from .channel import Kind
from .world import Config, Input, World


def install_parts(world):
    for path in sorted((Path(__file__).parent / "parts").glob("*.py")):
        if path.name == "__init__.py":
            continue
        part = importlib.import_module(f"{__package__}.parts.{path.stem}")
        install = getattr(part, "install", None)
        if install is not None:
            install(world)


def scenario_config(path, **options):
    data = json.loads(Path(path).read_text())
    if data.get("version") != 1:
        raise ValueError("WORLD_BAD_SCENARIO_VERSION")
    inputs = tuple(Input(event["radio"], Kind[event["kind"]], bytes.fromhex(event["payload_hex"]),
                         event["time_us"], event["source"]) for event in data["inputs"])
    return Config(inputs=inputs, end_us=data["end_us"], **options)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--scenario", type=Path)
    parser.add_argument("--executable", default=Config().executable)
    parser.add_argument("--radios", type=int, default=1)
    parser.add_argument("--ratio", default="1")
    args = parser.parse_args()
    options = {"executable": args.executable, "radios": args.radios}
    config = scenario_config(args.scenario, **options) if args.scenario else Config(**options)
    world = World(config)
    try:
        install_parts(world)
        path = world.api.bind(args.name)
        world.start()
        print(path, flush=True)
        if args.scenario:
            world.run("max" if args.ratio == "max" else float(args.ratio))
        while world.state != "Ended":
            world.api.poll(0.05 if world.state == "Paused" else 0)
            world.advance()
        world.api.poll(0)
        return 0 if world.reason in ("WORLD_END_TIME", "WORLD_STOPPED") else 1
    finally:
        world.close()


if __name__ == "__main__":
    raise SystemExit(main())
