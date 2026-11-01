// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// The console page. It reads copies from tools/console.py (GET /api/state, /api/frame/<radio>). The air timeline is
// built from observe.events: air_decision (one per rule, with its receivers) and air_delivery (one per receiver
// that heard a burst). World control is offered only when the console runs with --control: this page
// then pastes the one-time control code (POST /api/unlock) and sends POST /api/control. The console checks and
// audits each request and forwards it as a world API command; this page never talks to the world itself.
"use strict";

let proof;
async function bootstrap() {
  const key = "rf-browser-v1", fragment = location.hash;
  if (fragment.startsWith("#rf-token=")) {
    history.replaceState(null, "", "/");
    if (!/^#rf-token=[A-Za-z0-9_-]{43}$/.test(fragment)) throw Error("Invalid session");
    const response = await fetch("/session", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token: fragment.slice(10)})});
    if (!response.ok) throw Error("Invalid session");
    proof = (await response.json()).proof;
    sessionStorage.setItem(key, proof);
  } else proof = sessionStorage.getItem(key);
  if (!proof) throw Error("Invalid session");
}

const WINDOW_US = 2000000;          // the timeline shows the last 2 s of virtual time
const SLOT_US = 30000;              // one DMR burst (tools/conductor/airmodel.py SLOT_US)
const RECENT_US = 200000;           // a radio sent or heard within this long ago shows SENDING or HEARING
const POLL_MS = 150;
const HEADER = 72;                  // platform/host/vframe.h
const MODIFIERS = ["delay", "corrupt", "duplicate"];

const view = {
  cursor: 0, status: null, connected: false, ended: false, world: "", frames: {}, drawn: {},
  bursts: new Map(), events: [], policies: [], selected: null, follow: true, polledAt: 0, polledUs: 0,
};
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};
const seconds = (us) => (us / 1e6).toFixed(3) + " s";
const clock = (us) => {
  const ms = Math.floor(us / 1000), m = Math.floor(ms / 60000), s = Math.floor(ms / 1000) % 60;
  return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0") + "." + String(ms % 1000).padStart(3, "0");
};

function burstOf(sender, burst, t) {
  const key = sender + ":" + burst;
  let item = view.bursts.get(key);
  if (!item) {
    item = { key, sender, burst, t_us: t, results: new Map(), deliveries: new Map(), rules: [] };
    view.bursts.set(key, item);
  }
  if (t < item.t_us) item.t_us = t;
  return item;
}

function take(event) {
  switch (event.event) {
    case "air_decision": {
      const item = burstOf(event.sender, event.burst, event.t_us);
      item.rules.push(event);
      for (const receiver of event.receivers || []) {
        // "deliver" or a stopping rule decides; delay, corrupt and duplicate only add notes to a delivery.
        const entry = item.results.get(receiver) || { rule: null, reason: "", notes: [] };
        if (MODIFIERS.includes(event.rule)) entry.notes.push(event.rule);
        else Object.assign(entry, { rule: event.rule, reason: event.reason });
        item.results.set(receiver, entry);
      }
      break;
    }
    case "air_delivery": {
      const item = burstOf(event.sender, event.burst, event.air_start_us);
      if (!item.deliveries.has(event.receiver)) item.deliveries.set(event.receiver, event);
      break;
    }
    case "air_policy":
      view.policies.push(event);
      break;
    default:
      break;
  }
  view.events.push(event);
  if (view.events.length > 6000) view.events.splice(0, 1000);
}

function radios() {
  return (view.status && view.status.radios) || [];
}

function tuning(radio) {
  const air = (view.status && view.status.air) || {};
  return (air.radios || []).find((r) => r.radio === radio) || null;
}

function sharesAir(a, b) {
  const groups = ((view.status && view.status.air) || {}).groups || [];
  return groups.some((group) => group.includes(a) && group.includes(b));
}

function nowUs() {
  const status = view.status;
  if (!status) return 0;
  let now = status.time_us;
  // Between polls the line moves on by the ratio, never past one poll ahead.
  if (status.state === "Running" && typeof status.ratio === "number") {
    now += Math.min((performance.now() - view.polledAt) * 1000 * status.ratio, POLL_MS * 2000 * status.ratio);
  }
  return Math.max(now, view.polledUs);
}

function activity(radio, now) {
  let sending = false, hearing = false;
  for (const item of view.bursts.values()) {
    if (now - item.t_us > RECENT_US || item.t_us > now) continue;
    if (item.sender === radio) sending = true;
    if (item.deliveries.has(radio)) hearing = true;
  }
  return sending ? "sending" : hearing ? "hearing" : "";
}

// --- radio cards and their screens ---

function drawPanel(canvas, buffer) {
  const data = new DataView(buffer);
  const width = data.getUint16(32, true), height = data.getUint16(34, true), bpp = data.getUint8(36);
  const backlight = data.getUint8(37);
  const palette = [];
  for (let i = 0; i < 16; i++) palette.push(data.getUint16(40 + 2 * i, true));
  const panel = new Uint8Array(buffer, HEADER);
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  const image = ctx.createImageData(width, height);
  const px = image.data;
  const rgb565 = (v, o) => {
    px[o] = ((v >> 11) & 31) * 255 / 31;
    px[o + 1] = ((v >> 5) & 63) * 255 / 63;
    px[o + 2] = (v & 31) * 255 / 31;
    px[o + 3] = 255;
  };
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const o = (y * width + x) * 4;
      if (bpp === 16) {
        const at = (y * width + x) * 2;
        rgb565(panel[at] | (panel[at + 1] << 8), o);
      } else if (bpp === 4) {
        const byte = panel[y * ((width + 1) >> 1) + (x >> 1)];
        rgb565(palette[x & 1 ? byte & 15 : byte >> 4], o);
      } else {
        const on = panel[y * ((width + 7) >> 3) + (x >> 3)] & (0x80 >> (x & 7));
        const v = on ? 20 : 226;      // a set bit is a dark pixel (tools/rfoundry_debug.py to_rgb)
        px[o] = px[o + 1] = v;
        px[o + 2] = on ? 24 : 214;
        px[o + 3] = 255;
      }
    }
  }
  ctx.putImageData(image, 0, 0);
  canvas.style.filter = backlight ? "none" : "brightness(0.45)";
}

async function refreshFrames() {
  for (const [radio, info] of Object.entries(view.frames)) {
    if (view.drawn[radio] === info.frame_id + ":" + info.boot) continue;
    const reply = await fetch("/api/frame/" + radio, { headers: { "X-RadioFoundry-Session": proof }, cache: "no-store" });
    if (!reply.ok) continue;
    const canvas = document.querySelector('canvas[data-radio="' + radio + '"]');
    if (!canvas) continue;
    drawPanel(canvas, await reply.arrayBuffer());
    canvas.hidden = false;
    const none = canvas.parentElement.querySelector(".none");
    if (none) none.remove();
    view.drawn[radio] = info.frame_id + ":" + info.boot;
  }
}

function renderRadios(now) {
  const host = $("radios");
  for (const r of radios()) {
    let card = host.querySelector('[data-card="' + r.radio + '"]');
    if (!card) {
      card = el("article", "radio");
      card.dataset.card = r.radio;
      const head = el("div", "radio-head");
      head.append(el("span", "radio-name", "Radio " + r.radio), el("span", "tag"));
      const screen = el("div", "screen");
      const canvas = el("canvas");
      canvas.dataset.radio = r.radio;
      canvas.hidden = true;
      canvas.setAttribute("aria-label", "Radio " + r.radio + " screen");
      screen.append(canvas, el("span", "none", "waiting for a frame"));
      card.append(head, screen, el("div", "radio-foot"));
      host.append(card);
    }
    const state = !r.on ? "off" : activity(r.radio, now);
    card.className = "radio " + state;
    const tag = card.querySelector(".tag");
    tag.className = "tag " + state;
    tag.textContent = { off: "OFF", sending: "SENDING", hearing: "HEARING", "": "IDLE" }[state];
    const t = tuning(r.radio);
    const frame = view.frames[r.radio];
    const parts = [];
    if (t) parts.push(String(t.identity), "ch " + t.channel, "CC " + (t.cc === null ? "any" : t.cc), "TS " + t.ts);
    parts.push("boot " + r.boot);
    if (frame) parts.push("frame " + frame.frame_id + " at " + seconds(frame.time_us));
    card.querySelector(".radio-foot").textContent = parts.join(" · ");
  }
}

// --- the air timeline ---

function faultWindows(receiver, from, to) {
  // A window is a span where the air policy stops or thins this receiver's traffic: loss, a down link, air off.
  const affected = (policy) => policy && (!policy.link_up ||
    (policy.loss_ppm || []).some((rule) => rule[1] === receiver && rule[2] > 0) ||
    (policy.down_links || []).some((link) => link[1] === receiver));
  const windows = [];
  let start = null;
  for (const event of view.policies) {
    const on = affected(event.policy);
    if (on && start === null) start = event.time_us;
    if (!on && start !== null) { windows.push([start, event.time_us]); start = null; }
  }
  if (start !== null) windows.push([start, to]);
  return windows.filter(([a, b]) => b > from && a < to);
}

function renderAxis(from) {
  const axis = $("axis");
  axis.textContent = "";
  const step = 250000;
  for (let t = Math.ceil(from / step) * step; t <= from + WINDOW_US; t += step) {
    const mark = el("span", "", (t / 1e6).toFixed(2) + (axis.childElementCount ? "" : " s"));
    mark.style.left = ((t - from) / WINDOW_US * 100) + "%";
    axis.append(mark);
  }
}

function place(node, from, start, length) {
  node.style.left = ((start - from) / WINDOW_US * 100) + "%";
  node.style.width = (length / WINDOW_US * 100) + "%";
}

function renderLanes(now) {
  const from = Math.max(0, now - WINDOW_US);
  renderAxis(from);
  const host = $("lanes");
  host.textContent = "";
  const visible = [...view.bursts.values()].filter((b) => b.t_us + SLOT_US > from && b.t_us <= now);
  for (const r of radios()) {
    const row = el("div", "lane-grid");
    const lane = el("div", "lane");
    lane.setAttribute("aria-label", "Radio " + r.radio + " air lane");
    for (const [a, b] of faultWindows(r.radio, from, now)) {
      const win = el("div", "win fault");
      place(win, from, Math.max(a, from), Math.min(b, now) - Math.max(a, from));
      win.title = "fault window from " + seconds(a);
      lane.append(win);
    }
    for (const item of visible) {
      let kind = null;
      let label = "";
      if (item.sender === r.radio) {
        kind = "sent";
        label = "sent burst " + item.burst;
      } else if (item.deliveries.has(r.radio)) {
        kind = "heard";
        label = "heard burst " + item.burst + " from radio " + item.sender;
      } else {
        const result = item.results.get(r.radio);
        if (result && result.rule === "collision") kind = "collision";
        else if (result && result.rule && !["half_duplex", "deliver"].includes(result.rule)) kind = "stopped";
        if (result) label = result.rule + ": burst " + item.burst + " from radio " + item.sender;
      }
      if (!kind) continue;
      const bar = el("button", "b " + kind + (view.selected === item.key ? " selected" : ""));
      place(bar, from, item.t_us, SLOT_US);
      bar.title = label + " at " + seconds(item.t_us);
      bar.setAttribute("aria-label", bar.title);
      bar.addEventListener("click", () => { view.selected = item.key; view.follow = false; render(); });
      lane.append(bar);
    }
    for (const fault of (view.status && view.status.faults) || []) {
      if (fault.radio !== r.radio && fault.target !== r.radio) continue;
      if (fault.time_us < from || fault.time_us > now + WINDOW_US) continue;
      const mark = el("div", "mark");
      mark.style.left = ((fault.time_us - from) / WINDOW_US * 100) + "%";
      mark.title = "scheduled " + fault.kind + " at " + seconds(fault.time_us);
      lane.append(mark);
    }
    const line = el("div", "now");
    line.style.left = "calc(" + ((now - from) / WINDOW_US * 100) + "% - 1px)";
    lane.append(line);
    row.append(el("span", "lane-name", "Radio " + r.radio), lane);
    host.append(row);
  }
}

function describe(event) {
  const at = seconds(event.time_us || 0) + " ";
  switch (event.event) {
    case "input":
      if (event.kind === "KEY") return [at + (event.key || event.bit).toString().toUpperCase() + " " +
        (event.down ? "down" : "up") + ", radio " + event.radio, ""];
      if (event.kind === "ENC") return [at + "encoder " + event.detents + ", radio " + event.radio, ""];
      return null;
    case "air_decision":
      if (["deliver", "half_duplex"].includes(event.rule)) return null;
      return [at + event.rule.replace("_", " ") + " at radio " + (event.receivers || []).join(", ") +
        " (burst " + event.burst + " of radio " + event.sender + ")", "bad"];
    case "air_policy":
      return [at + "air policy: " + event.command.replace("air_", ""), ""];
    case "world_fault":
      return [at + "fault " + event.kind + ", radio " + event.radio +
        (event.target ? " to " + event.target : "") + (event.rate_ppm !== undefined ? " " + event.rate_ppm / 1e4 + "%" : ""), "bad"];
    case "radio_started":
      return [at + "radio " + event.radio + " started, boot " + event.boot, ""];
    case "radio_off":
      return [at + "radio " + event.radio + " off", "bad"];
    case "state":
      return [at + "world " + event.state + (event.reason ? " (" + event.reason + ")" : ""), ""];
    case "assertion":
      if (event.passed === null) return null;
      return [at + (event.passed ? "pass: " : "fail: ") + event.name, event.passed ? "good" : "bad"];
    case "audit":
      if (event.kind === "world_control" && event.result !== "refused") {
        return [at + event.principal + " " + (event.command || event.action) + " " + event.result, "good"];
      }
      return event.result === "refused" ? [at + "refused " + event.kind +
        (event.principal ? " from " + event.principal : "") + ": " + event.reason, "bad"] : null;
    default:
      return null;
  }
}

function renderEvents() {
  const host = $("events");
  host.textContent = "";
  const now = nowUs(), shown = [];
  for (let i = view.events.length - 1; i >= 0 && shown.length < 10; i--) {
    const event = view.events[i];
    const text = describe(event);
    // An input queued for a later time (a scenario's PTT up) is shown as scheduled.
    if (text && event.event === "input" && event.time_us > now) text[0] += " (scheduled)";
    if (text) shown.push([event.time_us || 0, event.seq, text]);
  }
  shown.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (!shown.length) host.append(el("span", "ev", "no events yet"));
  for (const [, , [text, tone]] of shown) host.append(el("span", "ev " + tone, text));
}

// --- detail panel and top bar ---

function renderDetail() {
  let item = view.selected ? view.bursts.get(view.selected) : null;
  if (!item || view.follow) {
    let latest = null;
    for (const b of view.bursts.values()) if (!latest || b.t_us >= latest.t_us) latest = b;
    item = latest;
    if (item) view.selected = item.key;
  }
  const facts = $("burst-facts"), decisions = $("burst-decisions");
  facts.textContent = "";
  decisions.textContent = "";
  if (!item) {
    $("burst-title").textContent = "No burst yet";
    decisions.append(el("div", "muted small", "Bursts appear when a radio transmits on a shared air."));
  } else {
    const t = tuning(item.sender);
    $("burst-title").textContent = "Burst " + item.burst + " of radio " + item.sender +
      (t ? ", TS" + t.ts : "") + (view.follow ? " (latest)" : "");
    const delivered = [...item.deliveries.values()].map((d) => d.input_us);
    const add = (name, value) => facts.append(el("dt", "", name), el("dd", "", value));
    add("air start", (item.t_us / 1e6).toFixed(6) + " s");
    add("sender", "radio " + item.sender + (t ? " · " + t.identity : ""));
    if (t) add("tuning", "ch " + t.channel + ", CC " + (t.cc === null ? "any" : t.cc) + ", TS " + t.ts);
    add("delivered", delivered.length ? (Math.min(...delivered) / 1e6).toFixed(6) + " s" : "none");
    const receivers = new Set([...item.results.keys(), ...item.deliveries.keys()]);
    for (const r of radios()) if (r.radio !== item.sender) receivers.add(r.radio);
    for (const receiver of [...receivers].sort((a, b) => a - b)) {
      if (receiver === item.sender) continue;
      const result = item.results.get(receiver);
      const heard = item.deliveries.has(receiver);
      const text = heard ? "heard" + (result && result.notes.length ? " (" + result.notes.join(", ") + ")" : "")
        : result && result.rule ? result.rule.replace("_", " ") + (result.reason ? ": " + result.reason : "")
          : sharesAir(item.sender, receiver) ? "no decision recorded" : "not on this air";
      const row = el("div", "row");
      row.append(el("span", "", "Radio " + receiver), el("span", "v " + (heard ? "good" : result ? "bad" : "wait"), text));
      decisions.append(row);
    }
    if (!view.follow) {
      const back = el("button", "row", "Follow the latest burst");
      back.addEventListener("click", () => { view.follow = true; render(); });
      decisions.append(back);
    }
  }
  const host = $("assertions");
  host.textContent = "";
  const list = (view.status && view.status.assertions) || [];
  if (!list.length) host.append(el("div", "muted small", "No assertions. Scenario files cannot declare them yet."));
  for (const a of list) {
    const row = el("div", "row");
    const word = a.passed === null ? "waiting" : a.passed ? "PASS" : "FAIL";
    row.append(el("span", "", a.name), el("span", "v " + (a.passed === null ? "wait" : a.passed ? "good" : "bad"),
      word + (a.detail ? " · " + a.detail : "")));
    host.append(row);
  }
}

function renderTop(now) {
  const status = view.status || {};
  $("world").textContent = view.world || "world";
  $("state").textContent = view.status ? status.state : "connecting";
  $("clock").textContent = clock(now);
  $("ratio").textContent = status.ratio === undefined ? "-" : status.ratio === "max" ? "max" : status.ratio + "x";
  $("seed").textContent = status.air && status.air.seed !== null && status.air.seed !== undefined ? status.air.seed : "-";
  $("count").textContent = radios().length || "-";
  const list = status.assertions || [];
  const verdict = $("verdict");
  const passed = list.filter((a) => a.passed === true).length, failed = list.filter((a) => a.passed === false).length;
  verdict.className = "chip mono" + (failed ? " fail" : list.length && passed === list.length ? " pass" : "");
  verdict.textContent = !list.length ? "no assertions" : failed ? failed + " / " + list.length + " FAIL"
    : passed + " / " + list.length + " PASS";
  const link = $("link");
  link.className = "chip mono chip-link" + (view.connected ? "" : " gone");
  link.textContent = !view.connected ? (view.ended ? "world ended" : "disconnected")
    : control.mode === "unlocked" ? "world control" : "observer";
}

function render() {
  const now = nowUs();
  renderTop(now);
  renderControls();
  renderRadios(now);
  renderLanes(now);
  renderEvents();
  renderDetail();
}

// --- world control (only with tools/console.py --control and the pasted code) ---

const control = { mode: "off", session: "", busy: false, picked: new Set(), result: null };
const chosenRatio = () => ($("speed").value === "max" ? "max" : Number($("speed").value));

async function post(path, body) {
  const reply = await fetch(path, {
    method: "POST", cache: "no-store", body: JSON.stringify(body),
    headers: { "X-RadioFoundry-Session": proof, "Content-Type": "application/json" },
  });
  const text = await reply.text();
  if (!reply.ok) return { ok: false, error: text.trim() || "HTTP " + reply.status };
  try { return JSON.parse(text); } catch (error) { return { ok: false, error: "CONSOLE_BAD_REPLY" }; }
}

function showResult(label, reply) {
  control.result = reply.ok ? [label + ": accepted" +
    (reply.window ? " " + seconds(reply.window[0]) + " to " + seconds(reply.window[1]) : ""), "good"]
    : [label + ": refused, " + reply.error, "bad"];
  renderControls();
}

async function act(label, body) {
  if (control.busy) return;
  control.busy = true;
  renderControls();
  try {
    showResult(label, await post("/api/control", body));
  } catch (error) {
    showResult(label, { ok: false, error: "CONSOLE_UNREACHABLE" });
  } finally {
    control.busy = false;
    renderControls();
  }
}

function renderControls() {
  const box = $("controls");
  const state = view.status ? view.status.state : "";
  const on = control.mode === "unlocked" && view.connected && !control.busy;
  box.dataset.mode = control.mode;
  $("control-mode").textContent = control.mode === "unlocked" ? "World control: " + control.session
    : control.mode === "locked" ? "Controls: paste the control code" : "Controls: needs world permission";
  $("unlock").hidden = control.mode !== "locked";
  $("unlock-button").disabled = control.mode !== "locked" || control.busy;
  const pause = $("pause");
  pause.textContent = state === "Paused" ? "Resume" : "Pause";
  pause.disabled = !on || !["Running", "Paused"].includes(state);
  $("step").disabled = !on || state !== "Paused";
  $("speed").disabled = !on;
  for (const id of ["rate", "length"]) $(id).disabled = !on;
  $("fault").disabled = !on || state !== "Running" || !control.picked.size;
  const why = control.mode === "unlocked" ? "" : "needs world permission";
  for (const id of ["pause", "step", "fault"]) $(id).title = why;
  const host = $("receivers");
  for (const r of radios()) {
    if (host.querySelector('[data-pick="' + r.radio + '"]')) continue;
    const label = el("label", "pick");
    const box = el("input");
    box.type = "checkbox";
    box.dataset.pick = r.radio;
    box.addEventListener("change", () => {
      if (box.checked) control.picked.add(r.radio); else control.picked.delete(r.radio);
      renderControls();
    });
    label.append(box, el("span", "", "radio " + r.radio));
    host.append(label);
  }
  for (const box of host.querySelectorAll("input")) box.disabled = !on;
  const note = $("control-note");
  if (control.result) {
    note.className = "small push result " + control.result[1];
    note.textContent = control.result[0];
  } else {
    note.className = "muted small push";
    note.textContent = control.mode === "off" ? "This console only watches. Watching changes nothing in the run."
      : control.mode === "locked" ? "World control needs the one-time code printed on the console's terminal."
        : "Each control is checked, audited and sent as a world API command.";
  }
}

function setupControls() {
  $("unlock-button").addEventListener("click", async () => {
    const code = $("code").value.trim();
    $("code").value = "";
    control.busy = true;
    try {
      const reply = await post("/api/unlock", { code });
      if (reply.ok) control.mode = "unlocked";
      showResult("unlock", reply);
    } catch (error) {
      showResult("unlock", { ok: false, error: "CONSOLE_UNREACHABLE" });
    } finally {
      control.busy = false;
      renderControls();
    }
  });
  $("pause").addEventListener("click", () => {
    const paused = view.status && view.status.state === "Paused";
    act(paused ? "resume" : "pause", paused ? { action: "resume", ratio: chosenRatio() } : { action: "pause" });
  });
  $("step").addEventListener("click", () => act("step 30 ms", { action: "step" }));
  $("speed").addEventListener("change", () => {
    const value = $("speed").value;
    if (view.status && view.status.state === "Running") {
      act("speed " + value, { action: "speed", ratio: chosenRatio() });
    } else {
      // Not running: nothing is sent now. Resume carries this speed, so the change is audited there.
      control.result = ["speed " + value + ": used on resume", "good"];
      renderControls();
    }
  });
  $("fault").addEventListener("click", () => act("loss window", {
    action: "fault", receivers: [...control.picked].sort((a, b) => a - b),
    rate_pct: Math.round(Number($("rate").value)), length_ms: Math.round(Number($("length").value)),
  }));
}

async function poll() {
  try {
    const reply = await fetch("/api/state?after=" + view.cursor, { headers: { "X-RadioFoundry-Session": proof }, cache: "no-store" });
    if (reply.ok) {
      const data = await reply.json();
      view.world = data.world;
      view.connected = data.connected;
      view.ended = data.ended;
      if (data.status && data.status.state) view.status = data.status;
      for (const event of data.events) {
        if (event.seq > view.cursor) {
          take(event);
          view.cursor = event.seq;
        }
      }
      view.frames = data.frames;
      if (data.control) {
        const ratio = data.status && data.status.ratio;
        if (control.mode !== data.control.mode && data.control.mode !== "off" &&
          [...$("speed").options].some((o) => o.value === String(ratio))) $("speed").value = String(ratio);
        control.mode = data.control.mode;
        control.session = data.control.session;
      }
      const refusal = (data.refusals || []).slice(-1)[0];
      if (refusal && refusal.at !== control.refusalAt) {
        control.refusalAt = refusal.at;
        if (control.mode === "off") control.result = [refusal.action + ": refused, " + refusal.reason, "bad"];
      }
      view.polledAt = performance.now();
      view.polledUs = view.status ? view.status.time_us : 0;
      render();
      await refreshFrames();
      document.body.dataset.ready = "1";
    } else {
      view.connected = false;
      render();
    }
  } catch (error) {
    view.connected = false;
  }
  setTimeout(poll, POLL_MS);
}

function tick() {
  if (view.status && view.status.state === "Running") {
    const now = nowUs();
    $("clock").textContent = clock(now);
  }
  requestAnimationFrame(tick);
}

setupControls();
bootstrap().then(poll).catch(() => { view.connected = false; });
requestAnimationFrame(tick);
