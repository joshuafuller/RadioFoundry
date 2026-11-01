#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""One command runs the virtual radio showcase: python3 tools/demo.py [--fast] [--out DIR].

What it shows, in order. Each step prints one plain line before it runs and a PASS or FAIL line with the key
evidence after it. At the end a table lists every step, its result, its evidence and its wall time.

  0 launcher   tools/vradio.py up --radios 4 --until-ms 3000: the launcher as a user runs it, each radio's page
               up, ended at its time limit.
  1 world      the showcase world: four radios in group talk (vradio.group_air: radio k is DMR ID 3100000+k, all
               on talkgroup 9), with tools/console.py --control attached to it.
  2 call     radio 1 keys a group call; radios 2, 3 and 4 show "call from 3100001".
  3 collision  radios 2 and 3 key at the same time; the air names a collision for radios 1 and 4.
  4 loss       the console (not this script) opens a 100 % loss window at radio 4 while radio 1 talks:
               radio 2 still hears radio 1, radio 4 does not.
  5 sms        radio 1 sends a text message to radio 3 through the debug link; radio 3 keeps it, radio 1 gets
               the ACK.
  6 record     the world ends at its time limit; its folder is now a complete recording.
  7 travel     tools/conductor/replay.py --stop-at-ms: the recording is replayed to the group call, checked,
               paused and each radio's page is opened again, still inside the call.
  8 interaction     tools/conductor/checks/interaction_check.py: two radios, call and SMS (and, in full mode, replay and
               privacy).
  9 parrot     tools/conductor/checks/parrot_check.py: caller echo bug. On this build radio 1 must NOT play the
               parrot's echo of its own call: the bug reproduces, so the step passes when the check fails that
               way. With the fixed build (see FIXED below) --part mixed must pass too.
 10 sms-reboot tools/conductor/checks/sms_restart_check.py --mode reboot: the SMS-after-reboot bug. The verdict
               must be CONFIRMED (the bug reproduces); UNTESTABLE is a FAIL.
 11 scale      tools/conductor/checks/scale_probe.py, small sizes.
 12 cleanup    no radio process of this build is left.

Modes. The default (full) takes a browser screenshot of the console after each world step, and of every radio
page after time travel, into --out (Playwright, as tools/conductor/checks/console_check.py does). --fast takes no
screenshots and runs the small version of each check, for CI, in well under 5 minutes.

Why the showcase world is not `vradio.py up` itself. A vradio world runs the web gateway, and the gateway's network
pushes a udp_delivery event to every world client, about 0.8 MB each virtual second with 4 idle radios. A client
that reads only between its own requests (the console polls every 100 ms) passes the world API's 64 KiB queue
limit and is dropped (worldapi.py _queue), so the console says "world ended" within seconds. The showcase world is
therefore built by this same script in a child process (demo.py --serve-world, hidden) from vradio's own pieces:
vradio.configuration, vradio.group_air, install_parts and the air_config command, the world socket and
frames.sock, and vradio's loop, with no web gateway. Step 0 still runs the real launcher.

How it drives the world. This script is one more world client on the world socket (as tools/console.py is), and
sends only ordinary world API commands: "key" for PTT, "run" with
until_us to move virtual time to the next step (the world then pauses there), "lease.grant" and
"debuglink.open" for the SMS, and observer debug link sessions to read each radio's view. The loss window is the
console's own world control: this script pastes the console's one-time control code and posts the window to the
console's page API, as the page's Fault button does. Example: step 2 reads GET_VIEW section 3 of radio 2 and gets
call.src=3100001.

Exit 0 when every step passes, 1 when one fails, 2 for bad arguments or a missing prerequisite.
"""

import argparse
from dataclasses import dataclass
import glob
import http.client
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.conductor.parts import dlcache, dlmux  # noqa: E402
from tools.conductor.run_hil import WorldClient  # noqa: E402

ELF = ROOT / "build/vhost/rfoundry_vhost.elf"
FIXED = Path.home() / "development/worktrees/radiofoundry-fix-parrot-own-id/build/vhost/rfoundry_vhost.elf"
CHECKS = ROOT / "tools/conductor/checks"
RADIOS = 4
IDS = {radio: 3100000 + radio for radio in range(1, RADIOS + 1)}
DMR, DMR_CALL, DMR_DATA_STATS = 2, 3, 11        # GET_VIEW sections (core/debuglink/debuglink.h)
DMR_DATA, OP_SMS = 0x0E, 1                      # core/debuglink/debuglink_proto.h

# The showcase timeline in virtual microseconds. Each running segment schedules its edges at 1x
# and is still before 2 s, so the run is the same every time.
T = {
    "ready": 2_000_000,                         # step 1 ends: radios up, console attached
    "call_down": 3_000_000, "call_up": 4_500_000, "call_read": 3_800_000,
    "collide_down": 6_000_000, "collide_up": 7_000_000, "collide_read": 6_500_000,
    "sms_send": 8_500_000, "sms_read": 12_500_000,
    "end": 14_000_000,
}
# The loss window runs in a second, short world of its own: a world fault ends the recording
# (RECORD_NO_V2_KIND:air_policy, parts/resolve.py), and the showcase world must stay a complete recording.
LOSS_T = {"ready": 2_000_000, "down": 2_500_000, "up": 4_000_000, "start": 2_400_000, "read": 3_900_000,
          "end": 5_000_000}
LOSS_KEYS = [(1, LOSS_T["down"], "down"), (1, LOSS_T["up"], "up")]
TRAVEL_MS = 3500                                # time travel target: inside radio 1's group call
TRAVEL_STEP_US = 200_000                        # after the travel pause (about 3.73 s): still before PTT up at 4.5 s
KEYS = [(1, "call_down", "down"), (1, "call_up", "up"),
        (2, "collide_down", "down"), (3, "collide_down", "down"), (2, "collide_up", "up"), (3, "collide_up", "up")]
KEYS = [(radio, T[at], state) for radio, at, state in KEYS]
LOSS_RECEIVER, LOSS_MS = 4, 500
SMS = {"radio": 1, "dst": IDS[3], "flags": 2, "text": "DEMO 1"}

# What each mode runs. Wall time limits (seconds) keep a stuck step from hanging CI.
PLANS = {
    "fast": {"interaction": ["--part", "air"], "parrot": [["--part", "echo"]], "sms_runs": "1",
             "scale": ["--sizes", "4", "--no-grow", "--virtual-s", "2", "--no-determinism"]},
    "full": {"interaction": ["--part", "all"], "parrot": [["--part", "echo"], ["--part", "replay"]], "sms_runs": "2",
             "scale": ["--sizes", "4,10", "--no-grow", "--virtual-s", "5", "--determinism-size", "4"]},
}
STEP_TIMEOUT_S = 240
LIVE = ("world", "group call", "collision", "loss fault", "sms", "record", "time travel")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"demo: {' '.join(message.split())}; run {self.prog} --help for usage.\n")


def arguments(argv):
    parser = Parser(prog="demo.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fast", action="store_true", help="no screenshots, small runs (CI, under 5 minutes)")
    parser.add_argument("--out", type=Path, help="folder for screenshots, logs and the recording "
                        "(default build/demo; --fast keeps only logs there)")
    parser.add_argument("--fixed", type=Path, default=FIXED,
                        help="the caller echo bug fixed build; when the file exists parrot --part mixed runs too")
    parser.add_argument("--serve-world", metavar="NAME", help=argparse.SUPPRESS)    # internal: the world child
    parser.add_argument("--serve-until-ms", type=int, default=T["end"] // 1000, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.mode = "fast" if args.fast else "full"
    args.out = (args.out or ROOT / "build/demo").resolve()
    if args.out.exists() and not args.out.is_dir():
        parser.error("--out must be a folder")
    return args


# --- the summary -----------------------------------------------------------------------------------------------

@dataclass
class Step:
    name: str
    result: str = "FAIL"            # PASS, FAIL or SKIP
    evidence: str = ""
    wall_s: float = 0.0
    shots: tuple = ()


def table(steps):
    """The summary as plain text columns: step, result, evidence, wall time, then a total line."""
    rows = [("step", "result", "evidence", "wall s")]
    rows += [(s.name, s.result, s.evidence, f"{s.wall_s:.1f}") for s in steps]
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    lines = []
    for index, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(width) for cell, width in zip(row[:3], widths)) + "  " + row[3].rjust(6))
        if index == 0:
            lines.append("  ".join("-" * width for width in widths) + "  " + "-" * 6)
    passed = sum(s.result == "PASS" for s in steps)
    lines.append(f"{passed} of {len(steps)} steps passed in {sum(s.wall_s for s in steps):.1f} s")
    return "\n".join(lines)


def exit_code(steps):
    return 0 if steps and all(s.result in ("PASS", "SKIP") for s in steps) else 1


class StepFailed(Exception):
    """A step's check did not hold; the message is its evidence."""


def require(condition, evidence):
    if not condition:
        raise StepFailed(evidence)


# --- verdicts of the check tools (pure, so they are unit tested) ------------------------------------------------

def last(lines, pattern):
    found = [line for line in lines if re.search(pattern, line)]
    return found[-1].strip() if found else ""


def interaction_verdict(code, lines):
    call = last(lines, r"^SC1 at .* call\.src=")
    match = re.search(r"call\.src=(\d+)", call)
    evidence = f"radio 2 shows call from {match.group(1)}" if match else "no SC1 call line"
    sms = last(lines, r"data\.sms_received=")
    if "data.sms_received=1" in sms:
        evidence += ", SMS received and ACKed"
    if last(lines, r"^SC2 PASS"):
        evidence += ", replay same"
    if last(lines, r"^SC3 PASS"):
        evidence += ", privacy lint pass"
    ok = code == 0 and bool(last(lines, r"^interaction_check: PASS"))
    return ("PASS" if ok else "FAIL"), evidence if ok else f"exit {code}: {last(lines, r'.') or evidence}"


def parrot_verdict(part, code, lines):
    """echo on the unfixed build returns zero with radio 1 showing rx_other. replay and mixed must pass."""
    if part == "echo":
        caller = last(lines, r"radio 1 \(the caller\)")
        state = re.search(r"dmr\.state/call\.src ([^\s;]+)", caller)
        played = re.search(r"rx_calls_played \+(\d+)", caller)
        reproduced = (code == 0 and last(lines, r"^echo PASS reproduced") and state is not None
                      and state.group(1).startswith("rx_other/") and played is not None and played.group(1) == "0")
        if reproduced:
            return "PASS", f"caller echo bug reproduced: radio 1 shows {state.group(1)} for its own echo, played +0"
        if code == 0:
            return "FAIL", "caller echo reproduction was not confirmed"
        return "FAIL", f"exit {code}: {last(lines, r'.')}"
    ok = code == 0 and bool(last(lines, rf"^parrot_check: PASS --part {part}"))
    if part == "mixed" and ok:
        return "PASS", "fixed radio 1 plays its echo; unfixed radio 2 does not"
    if ok:
        return "PASS", "replay of the failing run is byte for byte the same"
    return "FAIL", f"exit {code}: {last(lines, r'.')}"


def sms_reboot_verdict(code, lines):
    verdict = last(lines, r"^sms_restart_check: ")
    receiver = last(lines, r"^\[hypothesis\]\s+B \(radio 2\)")
    fields = dict(re.findall(r"(\w+)=(\S+)", receiver))
    detail = (f"B received={fields.get('sms_received')} duplicates={fields.get('sms_duplicates')}"
              if fields else "no B view")
    if code == 0 and verdict.endswith("CONFIRMED"):
        return "PASS", f"bug reproduced (CONFIRMED): after the reboot {detail}"
    if code == 0 and verdict.endswith("REFUTED"):
        return "FAIL", f"REFUTED: the bug did not reproduce ({detail})"
    return "FAIL", f"exit {code}: {verdict or last(lines, r'.')}"


def scale_verdict(code, lines):
    rows = [line.split() for line in lines if re.match(r"^\s+\d+\s+(yes|no)\s", line)]
    leftover = last(lines, r"^leftover processes")
    sizes = sorted({(int(r[0]), r[1], r[4]) for r in rows})
    evidence = ", ".join(f"{n} radios {'ok' if ok == 'yes' else 'failed'} at v/wall {ratio}"
                         for n, ok, ratio in sizes)
    ok = code == 0 and sizes and all(ok == "yes" for _, ok, _ in sizes) and leftover.endswith(": 0")
    if ok:
        return "PASS", evidence + ", 0 left over"
    return "FAIL", f"exit {code}: {evidence or last(lines, r'.')}"


# --- child processes ------------------------------------------------------------------------------------------

class Child:
    """A child process in its own session, its output read line by line into a queue and a log file."""

    def __init__(self, command, log, env=None):
        self.command = command
        self.lines = queue.Queue()
        self.seen = []
        self.log = open(log, "a")
        self.log.write(f"$ {' '.join(map(str, command))}\n")
        self.log.flush()
        self.process = subprocess.Popen([str(c) for c in command], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            self.log.write(line)
            self.log.flush()
            self.lines.put(line.rstrip("\n"))
        self.lines.put(None)

    def expect(self, pattern, timeout):
        """The next output line matching pattern (a regex), or StepFailed."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise StepFailed(f"timed out waiting for {pattern!r} from {Path(self.command[1]).name}") from None
            if line is None:
                self.lines.put(None)
                raise StepFailed(f"{Path(self.command[1]).name} ended (exit {self.process.wait()}) before "
                                 f"{pattern!r}: {self.seen[-1] if self.seen else ''}")
            self.seen.append(line)
            match = re.search(pattern, line)
            if match:
                return match

    def wait(self, timeout):
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            raise StepFailed(f"{Path(self.command[1]).name} did not end within {timeout} s") from None
        self.reader.join(5)
        while True:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                break
            if line is not None:
                self.seen.append(line)
        return code

    def stop(self):
        if self.process.poll() is None:
            for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
                try:
                    os.killpg(self.process.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    self.process.wait(timeout=grace)
                    break
                except subprocess.TimeoutExpired:
                    continue
        self.reader.join(5)
        self.log.close()


def radio_processes(executables):
    """PIDs whose /proc/PID/exe is exactly one of these files (other lanes run radios of other builds)."""
    wanted = {str(Path(path).resolve()) for path in executables}
    found = []
    for entry in glob.glob("/proc/[0-9]*/exe"):
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.removesuffix(" (deleted)") in wanted:
            found.append(int(entry.split("/")[2]))
    return sorted(found)


# --- the world client side ------------------------------------------------------------------------------------

class Showcase:
    """The live world: vradio up, the console, and this script's own world client."""

    def __init__(self, runtime, out, env, name=None):
        self.runtime, self.out, self.env = runtime, out, env
        self.name = name or f"demo-{os.getpid()}"
        self.folder = runtime / "rfoundry-vr" / self.name
        self.world = self.console = self.client = None
        self.views = {}             # radio -> observer socket
        self.controller = None
        self.cursor = 0
        self.console_url = self.code = self.cookie = self.proof = None
        self.browser = None
        self.keys = ()

    # world API
    def call(self, command, **fields):
        return self.client.call(command, **fields)

    def start_run(self, ratio="max", **fields):
        now = self.call("status")["time_us"]
        # Pauses revoke leases and cancel scheduled edges; each segment obtains new authority.
        self.call("run", ratio=1, **fields)
        for radio in sorted({radio for radio, _, _ in self.keys}):
            edges = [(at, state) for r, at, state in self.keys if r == radio]
            before = [state for at, state in edges if at <= now]
            active = bool(before and before[-1] == "down")
            due = [(at, state) for at, state in edges if now < at <= fields.get("until_us", float("inf"))]
            if not active and not due:
                continue
            granted = self.call("lease.grant", radio=radio, principal="script:demo")
            credentials = {key: granted[key] for key in ("principal", "generation", "secret")}
            if active:
                self.call("key", radio=radio, key="ptt", state="down", **credentials)
            for at, state in due:
                self.call("key", radio=radio, key="ptt", state=state, time_us=at, **credentials)
        self.call("set_ratio", ratio=ratio)

    def run_until(self, until_us, ratio="max", timeout=120):
        self.start_run(ratio=ratio, until_us=until_us)
        self.wait_paused(until_us, timeout)

    def wait_paused(self, until_us, timeout=120):
        deadline = time.monotonic() + timeout
        while True:
            state = self.call("status")
            if state["state"] == "Paused" and state["time_us"] >= until_us:
                return state
            require(state["state"] != "Ended", f"the world ended early at {state['time_us']} us")
            require(time.monotonic() < deadline, f"the world did not reach {until_us} us in {timeout} s")
            time.sleep(0.02)

    def events(self):
        """World events since the last call (observe.events), oldest first."""
        found = []
        while True:
            page = self.call("observe.events", after=self.cursor)
            items = page.get("events", [])
            found.extend(items)
            if not items:
                return found
            self.cursor = items[-1]["seq"]

    # debug link sessions
    def session(self, radio, principal, role, **lease):
        reply = self.call("debuglink.open", radio=radio, principal=principal, role=role, **lease)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(reply["path"])
        return sock

    @staticmethod
    def exchange(sock, frame):
        sock.sendall(frame)
        parser = dlmux.Parser(dlmux.REPLY_MAX)
        while True:
            data = sock.recv(65536)
            require(data, "the debug link session closed")
            replies = parser.feed(data)
            if replies:
                return replies[0]

    def view(self, radio, section):
        """One GET_VIEW section from the observer cache, as a dict of key=value lines (interaction_check.read_view)."""
        if radio not in self.views:
            self.views[radio] = self.session(radio, "debug:demo-view", "observer")
        reply = self.exchange(self.views[radio], dlmux.frame(dlcache.GET_VIEW, section, bytes((section,))))
        require(reply[0] == "frame" and reply[3][:1] == bytes((dlcache.ST_OK,)),
                f"radio {radio} GET_VIEW {section} failed: {reply[:3]}")
        text = reply[3][6:].decode("utf-8", "replace")
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    # the console page API
    def http(self, method, path, body=None, headers=()):
        port = int(self.console_url.split(":")[2].split("/")[0])
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        sent = {"Host": f"127.0.0.1:{port}", **dict(headers)}
        if self.cookie:
            sent["Cookie"] = self.cookie
            sent["X-RadioFoundry-Session"] = self.proof
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            sent.update({"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json",
                         "Sec-Fetch-Site": "same-origin"})
        try:
            connection.request(method, path, body=data, headers=sent)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def console_login(self):
        status, headers, body = self.http("POST", "/session", {"token": self.console_url.split("#rf-token=")[1]})
        require(status == 200 and "Set-Cookie" in headers, f"console login refused: {status}")
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.proof = json.loads(body)["proof"]

    def console_state(self):
        status, _, body = self.http("GET", "/api/state?after=0")
        require(status == 200, f"console state refused: {status}")
        return json.loads(body)

    def console_control(self, path, body):
        status, _, reply = self.http("POST", path, body)
        require(status == 200, f"console {path} refused: {status} {reply.decode(errors='replace').strip()}")
        return json.loads(reply)

    # screenshots
    def shot(self, name, url=None, wait=()):
        if self.browser is None:
            return ()
        return (self.browser.shoot(self.out / f"{name}.png", url or self.console_origin(), wait,
                                   None if url else (self.cookie, self.proof)),)

    def console_origin(self):
        return self.console_url.split("/#", 1)[0] + "/"

    def close(self):
        for sock in [*self.views.values(), self.controller]:
            if sock is not None:
                sock.close()
        self.views.clear()
        self.controller = None
        if self.client is not None:
            self.client.close()
            self.client = None
        for child in (self.console, self.world):
            if child is not None:
                child.stop()


class Browser:
    """One headless Chromium for all screenshots (Playwright, as console_check.py)."""

    def __init__(self):
        from playwright.sync_api import sync_playwright
        from tools.conductor.checks.console_check import chromium
        self.play = sync_playwright().start()
        executable = chromium()
        self.browser = self.play.chromium.launch(**({"executable_path": executable} if executable else {}))

    def open(self, url):
        """A radio page, kept open: it asks the firmware for its screen, which only answers while time runs."""
        context = self.browser.new_context(viewport={"width": 1440, "height": 960})
        page = context.new_page()
        page.goto(url)
        return context, page

    def shoot(self, path, url, wait=(), cookie=None):
        context = self.browser.new_context(viewport={"width": 1440, "height": 960})
        try:
            if cookie:
                cookie, proof = cookie
                name, value = cookie.split("=", 1)
                context.add_init_script("if (location.origin === " + json.dumps(url.rstrip("/")) + ") "
                                        "sessionStorage.setItem('rf-browser-v1', " + json.dumps(proof) + ");")
                context.add_cookies([{"name": name, "value": value, "url": url, "httpOnly": True,
                                      "sameSite": "Strict"}])
            page = context.new_page()
            page.goto(url)
            for selector in wait:
                try:
                    page.wait_for_selector(selector, timeout=15000)
                except Exception:       # a missing mark still gets a picture; the step's own check decides
                    pass
            page.wait_for_timeout(1500)
            page.screenshot(path=str(path), full_page=True)
            return path
        finally:
            context.close()

    def close(self):
        try:
            self.browser.close()
        finally:
            self.play.stop()


# --- the steps ------------------------------------------------------------------------------------------------

def serve_world(name, until_ms):
    """The showcase world child: vradio's group talk world with frames.sock and no web gateway (module text).
    Runs until the world ends; prints "world: NAME" once bound and "ended: virtual time limit" as vradio does."""
    from tools import vradio
    from tools.conductor.conductor import install_parts
    from tools.conductor.world import World
    options = argparse.Namespace(radios=RADIOS, build={}, scenario=None, until_ms=until_ms)
    world = World(vradio.configuration(options, vradio.build(True)))
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: signal.default_int_handler(signum, frame))
    try:
        install_parts(world)
        response = world.api.dispatch({**vradio.group_air(RADIOS), "version": 1, "command": "air_config"})
        if not response["ok"]:
            print(f"demo world: air settings refused: {response['error']}", flush=True)
            return 1
        path = world.api.bind(name, vradio.runtime())
        world.frames.bind()
        print(f"world: {name}\nworld folder: {path.parent}", flush=True)
        world.start()
        world.run(1)
        while world.state != "Ended":
            world.api.poll(0.05 if world.state == "Paused" else 0)
            world.advance()
        world.api.poll(0)
    except KeyboardInterrupt:
        world.end()
    finally:
        try:
            world.close()
        finally:
            signal.signal(signal.SIGTERM, previous)
    reasons = {"WORLD_END_TIME": "virtual time limit", "WORLD_STOPPED": "stopped"}
    print(f"ended: {reasons.get(world.reason, world.reason)}", flush=True)
    return 0 if world.reason in reasons else 1


def step_launcher(show, fixed=None):
    """The real launcher, as a user types it: 4 radio pages up, then it ends at its time limit. With the caller echo bug fixed
    build, radio 1 runs it (vradio --build 1=PATH, version mixing)."""
    build = ["--build", f"1={fixed}"] if fixed else []
    child = Child([sys.executable, ROOT / "tools/vradio.py", "up", "--radios", str(RADIOS), "--no-browser",
                   "--name", f"{show.name}-launcher", "--until-ms", "3000", *build], show.out / "logs/vradio.log",
                  show.env)
    try:
        code = child.wait(120)
    finally:
        child.stop()
    pages = [line for line in child.seen if re.match(r"^radio \d+: http://127\.0\.0\.1:\d+/#rf-token=", line)]
    require(code == 0 and len(pages) == RADIOS and "ended: virtual time limit" in child.seen,
            f"vradio exit {code}, {len(pages)} pages: {child.seen[-1] if child.seen else ''}")
    mixed = " (radio 1 on the caller echo bug fixed build)" if fixed else ""
    return f"vradio up: {len(pages)} radio pages up{mixed}, ended at the 3 s time limit", ()


def open_world(show, keys, ready_us, until_ms):
    """A world child, this script's world client and the console with --control. Key edges are scheduled with a fresh lease for each running segment."""
    log = show.out / "logs"
    show.world = Child([sys.executable, ROOT / "tools/demo.py", "--serve-world", show.name, "--serve-until-ms",
                        str(until_ms)], log / f"{show.name}.log", show.env)
    show.world.expect(rf"^world: {show.name}$", 60)
    deadline = time.monotonic() + 30
    while not (show.folder / "world.sock").exists():
        require(time.monotonic() < deadline and show.world.process.poll() is None, "no world socket")
        time.sleep(0.05)
    show.client = WorldClient(show.folder / "world.sock")
    while show.call("status")["state"] != "Running":
        require(time.monotonic() < deadline, "the world did not start")
        time.sleep(0.02)
    show.keys = keys
    show.start_run(ratio=1, until_us=ready_us)
    show.console = Child([sys.executable, ROOT / "tools/console.py", "--world", show.name, "--runtime",
                          show.runtime, "--no-browser", "--control"], log / f"console-{show.name}.log", show.env)
    show.console_url = show.console.expect(r"^console: (http://127\.0\.0\.1:\d+/#rf-token=\S+)$", 30).group(1)
    show.code = show.console.expect(r"^console: control code (\S+) ", 10).group(1)
    show.console_login()
    show.wait_paused(ready_us)
    deadline = time.monotonic() + 20
    while len(show.console_state().get("frames", {})) < RADIOS:
        require(time.monotonic() < deadline, "the console does not show every radio's screen")
        time.sleep(0.1)
    radios = [r for r in show.call("observe.status")["radios"] if r["on"]]
    require(len(radios) == RADIOS, f"{len(radios)} radios on")


def step_world(show):
    open_world(show, KEYS, T["ready"], T["end"] // 1000)
    shots = show.shot("01-world", wait=("canvas[data-radio='4']:not([hidden])",))
    return (f"{RADIOS} radios up (IDs {IDS[1]}..{IDS[RADIOS]}, TG 9), console sees {RADIOS} screens", shots)


def step_call(show):
    show.run_until(T["call_read"])
    sources = {r: show.view(r, DMR_CALL).get("call.src") for r in (2, 3, 4)}
    states = {r: show.view(r, DMR).get("dmr.state") for r in (2, 3, 4)}
    heard = sum(1 for e in show.events() if e.get("event") == "air_delivery" and e.get("sender") == 1)
    require(all(s == str(IDS[1]) for s in sources.values()) and all(s == "rx" for s in states.values()),
            f"call.src {sources}, dmr.state {states}")
    shots = show.shot("02-call", wait=(".lane .b.heard",))
    return f"radios 2, 3, 4 show call from {IDS[1]} (dmr.state=rx), {heard} bursts delivered", shots


def step_collision(show):
    show.run_until(T["collide_read"])
    collisions = [e for e in show.events() if e.get("event") == "air_decision" and e.get("rule") == "collision"]
    hit = sorted({t for e in collisions for t in e.get("receivers", ())})
    require(collisions and {1, 4} <= set(hit), f"{len(collisions)} collision decisions, receivers {hit}")
    shots = show.shot("03-collision", wait=(".lane .b.heard",))
    return f"{len(collisions)} collision decisions: radios {', '.join(map(str, hit))} heard 2 and 3 at once", shots


def step_loss(show):
    """A second short world (LOSS_T): radio 1 talks while the console opens the window at radio 4."""
    other = Showcase(show.runtime, show.out, show.env, f"{show.name}-loss")
    other.browser = show.browser
    try:
        return loss_window(other)
    finally:
        other.close()


def loss_window(show):
    open_world(show, LOSS_KEYS, LOSS_T["ready"], LOSS_T["end"] // 1000)
    show.run_until(LOSS_T["start"])
    show.events()
    unlocked = show.console_control("/api/unlock", {"code": show.code})
    require(unlocked.get("control") == "unlocked", f"unlock: {unlocked}")
    show.start_run(ratio=1, until_us=LOSS_T["read"])
    reply = show.console_control("/api/control", {"action": "fault", "receivers": [LOSS_RECEIVER],
                                                  "rate_pct": 100, "length_ms": LOSS_MS})
    require(reply.get("ok") and reply.get("window"), f"console fault: {reply}")
    start, end = reply["window"]
    show.wait_paused(LOSS_T["read"])
    events = show.events()

    def inside(e):
        return start <= e.get("time_us", -1) < end

    to = {r: sum(1 for e in events if e.get("event") == "air_delivery" and e.get("sender") == 1
                 and e.get("receiver") == r and inside(e)) for r in (2, LOSS_RECEIVER)}
    lost = sum(1 for e in events if e.get("event") == "air_decision" and e.get("rule") == "loss"
               and LOSS_RECEIVER in e.get("receivers", ()) and inside(e))
    audit = [json.loads(line) for line in (show.folder / "audit.jsonl").read_text().splitlines()]
    by_console = [a for a in audit if a.get("kind") == "world_control" and a.get("command") == "fault"
                  and str(a.get("principal", "")).startswith("console:")]
    require(to[2] > 0 and to[LOSS_RECEIVER] == 0 and lost > 0 and by_console,
            f"window {start}..{end} us: to radio 2 {to[2]}, to radio 4 {to[LOSS_RECEIVER]}, lost {lost}, "
            f"console audit lines {len(by_console)}")
    shots = show.shot("04-loss", wait=(f"[aria-label='Radio {LOSS_RECEIVER} air lane'] .win.fault",))
    show.start_run(ratio="max")
    code = show.world.wait(60)
    require(code == 0, f"the loss world ended with exit {code}")
    return (f"second world, console window {start / 1e6:.2f}..{end / 1e6:.2f} s: radio 2 heard {to[2]} bursts, "
            f"radio 4 lost {lost}, heard 0", shots)


def step_sms(show):
    show.run_until(T["sms_send"] - 500_000)        # after the collision: the channel is free again
    show.start_run(ratio=1, until_us=T["sms_send"])
    held = show.call("lease.status")
    if str(SMS["radio"]) in held:
        show.call("lease.revoke", radio=SMS["radio"])
    lease = show.call("lease.grant", radio=SMS["radio"], principal="debug:demo")
    show.controller = show.session(SMS["radio"], "debug:demo", "controller", generation=lease["generation"],
                                   secret=lease["secret"])
    payload = bytes((OP_SMS,)) + SMS["dst"].to_bytes(4, "little") + bytes((SMS["flags"],)) + SMS["text"].encode()
    reply = show.exchange(show.controller, dlmux.frame(DMR_DATA, 0x31, payload))
    require(reply[0] == "frame" and reply[1] == 0x80 | DMR_DATA and reply[3][:1] == b"\x00",
            f"SMS request refused: {reply[:3]} {reply[3][:1].hex() if reply[0] == 'frame' else ''}")
    show.wait_paused(T["sms_send"])
    show.run_until(T["sms_read"])
    receiver, sender = show.view(3, DMR_DATA_STATS), show.view(SMS["radio"], DMR_DATA_STATS)
    require(receiver.get("data.sms_received") == "1" and sender.get("data.sms_acked") == "1",
            f"radio 3 sms_received={receiver.get('data.sms_received')}, "
            f"radio 1 sms_acked={sender.get('data.sms_acked')}")
    shots = show.shot("05-sms", wait=("canvas[data-radio='3']:not([hidden])",))
    return f"'{SMS['text']}' from {IDS[1]}: radio 3 sms_received=1, radio 1 sms_acked=1", shots


def step_record(show):
    for sock in [*show.views.values(), show.controller]:
        if sock is not None:
            sock.close()
    show.views.clear()
    show.controller = None
    show.start_run(ratio="max")
    code = show.world.wait(120)
    show.client.close()
    show.client = None
    console = show.console.wait(20)
    require(code == 0 and any(line == "ended: virtual time limit" for line in show.world.seen),
            f"vradio exit {code}: {show.world.seen[-1] if show.world.seen else ''}")
    recording = json.loads((show.folder / "recording/scenario.json").read_text())
    traces = sorted((show.folder / "recording").glob("radio-*.trace"))
    end = recording["end"]
    require(end.get("complete") and end.get("reason") == "WORLD_END_TIME", f"recording end {end}")
    keep = show.out / "recording"
    shutil.rmtree(keep, ignore_errors=True)
    shutil.copytree(show.folder / "recording", keep)
    return (f"complete at {end['t_us'] / 1e6:g} s: {len(recording['events'])} inputs, {len(traces)} traces, "
            f"console exit {console}; copy in <out>/recording",
            ())


def step_travel(show):
    travel = Child([sys.executable, ROOT / "tools/conductor/replay.py", "replay", show.folder, "--stop-at-ms",
                    str(TRAVEL_MS), "--no-browser", "--runtime", show.runtime], show.out / "logs/replay.log", show.env)
    try:
        name = travel.expect(r"^serving until Ctrl-C or: .* down --name (\S+)$", 240).group(1)
        views = {}
        urls = {}
        for line in travel.seen:
            if match := re.search(r"radio-(\d+) view at ([\d.]+) ms: dmr\.state=(\S+) call\.src=(\S+)", line):
                views[int(match.group(1))] = (float(match.group(2)), match.group(3), match.group(4))
            if match := re.match(r"^radio (\d+): (http://\S+)", line):
                urls[int(match.group(1))] = match.group(2)
        shots, stepped = (), ""
        if show.browser is not None:
            # The radio's page is served by its firmware, so a paused world draws no screen. With every page open,
            # step the world STEP_US (still inside the call, as replay.py allows a world client to), then shoot.
            pages = {radio: show.browser.open(url) for radio, url in sorted(urls.items())}
            try:
                client = WorldClient(show.runtime / "rfoundry-vr" / name / "world.sock")
                try:
                    before = client.call("status")["time_us"]
                    client.call("step", until_us=before + TRAVEL_STEP_US)
                    deadline = time.monotonic() + 30
                    while (state := client.call("status"))["state"] != "Paused" or state["time_us"] <= before:
                        require(time.monotonic() < deadline, "the travel world did not finish its step")
                        time.sleep(0.05)
                finally:
                    client.close()
                stepped = f", stepped to {state['time_us'] / 1e6:.2f} s for the screens"
                paths = []
                for radio, (context, page) in pages.items():
                    page.wait_for_timeout(1000)
                    path = show.out / f"07-travel-radio-{radio}.png"
                    page.screenshot(path=str(path), full_page=True)
                    paths.append(path)
                shots = tuple(paths)
            finally:
                for context, _ in pages.values():
                    context.close()
        down = subprocess.run([sys.executable, ROOT / "tools/vradio.py", "down", "--name", name], cwd=ROOT,
                              env=show.env, capture_output=True, text=True, timeout=30)
        code = travel.wait(60)
        callers = {radio: v for radio, v in views.items() if radio != 1}
        require(down.returncode == 0 and code == 0 and len(urls) == RADIOS and callers
                and all(v[1] == "rx" and v[2] == str(IDS[1]) for v in callers.values()),
                f"down {down.returncode}, replay {code}, pages {len(urls)}, views {views}")
        at = min(v[0] for v in callers.values())
        return (f"replay same before {TRAVEL_MS} ms; at {at / 1000:.2f} s radio 2 shows call from "
                f"{callers[2][2]}; {len(urls)} radio pages opened{stepped}", shots)
    finally:
        travel.stop()


def check_step(show, script, extra, verdict, label):
    child = Child([sys.executable, CHECKS / script, *extra], show.out / f"logs/{label}.log", show.env)
    try:
        code = child.wait(STEP_TIMEOUT_S)
    finally:
        child.stop()
    result, evidence = verdict(code, child.seen)
    if result != "PASS":
        raise StepFailed(evidence)
    return evidence, ()


def build_firmware():
    """vradio's own build (make -q, then make only when stale), so every check uses the same fresh ELF."""
    from tools import vradio
    try:
        return vradio.build(False)
    except vradio.LauncherError as error:
        raise StepFailed(str(error)) from None


def plan(args):
    """(name, narration, function) for every step of this mode; function(show) returns (evidence, shots)."""
    p = PLANS[args.mode]
    steps = [
        ("launcher", f"Start {RADIOS} radios with the launcher (tools/vradio.py up"
                     + (", radio 1 on the fixed build with --build 1=PATH" if args.fixed.is_file() else "")
                     + ") and let it end at 3 s.",
         lambda show: step_launcher(show, args.fixed if args.fixed.is_file() else None)),
        ("world", f"Start the showcase world: {RADIOS} radios in group talk, with the console attached and world "
                  "control on.", step_world),
        ("group call", f"Radio 1 keys a group call on TG 9 for 1.5 s; the others should show a call from {IDS[1]}.",
         step_call),
        ("collision", "Radios 2 and 3 key at the same moment; the air should name a collision for radios 1 and 4.",
         step_collision),
        ("loss fault", "In a second short world, radio 1 talks while the console opens a 500 ms, 100 % loss "
                        "window at radio 4.", step_loss),
        ("sms", "Radio 1 sends the text 'DEMO 1' to radio 3 through the debug link, ACK requested.", step_sms),
        ("record", "The showcase world runs to its 14 s time limit; its folder becomes a complete recording.",
         step_record),
        ("time travel", f"Replay the recording to {TRAVEL_MS} ms (inside the call), check it, pause and open the "
                        "radios.", step_travel),
        ("interaction", f"interaction_check {' '.join(p['interaction'])}: two radios, a call and an SMS"
                   + (", then replay and privacy." if args.mode == "full" else "."),
         lambda show: check_step(show, "interaction_check.py", p["interaction"], interaction_verdict, "interaction")),
    ]
    for extra in p["parrot"]:
        part = extra[1]
        text = ("caller echo bug: the parrot echoes radio 1's call; on this build radio 1 should NOT play it (bug "
                "reproduces)." if part == "echo"
                else "caller echo bug: replay the failing parrot run; it must be the same.")
        steps.append((f"parrot {part}", text, lambda show, extra=extra, part=part: check_step(
            show, "parrot_check.py", extra, lambda c, l: parrot_verdict(part, c, l), f"parrot-{part}")))
    if args.fixed.is_file():
        steps.append(("parrot mixed", "caller echo bug with the fixed build on radio 1: it should play its echo, the "
                                      "unfixed radio 2 should not.",
                      lambda show: check_step(show, "parrot_check.py", ["--part", "mixed", "--fixed", args.fixed],
                                              lambda c, l: parrot_verdict("mixed", c, l), "parrot-mixed")))
    steps += [
        ("sms after reboot", "The SMS-after-reboot bug: A reboots (flash kept) and sends the same text again; B "
                             "should drop it as a duplicate (CONFIRMED).",
         lambda show: check_step(show, "sms_restart_check.py", ["--mode", "reboot", "--runs", p["sms_runs"]],
                                 sms_reboot_verdict, "sms-reboot")),
        ("scale", f"scale_probe {' '.join(p['scale'])}: how many radios one air carries here.",
         lambda show: check_step(show, "scale_probe.py", p["scale"], scale_verdict, "scale")),
    ]
    return steps


def main(argv=None):
    args = arguments(argv)
    if args.serve_world:
        return serve_world(args.serve_world, args.serve_until_ms)
    browser = None
    if args.mode == "full":
        try:
            import playwright  # noqa: F401
        except ImportError:
            print("demo: Playwright is missing; run uv run --with playwright python3 tools/demo.py, or use --fast.",
                  file=sys.stderr)
            return 2
    (args.out / "logs").mkdir(parents=True, exist_ok=True)
    for old in list(args.out.glob("*.png")) + list((args.out / "logs").glob("*.log")):
        old.unlink()
    # A short private runtime folder: Unix socket paths must stay under 108 bytes.
    runtime = Path(tempfile.mkdtemp(prefix="rfdemo-"))
    env = {**os.environ, "XDG_RUNTIME_DIR": str(runtime)}
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: signal.default_int_handler(signum, frame))
    steps, show = [], None
    executables = [ELF] + ([args.fixed] if args.fixed.is_file() else [])
    print(f"demo: {args.mode} mode; output in {args.out}", flush=True)
    try:
        started = time.monotonic()
        try:
            executable = build_firmware()
        except StepFailed as error:
            print(f"demo: {error}", file=sys.stderr)
            return 2
        print(f"demo: firmware {executable.relative_to(ROOT)} ready in {time.monotonic() - started:.1f} s", flush=True)
        if args.mode == "full":
            browser = Browser()
        show = Showcase(runtime, args.out, env)
        show.browser = browser
        live = True
        for index, (name, narration, run) in enumerate(plan(args), 1):
            print(f"\n[{index}] {narration}", flush=True)
            step = Step(name)
            started = time.monotonic()
            if name in LIVE and not live:
                step.result, step.evidence = "FAIL", "not run: an earlier world step failed"
            else:
                try:
                    step.evidence, step.shots = run(show)
                    step.result = "PASS"
                except StepFailed as error:
                    step.evidence = str(error)
                except (OSError, RuntimeError, ValueError, KeyError) as error:
                    step.evidence = f"{type(error).__name__}: {error}"
                if step.result != "PASS" and name in LIVE[:-1]:
                    live = False            # the world is broken: end it and skip the rest of the live steps
                    show.close()
            step.wall_s = time.monotonic() - started
            shots = f" (screenshot {', '.join(Path(s).name for s in step.shots)})" if step.shots else ""
            print(f"[{index}] {step.result}: {step.evidence}{shots}", flush=True)
            steps.append(step)
    except KeyboardInterrupt:
        print("\ndemo: interrupted; cleaning up", flush=True)
        steps.append(Step("interrupted", "FAIL", "stopped by a signal"))
    finally:
        try:
            if show is not None:
                show.close()
            if browser is not None:
                browser.close()
        finally:
            signal.signal(signal.SIGTERM, previous)
            shutil.rmtree(runtime, ignore_errors=True)
    print(f"\n[{len(steps) + 1}] Check that no radio process of this build is left.", flush=True)
    started = time.monotonic()
    left = radio_processes(executables)
    deadline = time.monotonic() + 10
    while left and time.monotonic() < deadline:     # a radio may take a moment to exit after its world ends
        time.sleep(0.2)
        left = radio_processes(executables)
    steps.append(Step("cleanup", "PASS" if not left else "FAIL",
                      f"radio processes of this build: {len(left)}" + (f" (pids {left})" if left else ""),
                      time.monotonic() - started))
    print(f"[{len(steps)}] {'PASS' if not left else 'FAIL'}: {steps[-1].evidence}")
    summary = table(steps)
    print(f"\n{summary}", flush=True)
    (args.out / "summary.txt").write_text(summary + "\n")
    return exit_code(steps)


if __name__ == "__main__":
    raise SystemExit(main())
