# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Emulator descriptions owned by boards/<id>/emu (also imported by Renode's IronPython)."""
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load(root=ROOT):
    configs = {}
    for path in sorted(glob.glob(os.path.join(root, "boards", "*", "emu", "config.json"))):
        directory = os.path.dirname(path)
        board = os.path.basename(os.path.dirname(directory))
        with open(path) as source:
            config = json.load(source)
        config["directory"] = directory
        config["elf"] = os.path.join(root, "build", board, "rfoundry_" + board + ".elf")
        configs[board] = config
    return configs


BOARDS = load()


def default_board(engine):
    return next(board for board, config in sorted(BOARDS.items())
                if config["engine"] == engine and config.get("default"))
