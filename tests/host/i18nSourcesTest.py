#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Disabled feature strings leave no hashes or translations in the language blob."""
import importlib.util
from pathlib import Path

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("i18n", root / "tools/i18n.py")
i18n = importlib.util.module_from_spec(spec)
spec.loader.exec_module(i18n)
full, _ = i18n.tables()
keys, languages = i18n.tables(["core/ui/ui_aprs_wx.c"])
assert i18n.fnv1a(b"Temperature") in full
assert i18n.fnv1a(b"Temperature") not in keys
assert i18n.fnv1a(b"OK choose  Back") in keys  # Also used by other screens.
for code, _, _, _, blob, offsets in languages:
    assert i18n.lookup(keys, blob, offsets, b"Temperature") is None
    assert i18n.lookup(keys, blob, offsets, b"OK choose  Back") is not None, code
print("i18nSourcesTest PASSED")
