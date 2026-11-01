#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""browser control: does the radio's own front panel page drive the virtual radio in a real browser?

Build first: make BOARD=vhost -j4
Run: python3 tools/conductor/checks/browser_check.py            (exit 0 = every claim below held)
Self-test, no browser and no radio: python3 tools/conductor/checks/browser_check.py --self-test

What it does. It starts one virtual radio at ratio 1 with the web gateway on (webgw.get(world, enabled=True)).
Two headless Chromium sessions open the page that the firmware itself serves (core/net/webctl_assets/index.html),
each from its own one-time token:
  * the controller: the world grants its principal the radio's lease, so the page shows "Connected" and its keys work;
  * the observer: no lease, so the page shows "View only".
It prints:
  1. the page loaded: the title and the key count of the panel (the "keys" list in the hello frame);
  2. the frame id and CRC of the last FRAME checkpoint (frames.py) before and after a click on a page key. They differ,
     and the page's own screen image changed too;
  3. the observer's click, started right after one of the radio's own 1 s redraws (they repeat the same pixels).
     The page itself refuses it (its buttons are disabled when it has no lease), so the check also
     sends the same "d<bit>" frame by hand over the observer's own /ws, as a hostile page would. The gateway drops it:
     no new FRAME in 500 virtual ms, and an audit line (reason LEASE_NOT_HOLDER) names that session;
  4. one screenshot of each session, under the world folder, with the path.

Words. A FRAME checkpoint is the header the radio sends each time it redraws its screen: a frame id and a CRC of the
pixels. A new frame id with a new CRC means the picture changed. A principal is the name the gateway gives a browser
session, "browser:s<n>".

The page's own files. The gateway fetches /keys.json, /radio.svg and /mic.svg from the radio's own web
control, as it does the page, and serves the same bytes. This check asks the gateway for each one with a raw HTTP
request (no decoding) and compares its sha256 with the bytes the radio served. A file the radio answers 404 to must be
a 404 at the gateway. Then it looks at the page: the radio picture is in the page iff the radio served /radio.svg, and
each key the radio's key map names is clicked by its name or its drawing, not by a number. The vhost board has no key
enum header and no drawing yet (boards/vhost), so its firmware serves all three as 404 and the page falls back to plain
buttons labeled with key numbers ("0" is SELECT in boards/vhost/keypad_vhost.c:15). --require-assets turns "the radio
serves none" into a failure, for a radio build that has a picture.

Needs Playwright with Chromium. Missing: this script runs itself again under
  uv run --with playwright python ...
and Chromium comes from `uv run --with playwright python -m playwright install chromium`.

Exit 0: all claims held. Exit 1: a claim failed (the failures are printed). Exit 2: a missing prerequisite.
"""

import argparse
import hashlib
import http.client
import http.server
import base64
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import queue
import re
import shutil
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

SETTLE_US = 500_000                 # no new FRAME in this long after the observer's click
KEY_CANDIDATES = (0, 3, 2)          # SELECT, DOWN, UP: the first one that redraws the screen is used
KEY_HOLD_S = 0.25                   # wall time a key is held; at ratio 1 this is the same in virtual time
WALL_S = 120                        # the whole run


class CheckFailed(Exception):
    pass


T0 = time.monotonic()


def tlog(world, what):
    """Timing line (BROWSER_CHECK_TIMING=1): wall seconds since the start, and the world's virtual time."""
    if os.environ.get("BROWSER_CHECK_TIMING"):
        now = None if world is None else world.now
        print(f"timing wall={time.monotonic() - T0:7.3f}s virtual={now} {what}", flush=True)


# --- pure parts (the self-test covers them) ---

def last_frame(checkpoints, radio=1):
    """(frame_id, crc) of the newest checkpoint of this radio, or None."""
    for checkpoint in reversed(list(checkpoints)):
        if checkpoint.radio == radio:
            return checkpoint.frame_id, checkpoint.crc
    return None


def judge_assets(assets, dom, require):
    """Failed claims about the page's own files. assets: {path: {"radio": sha256 or None for a 404,
    "gateway": (status, sha256 or None)}}. dom: {"svgs": drawings in the page, "named": key buttons or shapes with
    a name, "numbered": key buttons labeled with a bare number}."""
    failures = []
    for path, item in sorted(assets.items()):
        radio, (status, got) = item["radio"], item["gateway"]
        if radio is None and status != 404:
            failures.append(f"ASSET_NOT_404 {path} gateway={status}")
        elif radio is not None and (status != 200 or got != radio):
            failures.append(f"ASSET_BYTES_DIFFER {path} gateway={status} radio={radio[:12]} got={(got or '')[:12]}")
    served = lambda path: assets.get(path, {}).get("radio") is not None
    if require and not (served("/radio.svg") and served("/keys.json")):
        failures.append("RADIO_SERVES_NO_PICTURE_OR_KEY_NAMES")
    if served("/radio.svg") and not dom.get("svgs"):
        failures.append("NO_RADIO_PICTURE_IN_PAGE")
    if served("/keys.json") and not dom.get("named"):
        failures.append("NO_NAMED_KEYS_IN_PAGE")
    if not served("/keys.json") and dom.get("named"):
        failures.append("NAMED_KEYS_WITHOUT_KEYS_JSON")
    return failures


def judge(page, before, after, picture, observer, audits, observer_principal, assets=None, dom=None, require=False):
    """The list of failed claims; empty means browser control holds.

    page: {"title", "keys"}. before and after: (frame_id, crc) around the controller's click. picture: the page's own
    screen image digest before and after. observer: new FRAME checkpoints seen within 500 virtual ms of the click.
    audits: audit lines. """
    failures = []
    if not page.get("title") or not page.get("keys"):
        failures.append(f"PAGE_NOT_LOADED title={page.get('title')!r} keys={page.get('keys')!r}")
    if before is None or after is None:
        failures.append("NO_FRAME_AROUND_CLICK")
    elif before[0] == after[0] or before[1] == after[1]:
        failures.append(f"FRAME_UNCHANGED before={before} after={after}")
    if picture[0] == picture[1]:
        failures.append("PAGE_IMAGE_UNCHANGED")
    if observer:
        failures.append(f"OBSERVER_CLICK_MADE_FRAMES {observer}")
    named = [line for line in audits if line.get("principal") == observer_principal
             and line.get("reason") == "LEASE_NOT_HOLDER" and line.get("result") == "refused"]
    if not named:
        failures.append(f"NO_AUDIT_LINE_FOR {observer_principal}")
    if assets is not None:
        failures += judge_assets(assets, dom or {}, require)
    return failures


def self_test():
    class Point:
        def __init__(self, radio, frame_id, crc):
            self.radio, self.frame_id, self.crc = radio, frame_id, crc

    assert last_frame([], 1) is None
    assert last_frame([Point(1, 4, 9), Point(2, 7, 7), Point(1, 5, 10)], 1) == (5, 10)
    page = {"title": "OpenDigitalRadio", "keys": 16}
    audit_ok = [{"principal": "browser:s2", "reason": "LEASE_NOT_HOLDER", "result": "refused", "command": "web.d"}]
    good = (page, (3, 111), (4, 222), ("a", "b"), [], audit_ok, "browser:s2")
    assert judge(*good) == []
    # Each claim has a case that must fail, so a check that always passes is caught.
    bad = {
        "no page": (({"title": "", "keys": 0},) + good[1:], "PAGE_NOT_LOADED"),
        "same frame": ((page, (3, 111), (3, 111)) + good[3:], "FRAME_UNCHANGED"),
        "same crc": ((page, (3, 111), (4, 111)) + good[3:], "FRAME_UNCHANGED"),
        "no frame": ((page, None, (4, 222)) + good[3:], "NO_FRAME_AROUND_CLICK"),
        "image same": (good[:3] + (("a", "a"),) + good[4:], "PAGE_IMAGE_UNCHANGED"),
        "observer moved": (good[:4] + ([(5, 333)],) + good[5:], "OBSERVER_CLICK_MADE_FRAMES"),
        "no audit": (good[:5] + ([],) + good[6:], "NO_AUDIT_LINE_FOR"),
        "audit of another": (good[:5] + ([dict(audit_ok[0], principal="browser:s1")],) + good[6:],
                             "NO_AUDIT_LINE_FOR"),
        "audit accepted": (good[:5] + ([dict(audit_ok[0], result="accepted")],) + good[6:], "NO_AUDIT_LINE_FOR"),
    }
    for name, (args, expect) in bad.items():
        got = judge(*args)
        assert any(item.startswith(expect) for item in got), (name, got)
    sha = lambda c: c * 64
    served = {"/keys.json": {"radio": sha("a"), "gateway": (200, sha("a"))},
              "/radio.svg": {"radio": sha("b"), "gateway": (200, sha("b"))},
              "/mic.svg": {"radio": None, "gateway": (404, None)}}
    none = {path: {"radio": None, "gateway": (404, None)} for path in served}
    drawn = {"svgs": 1, "named": 12, "numbered": 0}
    assert judge(*good, assets=served, dom=drawn) == []
    assert judge(*good, assets=none, dom={"svgs": 0, "named": 0, "numbered": 16}) == []
    more = {
        "bytes differ": ((served | {"/radio.svg": {"radio": sha("b"), "gateway": (200, sha("c"))}}, drawn, False),
                         "ASSET_BYTES_DIFFER"),
        "served but 404": ((served | {"/keys.json": {"radio": sha("a"), "gateway": (404, None)}}, drawn, False),
                           "ASSET_BYTES_DIFFER"),
        "404 became 200": ((served | {"/mic.svg": {"radio": None, "gateway": (200, sha("d"))}}, drawn, False),
                           "ASSET_NOT_404"),
        "no picture": ((served, {"svgs": 0, "named": 12}, False), "NO_RADIO_PICTURE_IN_PAGE"),
        "no names": ((served, {"svgs": 1, "named": 0}, False), "NO_NAMED_KEYS_IN_PAGE"),
        "required": ((none, {"svgs": 0, "named": 0}, True), "RADIO_SERVES_NO_PICTURE_OR_KEY_NAMES"),
    }
    for name, (args, expect) in more.items():
        got = judge(*good, assets=args[0], dom=args[1], require=args[2])
        assert any(item.startswith(expect) for item in got), (name, got)
    print("browser_check self-test: ok (3 pass cases, %d failing cases)" % (len(bad) + len(more)))
    return 0


# --- the live run ---

def need_playwright():
    """Playwright's sync API, or None when it is not installed."""
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        return None


class Bridge:
    """The browser thread asks the world thread to do what changes the world (tokens, lease)."""

    def __init__(self):
        self.jobs = queue.Queue()

    def call(self, fn, wall_s=30):
        done = {"event": threading.Event()}
        self.jobs.put((fn, done))
        if not done["event"].wait(wall_s):
            raise CheckFailed("WORLD_THREAD_DID_NOT_ANSWER")
        if "error" in done:
            raise CheckFailed(done["error"])
        return done["value"]

    def serve(self):
        while True:
            try:
                fn, done = self.jobs.get_nowait()
            except queue.Empty:
                return
            try:
                done["value"] = fn()
            except Exception as error:      # handed to the browser thread
                done["error"] = f"{type(error).__name__}: {error}"
            done["event"].set()


def wait_for(test, wall_s, what):
    end = time.monotonic() + wall_s
    while time.monotonic() < end:
        value = test()
        if value:
            return value
        time.sleep(0.02)
    raise CheckFailed(f"TIMEOUT {what}")


def image_digest(page):
    """Digest of the screen image the page shows: the page's `scr`, an img (src) when the radio has no picture, or an
    SVG image inside the radio's drawing (href)."""
    source = page.evaluate("() => scr ? scr.getAttribute(scr.tagName === 'IMG' ? 'src' : 'href') || '' : ''")
    return hashlib.sha256(source.encode()).hexdigest()[:12] if source else ""


def show_first_screen(page, world, grace_s=3, wall_s=30):
    """Wait until the page shows the radio's screen. Cold-start fix.

    The page's panel() builds the picture element `scr` only after it has fetched /keys.json, /radio.svg and /mic.svg
    (core/net/webctl_assets/index.html:160-170). The gateway sends the whole screen once, when the page says "s1",
    which the page does right after its hello. If that snapshot arrives before `scr` exists (a slow start: Chromium
    took seconds, the radio had run on), frame() draws it into the page's hidden canvas and the draw callback finds no
    `scr`. Nothing shows until the radio changes a row, and the vhost radio's 1 s redraws repeat the same pixels, so
    nothing comes. A real missed frame is not the cause: the gateway held all 128 rows.
    If the image is still missing after grace_s, this asks for the snapshot again ("s1", the page's own request), as
    the page does after a reconnect. Then `scr` exists and the picture shows. A screen that never comes still times out.
    """
    try:
        wait_for(lambda: image_digest(page), grace_s, "the page's screen image")
    except CheckFailed:
        tlog(world, "page has no screen image yet (snapshot came before the page made its picture): asking for s1 again")
        page.evaluate("() => ws.send('s1')")
        wait_for(lambda: image_digest(page), wall_s, "the page's screen image")


def key_locator(page, bit):
    """The shape or button that presses this key: its drawing's key-<name> shape when the radio has a picture, else the
    button the page made (labeled with the key's name, or with its number when the radio gave no names)."""
    name = page.evaluate("(b) => bits[b] || ''", bit)
    if name:
        shape = page.locator(f"#radio [id='key-{name}']")
        if shape.count():
            return shape.first
    label = page.evaluate("(n) => label(n)", name or str(bit))
    button = page.locator("#keys button", has_text=re.compile(f"^{re.escape(label)}$"))
    return button.first if button.count() else None


def asset_evidence(gateway, page, url):
    """What the radio served for each of the page's own files, and what the gateway gives this session (raw bytes,
    no decoding): sha256 of each, None for a 404."""
    cookie = "; ".join(f"{c['name']}={c['value']}" for c in page.context.cookies())
    where = urlsplit(url)
    link = gateway._link(1)
    out = {}
    for path in webgw_paths():
        held = link.assets.get(path)
        conn = http.client.HTTPConnection(where.hostname, where.port, timeout=10)
        proof = page.evaluate("sessionStorage.getItem('rf-browser-v1')")
        conn.request("GET", path, headers={"Cookie": cookie, "X-RadioFoundry-Session": proof})
        reply = conn.getresponse()
        body = reply.read()
        conn.close()
        out[path] = {"radio": None if held is None else hashlib.sha256(held[2]).hexdigest(),
                     "gateway": (reply.status, hashlib.sha256(body).hexdigest() if reply.status == 200 else None)}
    return out


def webgw_paths():
    from tools.conductor.parts import webgw
    return webgw.ASSET_PATHS


def cross_port_cookie(page, cookie_name):
    """An unrelated loopback service receives the cookie but no origin proof."""
    received = []

    class Collector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, dict(self.headers)))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!doctype html><title>Other local service</title>")

        def log_message(self, *args):
            pass

    with http.server.HTTPServer(("127.0.0.1", 0), Collector) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        other = page.context.new_page()
        try:
            other.goto(f"http://127.0.0.1:{server.server_port}/")
            assert other.evaluate("sessionStorage.getItem('rf-browser-v1')") is None
            target, headers = received[0]
            cookie = headers.get("Cookie", "")
            assert any(item.strip().startswith(cookie_name + "=") for item in cookie.split(";"))
            assert target == "/" and "Referer" not in headers and "X-RadioFoundry-Session" not in headers
            print("cross-port: cookie received=true; proof/token/referrer received=false")
            return cookie
        finally:
            other.close()
            server.shutdown()
            thread.join()


def reject_cookie_replay(page, gateway, bridge, url, bit):
    where = urlsplit(url)
    cookie = cross_port_cookie(page, f"rfvr{where.port}")
    before = bridge.call(lambda: (len(gateway.forwarded), sum(b.ws for s in gateway.servers.values()
                                                            for b in s.browsers.values())))
    for proof in (None, "x" * 43):
        conn = http.client.HTTPConnection(where.hostname, where.port, timeout=10)
        headers = {"Cookie": cookie, "Origin": f"http://{where.netloc}", "Upgrade": "websocket",
                   "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
                   "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode()}
        if proof is not None:
            headers["Sec-WebSocket-Protocol"] = "rf-vr-v1, rf-vr-proof." + proof
        conn.request("GET", "/ws", headers=headers)
        response = conn.getresponse()
        assert response.status == 403 and not response.read()
        conn.close()
    after = bridge.call(lambda: (len(gateway.forwarded), sum(b.ws for s in gateway.servers.values()
                                                           for b in s.browsers.values())))
    assert after == before
    assert page.evaluate("ws.readyState === WebSocket.OPEN && ctl")
    page.evaluate("bit => {ws.send('d' + bit); ws.send('u' + bit);}", bit)
    wait_for(lambda: len(gateway.forwarded) >= before[0] + 2, 10, "legitimate commands after replay refusal")
    assert [entry[2] for entry in gateway.forwarded[before[0]:]] == [f"d{bit}", f"u{bit}"]
    print("cross-port: replay status=403,403; new sessions=0; attacker commands=0; legitimate commands=d,u")


def browser_script(world, bridge, audits, ready, folder, report, sync_playwright, gateway, require):
    """Runs in its own thread. world.frames.checkpoints and world.now are only read here."""
    def virtual(us):
        target = world.now + us
        wait_for(lambda: world.now >= target, 30, f"{us} virtual us")

    def after_redraw():
        """Wait for the radio's next periodic redraw. The vhost radio redraws its status every 1 virtual second, with
        the same pixels (same CRC), so a 500 ms window that starts right after one holds no redraw of its own."""
        count = len(world.frames.checkpoints)
        wait_for(lambda: len(world.frames.checkpoints) > count, 5, "the radio's periodic redraw")

    def tokens():
        out = []
        for _ in range(2):
            out.append(world.api.dispatch({"version": 1, "command": "webgw.token", "radio": 1}))
        for item in out:
            if not item["ok"]:
                raise CheckFailed(f"TOKEN {item}")
        granted = world.api.dispatch({"version": 1, "command": "lease.grant", "radio": 1,
                                      "principal": out[0]["result"]["principal"]})
        if not granted["ok"]:
            raise CheckFailed(f"LEASE_GRANT {granted}")
        return [item["result"] for item in out]

    wait_for(ready.is_set, 90, "the gateway link to the radio")
    tlog(world, "gateway link up")
    first, second = bridge.call(tokens)
    with sync_playwright() as pw:
        tlog(world, "chromium launch start")
        time.sleep(float(os.environ.get("BROWSER_CHECK_LAUNCH_DELAY_S", "0")))   # simulates a slow cold Chromium
        browser = pw.chromium.launch(headless=True)
        tlog(world, "chromium launched")
        try:
            pages = []
            for item in (first, second):
                context = browser.new_context(viewport={"width": 1100, "height": 900})
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                page.goto(item["url"], wait_until="load")
                page.wait_for_function("() => typeof hi !== 'undefined' && !!hi && "
                                       "document.querySelectorAll('#keys button, #radio svg .k').length > 0",
                                       timeout=30000)
                tlog(world, f"page loaded {item['principal']}")
                pages.append((item["principal"], page, errors))
            (_, control, control_errors), (observer_principal, watcher, _) = pages
            hello = control.evaluate("() => ({control: ctl, keys: hi.keys, screen: hi.screen})")
            seen = watcher.evaluate("() => ({control: ctl, keys: hi.keys})")
            title = control.title()
            dom = control.evaluate("""() => ({svgs: document.querySelectorAll('#radio svg').length,
                named: hi.keys.filter(b => bits[b]).length,
                numbered: [...document.querySelectorAll('#keys button')].filter(b => /^\\d+$/.test(b.textContent)).length,
                buttons: document.querySelectorAll('#keys button').length, shapes: document.querySelectorAll('#radio svg .k').length})""")
            assets = asset_evidence(gateway, control, first["url"])
            for path, item in assets.items():
                print(f"asset {path}: radio {item['radio'] and item['radio'][:12]}; gateway {item['gateway'][0]} "
                      f"{item['gateway'][1] and item['gateway'][1][:12]}")
            print(f"page picture: drawings={dom['svgs']} named_keys={dom['named']} key_shapes={dom['shapes']} "
                  f"key_buttons={dom['buttons']} (numbered {dom['numbered']})")
            print(f"page loaded: title={title!r} keys={len(hello['keys'])} "
                  f"status={control.inner_text('#st')!r} (controller {first['principal']})")
            print(f"observer page: status={watcher.inner_text('#st')!r} keys={len(seen['keys'])} "
                  f"({observer_principal})")
            if not hello["control"] or seen["control"]:
                raise CheckFailed(f"CONTROL_FLAGS controller={hello['control']} observer={seen['control']}")
            # The first screen: wait until the radio has drawn and the page shows it, then the radio's next periodic redraw.
            tlog(world, f"frames so far {len(world.frames.checkpoints)}")
            wait_for(lambda: last_frame(world.frames.checkpoints), 30, "the first FRAME")
            tlog(world, "first FRAME")
            show_first_screen(control, world)
            tlog(world, "first page screen image")
            after_redraw()

            # --- the controller clicks a key ---
            before = after = None
            pic_before = pic_after = ""
            used = None
            for bit in KEY_CANDIDATES:
                button = key_locator(control, bit)
                if button is None:
                    continue
                before, pic_before = last_frame(world.frames.checkpoints), image_digest(control)
                seen_before = len(world.frames.checkpoints)
                box = button.bounding_box()
                control.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                control.mouse.down()
                time.sleep(KEY_HOLD_S)
                control.mouse.up()
                try:
                    wait_for(lambda: len(world.frames.checkpoints) > seen_before and last_frame(world.frames.checkpoints) != before, 8, "a new FRAME")
                except CheckFailed:
                    continue
                virtual(300_000)
                after = last_frame(world.frames.checkpoints)
                wait_for(lambda: image_digest(control) != pic_before, 10, "the page image to change")
                pic_after = image_digest(control)
                used = bit
                break
            if used is None:
                raise CheckFailed(f"NO_KEY_REDREW_THE_SCREEN tried={list(KEY_CANDIDATES)}")
            print(f"controller click on key {used}: FRAME before id={before[0]} crc={before[1]:#010x} "
                  f"after id={after[0]} crc={after[1]:#010x}; page image {pic_before} -> {pic_after}")
            virtual(1_000_000)
            after_redraw()

            # --- the observer clicks the same key ---
            frames_before = last_frame(world.frames.checkpoints)
            count = len(world.frames.checkpoints)
            audits_before = len(audits)
            start = world.now
            button = key_locator(watcher, used)
            button.click(force=True)                                  # the page refuses: its buttons are disabled
            watcher.evaluate("(bit) => { ws.send('d' + bit); ws.send('u' + bit); }", used)
            virtual(SETTLE_US)
            moved = [(c.frame_id, c.crc) for c in list(world.frames.checkpoints)[count:] if c.radio == 1]
            new_audits = audits[audits_before:]
            named = [line for line in new_audits if line.get("principal") == observer_principal
                     and line.get("reason") == "LEASE_NOT_HOLDER"]
            print(f"observer click on key {used}: last FRAME stayed id={frames_before[0]} crc={frames_before[1]:#010x}; "
                  f"{len(moved)} new FRAME in {world.now - start} virtual us")
            for line in named[:1]:
                print("audit: " + json.dumps({k: line[k] for k in ("kind", "principal", "result", "reason", "command")
                                              if k in line}, sort_keys=True))

            reject_cookie_replay(control, gateway, bridge, first["url"], used)

            # --- screenshots ---
            paths = {}
            for label, page in (("controller", control), ("observer", watcher)):
                path = folder / f"browser-{label}.png"
                page.screenshot(path=str(path), full_page=True)
                paths[label] = str(path)
                print(f"screenshot {label}: {path}")
            if control_errors:
                print(f"page errors (controller): {control_errors}")
            report.update(page={"title": title, "keys": len(hello["keys"])}, before=before, after=after,
                          picture=(pic_before, pic_after), observer=moved, audits=list(new_audits),
                          observer_principal=observer_principal, screenshots=paths, assets=assets, dom=dom)
        finally:
            browser.close()


def run(executable, out, require=False):
    from tools.conductor.conductor import install_parts
    from tools.conductor.parts import audit, webgw
    from tools.conductor.world import Config, World

    sync_playwright = need_playwright()
    folder = Path(out) if out else Path(tempfile.mkdtemp(prefix="browser-check-"))
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime = Path(tempfile.mkdtemp(prefix="browser-check-run-"))
    runtime.chmod(0o700)
    world = World(Config(executable=executable, end_us=300_000_000))
    bridge, ready, report, failure = Bridge(), threading.Event(), {}, []
    audits = []
    thread = None
    try:
        install_parts(world)
        gateway = webgw.get(world, enabled=True)       # before the first radio starts
        world.api.bind("browser-check", runtime)
        audit.get(world).add_sink(audits.append)
        world.start()
        world.run(1.0)                                   # ratio 1: virtual time follows the wall clock

        def work():
            try:
                browser_script(world, bridge, audits, ready, folder, report, sync_playwright, gateway, require)
            except BaseException as error:               # handed to the main thread
                failure.append(f"{type(error).__name__}: {error}")

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        end = time.monotonic() + WALL_S
        while thread.is_alive():
            if world.state == "Ended" or time.monotonic() > end:
                failure.append(f"WORLD_ENDED_EARLY reason={world.reason}" if world.state == "Ended" else "WALL_TIMEOUT")
                break
            world.api.poll(0.002)
            bridge.serve()
            link = gateway._link(1)
            if not ready.is_set() and link is not None and link.state == "up" and link.status is not None \
                    and len(link.screen.rows) > 0:
                ready.set()
            if world.state == "Running":
                world.advance()
        folder_world = world.api.directory
        if folder_world is not None and (folder_world / "audit.jsonl").exists():
            shutil.copy(folder_world / "audit.jsonl", folder / "audit.jsonl")
    finally:
        world.close()
        if thread is not None:
            thread.join(10)
        shutil.rmtree(runtime, ignore_errors=True)
    if failure:
        print("FAILED: " + failure[0])
        return 1
    problems = judge(report["page"], report["before"], report["after"], report["picture"], report["observer"],
                     report["audits"], report["observer_principal"], assets=report["assets"], dom=report["dom"],
                     require=require)
    for problem in problems:
        print("FAILED: " + problem)
    if problems:
        return 1
    print(f"browser control ok. world folder: {folder}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--executable", default=str(ROOT / "build/vhost/rfoundry_vhost.elf"))
    parser.add_argument("--require-assets", action="store_true",
                        help="fail when the radio serves no /radio.svg and /keys.json (a build with a picture)")
    parser.add_argument("--out", help="folder for screenshots and the audit copy (default: a new temp folder)")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not Path(args.executable).is_file():
        print(f"BROWSER_CHECK_MISSING_EXECUTABLE {args.executable}; run make BOARD=vhost -j4")
        return 2
    if need_playwright() is None:
        if os.environ.get("BROWSER_CHECK_UV") or shutil.which("uv") is None:
            print("BROWSER_CHECK_NO_PLAYWRIGHT: run with uv run --with playwright python <this file>")
            return 2
        os.environ["BROWSER_CHECK_UV"] = "1"
        os.execvp("uv", ["uv", "run", "--quiet", "--with", "playwright", "python", *sys.argv])
    return run(args.executable, args.out, args.require_assets)


if __name__ == "__main__":
    sys.exit(main())
