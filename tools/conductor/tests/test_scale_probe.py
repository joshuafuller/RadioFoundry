# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Argument handling and classification of the scale probe. No radio is started here."""

import argparse
import errno

import pytest

from tools.conductor.checks import scale_probe


def test_defaults_are_the_stated_sizes():
    args = scale_probe.parse_args([])
    assert args.sizes == [10, 25, 50, 100]
    assert args.virtual_s == 10 and args.min_ratio == 0.1 and args.storage == "blank"


def test_sizes_parse_and_refuse_bad_lists():
    assert scale_probe.parse_args(["--sizes", "2,4"]).sizes == [2, 4]
    for bad in ("4,2", "3,3", "0,5", "a,b", ""):
        with pytest.raises(SystemExit):
            scale_probe.parse_args(["--sizes", bad])
    with pytest.raises(argparse.ArgumentTypeError):
        scale_probe.parse_sizes("1,x")


def test_non_positive_numbers_are_refused():
    for flag in ("--virtual-s", "--min-ratio", "--limit"):
        with pytest.raises(SystemExit):
            scale_probe.parse_args([flag, "0"])


def test_missing_executable_exits_2(capsys):
    assert scale_probe.main(["--executable", "/nonexistent/rfoundry_vhost.elf"]) == 2
    assert "SCALE_MISSING_EXECUTABLE" in capsys.readouterr().out


def test_failure_causes_are_named():
    assert "open-file limit" in scale_probe.classify(OSError(errno.EMFILE, "Too many open files"))
    assert "memory" in scale_probe.classify(MemoryError())
    assert "thread limit" in scale_probe.classify(RuntimeError("can't start new thread"))
