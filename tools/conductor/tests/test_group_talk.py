# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Live: four real virtual radios on vradio's default air (dmr-group start storage, talkgroup 9).

PTT on radio 1 for 1 s through the world API keypad. During the call every other radio's view (GET_VIEW section 3,
read through an observer as tools/conductor/checks/interaction_check.py does) shows call.src=3100001, and the air part
delivered radio 1's bursts to radios 2, 3 and 4 (recording/air-deliveries.jsonl in the world folder).
"""

import json

from tools import vradio
from tools.conductor.checks.interaction_check import DMR, DMR_CALL, read_view
from tools.conductor.conductor import install_parts
from tools.conductor.parts import dlcache
from tools.conductor.world import Config, World

RADIOS = 4
PTT_DOWN_US, PTT_UP_US, VIEW_US, END_US = 1_000_000, 2_000_000, 1_900_000, 2_500_000


def test_ptt_on_radio_1_reaches_every_group_member(vhost_elf, tmp_path):
    world = World(Config(executable=str(vhost_elf), radios=RADIOS, end_us=END_US))
    views = {}
    try:
        install_parts(world)
        reply = world.api.dispatch({**vradio.group_air(RADIOS), "version": 1, "command": "air_config"})
        assert reply["ok"], reply
        world.add_grant_limit(lambda now: VIEW_US if now < VIEW_US else None)
        dlcache.get(world, cadence_us=500_000)
        world.api.bind("group-talk", tmp_path)
        world.start()
        world.run("max")
        grants = {radio: world.leases.grant(radio, "script:keys") for radio in [1]}
        for state, time_us in (("down", PTT_DOWN_US), ("up", PTT_UP_US)):
            reply = world.api.dispatch({"version": 1, "command": "key", "radio": 1, "key": "ptt", "state": state,
                                        "time_us": time_us, **grants[1]})
            assert reply["ok"], reply
        observers = {r: world.dlmux.connect(r, "debug:group-view", "observer") for r in range(2, RADIOS + 1)}
        while world.state != "Ended":
            if world.now >= VIEW_US and not views:
                for radio, session in observers.items():
                    views[radio] = (world.now, read_view(world, session, DMR_CALL), read_view(world, session, DMR))
            world.api.poll(0)
            world.advance()
        assert world.reason == "WORLD_END_TIME", world.reason
        folder = world.api.directory
    finally:
        world.close()
    for radio, (t_us, call, state) in sorted(views.items()):
        print(f"radio {radio} at {t_us} us: call.src={call.get('call.src')} call.dst={call.get('call.dst')} "
              f"call.group={call.get('call.group')} dmr.state={state.get('dmr.state')}")
    assert sorted(views) == list(range(2, RADIOS + 1))
    for radio, (t_us, call, _) in views.items():
        assert PTT_DOWN_US <= t_us < PTT_UP_US
        assert call.get("call.src") == "3100001", (radio, call)
    # The recording's air file holds every delivery the air part handed to a receiver (parts/resolve.py)
    lines = [json.loads(line) for line in (folder / "recording/air-deliveries.jsonl").read_text().splitlines() if line]
    deliveries = [e for e in lines if e["sender"] == 1]
    receivers = {e["receiver"] for e in deliveries}
    print(f"air deliveries from radio 1: {len(deliveries)}, receivers {sorted(receivers)}")
    assert receivers == set(range(2, RADIOS + 1))
    assert all(e["sender"] == 1 for e in lines)
    # The fold turned each radio's dmr-group storage into editor steps, and the recording lint accepts them
    result = json.loads((folder / "result.json").read_text())
    assert result["lint_error"] is None, result["lint_error"]
