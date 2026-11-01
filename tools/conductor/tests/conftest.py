# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Shared real firmware and socket-free channel fixtures."""

from collections import deque
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.conductor.channel import DONE, Kind, Message
from tools.conductor.world import Config, World


@pytest.fixture(scope="session")
def vhost_elf():
    result = subprocess.run(["make", "-j8", "BOARD=vhost"], cwd=ROOT, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert result.returncode == 0, result.stdout
    assert "warning:" not in result.stdout.lower(), result.stdout
    return ROOT / "build/vhost/rfoundry_vhost.elf"


@pytest.fixture
def world(tmp_path, vhost_elf):
    instance = World(Config(executable=str(vhost_elf)))
    try:
        instance.api.bind("test", tmp_path)
        instance.start()
        yield instance
    finally:
        instance.close()


class RadioStub:
    def __init__(self, executable, *args):
        self.args = args
        self.sent = []
        self.wire = deque([Message(Kind.DONE, DONE.pack(1000, 0))])
        self.inputs = []
        self.outputs = []
        self.closed = False
        self.failure = None

    def send(self, message):
        self.sent.append(message)
        if message.kind not in (Kind.GRANT, Kind.DRAIN):
            self.inputs.append(message)
            return
        end = message.time_us
        inclusive = message.kind == Kind.DRAIN
        for event in list(self.inputs):
            if event.time_us < end or inclusive and event.time_us == end:
                self.inputs.remove(event)
                self.wire.append(Message(Kind.BYTES_OUT, event.payload, event.time_us))
        for kind, payload in self.outputs:
            self.wire.append(Message(kind, payload, end))
        self.outputs.clear()
        due = (end // 1000 + 1) * 1000 if inclusive else end
        self.wire.append(Message(Kind.DONE, DONE.pack(due, int(inclusive)), end))

    def receive(self):
        if self.failure:
            raise self.failure
        return self.wire.popleft()

    def close(self):
        self.closed = True


@pytest.fixture
def stub_world():
    made = []

    def factory(**options):
        clock = options.pop("clock", None)
        instance = World(Config(**options), channel_factory=RadioStub, clock=clock)
        made.append(instance)
        return instance

    yield factory
    for instance in made:
        instance.close()
