// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
//
// Minimal STM32 peripherals for unchanged firmware, based on the board power drivers (power_*.c) and the STM32F405/407
// reference manual RM0090, ADC and GPIO chapters. Board boot scripts supply external ADC fixtures.
//
//   Stm32Adc     ADC1: fills the DMA ring with fixed readings (battery, volume and the rest)
//   GpioSink     a receiver that ignores its inputs, to cut GPIO lines away from EXTI (md380.repl)
//
// The data flash and the LCD were stubs here in stage 1. Full models replace them: peripherals/spinor.cs (SPI.SpiNor)
// and peripherals/lcd_st7735.cs (RadioFoundry.St7735Lcd), wired in md380.repl and md380.resc.
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.RadioFoundry
{
    // ADC1 at 0x40012000 (and the common registers at +0x300). The firmware runs ADC1 in continuous scan mode and lets
    // DMA2 stream 4 copy every result into a circular buffer in RAM (boards/md380/power_md380.c adc_start). Renode
    // has no F4 ADC model, so nothing would ever land in that buffer and the battery would read 0 mV. This model
    // waits for the start bit (CR2 SWSTART), reads the buffer address and length the firmware gave the DMA stream,
    // and fills the buffer with one fixed value per input channel in the firmware's scan order (SQR1..SQR3). The DMA
    // itself never moves data, so the values stay. SetChannel changes a value and refills at once.
    public class Stm32Adc : IDoubleWordPeripheral, IKnownSize
    {
        public Stm32Adc(IMachine machine)
        {
            this.machine = machine;
            // External inputs are board wiring; only the chip's internal 25 C sensor has a default.
            values[16] = 943;
            Reset();
        }

        public void Reset()
        {
            Array.Clear(regs, 0, regs.Length);
            ringAddr = 0;
            ringLength = 0;
        }

        public uint ReadDoubleWord(long offset)
        {
            if(offset == SR)
            {
                return 0x12; // EOC and STRT, never OVR: adc_read restarts the converter on an overrun
            }
            return regs[offset / 4];
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            if(offset == SR)
            {
                regs[0] &= value;
                return;
            }
            if(offset == CR2 && (value & SwStart) != 0)
            {
                regs[offset / 4] = value & ~SwStart;
                Starts++;
                Fill();
                return;
            }
            regs[offset / 4] = value;
        }

        public void SetChannel(int channel, int counts)
        {
            values[channel] = (ushort)counts;
            Fill();
        }

        public int Starts { get; private set; }

        public long Size => 0x400;

        private void Fill()
        {
            var bus = machine.GetSystemBus(this);
            var addr = (ulong)bus.ReadDoubleWord(Dma2Stream4M0ar);
            var length = (int)bus.ReadDoubleWord(Dma2Stream4Ndtr);
            if(addr != 0 && length > 0)
            {
                ringAddr = addr;
                ringLength = length;
            }
            if(ringAddr == 0)
            {
                return;
            }
            var ranks = (int)((regs[SQR1 / 4] >> 20) & 0xF) + 1;
            for(var i = 0; i < ringLength; i++)
            {
                bus.WriteWord(ringAddr + (ulong)(2 * i), values[Rank(i % ranks)]);
            }
            this.Log(LogLevel.Info, "ring 0x{0:X8}: {1} results, {2} ranks", ringAddr, ringLength, ranks);
        }

        // ADC input channel of scan rank r (0-based): ranks 1-6 in SQR3, 7-12 in SQR2, 13-16 in SQR1, 5 bits each
        private int Rank(int r)
        {
            var reg = r < 6 ? SQR3 : (r < 12 ? SQR2 : SQR1);
            return (int)((regs[reg / 4] >> (5 * (r % 6))) & 0x1F);
        }

        private readonly IMachine machine;
        private readonly uint[] regs = new uint[0x400 / 4];
        private readonly ushort[] values = new ushort[32];
        private ulong ringAddr;
        private int ringLength;

        private const long SR = 0x00, CR2 = 0x08, SQR1 = 0x2C, SQR2 = 0x30, SQR3 = 0x34;
        private const uint SwStart = 1u << 30;
        private const ulong Dma2Stream4Ndtr = 0x40026474, Dma2Stream4M0ar = 0x4002647C;
    }

    // Takes GPIO lines and does nothing with them. The stock stm32f4.repl ties pin n of every port to EXTI line n, so
    // the lines of one EXTI input are ORed: any port's pin 0 held high hides a falling edge on PC0. md380.repl
    // connects the pins that must not reach EXTI here instead, which replaces their EXTI link
    public class GpioSink : IGPIOReceiver
    {
        public void OnGPIO(int number, bool value)
        {
        }

        public void Reset()
        {
        }
    }
}
