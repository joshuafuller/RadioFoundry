#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The repeat gate of the virtual radio (make -C tests/host vhost-repeat).

The same scenario (repeat_check.scenario) boots the virtual radio several times. The traces are compared byte for
byte. Exit 0 only when ALL of this holds, and any other result exits 1 (or 2 when the check could not start):

  repeatability  two plain runs exit 0 and their traces are equal
  CPU contention  one run with all its threads on one CPU, that two busy loops also keep busy at nice 0, exits 0, has the same
        trace, and was preempted at least once (its involuntary context switch count is above zero); eight runs at
        the same time each exit 0 (every exit code is collected) with the same trace
  input timing control  control 1 changes one input time. Its trace differs and radio.tx_off, present in both traces, moved
  storage control  control 2 changes what a key path stores. Its trace differs in a flash.write hash and in a display.flush
        (screen update) hash
  scan boundary  the key due at a scan time is seen by the scan: its key interrupt line, then the scan's "IN input" line at
        the same microsecond

It fails closed. A run that fails, times out, or writes no trace is a failure even when its trace matches. A trace
that cannot be read is a COMPARE ERROR, never "differs" and never "identical". A control passes only when both of its
traces were read and they really differ. A line that vanished is not a line that moved.

--self-test shows that the gate can fail: it runs the gate again with a fault put in by a wrapper around the radio,
and each fault must make the gate fail for that reason.
"""
import argparse
import hashlib
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RUN_TIMEOUT_S = 60
PARALLEL = 8
BURNERS_ONE_CPU = 2
LINE_RE = re.compile(r"^(\d{12}) (\d{10}) (.*)$")


class SetupError(Exception):
    pass


def read_scenario(path):
    """(base args as a list of lines, scan-key ms, {control name: (old line, new line)})"""
    base, scan_key, controls = [], None, {}
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as e:
        raise SetupError("cannot read scenario %s: %s" % (path, e))
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--"):
            base.append(" ".join(shlex.split(line)))
        elif line.startswith("@scan-key"):
            scan_key = int(line.split()[1])
        elif line.startswith("@control"):
            name, rest = line[1:].split(None, 1)
            if "=>" not in rest:
                raise SetupError("%s: no '=>' in %r" % (name, line))
            old, new = (" ".join(shlex.split(p)) for p in rest.split("=>", 1))
            controls[name] = (old, new)
        else:
            raise SetupError("scenario line not understood: %r" % line)
    if not base or scan_key is None or sorted(controls) != ["control1", "control2"]:
        raise SetupError("the scenario needs option lines, @scan-key, @control1 and @control2")
    return base, scan_key, controls


def args_of(lines):
    out = []
    for line in lines:
        out += shlex.split(line)
    return out


def edited(base, old, new):
    """The base lines with the one line OLD replaced by NEW. OLD must be there (a stale scenario is an error)."""
    if base.count(old) != 1:
        raise SetupError("control edit: %r is not exactly one line of the scenario" % old)
    return [new if line == old else line for line in base]


def scan_period_ms():
    path = os.path.join(ROOT, "core", "input", "input_engine.h")
    try:
        m = re.search(r"#define\s+INPUT_SCAN_MS\s+(\d+)", open(path, encoding="utf-8").read())
    except OSError as e:
        raise SetupError("cannot read %s: %s" % (path, e))
    if not m:
        raise SetupError("INPUT_SCAN_MS not found in %s" % path)
    return int(m.group(1))


def parse(data):
    """The trace lines as (us, text); lines that do not match the format keep us=None"""
    out = []
    for raw in data.decode("utf-8", "replace").split("\n"):
        if not raw:
            continue
        m = LINE_RE.match(raw)
        out.append((int(m.group(1)), m.group(3), raw) if m else (None, raw, raw))
    return out


class Gate:
    def __init__(self, elf, scenario, outdir, echo=True):
        self.elf = elf
        self.base, self.scan_key, self.controls = read_scenario(scenario)
        self.outdir = outdir
        self.echo = echo
        self.failures = []
        self.lines = []
        self.codes = {}		# run name -> exit code (None: timed out)
        self.nivcsw = {}	# run name -> involuntary context switches
        self.traces = {}	# run name -> bytes, only for traces that were read
        self.names = []

    # ---- output
    def say(self, text):
        self.lines.append(text)
        if self.echo:
            print(text, flush=True)

    def fail(self, text):
        self.failures.append(text)
        self.say("FAIL: " + text)

    # ---- running
    def path(self, name, ext="log"):
        return os.path.join(self.outdir, "%s.%s" % (name, ext))

    def start(self, name, lines):
        """Start one run. Returns the Popen. Its stderr goes to NAME.err, its trace to NAME.log"""
        self.names.append(name)
        cmd = [self.elf] + args_of(lines) + ["--trace", self.path(name)]
        err = open(self.path(name, "err"), "wb")
        try:
            return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
        except OSError as e:
            raise SetupError("cannot start %s: %s" % (self.elf, e))
        finally:
            err.close()

    def finish(self, name, proc):
        """Wait for a run (kill it after RUN_TIMEOUT_S). Records its exit code and involuntary context switches"""
        timed_out = []

        def kill():
            timed_out.append(1)
            try:
                proc.kill()
            except OSError:
                pass
        timer = threading.Timer(RUN_TIMEOUT_S, kill)
        timer.start()
        try:
            _, status, ru = os.wait4(proc.pid, 0)
        finally:
            timer.cancel()
        proc.returncode = 0	# reaped here: Popen must not wait again
        self.nivcsw[name] = ru.ru_nivcsw
        if timed_out:
            self.codes[name] = None
            self.fail("%s: TIMEOUT after %d s" % (name, RUN_TIMEOUT_S))
            return
        code = os.waitstatus_to_exitcode(status)
        self.codes[name] = code
        if code != 0:
            self.fail("%s: exit %d (a failed run, whatever its trace says)" % (name, code))

    def run(self, name, lines):
        self.finish(name, self.start(name, lines))

    # ---- the stages
    def burners(self, count, cpu):
        procs = []
        if cpu is not None:
            os.sched_setaffinity(0, {cpu})
        try:
            for _ in range(count):
                procs.append(subprocess.Popen([sys.executable, "-c", "while True: pass"],
                                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL))
        finally:
            pass
        return procs

    @staticmethod
    def stop_burners(procs):
        for p in procs:
            try:
                p.kill()
            except OSError:
                pass
        for p in procs:
            try:
                p.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def stage_plain(self):
        self.say("== plain runs, no load")
        for name in ("a", "b"):
            self.run(name, self.base)
            self.say("%s: exit %s" % (name, self.codes[name]))

    def stage_loaded(self):
        allowed = sorted(os.sched_getaffinity(0))
        cpu = allowed[0]
        self.say("== one run on CPU %d with %d busy loops on the same CPU at nice %d" %
                 (cpu, BURNERS_ONE_CPU, os.nice(0)))
        saved = os.sched_getaffinity(0)
        procs = []
        try:
            os.sched_setaffinity(0, {cpu})	# children inherit it, all their threads too
            procs = self.burners(BURNERS_ONE_CPU, None)
            time.sleep(0.5)			# the loops are running before the radio starts
            self.run("loaded", self.base)
        finally:
            self.stop_burners(procs)
            os.sched_setaffinity(0, saved)
        n = self.nivcsw.get("loaded", 0)
        self.say("loaded: exit %s, involuntary context switches %d" % (self.codes.get("loaded"), n))
        if n <= 0:
            self.fail("loaded: involuntary context switches %d: the load never preempted the run" % n)

    def stage_parallel(self):
        count = os.cpu_count() or 1
        self.say("== %d runs at the same time, %d busy loops on all CPUs" % (PARALLEL, count))
        procs = self.burners(count, None)
        try:
            time.sleep(0.5)
            started = [("par%d" % i, self.start("par%d" % i, self.base)) for i in range(1, PARALLEL + 1)]
            threads = [threading.Thread(target=self.finish, args=item) for item in started]
            for t in threads:
                t.start()
            for t in threads:
                t.join()	# every run is reaped and its exit code recorded, none is lost in a background shell
        finally:
            self.stop_burners(procs)
        for i in range(1, PARALLEL + 1):
            name = "par%d" % i
            if name not in self.codes:
                self.fail("%s: no exit code was collected" % name)
            self.say("%s: exit %s" % (name, self.codes.get(name)))

    def stage_controls(self):
        self.say("== control runs")
        for name, (old, new) in sorted(self.controls.items()):
            self.run(name, edited(self.base, old, new))
            self.say("%s: exit %s, change %s => %s" % (name, self.codes[name], old, new))

    # ---- reading and comparing
    def stage_read(self):
        self.say("== sha256 of every trace")
        for name in self.names:
            p = self.path(name)
            try:
                with open(p, "rb") as f:
                    data = f.read()
            except OSError as e:
                self.fail("COMPARE ERROR %s: cannot read the trace: %s" % (name, e))
                self.say("?  %s.log (unreadable)" % name)
                continue
            if not data:
                self.fail("%s: the trace is empty" % name)
                continue
            self.traces[name] = data
            self.say("%s  %s.log" % (hashlib.sha256(data).hexdigest(), name))

    def stage_identical(self):
        self.say("== must be identical to a")
        if "a" not in self.traces:
            self.fail("a: no readable trace, nothing to compare with")
            return
        for name in ("b", "loaded") + tuple("par%d" % i for i in range(1, PARALLEL + 1)):
            if name not in self.traces:
                self.say("%s: NOT COMPARED (no readable trace)" % name)
                continue
            if self.codes.get(name) != 0:
                self.say("%s: a failed run, its trace does not count" % name)
                continue
            if self.traces[name] == self.traces["a"]:
                self.say("%s: identical" % name)
            else:
                self.say("%s: DIFFERS" % name)
                self.fail("%s: DIFFERS from a: %s" % (name, self.first_difference("a", name)))

    def first_difference(self, x, y):
        lx, ly = self.traces[x].split(b"\n"), self.traces[y].split(b"\n")
        for i, (p, q) in enumerate(zip(lx, ly), 1):
            if p != q:
                return "line %d, %s: %s | %s: %s" % (i, x, p.decode("utf-8", "replace"), y,
                                                    q.decode("utf-8", "replace"))
        return "one trace is shorter (%d and %d lines)" % (len(lx), len(ly))

    def both(self, name):
        """True when a and NAME were both read and NAME's run exited 0: only then can a control say anything"""
        if "a" not in self.traces or name not in self.traces:
            self.say("%s: NOT COMPARED (no readable trace)" % name)
            return False
        if self.codes.get(name) != 0:
            self.say("%s: a failed run, its trace does not count" % name)
            return False
        return True

    def stage_scan_key(self):
        self.say("== the key due at a scan time is seen by that scan (scan boundary)")
        ms = self.scan_key
        period = scan_period_ms()
        keys = [a for a in args_of(self.base) if a.startswith("%d:" % ms)]
        if ms % period != 0:
            self.fail("scan key at %d ms is not a multiple of the scan period %d ms" % (ms, period))
            return
        if not any(line.startswith("--key %d:" % ms) for line in self.base) or not keys:
            self.fail("scan key at %d ms is not one of the scenario's --key lines" % ms)
            return
        if "a" not in self.traces:
            self.fail("a: no readable trace for the scan key check")
            return
        self.say("scan period %d ms, key due at %d ms" % (period, ms))
        self.check_scan_key(parse(self.traces["a"]), ms)

    def check_scan_key(self, lines, ms):
        t = ms * 1000
        # the first key interrupt at T, then a scan (the input task switching in) at the same microsecond after it
        key = next((i for i, (us, text, _) in enumerate(lines) if us == t and text == "ISR key"), None)
        if key is None:
            self.fail("no 'ISR key' line at %d us" % t)
            return
        scan = next((i for i in range(key + 1, len(lines)) if lines[i][0] == t and lines[i][1] == "IN input"), None)
        if scan is None:
            self.fail("the key interrupt at %d us is not followed by a scan ('IN input') at the same us" % t)
            return
        self.say("key  line %d: %s" % (key + 1, lines[key][2]))
        self.say("scan line %d: %s" % (scan + 1, lines[scan][2]))
        self.say("the scan at %d us comes after the key interrupt: seen" % t)

    @staticmethod
    def select(lines, prefix):
        return [raw for _, text, raw in lines if text.startswith(prefix)]

    def stage_control1(self):
        self.say("== control 1: one input time changed, radio.tx_off must move (input timing control)")
        if not self.both("control1"):
            self.fail("control1: not compared")
            return
        if self.traces["control1"] == self.traces["a"]:
            self.say("control1: IDENTICAL")
            self.fail("control1: IDENTICAL to a: the gate cannot see this change")
            return
        self.say("control1: differs")
        la = self.select(parse(self.traces["a"]), "BOARD radio.tx_off")
        lc = self.select(parse(self.traces["control1"]), "BOARD radio.tx_off")
        if not la or not lc:
            self.say("radio.tx_off: in a %d times, in control1 %d times" % (len(la), len(lc)))
            self.fail("control1: radio.tx_off is missing from %s (a line that vanished did not move)" %
                      ("a" if not la else "control1"))
            return
        if la == lc:
            self.fail("control1: radio.tx_off did not move")
            return
        self.say("radio.tx_off moved:")
        self.say("  a:        " + " | ".join(la))
        self.say("  control1: " + " | ".join(lc))

    def stage_control2(self):
        self.say("== control 2: the stored setting changed, a flash.write hash and a screen hash must change (storage control)")
        if not self.both("control2"):
            self.fail("control2: not compared")
            return
        if self.traces["control2"] == self.traces["a"]:
            self.say("control2: IDENTICAL")
            self.fail("control2: IDENTICAL to a: the gate cannot see this change")
            return
        self.say("control2: differs")
        pa, pc = parse(self.traces["a"]), parse(self.traces["control2"])
        for what, prefix in (("flash.write", "BOARD flash.write"), ("screen update (display.flush)",
                                                                  "BOARD display.flush")):
            xa, xc = self.select(pa, prefix), self.select(pc, prefix)
            if not xa or not xc:
                self.fail("control2: no %s line in %s" % (what, "a" if not xa else "control2"))
                continue
            only_a = [x for x in xa if x not in xc]
            only_c = [x for x in xc if x not in xa]
            if not only_a and not only_c:
                self.fail("control2: no %s hash changed" % what)
                continue
            self.say("%s hash differs:" % what)
            for x in only_a:
                self.say("  a:        " + x)
            for x in only_c:
                self.say("  control2: " + x)

    def execute(self):
        shutil.rmtree(self.outdir, ignore_errors=True)	# no stale trace of an earlier run can pass for a new one
        os.makedirs(self.outdir)
        self.stage_plain()
        self.stage_loaded()
        self.stage_parallel()
        self.stage_controls()
        self.stage_read()
        self.stage_identical()
        self.stage_scan_key()
        self.stage_control1()
        self.stage_control2()
        if self.failures:
            self.say("REPEAT CHECK FAILED (%d problem%s)" % (len(self.failures), "" if len(self.failures) == 1 else "s"))
            return 1
        self.say("REPEAT CHECK PASSED")
        return 0


# ---- the self-test: the gate must fail, each way, with a fault put in by a wrapper around the radio

WRAPPER = """#!/bin/sh
# Runs the real radio, then puts in the fault named by REPEAT_FAULT=kind:run (a run is named by its trace file)
"$REPEAT_REAL" "$@"
code=$?
t=; p=
for a in "$@"; do [ "$p" = --trace ] && t=$a; p=$a; done
n=$(basename "$t" .log)
kind=${REPEAT_FAULT%%:*}
run=${REPEAT_FAULT#*:}
[ "$n" = "$run" ] || exit $code
case $kind in
differ) echo "000099999999 0000099999 BOARD tampered" >> "$t" ;;
exit1) exit 1 ;;
unreadable) rm -f "$t"; mkdir "$t" ;;
empty) : > "$t" ;;
txoff_vanish) grep -v 'BOARD radio.tx_off' "$t" > "$t.new"; mv "$t.new" "$t" ;;
esac
exit $code
"""


def self_test(args):
    tmp = tempfile.mkdtemp(prefix="repeat-selftest-")
    wrapper = os.path.join(tmp, "radio-with-faults.sh")
    with open(wrapper, "w") as f:
        f.write(WRAPPER)
    os.chmod(wrapper, os.stat(wrapper).st_mode | stat.S_IXUSR)
    os.environ["REPEAT_REAL"] = args.elf
    base, scan_key, controls = read_scenario(args.scenario)
    # control 1 with its change removed: a scenario copy whose control1 changes nothing
    nochange = os.path.join(tmp, "control1-unchanged.scenario")
    with open(nochange, "w") as f:
        for line in open(args.scenario, encoding="utf-8").read().splitlines():
            if line.startswith("@control1"):
                old = controls["control1"][0]
                line = "@control1 %s => %s" % (old, old)
            f.write(line + "\n")
    cases = [
        # (what is shown, REPEAT_FAULT, scenario, text the gate's failure must contain)
        ("a forced trace difference in a parallel run", "differ:par3", args.scenario, "par3: DIFFERS from a"),
        ("a forced failed parallel run (exit 1, trace untouched)", "exit1:par5", args.scenario, "par5: exit 1"),
        ("an unreadable plain trace (compare error)", "unreadable:b", args.scenario, "COMPARE ERROR b:"),
        ("an unreadable control trace (compare error)", "unreadable:control1", args.scenario,
         "COMPARE ERROR control1:"),
        ("an empty trace", "empty:par2", args.scenario, "par2: the trace is empty"),
        ("control 1 with its change removed", "none:none", nochange, "control1: IDENTICAL to a"),
        ("radio.tx_off vanishing from control 1", "txoff_vanish:control1", args.scenario,
         "control1: radio.tx_off is missing"),
    ]
    bad = 0
    try:
        print("== self-test 0: no fault, the gate must pass")
        os.environ["REPEAT_FAULT"] = "none:none"
        g = Gate(wrapper, args.scenario, os.path.join(tmp, "out"), echo=False)
        rc = g.execute()
        if rc != 0:
            bad += 1
            print("SELF-TEST FAILED: the gate does not pass with no fault: " + "; ".join(g.failures))
        else:
            print("no fault: the gate exits 0, as it must")
        for i, (what, fault, scenario, marker) in enumerate(cases, 1):
            print("== self-test %d: %s" % (i, what))
            os.environ["REPEAT_FAULT"] = fault
            g = Gate(wrapper, scenario, os.path.join(tmp, "out"), echo=False)
            rc = g.execute()
            hits = [f for f in g.failures if marker in f]
            if rc != 0 and hits:
                print("the gate exits %d, as it must: %s" % (rc, hits[0]))
            else:
                bad += 1
                print("SELF-TEST FAILED: expected a non-zero exit with %r, got exit %d, failures: %s" %
                      (marker, rc, g.failures))
        # scan boundary must fail on a trace whose key is not at a scan: the check is not blind
        print("== self-test %d: the scan check on a key that is not at a scan time" % (len(cases) + 1))
        g = Gate(wrapper, args.scenario, os.path.join(tmp, "out"), echo=False)
        lines = parse(b"000003001000 0000003001 ISR key\n000003010000 0000003010 IN input\n")
        g.check_scan_key(lines, 3000)
        if g.failures:
            print("the check fails, as it must: %s" % g.failures[0])
        else:
            bad += 1
            print("SELF-TEST FAILED: the scan check passed a trace with no key at the scan time")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("SELF-TEST FAILED (%d)" % bad if bad else "SELF-TEST PASSED: the gate fails every way it must")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description="The repeat gate of the virtual radio")
    ap.add_argument("--elf", default=os.path.join(ROOT, "build", "vhost", "rfoundry_vhost.elf"))
    ap.add_argument("--scenario", default=os.path.join(HERE, "repeat_check.scenario"))
    ap.add_argument("--out", default=os.path.join("out", "vhost-repeat"), help="directory for the traces (emptied first)")
    ap.add_argument("--self-test", action="store_true", help="show that the gate fails each way it must")
    args = ap.parse_args()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, f: sys.exit(130))
    try:
        if not os.access(args.elf, os.X_OK):
            raise SetupError("the radio %s is not built or not executable" % args.elf)
        if args.self_test:
            return self_test(args)
        return Gate(args.elf, args.scenario, args.out).execute()
    except SetupError as e:
        print("repeat_check: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
