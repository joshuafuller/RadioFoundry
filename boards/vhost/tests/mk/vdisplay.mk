# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The display profiles (boards/vhost/display_profiles_vhost.c): vdisplayTest, one named case per profile (unit level,
# any host), and vhost-display, the elf of the default build run on the two profiles it holds (Linux only).
# vdisplay-profiles builds every profile and boots it for 3000 ms: six firmware builds, so it is not in run-boards
# (the list of profiles and the boards they cover comes from boards/*/board.toml)
VDISPLAY_PROFILES := rgb565-160x128 rgb565-240x320 indexed-160x128 indexed-160x120 indexed-240x320 mono-128x64
TESTS += vdisplayTest
# The firmware's linker section attributes are stripped from CFLAGS; the registry macros here need __attribute__
vdisplayTest: $(VHOST_TESTS)vdisplayTest.c $(VHOST_TESTS)../display_profiles_vhost.c $(VHOST_TESTS)../display_profiles_vhost.h
	$(CC) $(CFLAGS) -U__attribute__ -Wshadow -DGFX_INDEXED=1 -DGFX_FB_BYTES=153600u -DGFX_MAX_ROWS=320u \
		-I$(ROOT)/hal -I$(VHOST_TESTS).. -I$(ROOT)/platform/host -o $@ $(filter %.c,$^)

ifeq ($(shell uname -s),Linux)
run-boards:: vhost-display
endif
run-boards:: vdisplayTest
	./vdisplayTest

vhost-display: vhost-elf
	@mkdir -p out
	@for p in rgb565-160x128 mono-128x64; do \
		$(VHOST_ELF) --until-ms 3000 --no-stall-watch --display-profile $$p --trace out/vhost-display-$$p.log 2> /dev/null || \
			{ echo "vhost-display: $$p did not run"; exit 1; }; \
		n=$$(grep -c 'BOARD display.flush' out/vhost-display-$$p.log); \
		[ $$n -gt 0 ] || { echo "vhost-display: $$p drew no screen"; exit 1; }; \
		echo "# vhost-display: $$p, $$n screen lines"; \
	done
	@code=0; $(VHOST_ELF) --display-profile indexed-240x320 2> /dev/null || code=$$?; [ $$code -eq 2 ] || \
		{ echo "vhost-display: a profile the build cannot hold exited $$code, not 2"; exit 1; }
	@echo "# vhost-display: PASSED"
.PHONY: vhost-display

# Every profile in a build of its own, 3000 ms each. Slow: run it when the profiles or the core's gfx change
vdisplay-profiles:
	@mkdir -p out
	@for p in $(VDISPLAY_PROFILES); do \
		env -u MAKEFLAGS -u MFLAGS $(MAKE) -s -C $(ROOT) BOARD=vhost DISPLAY_PROFILE=$$p BUILD=build/vhost-$$p \
			> out/vdisplay-build-$$p.log 2>&1 || { cat out/vdisplay-build-$$p.log; exit 1; }; \
		$(ROOT)/build/vhost-$$p/rfoundry_vhost.elf --until-ms 3000 --no-stall-watch --trace out/vdisplay-$$p.log 2> /dev/null || \
			{ echo "vdisplay-profiles: $$p did not run"; exit 1; }; \
		n=$$(grep -c 'BOARD display.flush' out/vdisplay-$$p.log); \
		[ $$n -gt 0 ] || { echo "vdisplay-profiles: $$p drew no screen"; exit 1; }; \
		echo "$$p screen lines: $$n, build warnings: $$(grep -c 'warning:' out/vdisplay-build-$$p.log)"; \
	done
.PHONY: vdisplay-profiles
