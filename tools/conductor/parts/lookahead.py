# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Opt in through air_config's lookahead_proven boolean, before radios start.

The default stays 1000 us. Only a world whose check passed may set the flag.
The modem header owns the stated value. Pacing and other grant limits still apply.
This is a host scheduling claim, not proof of physical DMR timing (ADR 0005).
"""

import re

from ..channel import ROOT
from ..world import WorldError


_HEADER = (ROOT / "boards/vhost/dmr_vhost.h").read_text()
_DEFINES = dict(re.findall(r"^#define\s+(VDMR_\w+)\s+(\w+)", _HEADER, re.M))
_VALUE = _DEFINES["VDMR_LOOKAHEAD_US"]
VDMR_LOOKAHEAD_US = int(_DEFINES.get(_VALUE, _VALUE).rstrip("uU"))


class Lookahead:
    def __init__(self, world):
        self.world = world
        self.grant_us = 1000
        self.configure_air = world.api.handlers["air_config"]
        self.air_limit = self.configure_air.__self__.grant_limit
        # The air part registers first; replace only its cap, keeping every other limit.
        index = world._grant_limits.index(self.air_limit)
        world._grant_limits[index] = self.grant_limit
        world.api.handlers["air_config"] = self.configure

    def configure(self, request):
        proven = request.get("lookahead_proven", False)
        if type(proven) is not bool:
            raise WorldError("WORLD_BAD_LOOKAHEAD_PROVEN")
        result = self.configure_air(request)
        self.grant_us = VDMR_LOOKAHEAD_US if proven else 1000
        self.world.notify({"event": "air_lookahead", "time_us": self.world.now,
                           "lookahead_proven": proven, "grant_us": self.grant_us})
        return {**result, "lookahead_proven": proven, "grant_us": self.grant_us}

    def grant_limit(self, now):
        return now + self.grant_us if self.air_limit(now) is not None else None


def install(world):
    world.air_lookahead = Lookahead(world)
