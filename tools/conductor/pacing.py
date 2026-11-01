# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Wall time gates grants, never firmware events (ADR 0005)."""

import math
import time


class Pacing:
    def __init__(self, notes, *, clock=time.monotonic, lag_us=100000):
        self.notes = notes
        self.clock = clock
        self.lag_us = lag_us
        self.ratio = None
        self.behind = None
        self.reset(0, "max")

    def reset(self, now, ratio):
        if ratio != "max":
            if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
                raise ValueError("WORLD_BAD_RATIO")
            if not math.isfinite(ratio) or ratio <= 0:
                raise ValueError("WORLD_BAD_RATIO")
        self.finish(now)
        self.ratio = None if ratio == "max" else ratio
        self.origin_us = now
        self.origin_wall = self.clock()
        self.notes.append({"event": "pacing", "time_us": now, "ratio": ratio})

    def finish(self, now):
        if self.behind is not None:
            self.behind["end_us"] = now
            self.behind["end_wall"] = self.clock()
            self.behind = None

    def cap(self, live=False):
        return 1000 if self.ratio is not None or live else None

    def stamp(self, next_start):
        if self.ratio is None:
            return next_start
        arrival = self.origin_us + int((self.clock() - self.origin_wall) * self.ratio * 1000000)
        if arrival < next_start:
            self.notes.append({"event": "late_input", "time_us": next_start,
                               "late_us": next_start - arrival})
        return max(arrival, next_start)

    def delay(self, end, now):
        if self.ratio is None:
            return 0
        wall = self.clock()
        lag = (wall - self.origin_wall) * self.ratio * 1000000 - (now - self.origin_us)
        if lag > self.lag_us and self.behind is None:
            self.behind = {"event": "Behind", "start_us": now, "start_wall": wall}
            self.notes.append(self.behind)
        elif lag <= self.lag_us:
            self.finish(now)
        deadline = self.origin_wall + (end - self.origin_us) / (self.ratio * 1000000)
        return max(0, deadline - wall)
