#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Two radios, one PTT call, the read-only console attached, and a browser screenshot of it.

    uv run --with playwright python tools/conductor/checks/console_check.py --screenshot build/console.png

Steps. 1 Build the air_pair world (radios 1 and 2 on one air; radio 1 holds PTT from 3.0 s to 4.0 s). 2 Register
two assertions as the harness that owns the world (observe_api.add_assertion). 3 Start tools/console.py as its own
process, attached only through the world socket and frames.sock. 4 Run at full speed to 3.4 s and schedule a loss
fault from radio 1 to radio 2 for 3.45 to 3.55 s (the harness does this, not the console). 5 Run at 0.1x so the
call is on screen; a headless browser opens the console's one-time URL, waits for heard bursts, a fault window
and drawn screens, and saves the screenshot. 6 End the world; the console must exit by itself.
Exit 0 on success, 1 on a failed check, 2 when a prerequisite (firmware, Playwright) is missing.

With --control the console runs with world control and the page drives the world, not the harness:
the harness runs at full speed to 3.05 s and then only serves the world. The page pastes the control code from the
console's terminal, picks 0.25x while paused (resume carries it), resumes, pauses, steps 30 ms (virtual time must
move by exactly 30 000 us), resumes, and injects a 100 % loss window at radio 2 for 300 ms. The window must show on
radio 2's air lane with bursts not heard, and the world audit must hold each command as world_control from
console:s1.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import glob
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.conductor.parts import air, observe_api  # noqa: E402
from tools.conductor.world import Config  # noqa: E402

SCENARIO = ROOT / "tools/conductor/scenarios/air_pair.json"
HEARD, CLEAN = "radio 2 heard radio 1", "no collision on the air"


def chromium():
    found = sorted(glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
    return found[-1] if found else None


def shoot(url, path, executable):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as play:
        browser = play.chromium.launch(**({"executable_path": executable} if executable else {}))
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            page.goto(url)
            page.wait_for_selector(".lane .b.heard", timeout=20000)
            page.wait_for_selector(".lane .win.fault", timeout=20000)
            page.wait_for_selector("canvas[data-radio='2']:not([hidden])", timeout=20000)
            page.wait_for_timeout(800)
            page.screenshot(path=str(path), full_page=True)
            return page.evaluate("""() => ({
                sent: document.querySelectorAll('.lane .b.sent').length,
                heard: document.querySelectorAll('.lane .b.heard').length,
                faults: document.querySelectorAll('.lane .win.fault').length,
                stopped: document.querySelectorAll('.lane .b.stopped').length,
                screens: document.querySelectorAll('canvas:not([hidden])').length,
                disabled: [...document.querySelectorAll('.controls button')].every((b) => b.disabled),
                state: document.getElementById('state').textContent,
                verdict: document.getElementById('verdict').textContent})""")
        finally:
            browser.close()


def drive(url, code, path, executable):
    """The page controls the world: unlock, speed, resume, pause, step, resume, loss window. Returns page facts."""
    from playwright.sync_api import sync_playwright
    from tools.conductor.checks.browser_check import cross_port_cookie
    import http.client
    from urllib.parse import urlsplit
    state = "() => fetch('/api/state?after=999999999', {headers: {'X-RadioFoundry-Session': proof}}).then((r) => r.json())"
    with sync_playwright() as play:
        browser = play.chromium.launch(**({"executable_path": executable} if executable else {}))
        try:
            # bypass_csp lets the test poll with wait_for_function; the page itself keeps its CSP.
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, bypass_csp=True)
            page = context.new_page()
            page.goto(url)
            page.wait_for_selector("#unlock:not([hidden])", timeout=20000)
            where = urlsplit(url)
            cookie = cross_port_cookie(page, f"rfconsole{where.port}")
            before = page.evaluate(state)["status"]
            for method, route, body in (("GET", "/api/state", None),
                                        ("POST", "/api/unlock", {"code": code}),
                                        ("POST", "/api/control", {"action": "resume", "ratio": 1})):
                conn = http.client.HTTPConnection(where.hostname, where.port, timeout=10)
                conn.request(method, route, body=json.dumps(body) if body is not None else None,
                             headers={"Cookie": cookie, "Origin": f"http://{where.netloc}",
                                      "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"})
                response = conn.getresponse()
                assert response.status == 403
                response.read()
                conn.close()
            assert page.evaluate(state)["status"] == before
            print("console cross-port: state/unlock/control status=403,403,403; world unchanged=true")
            page.fill("#code", code)
            page.click("#unlock-button")
            page.wait_for_selector(".controls[data-mode='unlocked']", timeout=10000)
            page.wait_for_function("document.getElementById('state').textContent === 'Paused'", timeout=30000)
            page.select_option("#speed", "0.25")
            page.wait_for_function("document.getElementById('control-note').textContent.includes('used on resume')")
            page.click("#pause")                                    # it reads Resume while the world is paused
            page.wait_for_function("document.getElementById('state').textContent === 'Running'", timeout=10000)
            page.wait_for_selector(".lane .b.heard", timeout=20000)
            page.click("#pause")
            page.wait_for_function("document.getElementById('state').textContent === 'Paused'", timeout=10000)
            before = page.evaluate(state)["status"]["time_us"]
            page.click("#step")
            page.wait_for_function("document.getElementById('control-note').textContent.startsWith('step 30 ms')")
            deadline = time.monotonic() + 10
            while True:                 # the console's copy of the world status, as the page reads it
                status = page.evaluate(state)["status"]
                if status["state"] == "Paused" and status["time_us"] != before or time.monotonic() > deadline:
                    break
                page.wait_for_timeout(50)
            after = status["time_us"]
            page.click("#pause")
            page.wait_for_function("document.getElementById('state').textContent === 'Running'", timeout=10000)
            page.check("input[data-pick='2']")
            page.fill("#length", "300")
            page.click("#fault")
            page.wait_for_function("document.getElementById('control-note').textContent.startsWith('loss window')")
            note = page.text_content("#control-note")
            page.wait_for_selector("[aria-label='Radio 2 air lane'] .win.fault", timeout=20000)
            page.wait_for_selector("[aria-label='Radio 2 air lane'] .b.stopped", timeout=20000)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(path), full_page=True)
            return {"step_us": after - before, "before_us": before, "note": note,
                    **page.evaluate("""() => ({
                        heard: document.querySelectorAll('.lane .b.heard').length,
                        faults: document.querySelectorAll("[aria-label='Radio 2 air lane'] .win.fault").length,
                        stopped: document.querySelectorAll("[aria-label='Radio 2 air lane'] .b.stopped").length,
                        mode: document.getElementById('controls').dataset.mode,
                        state: document.getElementById('state').textContent})""")}
        finally:
            browser.close()


def control_demo(args):
    """The --control run: the page drives pause, step, resume, speed and a loss window."""
    with tempfile.TemporaryDirectory(prefix="rf-console-") as runtime, ThreadPoolExecutor(1) as pool:
        os.chmod(runtime, 0o700)
        world = air.scenario_world(SCENARIO)
        console = None
        try:
            world.api.bind("console-control", runtime)
            world.frames.bind()
            console = subprocess.Popen([sys.executable, str(ROOT / "tools/console.py"), "--world", "console-control",
                                        "--runtime", runtime, "--no-browser", "--control"], cwd=ROOT,
                                       stdout=subprocess.PIPE, text=True)
            url = console.stdout.readline().strip().split(" ", 1)[-1]
            words = console.stdout.readline().split()
            if not url.startswith("http://127.0.0.1:") or words[:3] != ["console:", "control", "code"]:
                print("console_check: FAIL control console did not start")
                return 1
            deadline = time.monotonic() + 10
            while len(world.frames.clients) != 1 or not world.api.live:
                if time.monotonic() > deadline:
                    print("console_check: FAIL console did not attach")
                    return 1
                world.api.poll(0.01)
            world.start()
            world.run("max", until=3_050_000)       # then the world waits, Paused, for the page
            shot = pool.submit(drive, url, words[3], args.screenshot, chromium())
            deadline = time.monotonic() + 180
            while world.state != "Ended" and time.monotonic() < deadline and not shot.done():
                world.api.poll(0)
                if world.state in ("Paused", "Ready"):
                    world.api.poll(0.005)           # nothing to advance; never resume on the page's behalf
                else:
                    world.advance()
            facts = shot.result(timeout=5) if shot.done() else None
            rows = [json.loads(line) for line in (world.api.directory / "audit.jsonl").read_text().splitlines()]
            done = [(row["command"], row["result"]) for row in rows
                    if row.get("kind") == "world_control" and row.get("principal") == "console:s1" and "command" in row]
            granted = any(row.get("kind") == "world_control" and row.get("result") == "granted" for row in rows)
            print(f"console_check: page {facts}")
            print(f"console_check: world_control audit console:s1 {done} unlock_granted={granted}")
            print("console_check: step lines " + str([(row["args"].get("until_us"), row["time_us"]) for row in rows
                                                       if row.get("command") == "step"]))
            # One fault: the window from radio 1 to radio 2 is one windowed fault (parts/faults.py until_us).
            want = [("run", "accepted"), ("pause", "accepted"), ("step", "accepted"), ("run", "accepted"),
                    ("fault", "accepted")]
            ok = (facts is not None and facts["step_us"] == 30000 and facts["faults"] > 0 and facts["stopped"] > 0
                  and facts["heard"] > 0 and facts["mode"] == "unlocked" and done == want and granted)
            print(f"console_check: {'PASS' if ok else 'FAIL'} control screenshot {args.screenshot}")
            return 0 if ok else 1
        finally:
            world.close()
            if console is not None:
                try:
                    console.wait(timeout=10)
                    print(f"console_check: console exited {console.returncode}: {console.stdout.read().strip()}")
                except subprocess.TimeoutExpired:
                    console.kill()
                    console.wait()
                    print("console_check: FAIL console did not exit after the world ended")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--screenshot", type=Path, required=True)
    parser.add_argument("--executable", default=Config().executable)
    parser.add_argument("--control", action="store_true", help="the page drives world control")
    args = parser.parse_args()
    if not Path(args.executable).is_file():
        print(f"console_check: INCOMPLETE: {args.executable} missing; run make -j8 BOARD=vhost")
        return 2
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("console_check: INCOMPLETE: Playwright missing; run with uv run --with playwright")
        return 2
    if args.control:
        return control_demo(args)
    with tempfile.TemporaryDirectory(prefix="rf-console-") as runtime, ThreadPoolExecutor(1) as pool:
        os.chmod(runtime, 0o700)
        world = air.scenario_world(SCENARIO)
        console = None
        try:
            observe = observe_api.get(world)
            observe.add_assertion(HEARD, "air_delivery from radio 1 to radio 2")
            observe.add_assertion(CLEAN, "checked at the end")
            notify = world.notify

            def judge(event):
                notify(event)
                if event.get("event") == "air_delivery" and event["sender"] == 1 and event["receiver"] == 2 \
                        and observe.assertions[HEARD]["passed"] is None:
                    observe.set_assertion(HEARD, True, f"at {event['input_us'] / 1e6:.3f} s")
                elif event.get("event") == "air_decision" and event["rule"] == "collision":
                    observe.set_assertion(CLEAN, False, f"burst {event['burst']} of radio {event['sender']}")

            world.notify = judge
            world.api.bind("console-check", runtime)
            world.frames.bind()
            console = subprocess.Popen([sys.executable, str(ROOT / "tools/console.py"), "--world", "console-check",
                                        "--runtime", runtime, "--no-browser"], cwd=ROOT, stdout=subprocess.PIPE,
                                       text=True)
            line = console.stdout.readline().strip()
            if not line.startswith("console: http://127.0.0.1:"):
                print(f"console_check: FAIL console did not start: {line!r}")
                return 1
            url = line.split(" ", 1)[1]
            deadline = time.monotonic() + 10
            while len(world.frames.clients) != 1 or not world.api.live:
                if time.monotonic() > deadline:
                    print("console_check: FAIL console did not attach")
                    return 1
                world.api.poll(0.01)
            world.start()
            world.run("max", until=3_400_000)
            # The harness (not the console) cuts radio 1 to radio 2 for 3.45 to 3.55 s, so a fault window shows.
            for at, rate in ((3_450_000, 1_000_000), (3_550_000, 0)):
                reply = world.api.dispatch({"version": 1, "command": "fault", "kind": "loss", "radio": 1,
                                            "target": 2, "rate_ppm": rate, "time_us": at})
                if not reply["ok"]:
                    print(f"console_check: FAIL fault refused: {reply['error']}")
                    return 1
            shot = None
            deadline = time.monotonic() + 120
            while world.state != "Ended" and time.monotonic() < deadline:
                world.api.poll(0)
                world.advance()
                if world.state == "Paused" and shot is None:
                    world.run(0.1)
                    shot = pool.submit(shoot, url, args.screenshot, chromium())
                if shot is not None and shot.done():
                    break
            facts = shot.result(timeout=5) if shot else None
            if observe.assertions[CLEAN]["passed"] is None:
                observe.set_assertion(CLEAN, True, f"none by {world.now / 1e6:.3f} s")
            print(f"console_check: page {facts}")
            ok = (facts and facts["sent"] > 0 and facts["heard"] > 0 and facts["screens"] == 2 and facts["disabled"]
                  and facts["faults"] > 0 and facts["stopped"] > 0)
            print(f"console_check: {'PASS' if ok else 'FAIL'} screenshot {args.screenshot}")
            return 0 if ok else 1
        finally:
            world.close()
            if console is not None:
                try:
                    console.wait(timeout=10)
                    print(f"console_check: console exited {console.returncode}: {console.stdout.read().strip()}")
                except subprocess.TimeoutExpired:
                    console.kill()
                    console.wait()
                    print("console_check: FAIL console did not exit after the world ended")


if __name__ == "__main__":
    raise SystemExit(main())
