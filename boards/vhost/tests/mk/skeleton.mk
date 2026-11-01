# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The walking skeleton: the virtual radio boots and runs 100 ms of virtual time, exit 0, with the first housekeeping
# read in its trace. The option registry: an unknown option and a bad value both exit 2, --help lists every option
# The host platform is Linux only (glibc threads, GNU ld link wraps): elsewhere the run says it is skipped
ifeq ($(shell uname -s),Linux)
run-boards:: vhost-skeleton
else
run-boards::
	@echo "# vhost-skeleton: skipped, the host platform builds on Linux only"
endif
vhost-skeleton: vhost-elf
	@mkdir -p out
	$(VHOST_ELF) --until-ms 100 --no-stall-watch --trace out/vhost-skeleton.log 2> out/vhost-skeleton.err
	@grep -q 'CREATE start' out/vhost-skeleton.log
	@grep -q 'END virtual time limit' out/vhost-skeleton.log
	@code=0; $(VHOST_ELF) --no-such-option 2> /dev/null || code=$$?; [ $$code -eq 2 ] || \
		{ echo "vhost-skeleton: an unknown option exited $$code, not 2"; exit 1; }
	@code=0; $(VHOST_ELF) --until-ms 1x 2> /dev/null || code=$$?; [ $$code -eq 2 ] || \
		{ echo "vhost-skeleton: a bad value exited $$code, not 2"; exit 1; }
	@$(VHOST_ELF) --help | grep -q -- '--until-ms N'
	@echo "# vhost-skeleton: boots, runs 100 ms, exit 0; the option checks PASSED"
.PHONY: vhost-skeleton
