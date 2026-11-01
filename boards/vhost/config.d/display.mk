# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
# The virtual board's display build (display_profiles_vhost.c, included from boards/vhost/config.mk). The core's frame
# buffer has a size fixed at build time, so a deep or large panel needs its own build:
#
#	make BOARD=vhost DISPLAY_PROFILE=indexed-240x320 BUILD=build/vhost-indexed-240x320
#
# Without DISPLAY_PROFILE the build is the standalone host one (rgb565-160x128 and mono-128x64 both fit) and --display-profile
# picks between them at run time. With it, the build holds that profile, runs it by default, and --display-profile still
# takes any profile the same frame buffer holds. BUILD is the caller's to set; it keeps one build apart from another
DISPLAY_PROFILE ?=
# <profile> = GFX_INDEXED GFX_FB_BYTES GFX_MAX_ROWS (core/ui/gfx.h); the same sizes as the boards' board.mk MCU_DEFS
VHOST_DISPLAY_rgb565-160x128 := 0 40960 160
VHOST_DISPLAY_mono-128x64 := 0 40960 160
VHOST_DISPLAY_indexed-160x128 := 1 10240 128
VHOST_DISPLAY_indexed-160x120 := 1 9600 120
VHOST_DISPLAY_indexed-240x320 := 1 38400 320
VHOST_DISPLAY_rgb565-240x320 := 0 153600 320
ifneq ($(DISPLAY_PROFILE),)
ifeq ($(VHOST_DISPLAY_$(DISPLAY_PROFILE)),)
$(error unknown DISPLAY_PROFILE=$(DISPLAY_PROFILE): the profiles are $(patsubst VHOST_DISPLAY_%,%,$(filter VHOST_DISPLAY_%,$(.VARIABLES))))
endif
MCU_DEFS += -DVDISPLAY_DEFAULT_PROFILE=\"$(DISPLAY_PROFILE)\" \
	-DGFX_INDEXED=$(word 1,$(VHOST_DISPLAY_$(DISPLAY_PROFILE))) \
	-DGFX_FB_BYTES=$(word 2,$(VHOST_DISPLAY_$(DISPLAY_PROFILE)))u \
	-DGFX_MAX_ROWS=$(word 3,$(VHOST_DISPLAY_$(DISPLAY_PROFILE)))u
endif
