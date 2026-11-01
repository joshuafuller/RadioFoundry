# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Lockstep: run tools/emu/hrc7000_emu.py's bridge as a world radio, stepped by the conductor's grants.

The bridge runs in a thread of the conductor's own process (hrc7000_emu.main(argv, control=Lockstep)). The world
talks to it through tools/conductor/parts/emu.py HrcChannel, which calls this object directly: no files and no sockets
between them.

Two things make a run repeat exactly (the same air decisions every run):

1. **Radio time never comes from the host clock.** The bridge runs with --scale 0, so the CPU's run time adds nothing
   to the virtual clock (hrc7000_soc.VirtualClock). Radio time moves only by what the firmware does:
   - a stubbed hal_delay_us(us) adds its own us (as before);
   - **the idle point**: when the FreeRTOS idle task calls hal_millis from vApplicationIdleHook (core/app/watchdog.c:82),
     every task is blocked. The clock then jumps to the next event that is due (a timer or modem interrupt, a burst's
     slot edge, or the grant end), and the interrupt is given right there, at the same instruction in every run;
   - a kick (the CPU made no stop for `wait` host seconds: a spin on a register, a `wait` instruction) is treated as
     an idle point too. Code that spins does not change state while it spins, so where the kick lands does not matter.
   So the firmware runs as if each burst of work took no time. Example: the tick is due at 5.000 s; the idle task
   calls hal_millis at 4.9993 s of radio time; the clock jumps to 5.000 s and vPortTickHandler runs.
2. **Inputs land at the paused machine time.** The clock never passes the grant end (Modem.stop_at, ModemSoc.limit).
   There the bridge waits in gate() until the next grant. Inputs the world sends in between (an AIR_IN, a debug link
   frame) are queued and applied by the bridge thread at that pause, before the CPU runs on.

**Free mode** (`Lockstep(free=True)`, `hrc7000_emu.py --lockstep`, which emu_check uses for --screen and --key): the
same clock with no world. Nothing pauses; an idle point or a kick adds at most FREE_JUMP_S (one RTOS tick) and never
passes the next due event, so with no event due (before the scheduler starts) time still moves in fixed steps. The
debug link requests of a run (GET_VERSION, the view at each screen) go in-process too: request() puts the frame into
the host port buffer at the current stop, and gate() hands each reply to its callback when the firmware has written it.
So the screen, the key press and the replies land at the same radio times in every run, whatever the host load.
"""
import math
import struct
import threading

FREE_JUMP_S = 0.001     # free mode: the most an idle point or kick adds (the RTOS tick period; module text)


class Lockstep:
    def __init__(self, wait_s=0.2, free=False):
        self.free = free
        self.calls = {}             # debug link sequence number -> callback(status, payload)
        self.seq = 0x40
        self.parsed = 0             # host_out bytes already looked at
        self.cv = threading.Condition()
        self.target = math.inf if free else 0.0     # radio seconds the bridge may run to
        self.paused_at = None       # radio seconds the bridge is paused at, None while it runs
        self.pending = []           # functions to run in the bridge thread at the pause
        self.closing = False
        self.finished = False       # the bridge's main() returned
        self.host_out = bytearray()
        self.wait = wait_s          # host seconds without a stop before a kick
        self.idle_points = self.kicks = 0
        self.r = self.clock = self.soc = self.modem = self.host = self.process = None
        self.idle_lo = None

    # --- the bridge side (its own thread) ---

    def attach(self, r, clock, soc, modem, host, process, syms):
        self.r, self.clock, self.soc, self.modem, self.host, self.process = r, clock, soc, modem, host, process
        self.idle_lo = syms.get("vApplicationIdleHook")
        host.conn = self            # the firmware's host port output comes to sendall() below
        if modem is not None:
            modem.stop_at = None if self.free else self.target

    def sendall(self, data):
        self.host_out += data

    def gate(self):
        """At the top of each loop pass. Returns False when the world is done with this radio."""
        with self.cv:
            while True:
                self._replies()
                for fn in self.pending:
                    fn()
                self.pending.clear()
                if self.closing:
                    return False
                if self.clock.now < self.target:
                    self.paused_at = None
                    if self.modem is not None and not self.free:
                        self.modem.stop_at = self.target
                    return True
                self.paused_at = self.clock.now
                self.cv.notify_all()
                self.cv.wait()

    def extra(self, hook, lr, kicked, extra):
        """The radio time this stop adds (module text, rule 1)."""
        idle = (hook is not None and hook[0] == "time" and hook[1] == "hal_millis" and self.idle_lo is not None
                and self.idle_lo <= lr < self.idle_lo + 0x100)
        jump = FREE_JUMP_S if self.free else math.inf
        if idle:
            self.idle_points += 1
            return jump
        if kicked:
            self.kicks += 1
            return jump
        return extra

    def request(self, cmd, payload, callback):
        """Bridge thread: a debug link request into the host port now; callback(status, payload) gets the reply."""
        import rfoundry_debug as rd
        self.seq = self.seq + 1 if self.seq < 0xFF else 0x40
        self.calls[self.seq] = callback
        with self.host.lock:
            self.host.rx += rd.frame(cmd, self.seq, payload)
        self.host.data.set()

    def _replies(self):
        """Hand every complete reply frame in host_out to its callback (tools/rfoundry_debug.py framing)."""
        if not self.calls:
            return
        import rfoundry_debug as rd
        data = bytes(self.host_out[self.parsed:])
        while True:
            start = data.find(b"\xA5\x5A")
            if start < 0 or len(data) - start < 8:
                break
            cmd, seq, n = struct.unpack_from("<BBH", data, start + 2)
            end = start + 8 + n
            if end > len(data):
                break
            pkt = data[start:end]
            if rd.crc16(pkt[2:-2]) == struct.unpack("<H", pkt[-2:])[0] and cmd >= 0x80 and seq in self.calls:
                self.calls.pop(seq)(pkt[6], pkt[7:-2])
                self.parsed += end
                data = data[end:]
            else:
                self.parsed += start + 2
                data = data[start + 2:]

    def done(self):
        with self.cv:
            self.finished = True
            self.paused_at = None
            self.cv.notify_all()

    # --- the world side ---

    def run_to(self, t_s, timeout=600.0):
        """Let the bridge run to radio time t_s and wait until it is paused there."""
        with self.cv:
            self.target = t_s
            self.cv.notify_all()
            ok = self.cv.wait_for(lambda: self.finished or (self.paused_at is not None and self.paused_at >= t_s),
                                  timeout)
            if not ok or self.finished:
                raise RuntimeError("bridge stopped" if self.finished else "bridge did not reach the grant end")

    def call(self, fn):
        """Run fn in the bridge thread at the current pause (an input at the paused machine time)."""
        with self.cv:
            self.pending.append(fn)

    def wait_paused(self, timeout=120.0):
        with self.cv:
            ok = self.cv.wait_for(lambda: self.finished or self.paused_at is not None, timeout)
            if not ok or self.finished:
                raise RuntimeError("bridge stopped before its first pause")

    def close(self):
        with self.cv:
            self.closing = True
            self.cv.notify_all()
