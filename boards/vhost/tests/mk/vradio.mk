# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The launcher builds as needed; a socket failure must fail this check.
vradio-smoke:
	cd $(ROOT) && env -u MAKEFLAGS -u MFLAGS python3 tools/vradio.py up --radios 2 --ratio max --until-ms 2000 --no-browser
.PHONY: vradio-smoke
