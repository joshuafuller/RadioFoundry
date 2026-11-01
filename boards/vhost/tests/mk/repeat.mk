# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The repeat gate: the same scenario boots the virtual radio many times (plain, on one loaded CPU, eight
# at once) and the traces must be byte-identical; two controls that change one input must differ. The gate's own
# self-test runs first and shows that it fails when it must (a forced difference, a failed parallel run, an unreadable
# trace, a control without its change). Not part of run-boards (it takes a while and busy loops load the PC): CI
# calls this target itself. Linux only, like the host platform
VREPEAT := $(ROOT)/platform/host/repeat_check.py
vhost-repeat: vhost-elf
	@mkdir -p out
	python3 $(VREPEAT) --self-test --elf $(VHOST_ELF)
	python3 $(VREPEAT) --elf $(VHOST_ELF) --out out/vhost-repeat
.PHONY: vhost-repeat
