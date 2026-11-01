// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
// W25Q128FV standard SPI subset; commands and limits are documented in spinor.md.
using System;
using System.IO;
using Antmicro.Renode.Core;

namespace Antmicro.Renode.Peripherals.SPI
{
    public sealed class SpiNor : ISPIPeripheral, IGPIOReceiver
    {
        public SpiNor(string imageFile)
        {
            this.imageFile = Path.GetFullPath(imageFile);
            memory = ReadImage(this.imageFile, Capacity);
            security = ReadImage(this.imageFile + ".security", 3 * PageSize);
            Reset();
        }

        public void Reset()
        {
            selected = false;
            writeEnabled = false;
            ClearTransaction();
        }

        // GPIO input 0 is active-low /CS. Repeated levels do not end a command.
        public void OnGPIO(int number, bool value)
        {
            if(number != 0)
            {
                return;
            }
            if(value)
            {
                FinishTransmission();
            }
            else if(!selected)
            {
                ClearTransaction();
                selected = true;
            }
        }

        public byte Transmit(byte data)
        {
            if(!selected)
            {
                return 0xFF;
            }
            var offset = count++;
            if(offset == 0)
            {
                command = data;
                return 0xFF;
            }
            switch(command)
            {
            case 0x9F:
                return offset <= Jedec.Length ? Jedec[offset - 1] : (byte)0xFF;
            case 0x05:
                // shortcut: writes finish at /CS release, add virtual busy time for timing tests.
                return writeEnabled ? (byte)2 : (byte)0;
            case 0x35:
                return 0;
            case 0x15:
                return 0x60; // power-on drive strength, W25Q128FV section 7.1.12
            case 0x4B:
                return offset >= 5 && offset < 13 ? UniqueId[offset - 5] : (byte)0xFF;
            case 0x03:
            case 0x0B:
            case 0x02:
            case 0x20:
            case 0x52:
            case 0xD8:
            case 0x48:
                if(offset <= 3)
                {
                    address = (address << 8) | data;
                    return 0xFF;
                }
                break;
            default:
                return 0xFF;
            }
            switch(command)
            {
            case 0x0B:
            case 0x48:
                if(offset == 4)
                {
                    return 0xFF; // one dummy byte after the address
                }
                if(command == 0x48)
                {
                    var register = address >> 12;
                    var result = register >= 1 && register <= 3
                        ? security[(register - 1) * PageSize + (address & 0xFF)] : (byte)0xFF;
                    address = (address & ~0xFF) | ((address + 1) & 0xFF);
                    return result;
                }
                goto case 0x03;
            case 0x03:
                var value = memory[address];
                address = (address + 1) & (Capacity - 1);
                return value;
            case 0x02:
                // Only the last byte sent to each page-buffer position is programmed.
                page[(address + offset - 4) & 0xFF] = data;
                break;
            }
            return 0xFF;
        }

        public void FinishTransmission()
        {
            if(!selected)
            {
                return;
            }
            selected = false;
            if(count == 1 && (command == 0x06 || command == 0x04))
            {
                writeEnabled = command == 0x06;
            }
            else if(writeEnabled && command == 0x02 && count > 4)
            {
                var start = address & ~(PageSize - 1);
                for(var i = 0; i < PageSize; i++)
                {
                    page[i] &= memory[start + i];
                }
                Persist(start, page);
                writeEnabled = false;
            }
            else if(writeEnabled && count == 4 && (command == 0x20 || command == 0x52 || command == 0xD8))
            {
                var size = command == 0x20 ? 0x1000 : command == 0x52 ? 0x8000 : 0x10000;
                var erased = new byte[size];
                FillErased(erased);
                Persist(address & ~(size - 1), erased);
                writeEnabled = false;
            }
            ClearTransaction();
        }

        private void Persist(int start, byte[] data)
        {
            using(var file = new FileStream(imageFile, FileMode.Open, FileAccess.Write, FileShare.Read))
            {
                if(file.Length != Capacity)
                {
                    throw new InvalidDataException("SPI NOR backing image changed size");
                }
                file.Position = start;
                file.Write(data, 0, data.Length);
                file.Flush(true);
            }
            Array.Copy(data, 0, memory, start, data.Length);
        }

        private void ClearTransaction()
        {
            command = 0;
            count = 0;
            address = 0;
            FillErased(page);
        }

        private static void FillErased(byte[] bytes)
        {
            for(var i = 0; i < bytes.Length; i++)
            {
                bytes[i] = 0xFF;
            }
        }

        private static byte[] ReadImage(string path, int size)
        {
            if(new FileInfo(path).Length != size)
            {
                throw new InvalidDataException("SPI NOR image must contain exactly " + size + " bytes: " + path);
            }
            return File.ReadAllBytes(path);
        }

        private const int Capacity = 0x1000000;
        private const int PageSize = 256;
        private static readonly byte[] Jedec = { 0xEF, 0x40, 0x18 };
        private static readonly byte[] UniqueId = { 0x52, 0x46, 0x45, 0x4D, 0x55, 0, 0, 1 };
        private readonly string imageFile;
        private readonly byte[] memory;
        private readonly byte[] security;
        private readonly byte[] page = new byte[PageSize];
        private bool selected;
        private bool writeEnabled;
        private byte command;
        private int count;
        private int address;
    }
}
