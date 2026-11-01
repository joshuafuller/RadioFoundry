#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Scale probe: how many virtual radios fit in one air on this PC.

Build first: make -j8 BOARD=vhost
Run: python3 tools/conductor/checks/scale_probe.py [--sizes 10,25,50,100] [--virtual-s 10]

What it does. For each size N it starts a world of N virtual radios, all in ONE air group (so every grant is at
most 1000 us, ADR 0005), and runs it for a fixed virtual time at ratio max. After the listed sizes it doubles N
(200, 400, ...) until a run fails, a run's virtual/wall ratio falls under --min-ratio, or --limit is reached.
It measures the conductor and the firmware load, not calls: the radios start with blank storage and send nothing.

What it records per size: startup time, run wall time, virtual/wall ratio, peak total RSS (conductor plus all
radio processes, from /proc/PID/status VmRSS; shared pages count once per process, so it over-counts), CPU seconds
(conductor and radios, from the kernel's per-process counters), peak open files (all processes), and the
failure reason if the run failed (ulimit -n, thread or process limit, memory, timeout, or a world error).

Determinism. Unless --no-determinism, the first size is run twice more at --determinism-size (default 10) and every
radio's trace sha256 must match across the two runs. The hash covers each trace line and its world time.

Isolation. Each run is a child process (--worker N) in its own session. The parent samples /proc, enforces a wall
timeout, and kills the whole session afterwards, so no rfoundry_vhost is left behind.
Exit code: 0 all runs fine and deterministic, 1 a run failed or hashes differ, 2 missing prerequisite or bad arguments.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

DEFAULT_SIZES = (10, 25, 50, 100)
HZ = os.sysconf("SC_CLK_TCK")
# Time allowed for starting N processes, on top of the run itself. Startup is not part of the ratio.
STARTUP_GRACE_S = 60.0
STARTUP_PER_RADIO_S = 0.5


def parse_sizes(text):
    try:
        sizes = [int(part) for part in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a list of whole numbers: {text!r}") from None
    if not sizes or any(n < 1 for n in sizes) or sizes != sorted(set(sizes)):
        raise argparse.ArgumentTypeError("sizes must be positive, increasing and without repeats")
    return sizes


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", type=parse_sizes, default=list(DEFAULT_SIZES),
                        help="comma list of radio counts to run first (default 10,25,50,100)")
    parser.add_argument("--virtual-s", type=float, default=10.0, help="virtual seconds per run (default 10)")
    parser.add_argument("--min-ratio", type=float, default=0.1,
                        help="stop growing when virtual/wall falls under this (default 0.1)")
    parser.add_argument("--max-wall-s", type=float, default=1500.0,
                        help="wall limit for one run of a listed size, which is always measured (default 1500)")
    parser.add_argument("--limit", type=int, default=3200, help="never grow past this many radios (default 3200)")
    parser.add_argument("--no-grow", action="store_true", help="run only --sizes, never double past them")
    parser.add_argument("--storage", default="blank", help="start storage for every radio (default blank)")
    parser.add_argument("--determinism-size", type=int, default=10)
    parser.add_argument("--no-determinism", action="store_true")
    parser.add_argument("--json", type=Path, help="also write the results to this file")
    parser.add_argument("--executable", default=str(ROOT / "build/vhost/rfoundry_vhost.elf"))
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.virtual_s <= 0 or args.min_ratio <= 0 or args.limit < 1 or args.determinism_size < 1:
        parser.error("--virtual-s, --min-ratio, --limit and --determinism-size must be positive")
    return args


# ---- worker: one world, one size ------------------------------------------------------------------------------

def worker(args):
    """Run one world and print one JSON line. Raises nothing the parent cannot read from the line."""
    from tools.conductor.channel import Kind
    from tools.conductor.conductor import install_parts
    from tools.conductor.world import Config, World, WorldError

    n = args.worker
    result = {"radios": n, "ok": False, "reason": None}
    hashes = {radio: hashlib.sha256() for radio in range(1, n + 1)}
    counts = dict.fromkeys(hashes, 0)
    world = None
    began = time.monotonic()
    try:
        config = Config(executable=args.executable, radios=n, end_us=int(args.virtual_s * 1_000_000))
        world = World(config)
        install_parts(world)
        radios = [{"radio": r, "storage": args.storage, "identity": r} for r in range(1, n + 1)]
        reply = world.api.dispatch({"version": 1, "command": "air_config", "seed": 0,
                                    "groups": [list(range(1, n + 1))], "radios": radios})
        if not reply["ok"]:
            raise WorldError(reply["error"])

        def trace(radio, message):
            hashes[radio].update(struct.pack("<Q", message.time_us) + message.payload + b"\n")
            counts[radio] += 1

        world.on_outbox(Kind.TRACE, trace)
        world.start()
        started = time.monotonic()
        result["start_s"] = started - began
        print("READY", flush=True)             # the parent starts the run clock sampling from here
        world.run("max")
        while world.state != "Ended":
            world.advance()
        result["run_s"] = time.monotonic() - started
        result["virtual_s"] = world.now / 1e6
        if world.reason != "WORLD_END_TIME":
            result["reason"] = f"world ended early: {world.reason}"
        else:
            result["ok"] = True
        result["trace_lines"] = sum(counts.values())
        result["sha256"] = {str(r): h.hexdigest() for r, h in hashes.items()}
    except Exception as error:          # a probe reports every failure; it must never hide one
        result["reason"] = classify(error)
        result["start_s"] = result.get("start_s", time.monotonic() - began)
    finally:
        if world is not None:
            try:
                world.close()
            except Exception as error:
                result["close_error"] = classify(error)
    print("RESULT " + json.dumps(result), flush=True)
    return 0


def classify(error):
    """Name the likely cause of a failure from the exception chain, in words the owner can act on."""
    chain, seen = [], error
    while seen is not None and len(chain) < 6:
        chain.append(seen)
        seen = seen.__cause__ or seen.__context__
    text = " | ".join(f"{type(e).__name__}: {e}" for e in chain)
    codes = {getattr(e, "errno", None) for e in chain}
    import errno
    if codes & {errno.EMFILE, errno.ENFILE}:
        cause = "open-file limit (ulimit -n)"
    elif errno.EAGAIN in codes and "fork" in text.lower() or "can't start new thread" in text:
        cause = "process or thread limit"
    elif codes & {errno.ENOMEM} or any(isinstance(e, MemoryError) for e in chain):
        cause = "memory"
    elif "timed out" in text.lower() or "VCHAN_TIMEOUT" in text:
        cause = "radio reply too slow (channel timeout)"
    else:
        cause = "other"
    return f"{cause}: {text}"[:400]


# ---- parent: sampling and orchestration -----------------------------------------------------------------------

def descendants(root_pid):
    """Pids of root_pid and everything under it, read from /proc (no ps, no psutil)."""
    parent_of = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as handle:
                stat = handle.read()
            parent_of[int(entry)] = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
    found, frontier = {root_pid}, [root_pid]
    while frontier:
        pid = frontier.pop()
        for child, parent in parent_of.items():
            if parent == pid and child not in found:
                found.add(child)
                frontier.append(child)
    return found


def read_process(pid):
    """(rss_kib, cpu_ticks, open_fds) for one pid, or None if it is gone."""
    try:
        rss = 0
        with open(f"/proc/{pid}/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
        with open(f"/proc/{pid}/stat") as handle:
            stat = handle.read()
        fields = stat[stat.rindex(")") + 2:].split()
        cpu = int(fields[11]) + int(fields[12])
        return rss, cpu, len(os.listdir(f"/proc/{pid}/fd"))
    except (OSError, ValueError, IndexError):
        return None


class Sampler(threading.Thread):
    """Poll /proc under a worker. Keeps the peak total RSS, peak total open files, and last-seen CPU per pid."""

    def __init__(self, root_pid, interval=0.5):
        super().__init__(daemon=True)
        self.root, self.interval = root_pid, interval
        self.peak_rss_kib = self.peak_fds = self.peak_procs = 0
        self.cpu_ticks = {}
        self.stop = threading.Event()

    def sample(self):
        rss = fds = procs = 0
        for pid in descendants(self.root):
            data = read_process(pid)
            if data is None:
                continue
            rss, fds, procs = rss + data[0], fds + data[2], procs + 1
            self.cpu_ticks[pid] = max(self.cpu_ticks.get(pid, 0), data[1])
        self.peak_rss_kib = max(self.peak_rss_kib, rss)
        self.peak_fds = max(self.peak_fds, fds)
        self.peak_procs = max(self.peak_procs, procs)

    def run(self):
        while not self.stop.wait(self.interval):
            self.sample()


def machine_busy():
    """(busy_jiffies, total_jiffies) for the whole machine, from the first line of /proc/stat."""
    with open("/proc/stat") as handle:
        values = [int(v) for v in handle.readline().split()[1:]]
    idle = values[3] + values[4]
    return sum(values[:8]) - idle, sum(values[:8])


def kill_session(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    process.wait()


def run_size(n, args, floor=True):
    """Run one worker and return its result dict plus measurements. Always leaves no process behind."""
    command = [sys.executable, "-I", str(Path(__file__).resolve()), "--worker", str(n),
               "--virtual-s", str(args.virtual_s), "--storage", args.storage, "--executable", args.executable]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, start_new_session=True,
                               cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)})
    sampler = Sampler(process.pid)
    sampler.start()
    load_before, busy_before = os.getloadavg()[0], machine_busy()
    deadline_start = time.monotonic() + STARTUP_GRACE_S + STARTUP_PER_RADIO_S * n
    # Grown sizes stop a little past the ratio floor. Listed sizes are always measured, up to --max-wall-s.
    deadline_run = (args.virtual_s / args.min_ratio * 1.25 + 30) if floor else args.max_wall_s
    lines, result, timeout_reason = [], None, None
    ready = threading.Event()

    def reader():
        for line in process.stdout:
            lines.append(line)
            if line.startswith("READY"):
                ready.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    began = time.monotonic()
    try:
        if not ready.wait(max(0.0, deadline_start - time.monotonic())) and process.poll() is None:
            timeout_reason = f"timeout: not all {n} radios started in {deadline_start - began:.0f} s"
        else:
            run_began = time.monotonic()
            try:
                process.wait(max(0.0, run_began + deadline_run - time.monotonic()))
            except subprocess.TimeoutExpired:
                timeout_reason = (f"timeout: run still going after {deadline_run:.0f} s wall for "
                                  f"{args.virtual_s:g} s virtual (ratio under {args.virtual_s / deadline_run:.3g})")
    finally:
        busy_after = machine_busy()
        sampler.sample()
        sampler.stop.set()
        kill_session(process)
        thread.join(2)
        sampler.join(2)
    for line in lines:
        if line.startswith("RESULT "):
            result = json.loads(line[7:])
    if result is None:
        result = {"radios": n, "ok": False,
                  "reason": timeout_reason or f"worker died without a result (exit {process.returncode})"}
    elif timeout_reason:
        result["ok"] = False
        result["reason"] = timeout_reason
    if result.get("ok") and result.get("run_s", 0) > 0:
        result["ratio"] = result["virtual_s"] / result["run_s"]
    result["wall_s"] = time.monotonic() - began
    result["peak_rss_mib"] = sampler.peak_rss_kib / 1024
    result["peak_open_files"] = sampler.peak_fds
    result["peak_processes"] = sampler.peak_procs
    result["cpu_s"] = sum(sampler.cpu_ticks.values()) / HZ
    # The conductor is the worker process. Its CPU against the radios' tells where the time goes.
    result["conductor_cpu_s"] = sampler.cpu_ticks.get(process.pid, 0) / HZ
    result["radios_cpu_s"] = result["cpu_s"] - result["conductor_cpu_s"]
    total = max(1, busy_after[1] - busy_before[1])
    result["load_1min_start"], result["load_1min_end"] = load_before, os.getloadavg()[0]
    # Busy CPUs of the whole machine during this run, ours included.
    result["busy_cpus"] = (busy_after[0] - busy_before[0]) / total * (os.cpu_count() or 1)
    return result


def leftovers(executable):
    """Count live processes running exactly this executable (other worktrees' radios are not ours)."""
    target, count = os.path.realpath(executable), 0
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                count += os.path.realpath(f"/proc/{entry}/exe") == target
            except OSError:
                pass
    return count


def table(results):
    head = f"{'N':>5} {'ok':>3} {'start s':>8} {'run s':>8} {'v/wall':>7} {'peak RSS MiB':>13} {'CPU s':>8} " \
           f"{'peak fds':>9} {'cond s':>7} {'radio s':>8} {'load':>5} {'busy':>5}  failure"
    rows = [head]
    for r in results:
        rows.append(f"{r['radios']:>5} {'yes' if r['ok'] else 'NO':>3} {r.get('start_s', float('nan')):>8.1f} "
                    f"{r.get('run_s', float('nan')):>8.1f} {r.get('ratio', float('nan')):>7.2f} "
                    f"{r['peak_rss_mib']:>13.0f} {r['cpu_s']:>8.1f} {r['peak_open_files']:>9} "
                    f"{r['conductor_cpu_s']:>7.1f} {r['radios_cpu_s']:>8.1f} {r['load_1min_start']:>5.1f} "
                    f"{r['busy_cpus']:>5.1f}  {r['reason'] or ''}")
    return "\n".join(rows)


def main(argv=None):
    args = parse_args(argv)
    if args.worker is not None:
        return worker(args)
    if not Path(args.executable).is_file():
        print(f"SCALE_MISSING_EXECUTABLE {args.executable}; run make -j8 BOARD=vhost", flush=True)
        return 2
    if leftovers(args.executable):
        print("SCALE_RADIOS_ALREADY_RUNNING: this executable is already running; stop it first", flush=True)
        return 2
    results, size_list, status = [], list(args.sizes), 0
    soft, hard = __import__("resource").getrlimit(__import__("resource").RLIMIT_NOFILE)
    print(f"cpus={os.cpu_count()} ulimit_n={soft}/{hard} virtual_s={args.virtual_s:g} storage={args.storage}", flush=True)
    index = 0
    while index < len(size_list):
        n = size_list[index]
        result = run_size(n, args, floor=index >= len(args.sizes))
        results.append(result)
        print(table([result]).splitlines()[-1], flush=True)
        index += 1
        if not result["ok"]:
            status = 1
            break
        if result["ratio"] < args.min_ratio and index >= len(args.sizes):
            print(f"ratio {result['ratio']:.3f} under {args.min_ratio:g}: ceiling reached at N={n}", flush=True)
            break
        if index == len(size_list) and not args.no_grow and result["ratio"] >= args.min_ratio and n * 2 <= args.limit:
            size_list.append(n * 2)
    checks = []
    if not args.no_determinism and results and results[0]["ok"]:
        n = args.determinism_size
        first = next((r for r in results if r["radios"] == n and r["ok"]), None) or run_size(n, args)
        second = run_size(n, args)
        same = bool(first.get("ok") and second.get("ok") and first.get("sha256") and first["sha256"] == second["sha256"]
                    and first.get("trace_lines"))
        checks.append({"size": n, "identical": same, "radios": n, "trace_lines": first.get("trace_lines"),
                       "sample_sha256": (first.get("sha256") or {}).get("1")})
        print(f"determinism N={n}: {'IDENTICAL' if same else 'DIFFERENT'} per-radio sha256 over {n} radios, "
              f"{first.get('trace_lines')} trace lines; radio 1 {(first.get('sha256') or {}).get('1')}", flush=True)
        if not same:
            status = 1
    left = leftovers(args.executable)
    print()
    print(table(results))
    print(f"leftover processes of this executable: {left}")
    if left:
        status = 1
    for r in results:
        r.pop("sha256", None)
    if args.json:
        args.json.write_text(json.dumps({"results": results, "determinism": checks}, indent=1))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
