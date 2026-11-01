# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Focused image-builder and Renode checks: python3 -m unittest discover -s tests/emu -p 'test_spinor.py' -v."""
import hashlib
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("flashimg", ROOT / "tools/emu/flashimg.py")
flashimg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flashimg)


class FlashImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix="rf-spinor-tests-")
        cls.addClassCleanup(cls.work.cleanup)
        cls.directory = Path(cls.work.name)
        cls.checker = cls.directory / "flashimg_check"
        subprocess.run(
            shlex.split(os.environ.get("CC", "cc"))
            + ["-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wshadow", "-O2"]
            + [f"-I{ROOT / d}" for d in ("boards/vhost", "platform/host", "core", "hal",
                                         "boards/md380", "platform/stm32f4")]
            + [str(ROOT / p) for p in ("boards/md380/emu/flashimg_check.c", "boards/vhost/flash_vhost.c",
                                        "core/codeplug/codeplug.c", "core/settings/settings.c",
                                        "core/storage/storage.c", "boards/md380/calibration_md380.c")]
            + ["-o", str(cls.checker)], check=True,
        )

    def test_fixtures(self):
        for fixture, index in (("blank", 1), ("dmr-pair", 1), ("dmr-pair", 2),
                               ("dmr-group", 4), ("dmr-group", 999)):
            with self.subTest(fixture=fixture, radio_index=index):
                output = self.directory / f"{fixture}-{index}.bin"
                flashimg.build_image(output, fixture, index)
                self.assertEqual(output.stat().st_size, flashimg.SIZE)
                self.assertEqual(Path(str(output) + ".security").stat().st_size, 768)
                subprocess.run([str(self.checker), str(output), str(output) + ".security", fixture, str(index)],
                               check=True)
                if fixture == "blank":
                    self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(),
                                     "dffab0dd410657cb30c7b2fd7f2586a4792e8472e58882b3532581f8111a646d")
        repeat = self.directory / "repeat.bin"
        flashimg.build_image(repeat, "dmr-pair", 2)
        self.assertEqual(repeat.read_bytes(), (self.directory / "dmr-pair-2.bin").read_bytes())

    def test_invalid_and_existing_files(self):
        output = self.directory / "protected.bin"
        for fixture, index in (("unknown", 1), ("dmr-pair", 3), ("dmr-pair", 0), ("dmr-group", 1000)):
            with self.assertRaises(ValueError):
                flashimg.build_image(output, fixture, index)
        self.assertFalse(output.exists())
        output.write_bytes(b"keep image")
        with self.assertRaises(FileExistsError):
            flashimg.build_image(output)
        self.assertEqual(output.read_bytes(), b"keep image")
        output.unlink()
        sidecar = Path(str(output) + ".security")
        sidecar.write_bytes(b"keep calibration")
        with self.assertRaises(FileExistsError):
            flashimg.build_image(output)
        self.assertFalse(output.exists())
        self.assertEqual(sidecar.read_bytes(), b"keep calibration")

    def test_cli(self):
        output = self.directory / "cli.bin"
        result = subprocess.run([sys.executable, str(ROOT / "tools/emu/flashimg.py"), str(output)],
                                capture_output=True, text=True, check=True)
        self.assertIn("16777216 bytes, blank, radio 1", result.stdout)
        print(result.stdout.strip())

    def test_renode(self):
        output = self.directory / "renode.bin"
        flashimg.build_image(output)
        script = self.directory / "check.resc"
        platform = self.directory / "flash.repl"
        platform.write_text(
            'spi: SPI.STM32SPI @ sysbus 0x40013000\n    series: STM32Series.F4\n'
            f'flash: SPI.SpiNor @ spi\n    imageFile: "{output}"\n'
        )
        script.write_text(
            f'include @{ROOT}/tools/emu/peripherals/spinor.cs\n'
            'mach create\n'
            f'machine LoadPlatformDescription @{platform}\n'
            'sysbus.spi.flash OnGPIO 0 false\n'
            'sysbus.spi WriteByte 0xC 0x9F\n'
            'sysbus.spi ReadByte 0xC\n'
            'sysbus.spi WriteByte 0xC 0\n'
            'sysbus.spi ReadByte 0xC\n'
            'sysbus.spi.flash OnGPIO 0 true\n'
            f'python "spinor_image_path = \'{output}\'; execfile(\'{ROOT}/tests/emu/spinor_check.py\')"\n'
            'quit\n'
        )
        renode = os.environ.get("RENODE", str(Path.home() / "tools/renode/renode"))
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.directory / "config"))
        result = subprocess.run([renode, "--disable-xwt", "--console", "--plain", str(script)],
                                capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0xEF", result.stdout, result.stdout + result.stderr)
        self.assertIn("spinor_check: PASSED", result.stdout, result.stdout + result.stderr)
        print(result.stdout)


if __name__ == "__main__":
    unittest.main()
