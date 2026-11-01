#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Run an unmodified HIL test against a virtual radio.

    python3 tools/conductor/run_hil.py tests/hil/test_boot.py::test_version_and_features [pytest options]

Steps, in order. 1 Start a conductor (one world, one radio) and run it at ratio 1, because HIL tests
sleep on the wall clock. 2 Ask it for a HIL endpoint (a Unix socket for this run). 3 Grant the radio's
controller lease to the endpoint and wait for the answer. 4 Wait for the UI's first draw after its
splash, then start pytest with --radio-port set to the endpoint. 5 Release the lease, end the world.
The world folder keeps events.jsonl and audit.jsonl; its path is printed. The exit code is pytest's.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.rfoundry_debug import DebugLink

VERSION = 1


class WorldClient:
    """A blocking client of the world socket (worldapi.py): one JSON line out, wait for its answer."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(30)
        self.sock.connect(str(path))
        self.buffer = b""
        self.next_id = 0

    def call(self, command, **fields):
        self.next_id += 1
        request = {"version": VERSION, "id": self.next_id, "command": command, **fields}
        self.sock.sendall(json.dumps(request).encode() + b"\n")
        while True:
            while b"\n" not in self.buffer:
                data = self.sock.recv(65536)
                if not data:
                    raise RuntimeError("the conductor closed the world socket")
                self.buffer += data
            line, _, self.buffer = self.buffer.partition(b"\n")
            reply = json.loads(line)
            if reply.get("id") == self.next_id:      # other lines are state events
                if not reply.get("ok"):
                    raise RuntimeError(f"{command}: {reply.get('error')}")
                return reply["result"]

    def close(self):
        self.sock.close()


def wait_for_ui(port):
    import serial

    handler = "tools.conductor.serial_handlers"
    if handler not in serial.protocol_handler_packages:
        serial.protocol_handler_packages.append(handler)
    deadline = time.monotonic() + 10
    with DebugLink(port) as link:
        # ui.screen already says home during the splash, when the UI discards keys.
        while int(link.view(["link"]).get("ui.draws", "0")) == 0:
            if time.monotonic() >= deadline:
                raise TimeoutError("the virtual radio UI did not leave its splash within 10 s")
            time.sleep(0.05)


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    name = f"hil{os.getpid()}"
    conductor = subprocess.Popen([sys.executable, "-m", "tools.conductor.conductor", "--name", name],
                                 cwd=ROOT, stdout=subprocess.PIPE, text=True)
    code = 1
    client = None
    try:
        world_socket = Path(conductor.stdout.readline().strip())
        client = WorldClient(world_socket)
        client.call("run", ratio=1)
        endpoint = client.call("hil.create", radio=1)
        granted = client.call("hil.grant", endpoint=endpoint["endpoint"])
        print(f"run_hil: {endpoint['principal']} holds radio 1 at lease generation {granted['generation']}",
              flush=True)
        wait_for_ui(endpoint["url"])
        code = subprocess.run([sys.executable, "-m", "pytest", *argv, "--radio-port", endpoint["url"]],
                              cwd=ROOT).returncode
        client.call("hil.release", endpoint=endpoint["endpoint"])
        client.call("end")
    finally:
        if client is not None:
            client.close()
        try:
            conductor.wait(timeout=10)
        except subprocess.TimeoutExpired:
            conductor.kill()
    print(f"run_hil: world folder {world_socket.parent} (events.jsonl, audit.jsonl)")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
