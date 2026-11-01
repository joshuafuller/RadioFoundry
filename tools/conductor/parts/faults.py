# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Scheduled world faults and the retired research-worker controls.

World API: fault {kind: loss, radio, target, rate_ppm, time_us, [until_us]}, or
fault {kind: restart, radio, time_us}. Times are world microseconds, round 0.
With until_us a loss fault is a window, accepted or refused as one: at time_us the link's rate becomes
rate_ppm, and at until_us it goes back to the rate it had just before time_us. A window on a link that already
has one pending or open is refused (WORLD_FAULT_OVERLAP), and so is a plain loss fault on that link, so no
window end ever undoes another change.
Example: the console's loss window, radio 1 to radio 2 at 100 % from 3.4 s to 3.7 s (tools/console.py).
For example, loss from radio 1 to target 2 at 3000000 us changes the air policy
after the preceding span and before inputs at that time (ADR 0005, item 6).
PTT already uses key bit 4 through parts/input.py and convert.py.

Restart uses World.restart: pending radio inputs are discarded and startup
storage is loaded again. It is not the research worker's journal recovery.
The scenario runner submits later inputs after the restart. Flash-preserving
firmware reboot remains the separate World.reboot command.

Retired inputs below called research internals, rather than firmware inputs.
Their completion/codeplug_channel observations and queue counters are also
retired: no equivalent exists in platform/host/vchan_proto.h. In particular,
VIEW depth is UI depth, not the research worker's data queue depth. Do not
fabricate those observations from successful input submission.
"""

import heapq

from ..world import WorldError, timestamp
from . import audit
from .air import integer
from .airctl import PPM, policy

SUPPORTED_INPUTS = ("loss", "restart")
RETIRED_INPUTS = {
    "queue_limits": "direct research queue configuration; no firmware input or world channel command",
    "miss_tx_done": "research modem completion suppression; the virtual modem exposes no suppression input",
    "channel_rename": "direct research codeplug edit; use startup storage steps or firmware editor inputs",
    "channel_queue": "direct research staged edit; firmware task scheduling does not expose that staging boundary",
    "channel_read": "direct research codeplug readback; no codeplug_channel observation on the world channel",
    "flash_cut": "research byte/erase budget; boards/vhost/flash_vhost.c has no cut-budget input",
}


class Faults:
    def __init__(self, world):
        self.world = world
        self.pending = []
        self.sequence = 0
        self.windows = {}           # (sender, receiver) -> rate to restore at the window's end; None until it opens
        world.api.register("fault", self.schedule)
        world.add_grant_limit(self.limit)
        world.on_grant_done(self.done)

    def schedule(self, request):
        world = self.world
        world._command()
        if world.state != "Running":
            raise WorldError("WORLD_PAUSED")
        kind = request["kind"]
        if kind in RETIRED_INPUTS:
            raise WorldError(f"WORLD_INPUT_RETIRED: {kind}; {RETIRED_INPUTS[kind]}")
        if kind not in SUPPORTED_INPUTS:
            raise WorldError("WORLD_UNKNOWN_FAULT")
        radio = request["radio"]
        world._check_radio(radio)
        if radio not in world.radios:
            raise WorldError("WORLD_RADIO_OFF")
        at = timestamp(request["time_us"])
        if at < world.next_start:
            raise WorldError("WORLD_PAST_INPUT")
        fault = {"kind": kind, "radio": radio, "time_us": at}
        until = None
        if kind == "loss":
            target = request["target"]
            world._check_radio(target)
            if radio == target:
                raise WorldError("WORLD_BAD_LINK")
            fault.update(target=target, rate_ppm=integer(request["rate_ppm"], 0, PPM, "WORLD_BAD_LOSS_RATE"))
            if (radio, target) in self.windows:
                raise WorldError("WORLD_FAULT_OVERLAP")     # its end would undo this change
            if request.get("until_us") is not None:
                until = timestamp(request["until_us"])
                if until <= at:
                    raise WorldError("WORLD_BAD_WINDOW")
                fault["until_us"] = until
        self.sequence += 1
        heapq.heappush(self.pending, (at, self.sequence, fault))
        if until is not None:
            # Both edges go in together, so no refusal can leave a window open without its end.
            self.windows[(radio, fault["target"])] = None
            heapq.heappush(self.pending, (until, self.sequence, {"kind": "loss", "radio": radio,
                                                                 "target": fault["target"], "time_us": until,
                                                                 "restore": True}))
        audit.emit(world, "world_fault", None, radio, "accepted", "OK", fault=fault)
        if at == world.now:
            self.done(at, {})
        return {"time_us": at, "sequence": self.sequence}

    def limit(self, now):
        return self.pending[0][0] if self.pending else None

    def done(self, target, outboxes):
        while self.pending and self.pending[0][0] <= target:
            _, _, fault = heapq.heappop(self.pending)
            if fault["kind"] == "loss":
                link = (fault["radio"], fault["target"])
                if fault.get("restore"):
                    fault = {**fault, "rate_ppm": self.windows.pop(link) or 0}
                elif "until_us" in fault:
                    self.windows[link] = dict(policy(self.world).loss_ppm).get(link, 0)
                self.world.api.handlers["air_loss"]({"sender": fault["radio"],
                    "receiver": fault["target"], "rate_ppm": fault["rate_ppm"]})
            else:
                state = self.world.state
                # This fault was accepted while Running; a step may now reach it.
                if state == "Stepping":
                    self.world.state = "Paused"
                try:
                    self.world.restart(fault["radio"])
                finally:
                    if self.world.state != "Ended":
                        self.world.state = state
            self.world.notify({"event": "world_fault", **fault})


def install(world):
    control = Faults(world)
    notify = world.notify

    def failure_notice(event):
        if event.get("event") == "state" and event.get("state") == "Ended" \
                and str(event.get("reason", "")).startswith("WORLD_RADIO_FAILURE"):
            reason = event["reason"]
            for radio, slot in sorted(world.radios.items()):
                read = getattr(slot.channel, "stderr_text", None)
                stderr = read() if read else ""
                if stderr and stderr not in reason:
                    reason += f"\nradio {radio} stderr:\n{stderr}"
            world.reason = reason
            event = {**event, "reason": reason}
            # Reuse the audit's secret filtering for child diagnostics too.
            audit.emit(world, "radio_failure", None, None, "failed", reason)
        notify(event)

    world.notify = failure_notice
    return control
