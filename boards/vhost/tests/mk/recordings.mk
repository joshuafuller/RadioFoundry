# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# Fail before compiling host tests, including a plain make's first test binary.
$(TESTS): | recordings-lint
run-boards:: recordings-lint

recordings-lint:
	python3 $(ROOT)/tools/digital-twin/recording_lint.py --shareable $(ROOT)/tests/recordings

.PHONY: recordings-lint
