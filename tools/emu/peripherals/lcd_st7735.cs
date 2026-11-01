// SPDX-License-Identifier: MIT
// Copyright (c) 2026 The RadioFoundry Authors
//
// The MD-380 display controller as a Renode peripheral: a 160 x 128 color TFT on FSMC bank 1, 8-bit 8080 bus
// (docs/spec/hardware-md380.md 7). A write to 0x60000000 is a command byte, a write to 0x60040000 a data byte
// (boards/md380/pins.h LCD_CMD_ADDR, LCD_DATA_ADDR). It keeps a frame buffer from the firmware's own writes and
// sends FRAME checkpoints in the virtual radio's format (platform/host/vframe.c).
//
// Written from this repository's driver (drivers/display/st7735/st7735.c, boards/md380/display_md380.c) and the
// public command set of the ST7735S class of controllers (Sitronix ST7735S datasheet, "System function command
// list": CASET 2Ah, RASET 2Bh, RAMWR 2Ch, MADCTL 36h, COLMOD 3Ah, RDDID 04h, RDID1-3 DAh-DCh).
//
// Attach (Renode platform file, after the stock repl's plain memory at 0x60000000 is removed):
//
//     fsmcBank1: @none
//     lcd: RadioFoundry.St7735Lcd @ sysbus 0x60000000
//     gpioPortD:
//         6 -> lcd@0
//         13 -> lcd@1
//
// and `include @tools/emu/peripherals/lcd_st7735.cs` in the .resc before the platform file loads. In stage 1's
// boards/md380/emu/platform.repl that is one changed line, `lcd: RadioFoundry.St7735Lcd @ sysbus 0x60000000` in place of the
// St7735LcdBus stub, plus the two gpioPortD lines. A file layered on top of md380.repl cannot redeclare `lcd` with
// another type (Renode error E02); it writes `lcd: @none` and attaches this model under a new name, as `display`.
//
// Inputs: GPIO 0 is chip select (PD6, active low), GPIO 1 is reset (PD13, active low). The firmware raises chip
// select and then writes 0x00 to the data address to park the shared keypad lines (display_md380.c bus_end). A
// controller that is not selected ignores that byte, and so does this model. With no wire on GPIO 0 the panel counts
// as always selected.
//
// Frame buffer: the panel's address space as the firmware's window commands (CASET, RASET) see it, 160 columns by 128
// rows, one RGB565 pixel per address. The MD-380 driver always ends with MADCTL 0xA0 (st7735.c madctl_by_type, types
// 0 and 3), the setting that shows the picture right side up on the one radio seen (hardware-md380.md 7). The model
// does not rotate for other MADCTL values; it logs a warning instead.
//
// Checkpoints: when chip select rises after a session that wrote pixels, the model takes one checkpoint, as vhost takes
// one for every flush. A session that only changed what the glass shows (DISPON, DISPOFF, INVON, INVOFF, SLPIN, SLPOUT,
// a reset) takes one when the picture is now different from the last checkpoint. It is one FRAME line, field for field the line vframe.c writes:
//
//     FRAME radio=1 boot=1 id=3 us=1234567 crc=89abcdef palette=- backlight=100
//
// The panel bytes are the vhost rgb565-160x128 profile's: each pixel little endian, row after row
// (boards/vhost/display_profiles_vhost.c stride_of, snapshot_pixel). crc is CRC-32 (zlib) over those 40960 bytes. us is
// Renode virtual time in microseconds. backlight is TIM8 CCR1 in percent (display_md380.c set_backlight), read when
// the checkpoint is taken. palette is "-": the bus carries RGB565, never the firmware's palette indexes. One gfx_flush
// can send several row bands, and each band is one checkpoint, so the emulator can show more FRAME lines than vhost.
//
// Lines go to the Renode log (Info) and, when FrameLog names a file, to that file. SavePng and SavePanel write the
// current picture on request.
using System;
using System.IO;
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.RadioFoundry
{
    public class St7735Lcd : IBytePeripheral, IKnownSize, IGPIOReceiver
    {
        public St7735Lcd(IMachine machine)
        {
            this.machine = machine;
            gram = new ushort[Width * Height];
            RadioId = 1;
            BootId = 1;
            BacklightAddress = 0x40010434; // TIM8 CCR1 (RM0090: TIM8 at 0x40010400, CCR1 at +0x34)
            PanelId = "009102";
            Reset();
        }

        public void Reset()
        {
            ControllerReset();
            selected = true;
            csWired = false;
            inReset = false;
            pixelsInSession = false;
            stateChanged = false;
            lastCrc = Crc32(new byte[Width * Height * 2], 0, Width * Height * 2);
            FrameId = 0;
            LastFrame = "";
            Commands = 0;
            PixelsWritten = 0;
            for(var i = 0; i < gram.Length; i++)
            {
                gram[i] = 0;
            }
        }

        // ---- the bus ----

        public byte ReadByte(long offset)
        {
            if(offset < DataOffset || !selected || inReset)
            {
                return 0;
            }
            var i = readIndex++;
            if(i == 0)
            {
                return 0; // the dummy byte every read starts with
            }
            switch(command)
            {
            case 0x04: // RDDID: ID1, ID2, ID3
                return i <= 3 ? id[i - 1] : (byte)0;
            case 0xDA: // RDID1-RDID3: one byte each
            case 0xDB:
            case 0xDC:
                return i == 1 ? id[command - 0xDA] : (byte)0;
            default:
                return 0;
            }
        }

        public void WriteByte(long offset, byte value)
        {
            if(!selected || inReset)
            {
                return; // not selected: the line parking after bus_end, or a write during reset
            }
            if(offset < DataOffset)
            {
                Command(value);
            }
            else
            {
                Data(value);
            }
        }

        public long Size => DataOffset + 4;

        // ---- chip select (0) and reset (1), both active low ----

        public void OnGPIO(int number, bool value)
        {
            if(number == 0)
            {
                csWired = true;
                var wasSelected = selected;
                selected = !value;
                if(wasSelected && !selected)
                {
                    EndSession();
                }
            }
            else if(number == 1)
            {
                var wasReset = inReset;
                inReset = !value;
                if(!wasReset && inReset)
                {
                    ControllerReset(); // the frame RAM keeps its contents, as on the part
                    stateChanged = true;
                }
            }
        }

        // ---- requests from the monitor or a test ----

        // Takes a checkpoint now, whether or not anything changed
        public string Checkpoint()
        {
            return Emit();
        }

        // Writes the picture as an RGB PNG, scale pixels per panel pixel (1 to 8)
        public void SavePng(string path, int scale = 1)
        {
            if(scale < 1 || scale > 8)
            {
                throw new ArgumentException("scale must be 1 to 8");
            }
            File.WriteAllBytes(path, Png(scale));
        }

        // Writes the panel bytes the FRAME crc covers (160 x 128 RGB565, little endian)
        public void SavePanel(string path)
        {
            File.WriteAllBytes(path, Panel());
        }

        public ushort Pixel(int x, int y)
        {
            return View(gram[y * Width + x]);
        }

        // The controller ID the read commands answer, six hex digits. 009102 is the one radio seen (GC9xxx, type 3;
        // hardware-md380.md 7); 7C89F0 is an ST7735S (its datasheet's RDDID default)
        public string PanelId
        {
            get => string.Format("{0:X2}{1:X2}{2:X2}", id[0], id[1], id[2]);
            set
            {
                if(value == null || value.Length != 6)
                {
                    throw new ArgumentException("PanelId is six hex digits, for example 009102");
                }
                for(var i = 0; i < 3; i++)
                {
                    id[i] = Convert.ToByte(value.Substring(i * 2, 2), 16);
                }
            }
        }

        public uint RadioId { get; set; }
        public uint BootId { get; set; }
        // Where the backlight percentage is read from; 0 turns that off and Backlight is used as it is
        public ulong BacklightAddress { get; set; }
        public uint Backlight { get; set; }
        public string FrameLog { get; set; }
        public uint FrameId { get; private set; }
        public string LastFrame { get; private set; }
        public long Commands { get; private set; }
        public long PixelsWritten { get; private set; }
        public byte Madctl => madctl;
        public byte Colmod => colmod;
        public bool DisplayOn => displayOn;
        public bool Sleeping => sleeping;

        // ---- the controller ----

        private void ControllerReset()
        {
            command = 0;
            readIndex = 0;
            paramIndex = 0;
            pending = 0;
            pendingCount = 0;
            madctl = 0;
            colmod = 0x06; // the reset default of the ST7735S class: 18 bits a pixel
            displayOn = false;
            sleeping = true;
            inverted = false;
            xs = 0; xe = Width - 1; ys = 0; ye = Height - 1;
            x = 0; y = 0;
        }

        private void Command(byte c)
        {
            Commands++;
            command = c;
            readIndex = 0;
            paramIndex = 0;
            pendingCount = 0;
            switch(c)
            {
            case 0x01: // SWRESET
                ControllerReset();
                stateChanged = true;
                break;
            case 0x10: sleeping = true; stateChanged = true; break; // SLPIN
            case 0x11: sleeping = false; stateChanged = true; break; // SLPOUT
            case 0x20: inverted = false; stateChanged = true; break; // INVOFF
            case 0x21: inverted = true; stateChanged = true; break; // INVON
            case 0x28: displayOn = false; stateChanged = true; break; // DISPOFF
            case 0x29: displayOn = true; stateChanged = true; break; // DISPON
            case 0x2C: // RAMWR: start at the window's first address
                x = xs;
                y = ys;
                break;
            }
        }

        private void Data(byte d)
        {
            var i = paramIndex++;
            switch(command)
            {
            case 0x2A: // CASET: XS high, XS low, XE high, XE low
                xs = Range(xs, i, 0, d);
                xe = Range(xe, i, 2, d);
                break;
            case 0x2B: // RASET: YS, YE the same way
                ys = Range(ys, i, 0, d);
                ye = Range(ye, i, 2, d);
                break;
            case 0x36: // MADCTL
                if(i == 0)
                {
                    madctl = d;
                    if(d != ExpectedMadctl)
                    {
                        this.Log(LogLevel.Warning, "MADCTL 0x{0:X2}: the frame buffer is not rotated for it (only 0x{1:X2} is modelled)", d, ExpectedMadctl);
                    }
                }
                break;
            case 0x3A: // COLMOD
                if(i == 0)
                {
                    colmod = d;
                    if((d & 7) != 5 && (d & 7) != 6)
                    {
                        this.Log(LogLevel.Warning, "COLMOD 0x{0:X2}: only 16 (5) and 18 (6) bits a pixel are modelled", d);
                    }
                }
                break;
            case 0x2C: // RAMWR
                Pixel(d);
                break;
            }
        }

        private static ushort Range(ushort current, int index, int first, byte d)
        {
            if(index == first)
            {
                return (ushort)((d << 8) | (current & 0xFF));
            }
            if(index == first + 1)
            {
                return (ushort)((current & 0xFF00) | d);
            }
            return current;
        }

        // 16 bits: two bytes, RGB565 high byte first (display_md380.c bus_pixels). 18 bits: three bytes, six bits of
        // R, G and B in the top of each
        private void Pixel(byte d)
        {
            pending = (pending << 8) | d;
            pendingCount++;
            var bytes = (colmod & 7) == 6 ? 3 : 2;
            if(pendingCount < bytes)
            {
                return;
            }
            ushort rgb;
            if(bytes == 3)
            {
                rgb = (ushort)((((pending >> 19) & 0x1F) << 11) | (((pending >> 10) & 0x3F) << 5) | ((pending >> 3) & 0x1F));
            }
            else
            {
                rgb = (ushort)pending;
            }
            pending = 0;
            pendingCount = 0;
            if(x < Width && y < Height)
            {
                gram[y * Width + x] = rgb;
            }
            PixelsWritten++;
            pixelsInSession = true;
            // The address counter walks the window and wraps to its start
            if(x >= xe)
            {
                x = xs;
                y = y >= ye ? ys : (ushort)(y + 1);
            }
            else
            {
                x++;
            }
        }

        private void EndSession()
        {
            pendingCount = 0;
            pending = 0;
            var pixels = pixelsInSession;
            var changed = stateChanged;
            pixelsInSession = false;
            stateChanged = false;
            if(pixels || (changed && Crc32(Panel(), 0, Width * Height * 2) != lastCrc))
            {
                Emit();
            }
        }

        // ---- checkpoints ----

        private string Emit()
        {
            FrameId++;
            var panel = Panel();
            lastCrc = Crc32(panel, 0, panel.Length);
            var line = string.Format("FRAME radio={0} boot={1} id={2} us={3} crc={4:x8} palette=- backlight={5}",
                RadioId, BootId, FrameId, NowMicroseconds(), lastCrc, ReadBacklight());
            LastFrame = line;
            this.Log(LogLevel.Info, line);
            if(!string.IsNullOrEmpty(FrameLog))
            {
                File.AppendAllText(FrameLog, line + "\n");
            }
            return line;
        }

        private ulong NowMicroseconds()
        {
            return (ulong)Math.Round(machine.LocalTimeSource.ElapsedVirtualTime.TotalMicroseconds);
        }

        private uint ReadBacklight()
        {
            if(BacklightAddress == 0)
            {
                return Math.Min(Backlight, 100u);
            }
            var v = machine.SystemBus.ReadDoubleWord(BacklightAddress);
            return Math.Min(v, 100u); // the timer counts 0 to 99 (display_md380.c bl_init), 100 or more is fully on
        }

        // What the glass shows for a stored pixel: black while asleep or off, inverted under INVON
        private ushort View(ushort stored)
        {
            if(sleeping || !displayOn)
            {
                return 0;
            }
            return inverted ? (ushort)~stored : stored;
        }

        private byte[] Panel()
        {
            var b = new byte[Width * Height * 2];
            for(var i = 0; i < gram.Length; i++)
            {
                var v = View(gram[i]);
                b[i * 2] = (byte)v;
                b[i * 2 + 1] = (byte)(v >> 8);
            }
            return b;
        }

        // A plain RGB8 PNG. The image data is zlib with stored (uncompressed) blocks, so no compression library is needed
        private byte[] Png(int scale)
        {
            var w = Width * scale;
            var h = Height * scale;
            var raw = new byte[h * (1 + w * 3)];
            var p = 0;
            for(var yy = 0; yy < h; yy++)
            {
                raw[p++] = 0; // filter: none
                for(var xx = 0; xx < w; xx++)
                {
                    var v = View(gram[(yy / scale) * Width + xx / scale]);
                    var r = (v >> 11) & 0x1F;
                    var g = (v >> 5) & 0x3F;
                    var bl = v & 0x1F;
                    raw[p++] = (byte)((r << 3) | (r >> 2));
                    raw[p++] = (byte)((g << 2) | (g >> 4));
                    raw[p++] = (byte)((bl << 3) | (bl >> 2));
                }
            }
            var z = new MemoryStream();
            z.WriteByte(0x78);
            z.WriteByte(0x01);
            for(var off = 0; off < raw.Length; off += 65535)
            {
                var n = Math.Min(65535, raw.Length - off);
                z.WriteByte((byte)(off + n == raw.Length ? 1 : 0));
                z.WriteByte((byte)n);
                z.WriteByte((byte)(n >> 8));
                z.WriteByte((byte)~n);
                z.WriteByte((byte)(~n >> 8));
                z.Write(raw, off, n);
            }
            WriteBE(z, Adler32(raw));

            var png = new MemoryStream();
            png.Write(new byte[] { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A }, 0, 8);
            var ihdr = new MemoryStream();
            WriteBE(ihdr, (uint)w);
            WriteBE(ihdr, (uint)h);
            ihdr.Write(new byte[] { 8, 2, 0, 0, 0 }, 0, 5); // 8 bits, RGB, deflate, no filter set, no interlace
            Chunk(png, "IHDR", ihdr.ToArray());
            Chunk(png, "IDAT", z.ToArray());
            Chunk(png, "IEND", new byte[0]);
            return png.ToArray();
        }

        private static void Chunk(Stream s, string type, byte[] data)
        {
            var t = Encoding.ASCII.GetBytes(type);
            WriteBE(s, (uint)data.Length);
            s.Write(t, 0, 4);
            s.Write(data, 0, data.Length);
            var both = new byte[4 + data.Length];
            Array.Copy(t, both, 4);
            Array.Copy(data, 0, both, 4, data.Length);
            WriteBE(s, Crc32(both, 0, both.Length));
        }

        private static void WriteBE(Stream s, uint v)
        {
            s.WriteByte((byte)(v >> 24));
            s.WriteByte((byte)(v >> 16));
            s.WriteByte((byte)(v >> 8));
            s.WriteByte((byte)v);
        }

        private static uint Adler32(byte[] d)
        {
            uint a = 1, b = 0;
            foreach(var x in d)
            {
                a = (a + x) % 65521;
                b = (b + a) % 65521;
            }
            return (b << 16) | a;
        }

        // CRC-32 as zlib and vdisplay_snapshot_crc compute it (reflected, polynomial 0xEDB88320)
        private static uint Crc32(byte[] d, int off, int n)
        {
            var crc = 0xFFFFFFFFu;
            for(var i = off; i < off + n; i++)
            {
                crc ^= d[i];
                for(var k = 0; k < 8; k++)
                {
                    crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
                }
            }
            return ~crc;
        }

        private readonly IMachine machine;
        private readonly ushort[] gram;
        private readonly byte[] id = new byte[3];

        private bool selected;
        private bool csWired;
        private bool inReset;
        private bool pixelsInSession;
        private bool stateChanged;
        private uint lastCrc;
        private byte command;
        private int readIndex;
        private int paramIndex;
        private uint pending;
        private int pendingCount;
        private byte madctl;
        private byte colmod;
        private bool displayOn;
        private bool sleeping;
        private bool inverted;
        private ushort xs, xe, ys, ye, x, y;

        private const int Width = 160;
        private const int Height = 128;
        private const long DataOffset = 0x40000;
        private const byte ExpectedMadctl = 0xA0;
    }
}
