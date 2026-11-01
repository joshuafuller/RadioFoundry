# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
#
# The USB CDC host port in Renode, without a USB model (docs/dev/virtual-radio-architecture.md).
#
# On the radio the debug link runs over USB CDC-ACM: board.host_port = &usb_cdc_stream (boards/md380/board.c), a
# hal_stream_t of five functions (drivers/usb/usb_device.c). Renode has no model of the STM32F4 USB core, so this file
# puts a hook on each of the five functions. A hook is Python that Renode runs when the CPU reaches an address. Each
# hook does the function's job against a Renode VirtualConsole (a byte queue that a socket terminal feeds), sets R0 to
# the return value and jumps to LR, so the real body never runs. The binary is unchanged: the hooks are found by
# reading the five function pointers out of the usb_cdc_stream table in the loaded ELF, not by patching it.
#
# What this skips: TinyUSB and the USB driver. What still runs for real: the hostlink task, the framer and the
# whole debug link (core/hostlink, core/debuglink).
#
# Loaded by boards/md380/emu/boot.resc: `include @tools/emu/hostport.py`, then `setup_hostport sysbus.hostport`.

def mc_setup_hostport(console):
    bus = monitor.Machine.SystemBus
    cpu = bus.GetCPUs()[0]

    found, addrs = bus.TryGetAllSymbolAddresses("usb_cdc_stream", context=cpu)
    if not found:
        raise Exception("hostport: no usb_cdc_stream symbol; load the ELF first")
    table = list(addrs)[0]

    # hal_stream_t field order (hal/hal_stream.h): start, connected, read, wait_rx, write. Thumb bit cleared.
    names = ("start", "connected", "read", "wait_rx", "write")
    fn = {}
    for i, n in enumerate(names):
        fn[n] = bus.ReadDoubleWord(table + 4 * i) & ~1

    def ret(c, value):
        c.SetRegisterUlong(0, value)
        c.PC = c.LR

    def h_start(c, _):
        ret(c, 1)       # no USB task: nothing to start, the port is "up"

    def h_connected(c, _):
        ret(c, 1)       # DTR is always set: a host is always there

    def h_read(c, _):
        buf = c.GetRegister(0).RawValue
        size = c.GetRegister(1).RawValue
        ret(c, console.WriteBufferToMemory(buf, size, c))

    def h_wait_rx(c, _):
        # With bytes waiting: return true at once. Without: let the real body run. cdc_start never ran, so its
        # semaphore is NULL and the body does vTaskDelay(timeout) and returns false, a real RTOS wait.
        if not console.IsEmpty():
            ret(c, 1)

    def h_write(c, _):
        buf = c.GetRegister(0).RawValue
        n = c.GetRegister(1).RawValue
        for i in range(n):
            console.DisplayChar(bus.ReadByte(buf + i))
        ret(c, n)

    hooks = {"start": h_start, "connected": h_connected, "read": h_read, "wait_rx": h_wait_rx, "write": h_write}
    for n in names:
        cpu.AddHook(fn[n], hooks[n])
        cpu.InfoLog("hostport: hook %s at 0x%08x" % (n, fn[n]))
