"""Execute the sender thunks as 32-bit x86 (no Windows/game process).

Requires the optional development dependency: pip install unicorn
Pass --client /path/to/mvmmoclient.exe to also check the owned PE signatures.
"""
import argparse
import struct
from pathlib import Path

from unicorn import Uc, UC_ARCH_X86, UC_MODE_32
from unicorn.x86_const import UC_X86_REG_EAX, UC_X86_REG_EBP, UC_X86_REG_EBX, UC_X86_REG_ECX, UC_X86_REG_EDI, UC_X86_REG_ESI, UC_X86_REG_ESP

from test_ironclad_swap_logic import iron

PAGE = 0x10000000
STACK = 0x20000000
END = 0x30000000
OUT = PAGE + 0x2000
THIS, KIND, MESSAGE = 0x12345678, 14, 0x76543210


def u32(value):
    return struct.pack('<I', value)


def machine():
    uc = Uc(UC_ARCH_X86, UC_MODE_32)
    uc.mem_map(PAGE, 0x4000)
    uc.mem_map(STACK, 0x10000)
    uc.mem_map(END, 0x1000)
    uc.mem_map(iron.SEND_MESSAGE & ~0xfff, 0x2000)
    for register in (UC_X86_REG_EBP, UC_X86_REG_EBX, UC_X86_REG_ESI, UC_X86_REG_EDI):
        uc.reg_write(register, 0xabcdef01)
    return uc


def record_native():
    return (b'\x89\x0d' + u32(OUT) +  # ecx=this
            b'\x8b\x44\x24\x04\xa3' + u32(OUT + 4) +
            b'\x8b\x44\x24\x08\xa3' + u32(OUT + 8) +
            b'\x89\xe0\x83\xe0\x0f\xa3' + u32(OUT + 12) +
            b'\xc2\x08\x00')


def execute(uc, entry, arguments):
    sp = STACK + 0x800c
    uc.mem_write(sp, b''.join(u32(x) for x in [END] + arguments))
    uc.reg_write(UC_X86_REG_ESP, sp)
    uc.reg_write(UC_X86_REG_ECX, THIS)
    uc.emu_start(entry, END, count=200)
    for register in (UC_X86_REG_EBP, UC_X86_REG_EBX, UC_X86_REG_ESI, UC_X86_REG_EDI):
        assert uc.reg_read(register) == 0xabcdef01
    return sp, struct.unpack('<IIII', uc.mem_read(OUT, 16))


def test_sender_call():
    uc = machine()
    fn = PAGE + 0x1000
    uc.mem_write(PAGE, iron.SEND_CALL_THUNK)
    uc.mem_write(fn, record_native())
    sp, seen = execute(uc, PAGE, [THIS, fn, KIND, MESSAGE])
    assert seen == (THIS, KIND, MESSAGE, 12), seen
    assert uc.reg_read(UC_X86_REG_ESP) == sp + 4  # cdecl caller owns args


def test_sender_entry(active):
    uc = machine()
    fn = PAGE + 0x1000
    cb, orig = PAGE + 0x300, PAGE + 0x304
    uc.mem_write(PAGE, iron.entry_thunk(cb, orig))
    uc.mem_write(cb, u32(fn if active else 0))
    uc.mem_write(orig, u32(fn))
    if active:
        # Model __stdcall callback(this,type,message), including ret 12.
        code = b''
        for i in range(3):
            code += b'\x8b\x44\x24' + bytes([4 + i * 4]) + b'\xa3' + u32(OUT + i * 4)
        code += b'\xc2\x0c\x00'
    else:
        code = record_native()
    uc.mem_write(fn, code)
    sp, seen = execute(uc, PAGE, [KIND, MESSAGE])
    assert seen[:3] == (THIS, KIND, MESSAGE)
    assert uc.reg_read(UC_X86_REG_ESP) == sp + 12


def test_passthrough_trampoline():
    uc = machine()
    at = PAGE + iron.SEND_AT
    original = PAGE + iron.SEND_ORIGINAL_AT
    uc.mem_write(at, iron.entry_thunk(at + 0x20, at + 0x24))
    uc.mem_write(at + 0x20, u32(0) + u32(original))
    uc.mem_write(original, iron.SEND_MESSAGE_SIG + iron.jump_to(original + len(iron.SEND_MESSAGE_SIG), iron.SEND_MESSAGE + len(iron.SEND_MESSAGE_SIG)))
    uc.mem_write(iron.SEND_MESSAGE, iron.sender_patch(PAGE))
    # Original sender body uses ebp args after the replayed native prologue.
    body = (b'\x89\x0d' + u32(OUT) +
            b'\x8b\x45\x08\xa3' + u32(OUT + 4) +
            b'\x8b\x45\x0c\xa3' + u32(OUT + 8) +
            b'\x8b\x5d\xfc\x89\xec\x5d\xc2\x08\x00')
    uc.mem_write(iron.SEND_MESSAGE + len(iron.SEND_MESSAGE_SIG), body)
    sp, seen = execute(uc, iron.SEND_MESSAGE, [KIND, MESSAGE])
    assert seen[:3] == (THIS, KIND, MESSAGE)
    assert uc.reg_read(UC_X86_REG_ESP) == sp + 12


def check_client(path):
    data = Path(path).read_bytes()
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    assert data[pe:pe + 4] == b'PE\0\0'
    machine_id, sections = struct.unpack_from('<HH', data, pe + 4)
    optional = struct.unpack_from('<H', data, pe + 20)[0]
    assert machine_id == 0x14c and struct.unpack_from('<H', data, pe + 24)[0] == 0x10b
    base = struct.unpack_from('<I', data, pe + 24 + 28)[0]
    def read(address, size):
        for i in range(sections):
            _, virtual_size, virtual, raw_size, raw = struct.unpack_from('<8sIIII', data, pe + 24 + optional + i * 40)
            offset = address - base - virtual
            if 0 <= offset and offset + size <= raw_size:
                return data[raw + offset:raw + offset + size]
        raise AssertionError('address not in a PE section')
    for name in ('HANDLE_MODIFY_INV', 'MODIFY_INV_CTOR', 'HAMMER_DOUSE', 'ITEM_DOUSE', 'GET_ITEM', 'TERRAIN', 'SEND_MESSAGE'):
        sig = getattr(iron, name + '_SIG')
        assert read(getattr(iron, name), len(sig)) == sig, name
    for _, slot, original in iron.HOOKS:
        assert struct.unpack('<I', read(slot, 4))[0] == original
    print('PASS: all seven Windows native signatures and both original vtable slots match the owned PE32 client')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--client')
    args = parser.parse_args()
    test_sender_call()
    test_sender_entry(True)
    test_sender_entry(False)
    test_passthrough_trampoline()
    print('PASS: 32-bit x86 sender arguments, stack alignment/cleanup, preserved registers, callback/pass-through and trampoline')
    if args.client:
        check_client(args.client)
