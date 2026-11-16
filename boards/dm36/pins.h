// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Shared assignments: docs/spec/hardware-dm36.md 4. Unresolved nets stay unused.
#ifndef DM36_PINS_H
#define DM36_PINS_H
#include "../dm32/pins.h"

#undef PIN_GPS_RXD
#undef PIN_GPS_POWER
#undef GPS_POWER_ON_LEVEL
#undef PIN_BCAST_SDA
#undef PIN_BCAST_SCL

// Volume encoder: A = PTB7, B = PTB6 (section 5.1), inherited PIN_ENC_A/B.
#define PIN_BT_ENABLE           0x30 // high enabled; held low until a transport driver exists
#define PIN_BT_RXD              0x0D // UART3 mux; module trace unverified
#define PIN_BT_TXD              0x0E // UART3 mux; module trace unverified
#define BT_UART_BASE            0x14090000u
#define BT_UART_BAUD            115200u

// Board hooks keep the shared implementation independent of radio identity.
#define BOARD_IO_HEADER "io_init.h"
#define BOARD_VOLUME_HEADER "volume.h"
// The control nets have known initial states, but their electrical functions remain unverified.
// Battery ADC2/scaling, JTAG and RTC wiring remain unverified (sections 1 and 9.3).
#endif
