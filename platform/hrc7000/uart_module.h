// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#ifndef HRC_UART_MODULE_H
#define HRC_UART_MODULE_H
#include "hal_stream.h"
// Configure before stream.start(), from the host task, while the module is off.
void hrc_module_uart_configure(uint32_t base, unsigned irq, uint32_t baud);
void hrc_module_uart_flush(void);
uint32_t hrc_module_uart_errors(void);
extern const hal_stream_t hrc_module_uart_stream;
#endif
