// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
#ifndef VCHAN_H
#define VCHAN_H

#include <stddef.h>
#include "vchan_proto.h"

// Register an input owner before the run (normally in a constructor). One handler per kind; BYTES_IN is built in.
// Called at the supplied clock stamp, with a message valid until the handler returns. No handler means a named
// error, not a silently lost input. KEY, ENC, ETH_IN and the recording parts register from their own files.
typedef void (*vchan_handler_fn)(const vchan_message_t *message);
void vchan_register(unsigned kind, vchan_handler_fn handler);

// Send one packet at the current virtual time. No --chan: no-op. A payload over 2048 bytes ends the run with
// VCHAN_BAD_LENGTH. Packet boundaries are preserved, including a whole 1032-byte debug link reply.
void vchan_send(unsigned kind, const void *buf, size_t len);

// Flush a part's outbox before each DONE (including the initial DONE at time zero), in registration order.
// Default: no callbacks. A callback may call vchan_send; it must not move time or post firmware events.
typedef void (*vchan_done_fn)(void);
void vchan_on_done(vchan_done_fn fn);

#endif
