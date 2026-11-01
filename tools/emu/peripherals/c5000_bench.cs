// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
//
// Test bench for the HR_C5000 model (c5000.cs): it stands in for the MCU and plays the register sequences our own
// firmware produces, through the STM32F4 GPIO, SPI2 and EXTI models, so the pins, the port protocol and the interrupt
// lines are all on the path. Each sequence is transcribed from the driver, with the C function it comes from:
//   the configuration port byte      boards/md380/rfbus_md380.c bus_byte, md380_rfbus_modem_transfer
//   the voice port frame             boards/md380/radio_md380.c voice_xfer, voice_spi_setup (mode 2, prescaler 32)
//   power-on and the mode set-ups    drivers/modem/hrc5000/hrc5000.c hrc5000_power_on, hrc5000_set_mode_dmr
//   configure, run, restart          drivers/modem/hrc_common/hrc6000_dmr.c hrc6000_dmr_configure, _run, do_restart
//   one burst set up                 hrc6000_dmr.c setup_burst with the HR_C5000 chip description (hrc5000.c chip)
//   the interrupts                   hrc6000_dmr.c hrc6000_dmr_isr_timeslot, sys_service, tr_begin / tr_tick
// The interrupt handlers are kept to what the chip sees (the registers read and written, in the engine's order), not
// the engine's call logic. The bench polls the EXTI pending register every PollUs of virtual time, as the NVIC would
// take the line (no NVIC or CPU code is involved).
using System;
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.RadioFoundry
{
    public class Hrc5000Bench : IDoubleWordPeripheral, IKnownSize
    {
        public Hrc5000Bench(IMachine machine, Hrc5000 chip)
        {
            this.machine = machine;
            this.chip = chip;
            PollUs = 50;
        }

        public long Size => 4;
        public ulong PollUs { get; set; }
        public uint ReadDoubleWord(long offset) { return 0; }
        public void WriteDoubleWord(long offset, uint value) { }
        public void Reset() { }

        // --- Scenarios (monitor commands) ---

        // Board and chip bring-up to DMR receive: radio_md380.c radio init (PWD low, hrc5000_power_on, the 0x10 read),
        // then the DMR mode (hrc5000_set_mode_dmr), hrc6000_dmr_configure and hrc6000_dmr_run
        public void Init(bool repeater, int cc, int ownId)
        {
            PinsSetup();
            Line(GPIOE, PWD, false);
            PowerOnFmRx();
            byte r10 = R(0, 0x10);
            Log("BENCH r10={0:X2}", r10);
            SetModeDmr(0x40, 0x28, 0x20);
            // hrc6000_dmr_configure: own ID low byte first (id_low_first), 0x10 by channel, color code
            W(0, 0x14, (byte)ownId);
            W(0, 0x15, (byte)(ownId >> 8));
            W(0, 0x16, (byte)(ownId >> 16));
            W(0, 0x10, repeater ? (byte)0x4F : (byte)0x6B);
            W(0, 0x1F, (byte)((cc & 0x0F) << 4));
            // hrc6000_dmr_run: MS sync, color code, do_restart
            Burst(1, 0x04, new byte[] { 0xD5, 0xD7, 0xF7, 0x7F, 0xD7, 0x57 });
            W(0, 0x1F, (byte)((cc & 0x0F) << 4));
            Restart();
            Log("BENCH init done r10={0:X2} r40={1:X2} r41={2:X2} r81={3:X2}", R(0, 0x10), R(0, 0x40), R(0, 0x41), R(0, 0x81));
            if(!polling)
            {
                polling = true;
                Schedule(PollUs, Poll);
            }
        }

        // hrc6000_dmr_tx_begin with own timing (direct mode): no tx_begin hook on this chip, 0x40 = 0xE3
        public void TxBegin()
        {
            tx = TxStart;
            txLastSetup = 0;
            txAny = false;
            Decoder(false);     // tr_end: a reception's decoder stops
            W(0, 0x40, 0xE3);
            Log("BENCH tx_begin");
        }

        // One burst for the queue: "KK II payload-hex" (kind as in hal_dmr_modem.h, voice index, payload)
        public void Queue(string kindHex, int voiceIndex, string payloadHex)
        {
            txq.Enqueue(new TxBurst { Kind = Convert.ToByte(kindHex, 16), Index = (byte)voiceIndex, Payload = Hrc5000.FromHex(payloadHex) });
        }

        // The embedded LC for B-E of the next superframe (hrc6000_dmr_set_embedded_lc)
        public void EmbeddedLc(string lcHex)
        {
            embLc = Hrc5000.FromHex(lcHex);
            embPending = true;
        }

        // An AIR record for the chip, handed over at the last microsecond of its slot as the conductor does
        // (tools/conductor/parts/air.py handover_us)
        public void Air(string hex)
        {
            var w = Hrc5000.FromHex(hex);
            ulong start = 0;
            for(int i = 0; i < 8; i++)
            {
                start |= (ulong)w[2 + i] << (8 * i);
            }
            ulong at = start + Hrc5000.SlotUs - 1;
            ulong now = Now();
            Schedule(at > now ? at - now : 0, () =>
            {
                string r = chip.AirIn(hex);
                Log("BENCH air start={0} -> {1}", start, r);
            });
        }

        // A plain config transaction, for the protocol test: write then read back
        public string Peek(int page, int reg)
        {
            byte v = R(page, reg);
            Log("BENCH PEEK p{0} {1:X2}={2:X2}", page, reg, v);
            return v.ToString("X2");
        }

        public void Poke(int page, int reg, int value)
        {
            W(page, reg, (byte)value);
        }

        // --- Interrupts ---

        private void Poll()
        {
            uint pr = Bus.ReadDoubleWord(EXTI_PR) & 7;
            if(pr != 0)
            {
                Bus.WriteDoubleWord(EXTI_PR, pr);
                if((pr & 1) != 0)
                {
                    TimeslotIsr();
                }
                if((pr & 2) != 0)
                {
                    SysIsr(false);
                }
                if((pr & 4) != 0)
                {
                    rftx++;
                    Log("BENCH RFTX n={0}", rftx);
                }
            }
            Schedule(PollUs, Poll);
        }

        private bool SysLineLow => (Bus.ReadDoubleWord(GPIOC + IDR) & 2) == 0;

        // hrc6000_dmr_isr_timeslot: a pending system report first, then the tick, 0x42, the voice read at the tick,
        // then what the next slot does
        private void TimeslotIsr()
        {
            if(SysLineLow)
            {
                SysIsr(true);
            }
            tick++;
            byte r42 = R(0, 0x42);
            string voice = "";
            if(decoder && tx == TxIdle)
            {
                var v = VoiceRead();
                voice = " voice=" + Hrc5000.Hex(v);
            }
            Log("BENCH TICK {0} r42={1:X2}{2}", tick, r42, voice);
            switch(tx)
            {
            case TxIdle:
                W(0, 0x41, 0x50);   // rx_ctl: receive in the next slot
                break;
            case TxStart:
                txPhase = (tick + 1) & 1;
                tx = TxRun;
                TxSlot();
                break;
            case TxRun:
                if(((tick + 1) & 1) == txPhase)
                {
                    TxSlot();
                }
                else
                {
                    W(0, 0x41, 0x00);
                }
                break;
            case TxEnding:
                W(0, 0x41, 0x00);
                if(!txAny || (tick - txLastSetup >= 2 && (r42 & 0x80) == 0) || tick - txLastSetup >= 4)
                {
                    // tx_finish in direct mode: passive timing again and the receiver restarted
                    Restart();
                    tx = TxIdle;
                    Log("BENCH TXDONE rftx={0}", rftx);
                }
                break;
            }
        }

        private void TxSlot()
        {
            if(txq.Count == 0)
            {
                tx = TxEnding;
                W(0, 0x41, 0x00);
                return;
            }
            SetupBurst(txq.Dequeue());
            txLastSetup = tick;
            txAny = true;
        }

        // sys_service: read 0x82 and 0x52, start the decoder on a header, late entry or information report, the LC on
        // late entry, the report on received data, then clear what was read; again while the line stays low
        private void SysIsr(bool atTick)
        {
            for(int pass = 0; pass < 3; pass++)
            {
                byte f = R(0, 0x82);
                byte r52 = R(0, 0x52);
                if(f == 0)
                {
                    break;
                }
                if((f & 0x1C) != 0 && tx == TxIdle)
                {
                    Decoder(true);      // tr_begin
                }
                if((f & 0x10) != 0)
                {
                    var lc = ReadBurst(2, 0x00, 9);
                    Log("BENCH LATE lc={0}", Hrc5000.Hex(lc));
                }
                if((f & 0x08) != 0)
                {
                    byte r51 = R(0, 0x51);
                    int kind = r51 >> 4;
                    int len = kind == 8 ? 18 : kind == 10 ? 24 : 12;
                    var p = ReadBurst(2, 0x00, len);
                    Log("BENCH RXD flags={0:X2} r51={1:X2} r52={2:X2} payload={3}{4}", f, r51, r52, Hrc5000.Hex(p), atTick ? " at_tick" : "");
                    if(kind == 2 && (r51 & 3) == 2)
                    {
                        Decoder(false); // tr_end at the terminator
                    }
                }
                else
                {
                    Log("BENCH SYS flags={0:X2} r52={1:X2}", f, r52);
                }
                W(0, 0x83, f);
                if(!SysLineLow)
                {
                    break;
                }
            }
        }

        private void Decoder(bool on)
        {
            if(on != decoder)
            {
                decoder = on;
                W(0, 0x22, on ? (byte)0x27 : (byte)0x17);   // hrc5000.c rx_decode
            }
        }

        // --- Driver sequences ---

        // hrc6000_dmr_chip_rx_restart(rx_ctl 0x50)
        private void Restart()
        {
            W(0, 0x40, 0xC3);
            W(0, 0x41, 0x20);
            W(0, 0x41, 0x00);
            W(0, 0x41, 0x20);
            W(0, 0x41, 0x50);
        }

        // setup_burst for the HR_C5000: no CRC_MCU_Control (mcu_crc false), the chip's CRC for CSBK / MBC header /
        // data header (crc_len 10), the stack's 12-byte LC (host_crc) for headers and terminators
        private void SetupBurst(TxBurst b)
        {
            bool voice = b.Kind == 0x10;
            int idx = b.Index % 6;
            if(voice)
            {
                if(idx == 0 && embPending)
                {
                    WritePage2(0x00, embLc, 12);
                    embPending = false;
                }
                var frame = b.Payload.Length >= 27 ? b.Payload : AmbeSilence;
                VoiceWrite(frame);
            }
            else if(b.Kind == 9)
            {
                WritePage2(0x18, b.Payload, 12);
            }
            else
            {
                int size = b.Kind == 8 ? 18 : b.Kind == 10 ? 24 : 12;
                if(b.Kind == 3 || b.Kind == 4 || b.Kind == 6)
                {
                    size = 10;
                }
                WritePage2(0x00, b.Payload, size);
                if(b.Kind == 2)
                {
                    VoiceWrite(AmbeSilence);
                }
            }
            W(0, 0x41, 0x80);
            byte r50 = voice ? (byte)(0x08 | (idx << 4) | Lcss[idx]) : (byte)((b.Kind & 0x0F) << 4);
            W(0, 0x50, r50);
            Log("BENCH setup kind={0:X2} idx={1} r50={2:X2}", b.Kind, idx, r50);
        }

        private void WritePage2(int addr, byte[] data, int size)
        {
            var buf = new byte[size];
            Array.Copy(data, buf, Math.Min(data.Length, size));
            Burst(2, addr, buf);
        }

        // hrc5000_set_mode_fm_rx from power-on: set_core(0x33, FM, 0x0E, 0x10, 0x00), the codec and interrupt writes
        private void PowerOnFmRx()
        {
            SetCore(0x33, 0x80, 0x0E, 0x10, 0x00);
            CodecAndInt(0xFE);
            W(0, 0x83, 0xFF);
            W(0, 0x87, 0x00);
            W(0, 0x81, 0x00);
            W(0, 0x60, 0x00);
            W(0, 0x00, 0x28);
        }

        private void SetClock()
        {
            W(0, 0x0A, 0x80);
            W(0, 0x0B, 40);
            W(0, 0x0C, 0x33);
            W(0, 0x0A, 0x00);
            W(0, 0xBA, 0x22);
            W(0, 0xBB, 0x11);
        }

        private void SetCore(byte b9, byte mode, byte ifHi, byte ifMid, byte vocoder)
        {
            SetClock();
            W(0, 0xB9, b9);
            W(0, 0x10, mode);
            if(mode != 0x80)
            {
                W(0, 0x40, 0x43);
                W(0, 0x41, 0x40);
            }
            W(0, 0x07, ifHi);
            W(0, 0x08, ifMid);
            W(0, 0x09, 0x00);
            if(mode != 0x80)
            {
                W(0, 0x06, vocoder);
            }
            Burst(0, 0x00, new byte[] { 0xFF, 0xB0, 0x00, 0x00, 0x00, 0x00 });
            if(mode == 0x80)
            {
                W(0, 0x06, vocoder);
            }
            Burst(0, 0x11, BlockSys);
        }

        private void CodecAndInt(byte rxPathB)
        {
            W(0, 0x0D, 0x8C);
            W(0, 0x0E, 0x44);
            W(0, 0x0F, 0xC8);
            W(0, 0x25, 0x0E);
            W(0, 0x26, rxPathB);
        }

        // hrc5000_set_mode_dmr
        private void SetModeDmr(byte mod2, byte mod1, byte mic)
        {
            SetCore(0x32, 0x6B, 0x0B, 0xB8, 0x21);
            W(0, 0x00, 0x28);
            Burst(1, 0x10, new byte[] { 0x69, 0x69, 0x96, 0x96, 0x96, 0x99, 0x99, 0x99, 0xA5, 0xA5, 0xAA, 0xAA, 0xCC, 0xCC, 0x00, 0xF0, 0x01, 0xFF, 0x01, 0x0F });
            Burst(1, 0x30, new byte[] { 0x30, 0x4E, 0x14, 0x1E, 0x1A, 0x30, 0x3D, 0x50, 0x07, 0x60 });
            Burst(1, 0x40, new byte[] { 0x90, 0x03, 0x01, 0x02, 0x05, 0x07, 0xF0 });
            Burst(1, 0x50, new byte[] { 0, 0, 0, 0, 0 });
            W(1, 0x52, 0x08); W(1, 0x53, 0xEB); W(1, 0x54, 0x78); W(1, 0x45, 0x1E); W(1, 0x37, 0x50); W(1, 0x35, 0xFF);
            W(0, 0x39, 0x02); W(0, 0x3D, 0x0A); W(0, 0x83, 0xFF); W(0, 0x87, 0x00); W(0, 0x65, 0x0A); W(0, 0x1D, 0xFF);
            W(0, 0x1E, 0xF1); W(0, 0x1F, 0x10);
            W(0, 0x0D, 0x8C);
            // hrc5000_dmr_path(rx)
            W(0, 0x0E, 0x44); W(0, 0x0F, mic); W(0, 0x37, 0xC2); W(0, 0x25, 0x0E); W(0, 0x26, 0xFD);
            W(0, 0x45, mod2); W(0, 0x46, mod1);
            W(0, 0x64, 0x00);
            for(int r = 0x24; r <= 0x27; r++)
            {
                W(1, r, 0x00);
            }
            W(0, 0x81, 0x19);
            W(0, 0x85, 0x00);
        }

        // --- The two ports ---

        // md380_rfbus_modem_transfer: CS low, header, data, CS high; bus_byte per byte
        private byte[] Transfer(byte[] hdr, byte[] tx, int len)
        {
            var rx = new byte[len];
            Line(GPIOC, CLK, false);
            Line(GPIOE, CS, false);
            foreach(var h in hdr)
            {
                BusByte(h);
            }
            for(int i = 0; i < len; i++)
            {
                rx[i] = BusByte(tx != null ? tx[i] : (byte)0xFF);
            }
            Line(GPIOE, CS, true);
            return rx;
        }

        // Per bit: clock high, MOSI, clock low (the chip samples MOSI), then MISO
        private byte BusByte(byte o)
        {
            byte i = 0;
            for(int b = 0; b < 8; b++)
            {
                Line(GPIOC, CLK, true);
                Line(GPIOE, MOSI, (o & 0x80) != 0);
                o = (byte)(o << 1);
                Line(GPIOC, CLK, false);
                i = (byte)((i << 1) | ((Bus.ReadDoubleWord(GPIOE + IDR) & MISO) != 0 ? 1 : 0));
            }
            return i;
        }

        private void W(int page, int reg, byte v)
        {
            Transfer(new byte[] { (byte)page, (byte)reg }, new[] { v }, 1);
        }

        private byte R(int page, int reg)
        {
            return Transfer(new byte[] { (byte)(0x80 | page), (byte)reg }, null, 1)[0];
        }

        private void Burst(int page, int reg, byte[] data)
        {
            Transfer(new byte[] { (byte)page, (byte)reg }, data, data.Length);
        }

        private byte[] ReadBurst(int page, int reg, int len)
        {
            return Transfer(new byte[] { (byte)(0x80 | page), (byte)reg }, null, len);
        }

        // voice_xfer: PB12 low, 0x03 / 0x83, 0x00, 27 bytes on SPI2, PB12 high
        private void VoiceWrite(byte[] frame)
        {
            Line(GPIOB, VCS, false);
            Spi(0x03);
            Spi(0x00);
            for(int i = 0; i < 27; i++)
            {
                Spi(frame[i]);
            }
            Line(GPIOB, VCS, true);
        }

        private byte[] VoiceRead()
        {
            var o = new byte[27];
            Line(GPIOB, VCS, false);
            Spi(0x83);
            Spi(0x00);
            for(int i = 0; i < 27; i++)
            {
                o[i] = Spi(0xFF);
            }
            Line(GPIOB, VCS, true);
            return o;
        }

        private byte Spi(byte b)
        {
            Bus.WriteDoubleWord(SPI2 + 0x0C, b);
            return (byte)Bus.ReadDoubleWord(SPI2 + 0x0C);
        }

        // The pins as the board sets them (radio_md380.c, hrc_bus / rfbus), EXTI0-2 falling (dmr_glue.c exti_setup),
        // SPI2 master, CPOL 1, CPHA 0, prescaler 32 (voice_spi_setup)
        private void PinsSetup()
        {
            Modify(GPIOE, 0, (2u << 4) | (2u << 10) | (2u << 12) | (3u << 6), (1u << 4) | (1u << 10) | (1u << 12)); // PE2, PE5, PE6 out; PE3 in
            Modify(GPIOC, 0, (3u << 26) | 0x3Fu, 1u << 26);                                                  // PC13 out; PC0-2 in
            Modify(GPIOB, 0, 3u << 24, 1u << 24);                                                            // PB12 out
            Line(GPIOE, CS, true);
            Line(GPIOB, VCS, true);
            Line(GPIOC, CLK, false);
            Bus.WriteDoubleWord(EXTI + 0x0C, Bus.ReadDoubleWord(EXTI + 0x0C) | 7);    // FTSR
            Bus.WriteDoubleWord(EXTI + 0x00, Bus.ReadDoubleWord(EXTI + 0x00) | 7);    // IMR
            Bus.WriteDoubleWord(EXTI_PR, 7);
            Bus.WriteDoubleWord(SPI2 + 0x00, (1u << 2) | (1u << 1) | (4u << 3) | (1u << 9) | (1u << 8) | (1u << 6));
        }

        private void Modify(ulong port, ulong off, uint clear, uint set)
        {
            Bus.WriteDoubleWord(port + off, (Bus.ReadDoubleWord(port + off) & ~clear) | set);
        }

        private void Line(ulong port, int pin, bool level)
        {
            Bus.WriteDoubleWord(port + 0x18, level ? (1u << pin) : (1u << (pin + 16)));
        }

        // --- Helpers ---

        private void Log(string fmt, params object[] args)
        {
            chip.Trace(string.Format(fmt, args));
        }

        private ulong Now()
        {
            return (ulong)Math.Round(machine.LocalTimeSource.ElapsedVirtualTime.TotalMicroseconds);
        }

        private void Schedule(ulong us, Action a)
        {
            machine.ScheduleAction(TimeInterval.FromMicroseconds(us), _ => a(), "bench");
        }

        private IBusController Bus => machine.SystemBus;

        private class TxBurst
        {
            public byte Kind, Index;
            public byte[] Payload;
        }

        private const ulong GPIOB = 0x40020400, GPIOC = 0x40020800, GPIOE = 0x40021000, IDR = 0x10;
        private const ulong EXTI = 0x40013C00, EXTI_PR = 0x40013C14, SPI2 = 0x40003800;
        private const int CS = 2, MOSI = 5, PWD = 6, CLK = 13, VCS = 12;
        private const uint MISO = 1u << 3;
        private const int TxIdle = 0, TxStart = 1, TxRun = 2, TxEnding = 3;

        private static readonly byte[] Lcss = { 0, 1, 3, 3, 2, 0 };     // hrc5000.c voice_lcss
        private static readonly byte[] AmbeSilence =
        {
            0xB9, 0xE8, 0x81, 0x52, 0x61, 0x73, 0x00, 0x2A, 0x6B, 0xB9, 0xE8, 0x81, 0x52, 0x61, 0x73, 0x00, 0x2A, 0x6B,
            0xB9, 0xE8, 0x81, 0x52, 0x61, 0x73, 0x00, 0x2A, 0x6B,
        };
        // hrc5000.c block_sys: page 0 from 0x11
        private static readonly byte[] BlockSys =
        {
            0x80, 0x0A, 0x22, 0x01, 0x00, 0x00, 0x33, 0xEF, 0x00, 0xFF, 0xFF, 0xFF, 0xF0, 0xF0, 0x10, 0x00,
            0x00, 0x07, 0x3B, 0xF8, 0x0E, 0xFD, 0x40, 0xFF, 0x00, 0x0B, 0x00, 0x00, 0x00, 0x04, 0x0B, 0x00,
            0x17, 0x02, 0xFF, 0xE0, 0x28, 0xF0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        };

        private readonly IMachine machine;
        private readonly Hrc5000 chip;
        private readonly Queue<TxBurst> txq = new Queue<TxBurst>();
        private bool polling, decoder, embPending, txAny;
        private byte[] embLc = new byte[9];
        private int tx;
        private ulong tick, txPhase, txLastSetup;
        private int rftx;
    }
}
