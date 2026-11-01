# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
#
# A small GDB remote serial protocol (RSP) client for tools/emu/hrc7000_emu.py.
#
# RSP is the wire protocol a debugger uses to drive a target: packets "$<data>#<two hex checksum digits>", each one
# answered with "+". The emulator (QEMU's -gdb port) is the target. Written from the public protocol description in
# the GDB manual, appendix "Remote Protocol" (https://sourceware.org/gdb/current/onlinedocs/gdb.html/Remote-Protocol.html).
# Only what the bridge needs: read and write registers and memory, software breakpoints, continue, and interrupt.

import socket
import struct


class Rsp:
    def __init__(self, host, port=None, timeout=10.0):
        if port is None:
            self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                self.s.settimeout(timeout)
                self.s.connect(host)
            except BaseException:
                self.s.close()
                raise
        else:
            self.s = socket.create_connection((host, port), timeout=timeout)
            # Nagle's algorithm and delayed acks otherwise add about 40 ms per step.
            self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = b""
        self.regsize = None
        # Added to every memory address. QEMU's csky virt machine shows the debugger memory through the CPU's kernel
        # segment (physical memory at 0x80000000 up), while deimos shows it at its own address; the CPU itself runs at
        # the low addresses on both, so breakpoints take the address as it is
        self.mem_base = 0

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass

    def _recv_byte(self):
        while not self.buf:
            chunk = self.s.recv(65536)
            if not chunk:
                raise EOFError("rsp: the emulator closed the connection")
            self.buf += chunk
        b, self.buf = self.buf[:1], self.buf[1:]
        return b

    def _send(self, data):
        pkt = b"$" + data + b"#" + b"%02x" % (sum(data) & 0xFF)
        self.s.sendall(pkt)
        # wait for the ack; stray "+" left over from earlier exchanges are skipped
        while True:
            c = self._recv_byte()
            if c == b"+":
                return
            if c == b"-":
                self.s.sendall(pkt)

    def _recv_packet(self, timeout=None):
        old = self.s.gettimeout()
        if timeout is not None:
            self.s.settimeout(timeout)
        try:
            while self._recv_byte() != b"$":
                pass
            data = b""
            while True:
                c = self._recv_byte()
                if c == b"#":
                    break
                data += c
            self._recv_byte()
            self._recv_byte()
            self.s.sendall(b"+")
            return data
        finally:
            self.s.settimeout(old)

    def cmd(self, data, timeout=None):
        self._send(data.encode() if isinstance(data, str) else data)
        return self._recv_packet(timeout)

    # registers: the target's "g" packet is the register file, 4 bytes each, little endian
    def regs(self):
        r = self.cmd("g")
        while r[:1] in (b"T", b"S"):
            # a stop reply left over from an interrupt (0x03) that crossed a breakpoint stop: the g answer follows
            r = self._recv_packet(5.0)
        raw = bytes.fromhex(r.decode())
        return list(struct.unpack("<%dI" % (len(raw) // 4), raw))

    def put_regs(self, regs):
        """Write the whole register file back in one G packet (one round trip, where set_reg costs two)."""
        r = self.cmd("G" + struct.pack("<%dI" % len(regs), *[v & 0xFFFFFFFF for v in regs]).hex())
        if r != b"OK":
            raise RuntimeError("rsp: G failed: %r" % r)

    def reg(self, n):
        # from the g packet: QEMU's csky "p" answers do not follow the g packet's numbering
        return self.regs()[n]

    def set_reg(self, n, value):
        regs = self.regs()
        regs[n] = value & 0xFFFFFFFF
        r = self.cmd("G" + struct.pack("<%dI" % len(regs), *regs).hex())
        if r != b"OK":
            raise RuntimeError("rsp: G (register %d) failed: %r" % (n, r))

    def set_regs(self, values):
        regs = self.regs()
        for n, v in values.items():
            regs[n] = v & 0xFFFFFFFF
        r = self.cmd("G" + struct.pack("<%dI" % len(regs), *regs).hex())
        if r != b"OK":
            raise RuntimeError("rsp: G failed: %r" % r)

    def read(self, addr, size):
        addr = (addr + self.mem_base) & 0xFFFFFFFF
        out = b""
        while size:
            n = min(size, 1024)
            r = self.cmd("m%x,%x" % (addr, n)).decode()
            if r.startswith("E"):
                raise RuntimeError("rsp: read 0x%08x failed: %s" % (addr, r))
            out += bytes.fromhex(r)
            addr += n
            size -= n
        return out

    def read32(self, addr):
        return struct.unpack("<I", self.read(addr, 4))[0]

    def write(self, addr, data):
        addr = (addr + self.mem_base) & 0xFFFFFFFF
        while data:
            part, data = data[:1024], data[1024:]
            r = self.cmd("M%x,%x:%s" % (addr, len(part), part.hex()))
            if r != b"OK":
                raise RuntimeError("rsp: write 0x%08x failed: %r" % (addr, r))
            addr += len(part)

    def break_at(self, addr):
        # Z0 = software breakpoint; kind 2 = a 16-bit instruction slot (C-SKY v2 instructions are 16 or 32 bits)
        r = self.cmd("Z0,%x,2" % addr)
        if r != b"OK":
            r = self.cmd("Z1,%x,2" % addr)
        if r != b"OK":
            raise RuntimeError("rsp: breakpoint at 0x%08x refused: %r" % (addr, r))

    def unbreak_at(self, addr):
        self.cmd("z0,%x,2" % addr)

    def watch_writes(self, addr, size):
        """Z2 = write watchpoint: the CPU stops after a store into [addr, addr + size). The address is the CPU's own
        (no mem_base). The stop reply names the watch: b'T05...watch:<addr>;'."""
        r = self.cmd("Z2,%x,%x" % (addr, size))
        if r != b"OK":
            raise RuntimeError("rsp: watchpoint at 0x%08x refused: %r" % (addr, r))

    def cont(self, timeout=None):
        """Continue; return the stop reply (b'T05...' on a breakpoint) or None after timeout seconds."""
        self._send(b"c")
        try:
            return self._recv_packet(timeout)
        except socket.timeout:
            return None

    def interrupt(self):
        self.s.sendall(b"\x03")
        return self._recv_packet(5.0)
