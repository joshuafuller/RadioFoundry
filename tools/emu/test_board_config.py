# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Board discovery and the Renode descriptions, without a firmware image."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from board_config import BOARDS, ROOT, load
from emu_check import default_renode
from flashimg import build_image


def test_discover_board_without_a_shared_table(tmp_path):
    directory = tmp_path / "boards/example/emu"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"engine": "qemu"}))
    assert load(str(tmp_path)) == {"example": {
        "engine": "qemu", "directory": str(directory),
        "elf": str(tmp_path / "build/example/rfoundry_example.elf"),
    }}


@pytest.mark.parametrize("board,config", [(b, c) for b, c in BOARDS.items() if c["engine"] == "renode"])
def test_renode_board_platform_flash_and_keys(board, config, tmp_path):
    renode = default_renode()
    if not os.path.exists(renode):
        pytest.skip(f"no Renode at {renode}")
    directory = Path(config["directory"])
    image = tmp_path / "flash.bin"
    build_image(image)
    flash = tmp_path / "flash.repl"
    flash.write_text((directory / config["flash"]).read_text().replace("{image}", str(image)))
    script = tmp_path / "board.resc"
    script.write_text("\n".join([
        f"include @{HERE}/stm32_stubs.cs",
        *[f"include @{HERE}/peripherals/{name}.cs" for name in
          ("lcd_st7735", "spinor", "at1846s", "c5000", "c6000")],
        "mach create",
        f"machine LoadPlatformDescription @{directory / config['repl']}",
        f"machine LoadPlatformDescription @{flash}",
        f"include @{HERE}/peripherals/keys.py",
        f'keys_layout "{board}"', "keys_attach", "keys_key 0 1",
        'python "assert _current_keys().held == set([\'green\']); print(\'BOARD_KEYS_OK\')"',
        "quit",
    ]))
    result = subprocess.run([renode, "--disable-xwt", "--console", "--plain", str(script)],
                            cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    output = result.stdout + result.stderr
    assert result.returncode == 0 and "BOARD_KEYS_OK" in output, output
    assert "There was an error" not in output and "Could not compile" not in output, output
