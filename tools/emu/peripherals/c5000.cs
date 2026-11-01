// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
//
// A Renode model of the HR_C5000 DMR baseband chip, as the MD-380 firmware drives it. Written from this
// repository's own driver and specs only: drivers/modem/hrc5000, drivers/modem/hrc_common, boards/md380/rfbus_md380.c,
// boards/md380/radio_md380.c, platform/stm32f4/hrc/dmr_glue.c, docs/spec/hardware-md380.md 4, docs/spec/modem-hrc6000.md
// (the family's register meanings) and docs/spec/dmr-modem.md, plus the HR_C5000 user manual where those cite it.
// The model works at the level of a burst, like the virtual board's modem (boards/vhost/dmr_vhost.c): it never makes
// symbols or FEC. What a burst carries on the air is an AIR record (boards/vhost/dmr_air_vchan.c,
// tools/conductor/parts/air.py).
//
// What it models
//   Configuration port: the bit-banged SPI on CS PE2 (input 0), CLK PC13 (input 1), MOSI PE5 (input 2), MISO PE3
//     (output Miso). The clock idles low; the chip takes MOSI at each falling clock edge and then drives that bit's
//     MISO (hardware-md380.md 4.2). Frames: cmd (bit 7 read, bit 6 an 11-bit address, bits 2-0 the page), address low,
//     [address high], data (hrc.c). Pages: 0 configuration, 1 auxiliary, 2 data RAM (writes go to the TX RAM, reads
//     come from the RX RAM), 3 vocoder.
//   Voice port: SPI2 (this peripheral is registered on it), chip select PB12 (input 4). 0x03 / 0x83, 0x00, then data
//     (radio_md380.c voice_xfer). 32-byte voice RAM: a longer access wraps (hardware-md380.md 4.4). Writes fill the
//     transmit voice RAM, reads come from the receive voice RAM.
//   Power: DMR_SLEEP PE6 (input 3), high = powered down: the chip forgets everything.
//   Interrupt lines, active low, idle high: TsInt PC0 (timeslot, a short low pulse every 30 ms), SysInt PC1 (system:
//     low while a flag of 0x82 that 0x81 enables is set, until 0x83 clears it), RfTxInt PC2 (a short low pulse
//     RfTxLeadUs before each burst the chip sends).
//   Slot clock: runs in DMR (0x10 bit 7 clear) with TX or RX enabled (0x40 bits 7, 6). Active timing (0x40 bit 5): it
//     starts 30 ms after the write. Passive timing: it starts at the first burst received and follows received bursts
//     (re-phases to a burst off its grid). At each slot start it latches what 0x41 says the slot does (bit 7 send,
//     bit 6 receive, modem-hrc6000.md 4.1: the MCU writes 0x41 during the slot before) and fires the timeslot pulse.
//   TX: at RfTxLeadUs before a slot whose 0x41 asks to send, it builds the burst from 0x50 (type, voice index), page 2
//     (LC / data) or the voice RAM, page 1 0x04 (the sync pattern, so the family) and 0x1F (the color code), pulses
//     the RF TX line, and at the slot start emits the AIR record. Like the chip it computes the RS(12,9) parity of an
//     LC header / terminator and the CRC-16 of a CSBK, MBC header, data header and PI header (gotchas 5.2).
//   RX: AirIn takes an AIR record no later than the last microsecond of its slot (the conductor hands records over at
//     start + 29999 us). A burst on an open receive slot, with our color code (or 0x41 bit 3, or a voice burst A), is
//     taken; at the tick that ends its slot the voice RAM gets a voice burst (only while the decoder runs: 0x22 bit 5
//     on, bit 4 off), and RxReportDelayUs later the registers and the system interrupt report it: data and LC as 0x82
//     bit 3 with 0x51 type / CRC / sync class and page 2 the payload; voice A as bit 2 (information); voice F with an
//     embedded LC in a reception that had no header as bit 4 (late entry) with the LC in page 2. 0x52 gets the color
//     code and the TC bit (bit 2, 1 after a TS1 burst: hardware-md380.md 4.3).
//
// Stubs (accepted and stored, no behavior): FM modes and every audio, codec, I2S and tone register; the CMX638 and
// AMBE3000 pages (4, 5); the 11-bit page 1 tone table; RSSI (0x43, 0x44 read 0); the access policy (0x20, 0x21: the
// chip never rejects a send, 0x82 bit 7 never set); TX start / end interrupts (0x82 bits 6, 5; 0x84, 0x86 read 0);
// voice abnormal exit (0x82 bit 1, 0x98 reads 0); physical-layer receive (bit 0); 0x90 reads 0 (information good);
// the layer 3 call control (0x60-0x7B); privacy (page 1 0x24-0x27); CRC_MCU_Control (0x40 bit 3) only sets the AIR
// host-CRC flag; reset bits of 0x00 (logged, not acted on); the 1.25 ms timing correction window (a burst off the grid
// re-phases the clock at once).
//
// The test bench is c5000_bench.cs, the checks c5000_test.py.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.SPI;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.RadioFoundry
{
    public class Hrc5000 : ISPIPeripheral, IGPIOReceiver
    {
        // GPIO inputs (the MCU's output pins, wired in the .repl)
        public const int InCs = 0, InClk = 1, InMosi = 2, InPwd = 3, InVoiceCs = 4;

        public const ulong SlotUs = 30000;     // ETSI TS 102 361-1 4.2, dmr_vhost.h VDMR_SLOT_US
        public const int RecordPrefix = 24;    // dmr_air_vchan.c AIR_PREFIX, air.py RECORD.size

        public Hrc5000(IMachine machine)
        {
            this.machine = machine;
            Miso = new GPIO();
            TsInt = new GPIO();
            SysInt = new GPIO();
            RfTxInt = new GPIO();
            RxReportDelayUs = 25000;
            RfTxLeadUs = 1000;
            PulseUs = 10;
            Reset();
            // The .repl connects the lines after the constructor ran, so the levels set in Reset never reached the GPIO
            // ports (Renode's GPIO passes on changes only). Drive them once the emulation runs, and at the first frame
            machine.ScheduleAction(TimeInterval.Empty, _ => InitLines(), "hrc5000");
        }

        // Every line to its idle level at the far end: low then high, so the port sees the high (a rising edge only)
        private void InitLines()
        {
            if(linesUp)
            {
                return;
            }
            linesUp = true;
            foreach(var g in new[] { Miso, TsInt, SysInt, RfTxInt })
            {
                g.Set(false);
                g.Set(true);
            }
            UpdateSysLine();
        }

        // MISO is forced at every bit: an ODR write to the port in Renode overwrites its input pins with the output
        // latch, and a plain Set of the level the GPIO already holds would not repair that
        private void DriveMiso(bool v)
        {
            Miso.Set(!v);
            Miso.Set(v);
        }

        public GPIO Miso { get; }
        public GPIO TsInt { get; }
        public GPIO SysInt { get; }
        public GPIO RfTxInt { get; }

        // The system interrupt for a received burst, this long after the tick that ends its slot. The engine notes that
        // the HR_C5000 "reports data 25 ms after the tick as a rule" (hrc6000_dmr.c rx_report); bench logs saw 12-22 ms
        public ulong RxReportDelayUs { get; set; }
        // The RF TX interrupt this long before a burst's slot starts (unknown 18 in hardware-md380.md 17)
        public ulong RfTxLeadUs { get; set; }
        // Low time of the timeslot and RF TX pulses
        public ulong PulseUs { get; set; }

        // A text log, one line per event (transactions, ticks, AIR records), each with the virtual time in us
        public string TracePath
        {
            get { return tracePath; }
            set
            {
                trace?.Dispose();
                trace = null;
                tracePath = value;
                if(!string.IsNullOrEmpty(value))
                {
                    trace = new StreamWriter(value, false) { AutoFlush = true };
                }
            }
        }

        public void Reset()
        {
            Array.Clear(page0, 0, page0.Length);
            Array.Clear(page1, 0, page1.Length);
            Array.Clear(ramTx, 0, ramTx.Length);
            Array.Clear(ramRx, 0, ramRx.Length);
            Array.Clear(voiceTx, 0, voiceTx.Length);
            Array.Clear(voiceRx, 0, voiceRx.Length);
            // Reset values from the user manual's register table (section 2) where it gives them
            foreach(var kv in ResetValues)
            {
                page0[kv.Key] = kv.Value;
            }
            page0[0x10] = 0x80;     // not in the table: FM, so nothing ticks before a set-up
            flags = 0;
            r51 = 0;
            r52 = 0;
            decoder = false;
            ramTxWritten = false;
            running = false;
            synced = false;
            generation++;
            curTx = false;
            curAction = 0;
            pendingTx = null;
            rxPending = null;
            rxPendingStart = 0;
            headerSeen = false;
            lateGiven = false;
            lastRxSlot = 0;
            slotIndex = 0;
            embLc = new byte[9];
            embValid = false;
            cs = true;
            clk = false;
            voiceCs = true;
            frameActive = false;
            voiceFrame = -1;
            airOut.Clear();
            Miso.Set(true);
            TsInt.Set(true);
            SysInt.Set(true);
            RfTxInt.Set(true);
        }

        // --- Monitor and test interface ---

        // One AIR record (hex, the air.py layout) arriving from the air. Returns why it was not taken, or "taken"
        public string AirIn(string hex)
        {
            byte[] w;
            try
            {
                w = FromHex(hex);
            }
            catch(FormatException)
            {
                return Drop("bad-hex", null);
            }
            var rec = AirRecord.Decode(w);
            if(rec == null)
            {
                return Drop("bad-record", null);
            }
            return Receive(rec);
        }

        // The next AIR record the chip sent (hex), or "" when there is none
        public string TakeAirOut()
        {
            if(airOut.Count == 0)
            {
                return "";
            }
            return airOut.Dequeue();
        }

        public int AirOutCount => airOut.Count;

        public byte PeekRegister(int page, int addr)
        {
            return Read(page & 7, addr);
        }

        public bool Decoding => decoder;
        public bool Running => running;
        public ulong SlotStartUs => curSlotStart;

        public void Trace(string line)
        {
            trace?.WriteLine("t={0} {1}", NowUs(), line);
        }

        // --- GPIO: the configuration port, power and the voice chip select ---

        public void OnGPIO(int number, bool value)
        {
            switch(number)
            {
            case InCs:
                if(!value && cs)
                {
                    BeginFrame();
                }
                else if(value && !cs)
                {
                    EndFrame();
                }
                cs = value;
                break;
            case InClk:
                if(!value && clk && !cs)
                {
                    ClockFalling();
                }
                clk = value;
                break;
            case InMosi:
                mosi = value;
                break;
            case InPwd:
                if(value != !powered)
                {
                    powered = !value;
                    Trace(powered ? "POWER on" : "POWER off");
                    if(!powered)
                    {
                        Reset();
                    }
                }
                break;
            case InVoiceCs:
                if(!value && voiceCs)
                {
                    voiceFrame = 0;
                    voiceBytes.Clear();
                }
                else if(value && !voiceCs)
                {
                    EndVoiceFrame();
                }
                voiceCs = value;
                break;
            default:
                this.Log(LogLevel.Warning, "HR_C5000: no input {0}", number);
                break;
            }
        }

        private void BeginFrame()
        {
            InitLines();
            frameActive = powered;
            bitCount = 0;
            shift = 0;
            frameBytes.Clear();
            frameData.Clear();
            Miso.Set(true);
        }

        private void ClockFalling()
        {
            if(!frameActive)
            {
                return;
            }
            int byteIndex = bitCount / 8, bit = bitCount % 8;
            // MISO for this bit: data bytes of a read, MSB first; 1 elsewhere (the line idles high)
            bool outBit = true;
            if(frameRead && byteIndex >= headerLen && frameBytes.Count >= headerLen)
            {
                if(bit == 0)
                {
                    outByte = Read(framePage, frameAddr);
                    frameData.Add(outByte);
                    frameAddr = NextAddr(framePage, frameAddr);
                }
                outBit = ((outByte >> (7 - bit)) & 1) != 0;
            }
            DriveMiso(outBit);
            shift = (byte)((shift << 1) | (mosi ? 1 : 0));
            bitCount++;
            if(bitCount % 8 == 0)
            {
                TakeByte(shift);
            }
        }

        private void TakeByte(byte b)
        {
            int n = frameBytes.Count;
            frameBytes.Add(b);
            if(n == 0)
            {
                frameRead = (b & 0x80) != 0;
                headerLen = (b & 0x40) != 0 ? 3 : 2;
                framePage = b & 7;
                return;
            }
            if(n == 1)
            {
                frameAddr = b;
                frameStart = b;
                return;
            }
            if(n == 2 && headerLen == 3)
            {
                frameAddr |= (b & 7) << 8;
                frameStart = frameAddr;
                return;
            }
            if(!frameRead)
            {
                frameData.Add(b);
                Write(framePage, frameAddr, b);
                frameAddr = NextAddr(framePage, frameAddr);
            }
        }

        private void EndFrame()
        {
            if(!frameActive)
            {
                return;
            }
            frameActive = false;
            Miso.Set(true);
            if(bitCount == 0)
            {
                return;     // chip select only (the pin set-up at boot), no transaction
            }
            if(bitCount % 8 != 0 || frameBytes.Count < headerLen)
            {
                Trace(string.Format("CFG BAD bits={0}", bitCount));
                this.Log(LogLevel.Warning, "HR_C5000: config frame of {0} bits", bitCount);
                return;
            }
            Trace(string.Format("CFG {0} p{1} {2:X3} [{3}] {4}", frameRead ? "R" : "W", framePage, frameStart,
                                frameData.Count, Hex(frameData.ToArray())));
        }

        private static int NextAddr(int page, int addr)
        {
            return page == 0 ? (addr + 1) & 0xFF : (addr + 1) & 0x7FF;
        }

        // --- SPI2: the voice port ---

        public byte Transmit(byte data)
        {
            if(voiceCs || !powered || voiceFrame < 0)
            {
                return 0xFF;
            }
            byte ret = 0xFF;
            if(voiceFrame == 0)
            {
                voiceCmd = data;
            }
            else if(voiceFrame >= 2)
            {
                int i = (voiceFrame - 2) % VoiceRam;
                if((voiceCmd & 0x80) == 0)
                {
                    voiceTx[i] = data;
                }
                else
                {
                    ret = voiceRx[i];
                }
                voiceBytes.Add((voiceCmd & 0x80) == 0 ? data : ret);
            }
            voiceFrame++;
            return ret;
        }

        public void FinishTransmission()
        {
            // The chip select GPIO frames a voice access, not the SPI controller
        }

        private void EndVoiceFrame()
        {
            if(voiceFrame >= 0)
            {
                Trace(string.Format("VOICE {0} [{1}] {2}", (voiceCmd & 0x80) == 0 ? "W" : "R", voiceBytes.Count,
                                    Hex(voiceBytes.ToArray())));
            }
            voiceFrame = -1;
        }

        // --- Register file ---

        private byte Read(int page, int addr)
        {
            switch(page)
            {
            case 0:
                switch(addr)
                {
                case 0x42: return SlotStatus();
                case 0x43: case 0x44: return 0;          // RSSI: stub
                case 0x51: return r51;
                case 0x52: return r52;
                case 0x82: return flags;
                case 0x84: case 0x86: case 0x90: case 0x98: return 0;
                default: return page0[addr & 0xFF];
                }
            case 1: return page1[addr & 0x7FF];
            case 2: return ramRx[addr & 0x7FF];
            case 3: return voiceRx[addr % VoiceRam];
            default: return 0xFF;
            }
        }

        private void Write(int page, int addr, byte v)
        {
            switch(page)
            {
            case 0:
                page0[addr & 0xFF] = v;
                WriteSys(addr & 0xFF, v);
                break;
            case 1:
                page1[addr & 0x7FF] = v;
                break;
            case 2:
                ramTx[addr & 0x7FF] = v;
                if(addr < 9)
                {
                    ramTxWritten = true;
                }
                break;
            case 3:
                voiceTx[addr % VoiceRam] = v;    // the vocoder flush (hrc_flush_vocoder): 128 bytes wrap
                break;
            default:
                break;  // CMX638 / AMBE3000 configuration: stub
            }
        }

        private void WriteSys(int reg, byte v)
        {
            switch(reg)
            {
            case 0x10:  // ModulatorMode bit 7: 1 FM, 0 DMR
            case 0x40:  // TxEn 7, RxEn 6, MasterMode 5 (active timing), CRC_MCU_Control 3
                EvaluateClock();
                break;
            case 0x41:  // TxNxtSlotEn 7, RxNxtSlotEn 6, SyncFail 5, CC_Match_Ctrl 3
                if((v & 0x20) != 0)
                {
                    synced = false;   // drop the sync: the next burst heard sets the phase again
                }
                break;
            case 0x22:  // DecodeStart 5, DecodeStop 4 (manual)
                if((v & 0x10) != 0)
                {
                    SetDecoder(false);
                }
                else if((v & 0x20) != 0)
                {
                    SetDecoder(true);
                }
                break;
            case 0x81:  // InterClass1Mask: 1 enables the flag onto the pin (modem-hrc6000.md 4.2)
                UpdateSysLine();
                break;
            case 0x83:  // InterClear: write 1 to clear
                flags = (byte)(flags & ~v);
                UpdateSysLine();
                break;
            default:
                break;
            }
        }

        private void SetDecoder(bool on)
        {
            if(on != decoder)
            {
                decoder = on;
                Trace(on ? "DECODE start" : "DECODE stop");
            }
        }

        private byte SlotStatus()
        {
            // 0x42 at the timeslot interrupt: what the slot now starting is (TransSlot 7, RecvSlot 6, UsedSlot 5)
            bool rx = !curTx && (curAction & 0x40) != 0;
            return (byte)((curTx ? 0x80 : 0) | (rx ? 0x40 : 0) | (curTx || rx ? 0x20 : 0));
        }

        private void UpdateSysLine()
        {
            SysInt.Set((flags & page0[0x81]) == 0);
        }

        // --- Slot clock ---

        private bool DmrEnabled => powered && (page0[0x10] & 0x80) == 0 && (page0[0x40] & 0xC0) != 0;
        private bool Active => (page0[0x40] & 0x20) != 0;

        private void EvaluateClock()
        {
            if(!DmrEnabled)
            {
                if(running)
                {
                    Trace("CLOCK stop");
                }
                running = false;
                synced = false;
                generation++;
                pendingTx = null;
                curTx = false;
                return;
            }
            if(!running && Active)
            {
                // Active timing: the chip makes its own slots, the first boundary one slot from now
                StartClock(NowUs(), "active");
            }
        }

        private void StartClock(ulong slotStart, string why)
        {
            running = true;
            synced = true;
            generation++;
            curSlotStart = slotStart;
            curAction = page0[0x41];
            curTx = false;
            pendingTx = null;
            Trace(string.Format("CLOCK {0} slot={1}", why, slotStart));
            ScheduleBoundary(slotStart + SlotUs);
        }

        private void ScheduleBoundary(ulong at)
        {
            uint gen = generation;
            ulong now = NowUs();
            nextSlotStart = at;
            ulong lead = Math.Min(RfTxLeadUs, SlotUs - 1);
            if(at - lead > now)
            {
                At(at - lead, () => { if(gen == generation) { TxLead(at); } });
            }
            At(at, () => { if(gen == generation) { Boundary(at); } });
        }

        // Before a slot that 0x41 asks to send in: the burst is set up now, the RF TX line pulses
        private void TxLead(ulong slotStart)
        {
            if((page0[0x41] & 0x80) == 0 || (page0[0x40] & 0x80) == 0)
            {
                return;
            }
            pendingTx = BuildTx(slotStart);
            if(pendingTx != null)
            {
                Pulse(RfTxInt);
                Trace("RFTX");
            }
        }

        private void Boundary(ulong at)
        {
            ulong ended = curSlotStart;
            curSlotStart = at;
            slotIndex++;
            // The slot that just ended: a burst received in it
            if(rxPending != null && rxPendingStart == ended)
            {
                CompleteRx(rxPending, at);
            }
            rxPending = null;
            if(headerSeen || lateGiven)
            {
                if(slotIndex - lastRxSlot > 13)    // RX_LOST_TICKS: the reception is over
                {
                    headerSeen = false;
                    lateGiven = false;
                }
            }
            // The slot starting now
            curAction = page0[0x41];
            curTx = pendingTx != null;
            if(curTx)
            {
                var hex = Hex(pendingTx.Encode());
                airOut.Enqueue(hex);
                Trace("AIR_OUT " + hex);
                pendingTx = null;
            }
            Trace(string.Format("TICK slot={0} ctl={1:X2} r42={2:X2}", slotIndex, curAction, SlotStatus()));
            Pulse(TsInt);
            ScheduleBoundary(at + SlotUs);
        }

        // --- TX ---

        private AirRecord BuildTx(ulong slotStart)
        {
            byte type = page0[0x50];
            bool voice = (type & 0x08) != 0;
            int hi = type >> 4;
            var r = new AirRecord { TUs = slotStart, Cc = (sbyte)(page0[0x1F] >> 4) };
            int family = SyncFamily();
            if(voice)
            {
                if(hi > 5)
                {
                    Trace(string.Format("TX BAD 0x50={0:X2}", type));
                    return null;
                }
                r.Kind = KindVoice;
                r.VoiceIndex = (byte)hi;
                if(hi == 0)
                {
                    // A new superframe: the EMB and embedded LC of B-E come from the LC page 2 holds now
                    Array.Copy(ramTx, 0, embLc, 0, 9);
                    embValid = ramTxWritten;
                }
                r.Sync = hi == 0 ? (byte)family : SyncNone;
                r.Payload = Slice(voiceTx, 0, 27);
                if(embValid)
                {
                    r.Flags |= FlagEmbValid;
                    Array.Copy(embLc, r.Lc, 9);
                }
            }
            else
            {
                if(hi > 0x0B)
                {
                    Trace(string.Format("TX BAD 0x50={0:X2}", type));   // the manual's custom frames: stub
                    return null;
                }
                r.Kind = (byte)hi;
                r.Sync = (byte)family;
                r.Payload = TxPayload(hi);
            }
            r.Ts = (byte)(family == SyncTs1 ? 1 : family == SyncTs2 ? 2 : 0);
            if((page0[0x40] & 0x08) != 0)
            {
                r.Flags |= FlagHostCrc;
            }
            return r;
        }

        private byte[] TxPayload(int kind)
        {
            switch(kind)
            {
            case 1: return LcFull(Slice(ramTx, 0, 9), 0x96);        // voice LC header: RS(12,9) by the chip
            case 2: return LcFull(Slice(ramTx, 0, 9), 0x99);        // terminator with LC
            case 0: return Crc16(Slice(ramTx, 0, 12), 0x6969);      // PI header
            case 3: return Crc16(Slice(ramTx, 0, 12), 0xA5A5);      // CSBK
            case 4: return Crc16(Slice(ramTx, 0, 12), 0xAAAA);      // MBC header
            case 6: return Crc16(Slice(ramTx, 0, 12), 0xCCCC);      // data header
            case 8: return Slice(ramTx, 0, 18);                     // rate 3/4
            case 10: return Slice(ramTx, 0, 24);                    // rate 1
            case 9: return Slice(ramTx, 0x18, 12);                  // idle: its own place (hrc6000_dmr.c setup_burst)
            default: return Slice(ramTx, 0, 12);                    // MBC continuation, rate 1/2, USBD
            }
        }

        // Page 1 0x04-0x09 hold the data sync pattern of the family the MCU chose (hrc6000_dmr_set_sync)
        private int SyncFamily()
        {
            for(int i = 0; i < DataSync.Length; i++)
            {
                bool same = true;
                for(int j = 0; j < 6 && same; j++)
                {
                    same = page1[4 + j] == DataSync[i][j];
                }
                if(same)
                {
                    return i + 1;   // dmr_sync_family_t: MS 1, BS 2, TS1 3, TS2 4
                }
            }
            return SyncMs;
        }

        // --- RX ---

        private string Receive(AirRecord rec)
        {
            ulong now = NowUs();
            ulong t = rec.TUs;
            if(t + SlotUs <= now)
            {
                return Drop("late", rec);
            }
            if(t > now)
            {
                return Drop("early", rec);
            }
            if(!DmrEnabled || (page0[0x40] & 0x40) == 0)
            {
                return Drop("deaf", rec);
            }
            if(!running)
            {
                if(Active)
                {
                    return Drop("deaf", rec);
                }
                StartClock(t, "passive");
            }
            else
            {
                long off = (long)t - (long)curSlotStart;
                if(Math.Abs(off) > 1250)
                {
                    if(Active || curTx || pendingTx != null)
                    {
                        return Drop("off-grid", rec);
                    }
                    // Passive timing follows the burst: its slot is the one now running
                    Trace(string.Format("CLOCK rephase off={0}", off));
                    StartClock(t, "rephase");
                }
                else if(!synced && !Active)
                {
                    synced = true;
                }
            }
            if(curTx || (curAction & 0x40) == 0)
            {
                return Drop("closed", rec);
            }
            bool voiceA = rec.Kind == KindVoice && rec.VoiceIndex == 0;
            int cc = page0[0x1F] >> 4;
            if(!voiceA && rec.Cc >= 0 && rec.Cc != cc && (curAction & 0x08) == 0)
            {
                return Drop("cc", rec);
            }
            if(rxPending != null)
            {
                return Drop("second", rec);
            }
            rxPending = rec;
            rxPendingStart = curSlotStart;
            Trace(string.Format("AIR_IN taken kind={0:X2} idx={1} start={2}", rec.Kind, rec.VoiceIndex, rec.TUs));
            return "taken";
        }

        private string Drop(string why, AirRecord rec)
        {
            Trace(string.Format("AIR_IN drop={0}{1}", why, rec == null ? "" : string.Format(" kind={0:X2} start={1}", rec.Kind, rec.TUs)));
            return why;
        }

        // At the tick that ends the burst's slot
        private void CompleteRx(AirRecord rec, ulong tick)
        {
            lastRxSlot = slotIndex;
            bool corrupt = (rec.Flags & FlagCorrupt) != 0;
            byte addFlags = 0;
            byte newR51 = r51;
            byte[] page2 = null;
            if(rec.Kind == KindVoice)
            {
                if(decoder)
                {
                    var v = corrupt ? Noise(rec.TUs) : rec.Payload;
                    Array.Clear(voiceRx, 0, voiceRx.Length);
                    Array.Copy(v, voiceRx, Math.Min(v.Length, 27));
                    Trace("VOICE_RX " + Hex(Slice(voiceRx, 0, 27)));
                }
                if(rec.VoiceIndex == 0)
                {
                    addFlags |= 0x04;     // information: the voice sync report (hardware-md380.md 4.8)
                    newR51 = 0x01;        // sync class 1, voice
                }
                else if(rec.VoiceIndex == 5 && (rec.Flags & FlagEmbValid) != 0 && !corrupt && !headerSeen && !lateGiven)
                {
                    addFlags |= 0x10;     // late entry: the call's LC from its embedded signalling
                    page2 = Slice(rec.Lc, 0, 9);
                    lateGiven = true;
                }
            }
            else
            {
                byte[] p = rec.Payload;
                if((rec.Kind == 1 || rec.Kind == 2) && p.Length == 9)
                {
                    p = LcFull(p, rec.Kind == 1 ? (byte)0x96 : (byte)0x99);
                }
                page2 = p;
                addFlags |= 0x08;
                newR51 = (byte)((rec.Kind << 4) | (corrupt ? 0x04 : 0) | 0x02);
                if(rec.Kind == 1 && !corrupt)
                {
                    headerSeen = true;
                }
                else if(rec.Kind == 2)
                {
                    headerSeen = false;
                    lateGiven = false;
                }
            }
            byte newR52 = r52;
            if(rec.Cc >= 0 && !(rec.Kind == KindVoice && rec.VoiceIndex == 0))
            {
                newR52 = (byte)((newR52 & 0x0F) | (rec.Cc << 4));
            }
            if(rec.Ts == 1)
            {
                newR52 |= 0x04;
            }
            else if(rec.Ts == 2)
            {
                newR52 = (byte)(newR52 & ~0x04);
            }
            if(addFlags == 0)
            {
                r52 = newR52;
                return;
            }
            uint gen = generation;
            At(tick + RxReportDelayUs, () =>
            {
                if(!powered)
                {
                    return;
                }
                if(page2 != null)
                {
                    Array.Copy(page2, ramRx, page2.Length);
                }
                r51 = newR51;
                r52 = newR52;
                flags |= addFlags;
                Trace(string.Format("REPORT flags={0:X2} r51={1:X2} r52={2:X2}{3}", flags, r51, r52,
                                    page2 == null ? "" : " page2=" + Hex(page2)));
                UpdateSysLine();
            });
        }

        // A corrupted voice burst: bits that are no voice (deterministic, from the burst's start)
        private static byte[] Noise(ulong seed)
        {
            var b = new byte[27];
            ulong x = seed * 6364136223846793005UL + 1442695040888963407UL;
            for(int i = 0; i < b.Length; i++)
            {
                x = x * 6364136223846793005UL + 1442695040888963407UL;
                b[i] = (byte)(x >> 56);
            }
            return b;
        }

        // --- FEC the chip does for the MCU (ETSI TS 102 361-1 B.3; the same as core/dmr/call/dmr_lc.c) ---

        private static byte GfMul(byte a, byte b)
        {
            byte p = 0;
            while(b != 0)
            {
                if((b & 1) != 0)
                {
                    p ^= a;
                }
                a = (byte)((a << 1) ^ ((a & 0x80) != 0 ? 0x1D : 0));
                b >>= 1;
            }
            return p;
        }

        // LC + RS(12,9) parity (generator x^3 + 0x0E x^2 + 0x38 x + 0x40 over GF(256), 0x11D) xor the mask
        public static byte[] LcFull(byte[] lc, byte mask)
        {
            byte r2 = 0, r1 = 0, r0 = 0;
            for(int i = 0; i < 9; i++)
            {
                byte fb = (byte)(lc[i] ^ r2);
                r2 = (byte)(r1 ^ GfMul(fb, 0x0E));
                r1 = (byte)(r0 ^ GfMul(fb, 0x38));
                r0 = GfMul(fb, 0x40);
            }
            var o = new byte[12];
            Array.Copy(lc, o, 9);
            o[9] = (byte)(r2 ^ mask);
            o[10] = (byte)(r1 ^ mask);
            o[11] = (byte)(r0 ^ mask);
            return o;
        }

        // The first 10 bytes + CRC-CCITT (inverted) xor the mask, MSB first
        public static byte[] Crc16(byte[] buf, ushort mask)
        {
            ushort crc = 0;
            for(int i = 0; i < 10; i++)
            {
                crc ^= (ushort)(buf[i] << 8);
                for(int b = 0; b < 8; b++)
                {
                    crc = (crc & 0x8000) != 0 ? (ushort)((crc << 1) ^ 0x1021) : (ushort)(crc << 1);
                }
            }
            crc = (ushort)(~crc ^ mask);
            var o = new byte[12];
            Array.Copy(buf, o, 10);
            o[10] = (byte)(crc >> 8);
            o[11] = (byte)crc;
            return o;
        }

        // --- Helpers ---

        private ulong NowUs()
        {
            return (ulong)Math.Round(machine.LocalTimeSource.ElapsedVirtualTime.TotalMicroseconds);
        }

        private void At(ulong when, Action a)
        {
            ulong now = NowUs();
            ulong delay = when > now ? when - now : 0;
            machine.ScheduleAction(TimeInterval.FromMicroseconds(delay), _ => a(), "hrc5000");
        }

        private void Pulse(GPIO line)
        {
            line.Set(false);
            uint gen = generation;
            At(NowUs() + PulseUs, () => line.Set(true));
        }

        private static byte[] Slice(byte[] a, int from, int n)
        {
            var o = new byte[n];
            Array.Copy(a, from, o, 0, n);
            return o;
        }

        public static string Hex(byte[] b)
        {
            var s = new StringBuilder(b.Length * 2);
            foreach(var x in b)
            {
                s.Append(x.ToString("X2"));
            }
            return s.ToString();
        }

        public static byte[] FromHex(string h)
        {
            h = h.Replace(" ", "");
            if(h.Length % 2 != 0)
            {
                throw new FormatException();
            }
            var o = new byte[h.Length / 2];
            for(int i = 0; i < o.Length; i++)
            {
                o[i] = Convert.ToByte(h.Substring(2 * i, 2), 16);
            }
            return o;
        }

        // The AIR record (boards/vhost/dmr_air_vchan.c, tools/conductor/parts/air.py RECORD = "<BBQbBBB9sB")
        public class AirRecord
        {
            public byte Ts;
            public byte Kind;
            public ulong TUs;
            public sbyte Cc;
            public byte VoiceIndex;
            public byte Sync;
            public byte Flags;
            public byte[] Lc = new byte[9];
            public byte[] Payload = new byte[0];

            public byte[] Encode()
            {
                var w = new byte[RecordPrefix + Payload.Length];
                w[0] = Ts;
                w[1] = Kind;
                for(int i = 0; i < 8; i++)
                {
                    w[2 + i] = (byte)(TUs >> (8 * i));
                }
                w[10] = (byte)Cc;
                w[11] = VoiceIndex;
                w[12] = Sync;
                w[13] = Flags;
                Array.Copy(Lc, 0, w, 14, 9);
                w[23] = (byte)Payload.Length;
                Array.Copy(Payload, 0, w, RecordPrefix, Payload.Length);
                return w;
            }

            // The checks of air.py Record.decode; null when the record is not valid
            public static AirRecord Decode(byte[] w)
            {
                if(w.Length < RecordPrefix || w.Length != RecordPrefix + w[23] || w[23] > 27)
                {
                    return null;
                }
                var r = new AirRecord
                {
                    Ts = w[0], Kind = w[1], Cc = (sbyte)w[10], VoiceIndex = w[11], Sync = w[12], Flags = w[13],
                };
                for(int i = 0; i < 8; i++)
                {
                    r.TUs |= (ulong)w[2 + i] << (8 * i);
                }
                Array.Copy(w, 14, r.Lc, 0, 9);
                r.Payload = new byte[w[23]];
                Array.Copy(w, RecordPrefix, r.Payload, 0, r.Payload.Length);
                bool kindOk = r.Kind <= 0x0B || r.Kind == KindVoice;
                if(r.Ts > 2 || !kindOk || r.Cc < -1 || r.Cc > 15 || r.VoiceIndex > 5 || r.Sync > 4 || (r.Flags & ~7) != 0)
                {
                    return null;
                }
                if(r.Kind == KindVoice && r.Payload.Length != 27)
                {
                    return null;
                }
                return r;
            }
        }

        public const byte KindVoice = 0x10;     // hal_dmr_modem.h DMR_BURST_VOICE
        private const byte SyncNone = 0, SyncMs = 1, SyncTs1 = 3, SyncTs2 = 4;
        private const byte FlagHostCrc = 1, FlagEmbValid = 2, FlagCorrupt = 4;
        private const int VoiceRam = 32;

        // Data sync patterns by family, as hrc6000_dmr.c sync_patterns writes them to page 1 0x04: MS, BS, TS1, TS2
        private static readonly byte[][] DataSync =
        {
            new byte[] { 0xD5, 0xD7, 0xF7, 0x7F, 0xD7, 0x57 },
            new byte[] { 0xDF, 0xF5, 0x7D, 0x75, 0xDF, 0x5D },
            new byte[] { 0xF7, 0xFD, 0xD5, 0xDD, 0xFD, 0x55 },
            new byte[] { 0xD7, 0x55, 0x7F, 0x5F, 0xF7, 0xF5 },
        };

        // The user manual's reset values (section 2 register table, hardware-md380.md 4.7)
        private static readonly Dictionary<int, byte> ResetValues = new Dictionary<int, byte>
        {
            { 0x06, 0x40 }, { 0x07, 0x0B }, { 0x08, 0xD9 }, { 0x09, 0x54 }, { 0x0A, 0x02 }, { 0x0B, 0x0A },
            { 0x0C, 0x82 }, { 0x0D, 0xC8 }, { 0x0E, 0x40 }, { 0x0F, 0xB8 }, { 0x11, 0x80 }, { 0x14, 0x01 },
            { 0x17, 0x01 }, { 0x1A, 0x01 }, { 0x1F, 0x10 }, { 0x20, 0xAA }, { 0x21, 0xA0 }, { 0x34, 0xF0 },
            { 0x35, 0xA0 }, { 0x61, 0x03 },
        };

        private readonly IMachine machine;
        private readonly byte[] page0 = new byte[256];
        private readonly byte[] page1 = new byte[2048];
        private readonly byte[] ramTx = new byte[2048];
        private readonly byte[] ramRx = new byte[2048];
        private readonly byte[] voiceTx = new byte[VoiceRam];
        private readonly byte[] voiceRx = new byte[VoiceRam];
        private readonly Queue<string> airOut = new Queue<string>();
        private readonly List<byte> frameBytes = new List<byte>();
        private readonly List<byte> frameData = new List<byte>();
        private readonly List<byte> voiceBytes = new List<byte>();
        private StreamWriter trace;
        private string tracePath;

        private bool powered = true;
        private bool linesUp;
        private bool cs, clk, mosi, voiceCs;
        private bool frameActive, frameRead;
        private int bitCount, headerLen, framePage, frameAddr, frameStart;
        private byte shift, outByte;
        private int voiceFrame;
        private byte voiceCmd;

        private byte flags, r51, r52;
        private bool decoder;
        private bool ramTxWritten;
        private byte[] embLc = new byte[9];
        private bool embValid;

        private bool running, synced;
        private uint generation;
        private ulong curSlotStart, nextSlotStart;
        private ulong slotIndex, lastRxSlot;
        private byte curAction;
        private bool curTx;
        private AirRecord pendingTx;
        private AirRecord rxPending;
        private ulong rxPendingStart;
        private bool headerSeen, lateGiven;
    }
}
