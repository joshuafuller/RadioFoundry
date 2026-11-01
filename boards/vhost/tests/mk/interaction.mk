# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# recording acceptance (tools/conductor/checks/interaction_check.py): a voice header and an SMS between two radios through the
# simulated air, the recording replayed at max and at r = 1, and the privacy rules. It is not part of run-boards, because it starts firmware processes for about 25 s.
interaction-check: vhost-elf recordings-lint
	cd $(ROOT) && env -u MAKEFLAGS -u MFLAGS python3 tools/conductor/checks/interaction_check.py --part all

.PHONY: interaction-check
