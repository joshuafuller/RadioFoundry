// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// Register-only I2C target: docs/spec/hardware-dm1701.md 5.1 and drivers/rf/at1846s/at1846s.c.
// shortcut: no RF or calibration timing, extend before testing reception or transmission.
using System;

namespace Antmicro.Renode.Peripherals.I2C
{
    public sealed class At1846s : II2CPeripheral
    {
        public At1846s()
        {
            Reset();
        }

        public void Reset()
        {
            Array.Clear(registers, 0, registers.Length);
            registers[0, 0x1B] = 0x00FF; // no received signal, maximum noise (at1846s_read_signal)
            page = 0;
            address = 0;
        }

        public void Write(byte[] data)
        {
            if((data.Length != 1 && data.Length != 3) || data[0] > 0x7F)
            {
                return;
            }
            address = data[0];
            if(data.Length == 1)
            {
                return; // register selection before a read, including a STOP between the two transfers
            }
            var value = (ushort)((data[1] << 8) | data[2]);
            if(address == 0x7F)
            {
                if(value <= 1)
                {
                    page = value;
                }
            }
            else
            {
                if(page == 0 && address == 0x30 && (value & 1) != 0)
                {
                    Reset();
                    address = 0x30;
                }
                registers[page, address] = value;
            }
        }

        public byte[] Read(int count = 1)
        {
            var result = new byte[count];
            var value = address == 0x7F ? page : registers[page, address];
            if(count > 0)
            {
                result[0] = (byte)(value >> 8);
            }
            if(count > 1)
            {
                result[1] = (byte)value;
            }
            return result;
        }

        public void FinishTransmission()
        {
            // The selected register survives STOP (hardware spec 5.1).
        }

        private readonly ushort[,] registers = new ushort[2, 128];
        private ushort page;
        private byte address;
    }
}
