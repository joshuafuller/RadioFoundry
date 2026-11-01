# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""The HR_C7000's built-in DMR modem at burst level, for tools/emu/hrc7000_emu.py (route A, emu-hrc7000.md 10.2).

Written from docs/spec/modem-hrc7000.md only. The chip does all FEC, sync and slot timing in hardware, and the CPU
reads and writes whole bursts of information bits (section 1). So this model moves whole bursts: no 4FSK, no FEC.
On QEMU's virt machine the register block (0x11000000) and the modem RAM (0x16000000) are plain RAM, so the firmware's
writes land there by themselves. The model only has to (a) see the writes that act, (b) raise the interrupts on the
one radio clock (hrc7000_soc.VirtualClock) and (c) take each burst the firmware sends.

M1, init and the slot clock:
- The bridge sets a write watchpoint on LAYER2_CONTROL, LAYER2_SLOTON and LAYER2_TXRX_CTRL (0x400-0x40B, section 2.5).
  Each store there stops the CPU once and the model reads the three registers.
- At the first DMR entry (WORK_MODE 0x6E) the model reads the whole block once and checks the init values of
  section 4 (`init_check`).
- Slot interrupts. A receiver on passive timing (LAYER2_CONTROL 0xC0) that hears nothing gets no timeslot interrupts
  (section 5.3: they stop once the timing is released; dmr_glue.c's diagnostics: "ticks climb by 33 per second once a
  DMR signal gave the chip timing"). With active TX timing (LAYER2_CONTROL b1, 0xC3 at a transmission on our own
  timing, section 6.1) the chip owns the slot clock: every 30 ms it raises PIC source 30 (Time_slot_inter_tx, vector
  62). The slots sit on the radio's 30 ms grid (slot k starts at k * 30 ms), as hrc7000_soc.SlotClock and the
  conductor's air model count them. Source 31 (the RX slot clock) needs a received signal: that is M3.
- RF TX. Before a slot armed with TXRX_CTRL b7, source 32 (Rf_tx_inter, vector 64) is raised rf_pre_on_tx * 100 us
  early: RF_CONTROL b13:8, 0x1F = 3.1 ms with the driver's 0x00041F1A (sections 2.1, 3.1). Read from the register.

M2, a TX burst out as an air record:
- The firmware arms a slot in its TX timeslot interrupt: payload to TX RAM, LAYER2_SEND_TYPE, then TXRX_CTRL = 0x80
  (section 6.4). The watch stops the CPU right after that store, so TX RAM and SEND_TYPE are what was armed. The burst
  is on air in the next slot (the driver's TXS_DRAIN comment, hrc7000_dmr.c: "the burst armed at tick k is on air in
  the slot from tick k+1 to k+2"), so its air start is the next 30 ms boundary.
- SEND_TYPE (section 2.6): b3 set is a voice burst, index b6:4; else the data type is b7:4 (the same numbers as
  hal/hal_dmr_modem.h dmr_burst_kind_t). Payload: 12 bytes for most kinds, 18 rate 3/4, 24 rate 1, 27 voice from the
  voice RAM (section 2.8). For the 10-byte kinds (PI, CSBK, MBC header, data header) with SEND_TYPE b8 = 0 the chip
  appends the CRC-16 with its mask register (section 7), and so does this model: CRC-CCITT as core/dmr/call/dmr_lc.c
  computes it, XOR CSBK_CRC_MASK (0x364) and so on.
- The record is the conductor's AIR_OUT layout (boards/vhost/dmr_air_vchan.c, tools/conductor/parts/air.py Record):
  ts u8, kind u8, air start us u64, cc i8, voice index u8, sync u8, flags u8, embedded LC[9], length u8, payload.
  cc is LOCAL_CC b3:0. sync is the family of SEND_DATA_SYNC (MS, BS, TS1, TS2), none for voice B-F. ts is 1 or 2 only
  when the sync family shows it (TS1 / TS2 direct mode syncs), else 0 ("the framing does not show it", airmodel.py).
  flags 1 (host CRC): the payload carries its CRC, as the virtual radio's records for the same burst do.

Example: the stack sends a call alert. In one TX timeslot interrupt the firmware writes 10 bytes 9F 00 00 00 2F 4D 62
2F 4D 61 to TX RAM, SEND_TYPE 0x30 and TXRX 0x80. The model emits kind 3 (CSBK), cc 1, 12 bytes with the CRC, at the
next slot edge; air.Record.decode reads it back, bytes 7-9 are the source ID 3100001.
"""
import math
import struct

from hrc7000_soc import SLOT_S, Soc

MODEM_BASE = 0x11000000
MODEM_RAM = 0x16000000
TX_RAM, VOICE_RAM = 0x000, 0x960            # offsets in the modem RAM (section 1)
# register offsets (section 2; the same names as drivers/modem/hrc7000/hrc7000.h)
WORK_MODE, RF_MODE, RF_CONTROL = 0x100, 0x104, 0x110
SEND_DATA_SYNC_H, SEND_DATA_SYNC_L = 0x12C, 0x130
PI_CRC_MASK, CSBK_CRC_MASK, MBCH_CRC_MASK, DATAH_CRC_MASK = 0x350, 0x364, 0x368, 0x36C
LOCAL_CC, SYS_INTERP_MASK = 0x384, 0x39C
LAYER2_CONTROL, LAYER2_SLOTON, LAYER2_TXRX_CTRL, LAYER2_SEND_TYPE = 0x400, 0x404, 0x408, 0x418
LAYER2_SLOT_PRE = 0x424
# One write watchpoint per register: QEMU's stop reply names the watch's start address, not the store's, so one watch
# per register tells which one was written (also when the same value is written again, as TXRX 0x80 twice)
WATCHES = (LAYER2_CONTROL, LAYER2_SLOTON, LAYER2_TXRX_CTRL, 0x3A0)       # 0x3A0: SYS_INTERP_CLEAR (M3)
# receive (M3): sections 2.4, 2.7, 2.8, 3
RX_RAM = 0x4B0
RX_TYPE_INFO, SYS_INTERP_LIST, SYS_INTERP_CLEAR, LAYER2_STATUS = 0x390, 0x398, 0x3A0, 0x41C
SYS_DEFRAME = 1 << 3
TXRX_RX, TXRX_VOICE_ACCESS, TXRX_RX_SYNC_RELEASE = 0x40, 0x8000, 0x800000
L2_RXEN = 0x40
SLOTON_RX_OPEN_TX = 0x20
DEFRAME_S = 0.0275                          # a burst is deframed when its 27.5 ms of symbols are in (section 3.1)
RTI_CRC16, RTI_RS = 1 << 26, 1 << 25
HEADER_RS_MASK, TERMINATOR_RS_MASK = 0x96, 0x99   # RS(12,9) masks per byte (section 2.3: 0x969696, 0x999999)
SRC_SYS, SRC_TS_TX, SRC_TS_RX, SRC_RF_TX = 29, 30, 31, 32   # PIC sources (platform/hrc7000/platform.h)
L2_TX_MASTER = 0x02                         # LAYER2_CONTROL b1: active TX timing
TXRX_TX = 0x80                              # LAYER2_TXRX_CTRL b7: transmit in the next slot
WORK_MODE_DMR = 0x6E
# hal/hal_dmr_modem.h: dmr_burst_kind_t, dmr_sync_family_t
KIND_PI, KIND_LC_HEADER, KIND_TERMINATOR, KIND_CSBK, KIND_MBC_HEADER = 0x0, 0x1, 0x2, 0x3, 0x4
KIND_DATA_HEADER, KIND_RATE_34, KIND_RATE_1, KIND_VOICE = 0x6, 0x8, 0xA, 0x10
KIND_NAMES = {0x0: "pi_header", 0x1: "voice_lc_header", 0x2: "terminator_lc", 0x3: "csbk", 0x4: "mbc_header",
              0x5: "mbc_cont", 0x6: "data_header", 0x7: "rate_12_data", 0x8: "rate_34_data", 0x9: "idle",
              0xA: "rate_1_data", 0xB: "usbd", 0x10: "voice"}
SYNC_NONE, SYNC_MS, SYNC_BS, SYNC_TS1, SYNC_TS2 = range(5)
SYNC_WORDS = {(0xD5D7F7, 0x7FD757): SYNC_MS, (0xDFF57D, 0x75DF5D): SYNC_BS,
              (0xF7FDD5, 0xDDFD55): SYNC_TS1, (0xD7557F, 0x5FF7F5): SYNC_TS2}    # section 2.2
CRC_MASK_REG = {KIND_PI: PI_CRC_MASK, KIND_CSBK: CSBK_CRC_MASK, KIND_MBC_HEADER: MBCH_CRC_MASK,
                KIND_DATA_HEADER: DATAH_CRC_MASK}
# section 4: what the driver writes once at power-on and section 5.1 at DMR entry (register: value)
# RF_CONTROL is checked without b13:8, the RF TX lead, which is per board (section 2.1: 0x1F, 0x14, 0x06 or 0x04)
RF_CONTROL_NO_LEAD = 0xFFFFC0FF
INIT_VALUES = {WORK_MODE: WORK_MODE_DMR, RF_MODE: 0x03269860, RF_CONTROL: 0x0004001A, SYS_INTERP_MASK: 0x1187F,
               LAYER2_SLOT_PRE: 0x551010, PI_CRC_MASK: 0x6969, CSBK_CRC_MASK: 0xA5A5, MBCH_CRC_MASK: 0xAAAA,
               DATAH_CRC_MASK: 0xCCCC, SEND_DATA_SYNC_H: 0xD5D7F7, SEND_DATA_SYNC_L: 0x7FD757}
RECORD = struct.Struct("<BBQbBBB9sB")       # boards/vhost/dmr_air_vchan.c


def crc_ccitt(data):
    """CRC-CCITT as core/dmr/call/dmr_lc.c dmr_crc_ccitt: polynomial 0x1021, start 0, high bit first, inverted."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc ^ 0xFFFF


def gf_mul(a, b):
    """GF(256) product with the field polynomial 0x11D (ETSI TS 102 361-1 B.3.6), as core/dmr/call/dmr_lc.c."""
    p = 0
    while b:
        if b & 1:
            p ^= a
        a = ((a << 1) ^ (0x1D if a & 0x80 else 0)) & 0xFF
        b >>= 1
    return p


def rs_lc(lc, mask):
    """9 LC bytes + their RS(12,9) parity, each parity byte XOR mask (tools/emu/peripherals/c6000_test.py rs_lc)."""
    r2 = r1 = r0 = 0
    for x in lc:
        fb = x ^ r2
        r2, r1, r0 = r1 ^ gf_mul(fb, 0x0E), r0 ^ gf_mul(fb, 0x38), gf_mul(fb, 0x40)
    return bytes(lc) + bytes((r2 ^ mask, r1 ^ mask, r0 ^ mask))


def lc_record(kind, src, dst, group=True, t_us=0, cc=1):
    """An LC header (kind 1) or terminator (2) air record: FLCO 0 group or 3 private, FID 0, service options 0,
    destination and source big endian (ETSI TS 102 361-2 7.1.1), then RS(12,9). flags 1: the payload has its parity,
    as the virtual radio's records do."""
    lc = bytes((0 if group else 3, 0, 0)) + dst.to_bytes(3, "big") + src.to_bytes(3, "big")
    mask = HEADER_RS_MASK if kind == KIND_LC_HEADER else TERMINATOR_RS_MASK
    return encode_record(0, kind, t_us, cc, 0, SYNC_MS, 1, bytes(9), rs_lc(lc, mask))


def kind_of(send_type):
    """(kind, voice index) from LAYER2_SEND_TYPE (section 2.6). Example: 0x30 is (3, 0), a CSBK; 0x2B is voice C."""
    if send_type & 0x08:
        return KIND_VOICE, (send_type >> 4) & 0x7
    return (send_type >> 4) & 0xF, 0


def payload_len(kind):
    """Bytes of the burst's information (section 2.8), as hrc7000_dmr.c hrc7000_dmr_payload_len."""
    return {KIND_VOICE: 27, KIND_RATE_34: 18, KIND_RATE_1: 24}.get(kind, 12)


def encode_record(ts, kind, t_us, cc, voice_index, sync, flags, lc, payload):
    return RECORD.pack(ts, kind, t_us, cc, voice_index, sync, flags, bytes(lc).ljust(9, b"\0")[:9],
                       len(payload)) + bytes(payload)


class Modem:
    """The modem's state as the firmware programs it, and the bursts it sends. The bridge calls on_watch() at a
    watch stop in the LAYER2 block, events() and fire() through ModemSoc, and reads `sent` for the records."""

    def __init__(self, r, clock):
        self.r = r                  # the RSP link (hrc7000_rsp.Rsp)
        self.clock = clock
        self.l2 = {LAYER2_CONTROL: 0, LAYER2_SLOTON: 0, LAYER2_TXRX_CTRL: 0}
        self.writes = {}            # (register, value) -> count
        self.slot_next = None       # radio time of the next TX slot interrupt, None while the chip has no TX timing
        self.armed = []             # [air start, rf tx time or None, burst dict]
        self.sent = []              # (air start s, record bytes, burst dict)
        self.raised = {}            # PIC source -> count
        self.init = None            # (matches, total, mismatches) at the first DMR entry
        self.log = []
        # receive (M3)
        self.inbox = []             # [air start s, record] waiting for their slot
        self.deframes = []          # [deframe time s, record, slot number]
        self.rx_next = None         # radio time of the next RX slot interrupt, None without receive timing
        self.received = []          # (time, record, RX_TYPE_INFO, what happened)
        self.sys_list = 0
        self.rx_parity = 0          # chip slot of slot k = (k & 1) ^ rx_parity, set by the burst that gave the timing
        self.stop_at = None         # lockstep (hrc7000_lockstep.py): the clock may not pass this radio time

    # --- the firmware's writes ---------------------------------------------------------------------------------------

    def _reg(self, off):
        return struct.unpack("<I", self.r.read(MODEM_BASE + off, 4))[0]

    def on_watch(self, addr):
        """A store into one of WATCHES. addr: the watch the stop reply names (MODEM_BASE + the register)."""
        raw = self.r.read(MODEM_BASE + LAYER2_CONTROL, 12)
        self.l2 = dict(zip((LAYER2_CONTROL, LAYER2_SLOTON, LAYER2_TXRX_CTRL), struct.unpack("<3I", raw)))
        now = self.l2
        for off in [(addr - MODEM_BASE) & ~3]:
            if off not in now:
                continue
            v = now[off]
            self.writes[(off, v)] = self.writes.get((off, v), 0) + 1
            if off == LAYER2_CONTROL:
                if self.init is None and self._reg(WORK_MODE) == WORK_MODE_DMR:
                    self.init = self.init_check()
                if v & L2_TX_MASTER and self.slot_next is None:
                    # active timing: the chip's own slot clock on the radio's 30 ms grid
                    self.slot_next = (int(self.clock.now / SLOT_S) + 1) * SLOT_S
                elif not v & L2_TX_MASTER:
                    self.slot_next = None
            elif off == LAYER2_SLOTON and v & SLOTON_RX_OPEN_TX and self.rx_next is not None:
                # an aligned start: the TX slot built from the receive slot (section 2.5, SLOTON 0x25). The slot
                # right after the strobe has no TX tick (section 2.5, the SFR bench note: the first TX-vector
                # interrupt two slots after the last receive one)
                self.slot_next = self.rx_next + SLOT_S
            elif off == LAYER2_TXRX_CTRL and v & TXRX_TX:
                self.arm()
            elif off == LAYER2_TXRX_CTRL and v & TXRX_RX_SYNC_RELEASE:
                self.rx_next = None     # the receiver drops the sender's slot timing (section 5.2 step 5)
        if (addr - MODEM_BASE) & ~3 == SYS_INTERP_CLEAR:
            # write the bits read from the list; the ISR repeats until the list reads 0 (section 2.4)
            v = self._reg(SYS_INTERP_CLEAR)
            self.sys_list &= ~v
            self.r.write(MODEM_BASE + SYS_INTERP_LIST, struct.pack("<I", self.sys_list))

    def init_check(self):
        got = {off: self._reg(off) for off in INIT_VALUES}
        self.rf_lead_us = ((got[RF_CONTROL] >> 8) & 0x3F) * 100
        got[RF_CONTROL] &= RF_CONTROL_NO_LEAD
        bad = {off: (want, got[off]) for off, want in INIT_VALUES.items() if got[off] != want}
        return len(INIT_VALUES) - len(bad), len(INIT_VALUES), bad

    def arm(self):
        """TXRX_CTRL b7 just written: take the burst now (TX RAM and SEND_TYPE as armed), on air at the next edge."""
        now = self.clock.now
        start = (int(now / SLOT_S + 1e-9) + 1) * SLOT_S
        send_type = self._reg(LAYER2_SEND_TYPE)
        kind, index = kind_of(send_type)
        n = payload_len(kind)
        if kind == KIND_VOICE:
            payload = self.r.read(MODEM_RAM + VOICE_RAM, 27)
            lc = self.r.read(MODEM_RAM + TX_RAM, 9) if index == 0 else bytes(9)
        else:
            payload = bytearray(self.r.read(MODEM_RAM + TX_RAM, n))
            lc = bytes(9)
            if kind in CRC_MASK_REG and not send_type & 0x100:
                crc = crc_ccitt(payload[:10]) ^ (self._reg(CRC_MASK_REG[kind]) & 0xFFFF)
                payload[10:12] = bytes((crc >> 8, crc & 0xFF))
            payload = bytes(payload)
        sync = SYNC_WORDS.get((self._reg(SEND_DATA_SYNC_H) & 0xFFFFFF, self._reg(SEND_DATA_SYNC_L) & 0xFFFFFF),
                              SYNC_MS)
        if kind == KIND_VOICE and index != 0:
            sync = SYNC_NONE        # voice B-F carry the EMB, not a sync
        ts = {SYNC_TS1: 1, SYNC_TS2: 2}.get(sync, 0)
        cc = self._reg(LOCAL_CC) & 0xF
        lead = ((self._reg(RF_CONTROL) >> 8) & 0x3F) * 1e-4
        burst = {"send_type": send_type, "kind": kind, "voice_index": index, "cc": cc, "sync": sync, "ts": ts,
                 "payload": payload, "lc": lc, "armed_at": now, "lead": lead}
        self.armed.append([start, max(start - lead, now), burst])

    # --- the clock side ----------------------------------------------------------------------------------------------

    def events(self):
        """(time, PIC source) of what is due next."""
        ev = []
        if self.slot_next is not None:
            ev.append((self.slot_next, SRC_TS_TX))
        if self.rx_next is not None:
            ev.append((self.rx_next, SRC_TS_RX))
        ev += [(t, SRC_SYS) for t, _, _ in self.deframes]
        ev += [(rf, SRC_RF_TX) for _, rf, _ in self.armed if rf is not None]
        return ev

    def fire(self, now, pic):
        """Raise what fell due by radio time `now`, and send the bursts whose air start has come."""
        self.fire_rx(now, pic)
        while self.slot_next is not None and self.slot_next <= now + 1e-12:
            # LAYER2_STATUS b31: the chip slot a burst armed now goes out in (the next one); an aligned start sends in
            # the slot whose b31 is the received burst's chip slot (hrc7000_dmr.c tx_tick, section 2.5)
            k = int(self.slot_next / SLOT_S + 1e-9) + 1
            st = self._reg(LAYER2_STATUS) & ~(1 << 31) | self.chip_slot(k) << 31
            self.r.write(MODEM_BASE + LAYER2_STATUS, struct.pack("<I", st))
            self._raise(pic, SRC_TS_TX)
            self.slot_next += SLOT_S
        for item in self.armed:
            if item[1] is not None and item[1] <= now + 1e-12:
                self._raise(pic, SRC_RF_TX)
                item[1] = None
        while self.armed and self.armed[0][1] is None and self.armed[0][0] <= now + 1e-12:
            start, _, b = self.armed.pop(0)
            wire = encode_record(b["ts"], b["kind"], int(round(start * 1e6)), b["cc"], b["voice_index"], b["sync"],
                                 1 if b["kind"] != KIND_VOICE else 0, b["lc"], b["payload"])
            self.sent.append((start, wire, b))

    # --- receive (M3) ------------------------------------------------------------------------------------------------

    def feed(self, wires, at_s):
        """Air records (the AIR_OUT / AIR_IN layout) to receive. The first goes on air at the first slot edge at or
        after radio time at_s, on an even slot for TS1 or no TS and an odd one for TS2 (the chip slot b19 then reads
        0 or 1, and the driver names a slot without TS sync by it: hrc7000_dmr.c slot_ts). The others keep their
        spacing from their t_us, rounded to whole slots. Example: records at t_us 0, 60000, 120000 fed at 2.5 s go on
        air at 2.52, 2.58 and 2.64 s."""
        from conductor.parts.air import Record
        recs = [Record.decode(w) for w in wires]
        if not recs:
            return
        k0 = math.ceil(at_s / SLOT_S - 1e-9)
        if (k0 & 1) != (1 if recs[0].ts == 2 else 0):
            k0 += 1
        for rec in recs:
            k = k0 + round((rec.t_us - recs[0].t_us) / (SLOT_S * 1e6))
            self.inbox.append([k * SLOT_S, rec])
        self.inbox.sort(key=lambda x: x[0])

    def chip_slot(self, k):
        return (k & 1) ^ self.rx_parity

    def air_in(self, rec):
        """A record handed over by the conductor (tools/conductor/parts/air.py) now, at the last microsecond of its
        slot: it is deframed at once, and the receive timing starts at the slot end (air start + 30 ms), where the
        virtual radio's modem reports RX too (dmr_air_vchan.c)."""
        self.deframes.append([self.clock.now, rec, int(rec.t_us / (SLOT_S * 1e6) + 1e-9)])
        self.deframes.sort(key=lambda d: d[0])

    def rx_type_info(self, rec, slot):
        """RX_TYPE_INFO for a burst (section 2.7). The CRC flags are the chip's verdict: CRC-16 checked with the
        mask register for the 10-byte kinds, RS(12,9) with its mask for LC headers and terminators, and the
        record's own "corrupted" flag (4) on top."""
        fam = {SYNC_MS: 0, SYNC_BS: 1, SYNC_TS1: 2, SYNC_TS2: 3}.get(rec.sync, 0)
        rti = self.chip_slot(slot) << 19 | (rec.cc & 0xF if rec.cc >= 0 else self._reg(LOCAL_CC) & 0xF) << 4 | fam << 2
        p = rec.payload
        bad = bool(rec.flags & 4)
        if rec.kind == KIND_VOICE:
            rti |= (rec.voice_index + 1) << 12 | (1 if rec.voice_index == 0 else 0)
        else:
            rti |= (rec.kind & 0xF) << 8 | 2
            if rec.kind in CRC_MASK_REG:
                good = len(p) >= 12 and (crc_ccitt(p[:10]) ^ (self._reg(CRC_MASK_REG[rec.kind]) & 0xFFFF)) == \
                    int.from_bytes(p[10:12], "big")
                rti |= RTI_CRC16 if bad or not good else 0
            elif rec.kind in (KIND_LC_HEADER, KIND_TERMINATOR):
                mask = HEADER_RS_MASK if rec.kind == KIND_LC_HEADER else TERMINATOR_RS_MASK
                rti |= RTI_RS if bad or len(p) < 12 or rs_lc(p[:9], mask) != bytes(p[:12]) else 0
        return rti

    def fire_rx(self, now, pic):
        eps = 1e-12
        while self.inbox and self.inbox[0][0] <= now + eps:
            start, rec = self.inbox.pop(0)
            self.deframes.append([start + DEFRAME_S, rec, int(start / SLOT_S + 1e-9)])
        while self.rx_next is not None and self.rx_next <= now + eps:
            k = int(self.rx_next / SLOT_S + 1e-9)
            # LAYER2_STATUS b30: the RX chip slot that the interrupt decides (hrc7000_dmr.c rx_tick)
            st = self._reg(LAYER2_STATUS) & ~(1 << 30) | self.chip_slot(k) << 30
            self.r.write(MODEM_BASE + LAYER2_STATUS, struct.pack("<I", st))
            self._raise(pic, SRC_TS_RX)
            self.rx_next += SLOT_S
        while self.deframes and self.deframes[0][0] <= now + eps:
            t, rec, k = self.deframes.pop(0)
            self.deframe(t, rec, k, pic)

    def deframe(self, t, rec, k, pic):
        if self._reg(WORK_MODE) != WORK_MODE_DMR or not self.l2[LAYER2_CONTROL] & L2_RXEN:
            self.received.append((t, rec, None, "not in DMR receive"))
            return
        if self.rx_next is None:
            # Passive timing follows the sender (section 2.5, LAYER2_CONTROL 0xC0): the chip's slot numbering starts
            # from this burst. A TS1 or TS2 sender's burst gets chip slot 0 or 1, so the driver, which names a slot
            # without a TS sync by its chip slot (hrc7000_dmr.c slot_ts), names it the sender's TS
            want = rec.ts - 1 if rec.ts in (1, 2) else k & 1
            self.rx_parity = (k & 1) ^ want
        txrx = self.l2[LAYER2_TXRX_CTRL]
        if not txrx & TXRX_RX:
            self.received.append((t, rec, None, f"not deframed: TXRX 0x{txrx:x} has no receive (b6)"))
            return
        if rec.cc >= 0 and rec.cc != self._reg(LOCAL_CC) & 0xF:
            self.received.append((t, rec, None, "not deframed: color code is not LOCAL_CC (cc_opt 0)"))
            return
        rti = self.rx_type_info(rec, k)
        what = "deframed"
        if rec.kind == KIND_VOICE:
            if txrx & TXRX_VOICE_ACCESS:
                self.r.write(MODEM_RAM + VOICE_RAM, bytes(rec.payload).ljust(27, b"\0")[:27])
            else:
                what = "deframed; voice RAM left as it was (no voice access flag, section 5.3)"
        else:
            n = payload_len(rec.kind)
            self.r.write(MODEM_RAM + RX_RAM, bytes(rec.payload).ljust(n, b"\0")[:n])
        self.r.write(MODEM_BASE + RX_TYPE_INFO, struct.pack("<I", rti))
        self.received.append((t, rec, rti, what))
        if self._reg(SYS_INTERP_MASK) & SYS_DEFRAME:
            self.sys_list |= SYS_DEFRAME
            self.r.write(MODEM_BASE + SYS_INTERP_LIST, struct.pack("<I", self.sys_list))
            self._raise(pic, SRC_SYS)
        # the receiver now has the sender's slot timing: RX slot interrupts from the next edge, for as long as
        # bursts come (the driver's own restart after 390 ms of nothing drops it, section 5.3)
        if self.rx_next is None:
            self.rx_next = (k + 1) * SLOT_S

    def _raise(self, pic, source):
        pic.request(source)
        self.raised[source] = self.raised.get(source, 0) + 1

    def report(self):
        lines = []
        if self.init:
            ok, total, bad = self.init
            lines.append(f"modem init: {ok} of {total} registers as modem-hrc7000.md sections 4 and 5.1 (RF TX lead "
                         f"{self.rf_lead_us} us, the board's RF_CONTROL b13:8)"
                         + "".join(f"; 0x{o:03x} is 0x{g:x}, want 0x{w:x}" for o, (w, g) in bad.items()))
        else:
            lines.append("modem init: the firmware never entered DMR (WORK_MODE 0x6E with a LAYER2_CONTROL write)")
        w = ", ".join(f"{name} 0x{v:x} x{n}" for (o, v), n in sorted(self.writes.items())
                      for name in [{LAYER2_CONTROL: "CONTROL", LAYER2_SLOTON: "SLOTON",
                                    LAYER2_TXRX_CTRL: "TXRX"}[o]])
        lines.append(f"modem writes: {w or 'none'}")
        for t, rec, rti, what in self.received:
            lines.append(f"AIR_IN at radio {t:.4f} s: kind={rec.kind} ({KIND_NAMES.get(rec.kind, '?')}) cc={rec.cc} "
                         f"len={len(rec.payload)} payload={bytes(rec.payload).hex()}: {what}"
                         + (f", RX_TYPE_INFO 0x{rti:08x}" if rti is not None else ""))
        lines.append("modem interrupts raised (PIC source: count): "
                     + (", ".join(f"{s}: {n}" for s, n in sorted(self.raised.items())) or "none"))
        return lines


class ModemSoc(Soc):
    """hrc7000_soc.Soc plus the modem's interrupts on the same clock: the clock holds at each modem event as it does
    at a timer expiry, so a slot interrupt is given at exactly its time."""

    def __init__(self, clock, modem=None, **kw):
        super().__init__(clock, **kw)
        self.modem = modem

    def limit(self):
        lim, held = super().limit()
        stop = self.modem.stop_at if self.modem is not None else None
        if stop is not None and (lim is None or stop < lim):
            return stop, False
        return lim, held

    def _events(self):
        ev = super()._events()
        if self.modem is not None:
            ev += [(t, s, self.modem) for t, s in self.modem.events()]
        return ev

    def step(self, extra=0.0):
        now = super().step(extra)
        if self.modem is not None:
            self.modem.fire(now, self.pic)
        return now


def decode_line(start, wire):
    """One line for the report, decoded by the conductor's own reader (tools/conductor/parts/air.py)."""
    from conductor.parts.air import Record      # tools/ is on sys.path in hrc7000_emu.py
    rec = Record.decode(wire)
    p = rec.payload
    text = (f"AIR_OUT t_us={rec.t_us} ts={rec.ts} kind={rec.kind} ({KIND_NAMES.get(rec.kind, '?')}) cc={rec.cc} "
            f"sync={rec.sync} flags={rec.flags} len={len(p)} payload={p.hex()}")
    if rec.kind in (KIND_CSBK, KIND_DATA_HEADER) and len(p) >= 12:
        dst, src = int.from_bytes(p[4:7], "big"), int.from_bytes(p[7:10], "big")
        mask = 0xA5A5 if rec.kind == KIND_CSBK else 0xCCCC
        good = (crc_ccitt(p[:10]) ^ mask) == int.from_bytes(p[10:12], "big")
        text += (f"; opcode 0x{p[0] & 0x3F:02x} fid 0x{p[1]:02x} dst {dst} src {src} "
                 f"crc {'good' if good else 'BAD'}")
    return rec, text


def send_driver(url, lock, clock, modem, action, start_s, res, wait_s=3.0):
    """The --dmr-send step, on its own thread: over the debug link, once the radio has run `start_s` seconds of radio
    time, read the firmware's own view (mode, channel, own ID), ask for a CSBK (call alert or radio check, no codec
    needed: core/dmr/dmrDataService.c signalTransmit), wait for the modem to send it, then read the firmware's DMR
    diagnostics (dmr_glue.c hrc_dmr_stats_line: ts, tx, rf counters). Results go in `res`; res["done"] ends it.

    action: "call_alert:DST" or "radio_check:DST"."""
    import time
    import rfoundry_debug as rd
    what, _, dst = (action or "").partition(":")
    try:
        while clock.now < start_s:
            time.sleep(0.05)
        with lock:
            with rd.DebugLink(url, timeout=5.0) as d:
                kv = d.view()
                res["view"] = {k: kv.get(k) for k in sorted(kv) if k.split(".")[-1] in (
                    "mode", "own_id", "cc", "ts", "state", "tx_deny", "rx_hz", "tx_hz", "name", "ch", "channel")}
                if what:
                    res["asked_at"] = clock.now
                    getattr(d, what)(int(dst))
        t = clock.now
        while what and not modem.sent and clock.now < t + wait_s:
            time.sleep(0.05)
        while modem.inbox or modem.deframes:        # everything fed is received
            time.sleep(0.05)
        t = clock.now
        while clock.now < t + 0.6:      # the burst's own slot and the drain after it, or an answer
            time.sleep(0.05)
        with lock:
            with rd.DebugLink(url, timeout=5.0) as d:
                res["diag"] = [ln for ln in d.diag()[1] if ln.startswith(("DMR", "ts ", "tx", "un", "V", "C"))]
                kv = d.view()
                res["after"] = {k: kv.get(k) for k in sorted(kv) if ("data" in k and ("sent" in k or "state" in k
                                or "received" in k or "acked" in k)) or k.startswith(("call.", "last."))
                                and any(w in k for w in ("src", "dst", "rx_calls", "group", "kind", "state"))}
    except Exception as e:  # noqa: BLE001 - printed as the result
        res["error"] = repr(e)
    res["done"] = True


def read_records(path):
    """The wires of a JSON lines file as --air-out writes it ({"wire": hex, ...} per line)."""
    import json
    with open(path) as f:
        return [bytes.fromhex(json.loads(line)["wire"]) for line in f if line.strip()]


def main():
    """Write air records to feed with --air-in: `lc SRC DST FILE` gives a group call's start and end as the stack
    sends it (three LC headers 60 ms apart, then a terminator), with no voice in between."""
    import argparse
    import json
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("what", choices=["lc"])
    ap.add_argument("src", type=int)
    ap.add_argument("dst", type=int)
    ap.add_argument("out")
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--cc", type=int, default=1)
    a = ap.parse_args()
    kinds = [KIND_LC_HEADER] * 3 + [KIND_TERMINATOR]
    with open(a.out, "w") as f:
        for i, kind in enumerate(kinds):
            wire = lc_record(kind, a.src, a.dst, not a.private, t_us=i * 60000, cc=a.cc)
            f.write(json.dumps({"t_us": i * 60000, "kind": kind, "wire": wire.hex()}) + "\n")
    print(f"{a.out}: {len(kinds)} records")


if __name__ == "__main__":
    main()
