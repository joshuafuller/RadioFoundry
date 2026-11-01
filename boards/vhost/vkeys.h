// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The virtual board's key list. The web control page (CONFIG_NET_WEBCTL) reads this enum, through
// tools/gen_webctl_assets.py, to name each key: KEY_SELECT becomes "select". The order is the key map index
// (keypad_vhost.c: --key MS:BIT:HOLD counts the same way), and the drawing, boards/vhost/vhost.svg, tags one shape
// "key-<name>" for each. Add a key here, in the key map and in the drawing together.
#ifndef VKEYS_H
#define VKEYS_H

enum {
	KEY_SELECT, KEY_BACK, KEY_UP, KEY_DOWN, KEY_PTT, KEY_MONITOR,
	KEY_0, KEY_1, KEY_2, KEY_3, KEY_4, KEY_5, KEY_6, KEY_7, KEY_8, KEY_9,
	KEY_COUNT
};

#endif
