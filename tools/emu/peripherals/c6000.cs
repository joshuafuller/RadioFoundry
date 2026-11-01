// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
//
// A Renode model of the HR_C6000 DMR baseband chip, as the DM-1701 firmware drives it. Written from this
// repository's own driver and specs only: drivers/modem/hrc6000, drivers/modem/hrc_common (hrc.c, hrc6000_dmr.c),
// platform/stm32f4/hrc/hrc_bus.c, boards/uv380_family/radio_uvf.c, docs/spec/hardware-dm1701.md 2 and 4,
// docs/spec/modem-hrc6000.md and docs/spec/dmr-modem.md. It started as a copy of the HR_C5000 model (c5000.cs, the same
// chip family) and keeps its burst-level design: it never makes symbols or FEC. What a burst carries on the air is an
// AIR record (boards/vhost/dmr_air_vchan.c, tools/conductor/parts/air.py).
//
// What differs from the HR_C5000 model, and why
//   Pages: the system registers are page 4 (hrc6000.c chip.cfg_page = 4). Page 0 is not used by the driver: a stub.
//   Configuration port clock idles HIGH (hardware-dm1701.md 4.2, hrc_bus.c cfg_byte): the MCU sets MOSI, drives CLK low,
//     reads MISO, drives CLK high. The model takes the MOSI bit at the falling edge (MOSI is already set; the chip
//     latches it on the rising edge, before MOSI changes again) and puts out each MISO bit there. MISO (PE5) is on the
//     clock's own port, where a level set from inside the port's write does not stay, so the model sets MISO again
//     just before each read of that port's input register (a watchpoint; InstallMisoHook, misoPort in the .repl).
//   CRC_MCU_Control (0x40 bit 3, modem-hrc6000.md 6.1): set, the chip sends the 12 bytes of page 2 as written; clear,
//     it computes the RS(12,9) parity of an LC and the CRC-16 of CSBK / MBC / data / PI headers itself.
//   Receive reports (modem-hrc6000.md 4.2, 5.2; hrc6000_dmr.c hrc6000_dmr_decode): every burst taken is reported with
//     0x82 bit 3 (received data), voice too. 0x51: data = type << 4 | CRC error << 2 | class 2; voice burst n (0..5 for
//     A..F) = (8 | (n + 1)) << 4 | class (1 for A, voice sync; 0 for B-F). 0x5F bits 1-0: the sync type received
//     (0 MS, 1 BS, 2 TS1, 3 TS2). The report comes RxReportDelayUs (4 ms) after the tick that ends the burst's slot:
//     the chip's normal 3-5 ms (modem-hrc6000.md 4.2). The voice RAM gets the burst's 27 bytes at that report; there is
//     no decoder gate (the HR_C6000 driver has no rx_decode). Burst F with a valid embedded LC puts that LC in page 2
//     (lc_read reads it at F), and in a reception that had no header also raises bit 4 (post access, late entry).
//   0x52 bit 2 (TC): the slot of the burst received, 1 = TS2 (modem-hrc6000.md 4.1, 5.3: the driver reads it
//     as the slot that just ended, HRC6000_DMR_TC_IS_SLOT_NOW 0).
//
// What it models (as in c5000.cs otherwise)
//   Configuration port: bit-banged SPI on CS PE2 (input 0), CLK PE3 (input 1), MOSI PE4 (input 2), MISO PE5 (output
//     Miso). Frames: cmd (bit 7 read, bit 6 an 11-bit address, bits 2-0 the page), address low, [address high], data
//     (hrc.c). Pages: 1 auxiliary (sync pattern at 0x04), 2 data RAM (writes go to the TX RAM, reads come from the RX
//     RAM), 3 vocoder, 4 system.
//   Voice port: SPI2 (this peripheral is registered on it), chip select PB12 (input 4). 0x03 / 0x83, 0x00, then 27 data
//     bytes (hrc_bus.c voice_xfer). 32-byte voice RAM: a longer access wraps.
//   Power: PWD PE6 (input 3), high = powered down: the chip forgets everything.
//   Interrupt lines, active low, idle high, on PC0 (TsInt, timeslot: a short low pulse every 30 ms), PC1 (SysInt: low
//     while a flag of 0x82 that 0x81 enables is set, until 0x83 clears it) and PC2 (RfTxInt: a short low pulse
//     RfTxLeadUs before each burst the chip sends).
//   Slot clock: runs in DMR (0x10 bit 7 clear) with TX or RX enabled (0x40 bits 7, 6). Active timing (0x40 bit 5): it
//     starts 30 ms after the write. Passive timing: it starts at the first burst received and follows received bursts.
//     At each slot start it latches what 0x41 says the slot does (bit 7 send, bit 6 receive) and fires the timeslot
//     pulse. 0x42 reads what the slot now starting is.
//   TX: at RfTxLeadUs before a slot whose 0x41 asks to send, it builds the burst from 0x50 (type, voice index), page 2
//     (LC / data) or the voice RAM, page 1 0x04 (the sync pattern, so the family) and 0x1F (the color code), pulses
//     the RF TX line, and at the slot start emits the AIR record.
//   RX: AirIn takes an AIR record no later than the last microsecond of its slot (the conductor hands records over at
//     start + 29999 us). A burst on an open receive slot with our color code (or 0x41 bit 3, or a voice burst A) is
//     taken and reported as above. AirInAfter (for tests) hands a record over inside its own slot.
//
// Stubs (accepted and stored, no behavior): FM modes, every audio, codec, I2S and tone register; page 0 and the pages
// above 4; the DTMF table; RSSI (0x43, 0x44 read 0); the access policy (0x20, 0x21: the chip never rejects a send, 0x82
// bit 7 never set); TX start / end interrupts (0x82 bits 6, 5; 0x84, 0x86 read 0); voice abnormal exit (bit 1, 0x98
// reads 0); physical-layer receive (bit 0); 0x90 reads 0; the layer 3 call control; privacy; reset bits of 0x00; the
// second (EMB) report of a voice burst the real chip makes (modem-hrc6000.md 4.2: one report per burst here); the
// 1.25 ms timing correction window (a burst off the grid re-phases the clock at once).
//
// The checks are c6000_test.py (the unchanged DM-1701 ELF in boards/dm1701/emu/platform.repl).
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.SPI;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.RadioFoundry
{
    public class Hrc6000 : ISPIPeripheral, IGPIOReceiver
    {
        // GPIO inputs (the MCU's output pins, wired in the .repl)
        public const int InCs = 0, InClk = 1, InMosi = 2, InPwd = 3, InVoiceCs = 4;

        public const ulong SlotUs = 30000;     // ETSI TS 102 361-1 4.2, dmr_vhost.h VDMR_SLOT_US
        public const int RecordPrefix = 24;    // dmr_air_vchan.c AIR_PREFIX, air.py RECORD.size

        // misoPort / misoPin: the GPIO port and pin the MCU reads MISO from, misoIdr the address of that port's input
        // data register (dm1701.repl: gpioPortE, 5, 0x40021010). See InstallMisoHook for why the model needs them
        public Hrc6000(IMachine machine, IGPIOReceiver misoPort = null, int misoPin = 0, ulong misoIdr = 0)
        {
            this.machine = machine;
            this.misoPort = misoPort;
            this.misoPin = misoPin;
            this.misoIdr = misoIdr;
            Miso = new GPIO();
            TsInt = new GPIO();
            SysInt = new GPIO();
            RfTxInt = new GPIO();
            RxReportDelayUs = 4000;
            RfTxLeadUs = 1000;
            PulseUs = 10;
            Reset();
            // The .repl connects the lines after the constructor ran, so the levels set in Reset never reached the GPIO
            // ports (Renode's GPIO passes on changes only). Drive them once the emulation runs, and at the first frame
            machine.ScheduleAction(TimeInterval.Empty, _ => InitLines(), "hrc6000");
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

        // On the DM-1701 MISO (PE5) is on the same port as the clock and MOSI it follows (PE3, PE4). A level this model
        // sets on the port from inside the port's own write (the clock edge that calls OnGPIO) does not stay: the port
        // ends that write with the input levels it had before it, so the MCU would read the old MISO (always 1, bench
        // probe 2026-11-01: a read of 0x0B gave 0xFF). So a watchpoint sets MISO again just before every read of the
        // port's input register, outside any write. A watchpoint, not a read hook: Renode keeps one read hook per
        // peripheral, and peripherals/keys.py has the one on this port (the keypad columns, which need the same fix)
        private void InstallMisoHook()
        {
            if(misoPort == null || misoHooked)
            {
                return;     // installed at the first frame: the port is not on the bus yet when the .repl builds this
            }
            misoHooked = true;
            machine.GetSystemBus((IBusPeripheral)misoPort).AddWatchpointHook(misoIdr, SysbusAccessWidth.DoubleWord,
                Access.Read, (cpu, address, width, value) => misoPort.OnGPIO(misoPin, misoLevel));
        }

        // MISO is forced at every bit: an ODR write to the port in Renode overwrites its input pins with the output
        // latch, and a plain Set of the level the GPIO already holds would not repair that
        private void DriveMiso(bool v)
        {
            misoLevel = v;
            Miso.Set(!v);
            Miso.Set(v);
        }

        public GPIO Miso { get; }
        public GPIO TsInt { get; }
        public GPIO SysInt { get; }
        public GPIO RfTxInt { get; }

        // The system interrupt for a received burst, this long after the tick that ends its slot. In step the HR_C6000
        // reports 3-5 ms after the timeslot interrupt (modem-hrc6000.md 4.2); 22 ms or more is out of step for the
        // engine (hrc6000_dmr.c OUT_OF_STEP_US), which then restarts the receiver
        public ulong RxReportDelayUs { get; set; }
        // The RF TX interrupt this long before a burst's slot starts ("shortly before", modem-hrc6000.md 4.3; the exact lead is not documented)
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
            Array.Clear(sys, 0, sys.Length);
            Array.Clear(page1, 0, page1.Length);
            Array.Clear(ramTx, 0, ramTx.Length);
            Array.Clear(ramRx, 0, ramRx.Length);
            Array.Clear(voiceTx, 0, voiceTx.Length);
            Array.Clear(voiceRx, 0, voiceRx.Length);
            // No reset table: the driver writes every register it relies on (hrc6000.c hrc6000_power_on_init)
            sys[0x10] = 0x80;     // FM, so nothing ticks before a set-up
            flags = 0;
            r51 = 0;
            r52 = 0;
            r5fSync = 0;
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
            // cs, clk, mosi and voiceCs are the MCU's wire levels, not chip state: they survive a power down (Renode
            // passes level changes only, so a reset tracker would miss the next edge)
            frameActive = false;
            voiceFrame = -1;
            airOut.Clear();
            misoLevel = true;
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

        // For tests: one AIR record whose slot starts delayUs from now. Its start time is rewritten to that moment and it
        // is handed over half a slot later, inside its own slot, as the conductor would. The result goes to the trace
        public void AirInAfter(ulong delayUs, string hex)
        {
            byte[] w;
            try
            {
                w = FromHex(hex);
            }
            catch(FormatException)
            {
                Drop("bad-hex", null);
                return;
            }
            var rec = AirRecord.Decode(w);
            if(rec == null)
            {
                Drop("bad-record", null);
                return;
            }
            rec.TUs = NowUs() + delayUs;
            At(rec.TUs + SlotUs / 2, () => Receive(rec));
        }

        public ulong NowMicros => NowUs();

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
                this.Log(LogLevel.Warning, "HR_C6000: no input {0}", number);
                break;
            }
        }

        private void BeginFrame()
        {
            InitLines();
            InstallMisoHook();
            frameActive = powered;
            bitCount = 0;
            shift = 0;
            frameBytes.Clear();
            frameData.Clear();
            outByteIndex = -1;
            misoLevel = true;
            Miso.Set(true);
        }

        private void ClockFalling()
        {
            if(!frameActive)
            {
                return;
            }
            DriveMiso(OutBit());
            shift = (byte)((shift << 1) | (mosi ? 1 : 0));
            bitCount++;
            if(bitCount % 8 == 0)
            {
                TakeByte(shift);
            }
        }

        // MISO for bit bitCount: data bytes of a read, MSB first; 1 elsewhere (the line idles high). A data byte is read
        // from the register file once, when its first bit is first asked for
        private bool OutBit()
        {
            int byteIndex = bitCount / 8, bit = bitCount % 8;
            if(!frameRead || byteIndex < headerLen || frameBytes.Count < headerLen)
            {
                return true;
            }
            if(byteIndex != outByteIndex)
            {
                outByteIndex = byteIndex;
                outByte = Read(framePage, frameAddr);
                frameData.Add(outByte);
                frameAddr = NextAddr(framePage, frameAddr);
            }
            return ((outByte >> (7 - bit)) & 1) != 0;
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
            misoLevel = true;
            Miso.Set(true);
            if(bitCount == 0)
            {
                return;     // chip select only (the pin set-up at boot), no transaction
            }
            if(bitCount % 8 != 0 || frameBytes.Count < headerLen)
            {
                Trace(string.Format("CFG BAD bits={0}", bitCount));
                this.Log(LogLevel.Warning, "HR_C6000: config frame of {0} bits", bitCount);
                return;
            }
            Trace(string.Format("CFG {0} p{1} {2:X3} [{3}] {4}", frameRead ? "R" : "W", framePage, frameStart,
                                frameData.Count, Hex(frameData.ToArray())));
        }

        private static int NextAddr(int page, int addr)
        {
            return page == SysPage ? (addr + 1) & 0xFF : (addr + 1) & 0x7FF;
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
            case SysPage:
                switch(addr & 0xFF)
                {
                case 0x42: return SlotStatus();
                case 0x43: case 0x44: return 0;          // RSSI: stub
                case 0x51: return r51;
                case 0x52: return r52;
                case 0x5F: return (byte)((sys[0x5F] & 0xF0) | r5fSync);   // detect enables 7-4, sync type 1-0
                case 0x82: return flags;
                case 0x84: case 0x86: case 0x90: case 0x98: return 0;
                default: return sys[addr & 0xFF];
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
            case SysPage:
                sys[addr & 0xFF] = v;
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
                break;  // page 0 and pages 5-7: not used by the driver, stub
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

        private byte SlotStatus()
        {
            // 0x42 at the timeslot interrupt: what the slot now starting is (TransSlot 7, RecvSlot 6, UsedSlot 5)
            bool rx = !curTx && (curAction & 0x40) != 0;
            return (byte)((curTx ? 0x80 : 0) | (rx ? 0x40 : 0) | (curTx || rx ? 0x20 : 0));
        }

        private void UpdateSysLine()
        {
            SysInt.Set((flags & sys[0x81]) == 0);
        }

        // --- Slot clock ---

        private bool DmrEnabled => powered && (sys[0x10] & 0x80) == 0 && (sys[0x40] & 0xC0) != 0;
        private bool Active => (sys[0x40] & 0x20) != 0;

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
            curAction = sys[0x41];
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
            if((sys[0x41] & 0x80) == 0 || (sys[0x40] & 0x80) == 0)
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
            curAction = sys[0x41];
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
            byte type = sys[0x50];
            bool voice = (type & 0x08) != 0;
            int hi = type >> 4;
            var r = new AirRecord { TUs = slotStart, Cc = (sbyte)(sys[0x1F] >> 4) };
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
            if(McuCrc && !voice && hi != 9)
            {
                r.Flags |= FlagHostCrc;
            }
            return r;
        }

        // CRC_MCU_Control (0x40 bit 3): the MCU supplies the CRC, the 96 info bits go as written (modem-hrc6000.md 6.1)
        private bool McuCrc => (sys[0x40] & 0x08) != 0;

        private byte[] TxPayload(int kind)
        {
            if(McuCrc && kind != 8 && kind != 9 && kind != 10)
            {
                return Slice(ramTx, 0, 12);
            }
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
            if(!DmrEnabled || (sys[0x40] & 0x40) == 0)
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
            int cc = sys[0x1F] >> 4;
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

        // At the tick that ends the burst's slot: the report comes RxReportDelayUs later (modem-hrc6000.md 4.2, 5.2)
        private void CompleteRx(AirRecord rec, ulong tick)
        {
            lastRxSlot = slotIndex;
            bool corrupt = (rec.Flags & FlagCorrupt) != 0;
            byte addFlags = 0x08;      // received data, for every burst (the mask 0x81 = 0x19 lets bits 4, 3, 0 through)
            byte newR51;
            byte[] page2 = null;
            byte[] voice = null;
            if(rec.Kind == KindVoice)
            {
                // Voice burst n: bit 7 set, bits 6-4 n + 1, class 1 (voice sync) for A, 0 for B-F (hrc6000_dmr_decode)
                int n = rec.VoiceIndex;
                newR51 = (byte)(((0x8 | (n + 1)) << 4) | (n == 0 ? 1 : 0) | (corrupt ? 0x04 : 0));
                voice = corrupt ? Noise(rec.TUs) : rec.Payload;
                bool emb = (rec.Flags & FlagEmbValid) != 0 && !corrupt;
                if(n == 5 && emb)
                {
                    // The embedded LC of B-E, assembled by F: page 2 from 0x00 (modem-hrc6000.md 5.2)
                    page2 = LcFull(Slice(rec.Lc, 0, 9), 0x96);
                    if(!headerSeen && !lateGiven)
                    {
                        addFlags |= 0x10;     // post access: a call in progress acquired from its embedded signaling
                        lateGiven = true;
                    }
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
            if(rec.Ts == 2)
            {
                newR52 |= 0x04;
            }
            else if(rec.Ts == 1)
            {
                newR52 = (byte)(newR52 & ~0x04);
            }
            // Recv_Sync_Type: AIR sync 1..4 (MS, BS, TS1, TS2) is 0..3; a voice burst B-F carries no sync, keep the last
            byte newSync = rec.Sync >= 1 && rec.Sync <= 4 ? (byte)(rec.Sync - 1) : r5fSync;
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
                if(voice != null)
                {
                    Array.Clear(voiceRx, 0, voiceRx.Length);
                    Array.Copy(voice, voiceRx, Math.Min(voice.Length, 27));
                }
                r51 = newR51;
                r52 = newR52;
                r5fSync = newSync;
                flags |= addFlags;
                Trace(string.Format("REPORT flags={0:X2} r51={1:X2} r52={2:X2} r5f={3}{4}", flags, r51, r52, r5fSync,
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
            machine.ScheduleAction(TimeInterval.FromMicroseconds(delay), _ => a(), "hrc6000");
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
        private const int SysPage = 4;           // hrc6000.c chip.cfg_page

        // Data sync patterns by family, as hrc6000_dmr.c sync_patterns writes them to page 1 0x04: MS, BS, TS1, TS2
        private static readonly byte[][] DataSync =
        {
            new byte[] { 0xD5, 0xD7, 0xF7, 0x7F, 0xD7, 0x57 },
            new byte[] { 0xDF, 0xF5, 0x7D, 0x75, 0xDF, 0x5D },
            new byte[] { 0xF7, 0xFD, 0xD5, 0xDD, 0xFD, 0x55 },
            new byte[] { 0xD7, 0x55, 0x7F, 0x5F, 0xF7, 0xF5 },
        };

        private readonly IMachine machine;
        private readonly IGPIOReceiver misoPort;
        private readonly int misoPin;
        private readonly ulong misoIdr;
        private bool misoLevel = true;
        private bool misoHooked;
        private readonly byte[] sys = new byte[256];
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
        private int outByteIndex = -1;
        private int bitCount, headerLen, framePage, frameAddr, frameStart;
        private byte shift, outByte;
        private int voiceFrame;
        private byte voiceCmd;

        private byte flags, r51, r52, r5fSync;
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
