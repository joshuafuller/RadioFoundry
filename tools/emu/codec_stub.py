# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
#
# A stand-in for the AMBE+2 voice codec of an STM32 radio in Renode, without the codec.
#
# The radio runs the stock codec in place from flash (platform/stm32f4/hrc/vocoder.c). This repository never holds that
# code (codecs/ is ignored), so in Renode present() is false and the radio refuses voice: PTT on a DMR channel shows
# "Can't transmit / No codec: voice off" (core/radio/radio_sm.c dmr_ptt_down, TXDENY_NO_CODEC) and no burst goes out.
#
# This file puts a hook on five entries of the board's codec table, the way hostport.py does for the host port: the hook
# does the job, sets R0, jumps to LR, and the real body never runs. The binary is unchanged; the five addresses are read
# out of the stm32_vocoder table (hal/hal_vocoder.h field order: present, reset, encode, decode, profile) in the ELF.
#   present: true, so the radio keys up DMR voice.
#   reset:   nothing to reset.
#   encode:  49 zero bits (VOCODER_BITS) for every 20 ms frame: the voice bursts carry silence-shaped zeros, not speech.
#   decode:  160 zero samples (VOCODER_FRAME_SAMPLES) of PCM: received voice plays as silence.
#   profile: NULL (no codec profile found in flash), as the hal allows. Hooked too: a hook fires at the function's
#            address, so the real profile() would get true from its own present() call and read a profile it never found.
#
# What this changes: only the voice payload bits. The call set-up, LC header, embedded LC, terminator, slot timing and
# everything the DMR stack and the HR_C5000 model do is the real firmware.
#
# Loaded after the ELF: `include @tools/emu/codec_stub.py`, then `setup_codec_stub`.

def mc_setup_codec_stub():
    bus = monitor.Machine.SystemBus
    cpu = bus.GetCPUs()[0]

    found, addrs = bus.TryGetAllSymbolAddresses("stm32_vocoder", context=cpu)
    if not found:
        raise Exception("codec_stub: no stm32_vocoder symbol; load the ELF first")
    table = list(addrs)[0]
    names = ("present", "reset", "encode", "decode", "profile")
    fn = {}
    for i, n in enumerate(names):
        fn[n] = bus.ReadDoubleWord(table + 4 * i) & ~1

    def ret(c, value):
        c.SetRegisterUlong(0, value)
        c.PC = c.LR

    def zero(addr, size):
        for i in range(0, size - size % 4, 4):
            bus.WriteDoubleWord(addr + i, 0)
        for i in range(size - size % 4, size):
            bus.WriteByte(addr + i, 0)

    def h_present(c, _):
        ret(c, 1)

    def h_reset(c, _):
        ret(c, 0)

    def h_encode(c, _):
        zero(c.GetRegister(1).RawValue, 49)       # bits[VOCODER_BITS]
        ret(c, 0)

    def h_decode(c, _):
        zero(c.GetRegister(1).RawValue, 160 * 2)  # pcm[VOCODER_FRAME_SAMPLES]
        ret(c, 0)

    hooks = {"present": h_present, "reset": h_reset, "encode": h_encode, "decode": h_decode, "profile": h_reset}
    for n in names:
        cpu.AddHook(fn[n], hooks[n])
        cpu.InfoLog("codec_stub: hook %s at 0x%08x" % (n, fn[n]))
