import logging
import sys

import cffi

from _remote import ffi, lib
from manager import PluginBase
import util

# Ironclad gear swap for Windows SBPE.
#
# Hammer swings are made in a Frenzied Berserker and mid-air boosts in
# Boosters, while you keep your normal armor and Stompers on. Each item is
# worn only inside the one game call that reads it, so nothing ever shows as
# equipped, not even for a frame.
#
# Measured in the Steam Windows client (the same build volta6 targets):
# - Hammer::doUse is where a swing works out its damage: it calls
#   Item::getDamageGenerator, which reads the player's damage, crit chance
#   and crit damage bonuses at that moment (+0x224/+0x228, +0x244/+0x248,
#   +0x24c/+0x250). Nothing reads them again later in the swing.
# - WorldClient::handleModifyInv charges 75 input buffer, then
#   PlayerCharacter::moveItem moves the item and runs recalculateBonuses
#   before returning.
# - Booster::doUse copies the boost's speed and length into its action; the
#   action never looks at the boots again, so the boost runs in full after
#   the Boosters come off.
# - A jump that can't jump normally (mid-air, air uses left, nothing else
#   going on) calls the boots' doUse(0x10). Plain boots such as Stompers are
#   class Item, whose doUse does nothing.
# Every address below is checked against the expected bytes first; on any
# other build the plugin logs it and stays off.

HANDLE_MODIFY_INV = 0x4db870
HANDLE_MODIFY_INV_SIG = bytes.fromhex('5589e5565389ce83ec108b5d088b4948')
MODIFY_INV_CTOR = 0x53f670
MODIFY_INV_CTOR_SIG = bytes.fromhex('55c701')  # followed by the vtable
HAMMER_DOUSE = 0x438c30
HAMMER_DOUSE_SIG = bytes.fromhex('5589e557565381ecbc0100008d7de88d')
HAMMER_SLOT = 0x68d37c  # _ZTV6Hammer + 8 + 5 * 4: doUse
ITEM_DOUSE = 0x5a0590
ITEM_DOUSE_SIG = bytes.fromhex('5589e55dc20400')
ITEM_SLOT = 0x68c17c  # _ZTV4Item + 8 + 5 * 4: doUse of plain items
GET_ITEM = 0x41bd00
GET_ITEM_SIG = bytes.fromhex('5581c18402000089e5508d4508890424')
TERRAIN = 0x443280  # NativeObject::attachedTerrainType, 0 = mid-air
TERRAIN_SIG = bytes.fromhex('5589e583ec08e865ffffff')
USE_INDEX = 5  # vtable index of Item::doUse
CAN_ACT_INDEX = 41  # vtable index of canDoAction (PlayerCharacter::canDoAction)

INPUT_COST = 75
MODINV_MOVE = 2
AIR_USE = 0x10
SLOT_MAIN = 0x100
SLOT_BODY = 0x400
SLOT_MOB = 0x500
SLOT_INV = 0xf00
# what Item::getDamageGenerator adds up: (base, gear bonus)
DAMAGE = (0x224, 0x228)
CRIT = (0x244, 0x248)
CRIT_DAMAGE = (0x24c, 0x250)
# armor, speed, jump, damage, crit damage, crit chance. None of these is a
# capacity; max HP (2), max ammo (4), rockets (27) would be cut on the way out
SAFE_STATS = {10, 11, 12, 18, 20, 23}

# cdecl -> thiscall with one argument: uint32 thunk(this, function, arg)
CALL_THUNK = bytes.fromhex(
    '5589e5'    # push ebp; mov ebp, esp
    '83e4f0'    # and esp, -16
    '83ec0c'    # sub esp, 12
    '8b4d08'    # mov ecx, [ebp+8]
    '8b450c'    # mov eax, [ebp+12]
    'ff7510'    # push [ebp+16]
    'ffd0'      # call eax
    '89ec5dc3'  # mov esp, ebp; pop ebp; ret
)


def entry_thunk(cb_cell, orig_cell):
    '''thiscall doUse(this, use) -> __stdcall cb(this, use), or straight to
    the original while cb_cell is 0. The callback's `ret 8` removes the
    pushed `this` and the caller's `use`, which is what the caller expects
    from a thiscall with one argument.'''
    return (b'\xa1' + cb_cell.to_bytes(4, 'little') +   # mov eax, [cb_cell]
            b'\x85\xc0'                                 # test eax, eax
            b'\x74\x05'                                 # jz passthrough
            b'\x5a\x51\x52'                             # pop edx; push ecx; push edx
            b'\xff\xe0'                                 # jmp eax
            b'\xff\x25' + orig_cell.to_bytes(4, 'little'))  # jmp [orig_cell]


THUNK_MAGIC = b'IRNS'
CALL_AT = 0x80  # CALL_THUNK's offset in the page
HOOKS = (('hammer', HAMMER_SLOT, HAMMER_DOUSE), ('plain', ITEM_SLOT, ITEM_DOUSE))

kffi = cffi.FFI()
kffi.cdef('''
typedef uint32_t (__stdcall *iron_hook)(uint32_t self, uint32_t use);
typedef uint32_t (*iron_call)(uint32_t self, uint32_t fn, uint32_t arg);
void *__stdcall VirtualAlloc(void *addr, size_t size, uint32_t type, uint32_t protect);
int __stdcall VirtualProtect(void *addr, size_t size, uint32_t protect, uint32_t *old);
void *__stdcall GetCurrentProcess(void);
int __stdcall ReadProcessMemory(void *proc, const void *addr, void *buf, size_t size, size_t *read);
int __stdcall FlushInstructionCache(void *proc, const void *addr, size_t size);
''')


class Native(object):
    '''Everything that touches the game. Addresses are ints.'''

    def __init__(self, plugin):
        self.plugin = plugin
        self.k32 = kffi.dlopen('kernel32.dll')
        self.call = None
        self.vtable = 0
        self.mine = {}  # cb cell -> this instance's callback

    def read(self, addr, size):
        buf = kffi.new('uint8_t[]', size)
        ok = self.k32.ReadProcessMemory(self.k32.GetCurrentProcess(), kffi.cast('void *', addr), buf, size, kffi.NULL)
        return bytes(kffi.buffer(buf)) if ok else None

    def u32(self, addr):
        return kffi.cast('uint32_t *', addr)[0]

    def i32(self, addr):
        return kffi.cast('int32_t *', addr)[0]

    def player(self):
        cw = self.plugin.refs.ClientWorld
        if cw == ffi.NULL or cw.player == ffi.NULL:
            return 0
        return int(ffi.cast('uintptr_t', cw.player))

    def world_client(self):
        wc = self.plugin.refs.WorldClient
        return 0 if wc == ffi.NULL else int(ffi.cast('uintptr_t', wc))

    def buffer(self):
        cw = self.plugin.refs.ClientWorld
        return 0 if cw == ffi.NULL else cw.inputBuffer

    def class_name(self, addr):
        return util.getClassName(ffi.cast('void *', addr)) if addr else ''

    def get_item(self, pc, slot):
        return self.call(pc, GET_ITEM, slot)

    def bag(self, pc):
        '''bag slot -> item, from PlayerCharacter.items (node = [next, key, Item*])'''
        found = {}
        ht = ffi.cast('struct PlayerCharacter *', pc).items.t
        node = ffi.cast('void **', ht.before_begin)
        n = 0
        while node != ffi.NULL and n < min(ht.element_count, 512):
            key = int(ffi.cast('uintptr_t', node[1])) & 0xffff
            if SLOT_INV <= key < SLOT_INV + 0x100 and node[2] != ffi.NULL:
                found[key] = int(ffi.cast('uintptr_t', node[2]))
            node = ffi.cast('void **', node[0])
            n += 1
        return found

    def item(self, addr):
        it = ffi.cast('struct Item *', addr)
        desc = it.desc
        stats = []
        for field in ((desc.statboosts,) if desc != ffi.NULL else ()) + (it.itemProps.statboosts,):
            for i in range(min(max(field.current_size, 0), 64)):
                boost = ffi.cast('struct StatBoost *', field.elements[i])
                stats.append(boost.stat)
        return {'user': int(ffi.cast('uintptr_t', it.user)), 'slot_pos': it.slotPos,
                'name': util.getstr(desc.name) if desc != ffi.NULL else '',
                'desc_slot': desc.slot if desc != ffi.NULL else -1,
                'stats': stats, 'cls': self.class_name(addr)}

    def can_act(self, pc):
        fn = self.u32(self.u32(pc) + CAN_ACT_INDEX * 4)
        return bool(self.call(pc, fn, 0) & 0xff)

    def airborne(self, pc):
        return self.call(pc, TERRAIN, 0) == 0

    def move(self, frm, to):
        msg = kffi.new('uint32_t[6]', [self.vtable, MODINV_MOVE, frm, to, 0, 7])
        self.call(self.world_client(), HANDLE_MODIFY_INV, int(kffi.cast('uintptr_t', msg)))

    def bonuses(self, pc):
        return tuple(self.i32(pc + a) + self.i32(pc + b) for a, b in (DAMAGE, CRIT, CRIT_DAMAGE))

    def use(self, fn, item, use):
        return self.call(item, fn, use)

    def item_use_fn(self, item):
        return self.u32(self.u32(item) + USE_INDEX * 4)

    def install(self, callbacks):
        '''Point the two vtable entries at entry thunks that call `callbacks`.
        The thunk page lives in sys so a reloaded plugin reuses it.'''
        checks = ((HANDLE_MODIFY_INV, HANDLE_MODIFY_INV_SIG), (MODIFY_INV_CTOR, MODIFY_INV_CTOR_SIG),
                  (HAMMER_DOUSE, HAMMER_DOUSE_SIG), (ITEM_DOUSE, ITEM_DOUSE_SIG),
                  (GET_ITEM, GET_ITEM_SIG), (TERRAIN, TERRAIN_SIG))
        for addr, sig in checks:
            if self.read(addr, len(sig)) != sig:
                return 'unknown client build (bytes at %#x differ)' % addr
        ctor = self.read(MODIFY_INV_CTOR, 7)
        self.vtable = int.from_bytes(ctor[3:], 'little')
        state = getattr(sys, '_sbpe_ironclad_swap', None)
        if state is None:
            page = self.k32.VirtualAlloc(kffi.NULL, 4096, 0x3000, 0x40)  # commit|reserve, RWX
            if page == kffi.NULL:
                return 'VirtualAlloc failed'
            page = int(kffi.cast('uintptr_t', page))
            kffi.memmove(kffi.cast('void *', page + CALL_AT), CALL_THUNK, len(CALL_THUNK))
            for i, (name, slot, orig) in enumerate(HOOKS):
                at = page + 0x40 * i
                cb_cell, orig_cell = at + 0x20, at + 0x24
                kffi.cast('uint32_t *', cb_cell)[0] = 0
                kffi.cast('uint32_t *', orig_cell)[0] = orig
                code = entry_thunk(cb_cell, orig_cell)
                kffi.memmove(kffi.cast('void *', at), code, len(code))
                kffi.memmove(kffi.cast('void *', at + 0x28), THUNK_MAGIC, 4)
            self.k32.FlushInstructionCache(self.k32.GetCurrentProcess(), kffi.cast('void *', page), 4096)
            state = {'page': page, 'keep': []}
            setattr(sys, '_sbpe_ironclad_swap', state)
        page = state['page']
        self.call = kffi.cast('iron_call', page + CALL_AT)
        for i, (name, slot, orig) in enumerate(HOOKS):
            at = page + 0x40 * i
            current = self.u32(slot)
            if current not in (orig, at):
                return '%s vtable entry is %#x, not the game\'s or ours' % (name, current)
            cb = kffi.callback('iron_hook', callbacks[name])
            state['keep'].append(cb)  # callbacks live as long as the game
            self.mine[at + 0x20] = int(kffi.cast('uintptr_t', cb))
            kffi.cast('uint32_t *', at + 0x20)[0] = self.mine[at + 0x20]
            if current != at:
                old = kffi.new('uint32_t *')
                if not self.k32.VirtualProtect(kffi.cast('void *', slot), 4, 0x04, old):  # PAGE_READWRITE
                    kffi.cast('uint32_t *', at + 0x20)[0] = 0
                    return 'VirtualProtect failed'
                kffi.cast('uint32_t *', slot)[0] = at
                self.k32.VirtualProtect(kffi.cast('void *', slot), 4, old[0], old)
        return None

    def release(self):
        '''Send calls straight to the game again (plugin unloaded). A cell a
        reloaded copy already took over is left alone.'''
        for cell, cb in self.mine.items():
            if self.u32(cell) == cb:
                kffi.cast('uint32_t *', cell)[0] = 0
        self.mine = {}


class Swapper(object):
    '''The swap rules, independent of how the game is reached.'''

    def __init__(self, native, config, log):
        self.n = native
        self.config = config
        self.log = log
        self.busy = False
        self.pending = None
        self.swings = self.boosts = self.skipped_buffer = 0
        self.said = set()

    def once(self, key, message):
        if key not in self.said:
            self.said.add(key)
            self.log(message)

    def common(self, item, slot):
        '''our Ironclad, the item worn in `slot`, the call will act, buffer
        for both moves'''
        if self.busy or self.pending:
            return None
        pc = self.n.player()
        # Ironclad only: any other character's swings and jumps pass through
        if not pc or self.n.class_name(pc) != 'Ironclad':
            return None
        info = self.n.item(item)
        if info['user'] != pc or info['slot_pos'] != slot or not self.n.can_act(pc):
            return None
        need = 2 * INPUT_COST + max(self.config.buffer_reserve, 0)
        if self.n.buffer() < need:
            self.skipped_buffer += 1
            self.once('buffer', 'ironclad_swap: input buffer below %d; skipping swaps until it refills' % need)
            return None
        return pc, info

    def safe(self, info, what):
        if all(stat in SAFE_STATS for stat in info['stats']):
            return True
        self.once('unsafe', 'ironclad_swap: %s carries a capacity stat (max HP/ammo/rockets); %s swap off'
                  % (info['name'] or 'worn item', what))
        return False

    def find(self, pc, slot, want, cls=None):
        want = want.lower()
        for key, item in sorted(self.n.bag(pc).items()):
            info = self.n.item(item)
            if info['desc_slot'] != slot or (cls and info['cls'] != cls):
                continue
            if want and want in info['name'].lower():
                return key, item
        return None, None

    def swap_in(self, pc, worn_slot, bag_slot, worn, swap):
        self.n.move(bag_slot, worn_slot)
        return self.n.get_item(pc, worn_slot) == swap and self.n.get_item(pc, bag_slot) == worn

    def swap_out(self, pc, worn_slot, bag_slot, worn, swap):
        self.n.move(bag_slot, worn_slot)
        if self.n.get_item(pc, worn_slot) == worn:
            return True
        self.pending = (pc, worn_slot, bag_slot, worn, swap)
        self.log('ironclad_swap: putting the worn item back was refused; retrying each frame')
        return False

    def on_hammer(self, item, use, original):
        plan = None
        if self.config.berserker:
            got = self.common(item, SLOT_MAIN)
            if got:
                pc = got[0]
                worn = self.n.get_item(pc, SLOT_BODY)
                want = self.config.berserker_name
                if worn:
                    worn_info = self.n.item(worn)
                    if want.lower() not in worn_info['name'].lower() and self.safe(worn_info, 'Berserker'):
                        key, berserker = self.find(pc, SLOT_BODY, want)
                        if berserker:
                            plan = (pc, SLOT_BODY, key, worn, berserker, worn_info['name'])
                        else:
                            self.once('no-berserker', 'ironclad_swap: no "%s" in the bag; swings stay normal' % want)
        if not plan:
            return original(item, use)
        pc, worn_slot, bag_slot, worn, swap, armor = plan
        normal = self.n.bonuses(pc)
        self.busy = True
        try:
            if not self.swap_in(pc, worn_slot, bag_slot, worn, swap):
                return original(item, use)
            during = self.n.bonuses(pc)
            try:
                return original(item, use)
            finally:
                back = self.swap_out(pc, worn_slot, bag_slot, worn, swap)
                self.swings += 1
                if self.swings == 1 or self.swings % 25 == 0:
                    boots = self.n.get_item(pc, SLOT_MOB)
                    self.log('ironclad_swap: swing #%d made in the Frenzied Berserker with %s on: damage bonus %d '
                             '(normally %d), crit chance %d (normally %d), crit damage %d (normally %d); %s back on. '
                             'Skipped for low buffer: %d'
                             % (self.swings, self.n.item(boots)['name'] if boots else 'no boots', during[0], normal[0],
                                during[1], normal[1], during[2], normal[2], armor if back else 'armor NOT',
                                self.skipped_buffer))
        finally:
            self.busy = False

    def on_plain(self, item, use, original):
        plan = None
        if use == AIR_USE and self.config.boosters:
            got = self.common(item, SLOT_MOB)
            if got and self.n.airborne(got[0]) and self.safe(got[1], 'Boosters'):
                pc, info = got
                want = self.config.booster_name
                key, boosters = self.find(pc, SLOT_MOB, want, 'Booster')
                if boosters:
                    plan = (pc, SLOT_MOB, key, item, boosters, info['name'])
                else:
                    self.once('no-boosters', 'ironclad_swap: no "%s" in the bag; mid-air jumps stay normal' % want)
        if not plan:
            return original(item, use)
        pc, worn_slot, bag_slot, worn, swap, boots = plan
        self.busy = True
        try:
            if not self.swap_in(pc, worn_slot, bag_slot, worn, swap):
                return original(item, use)
            try:
                # the Boosters' own doUse (Booster::doUse) makes the boost
                return self.n.use(self.n.item_use_fn(swap), swap, use)
            finally:
                back = self.swap_out(pc, worn_slot, bag_slot, worn, swap)
                self.boosts += 1
                if self.boosts == 1 or self.boosts % 25 == 0:
                    self.log('ironclad_swap: boost #%d started in the Boosters; %s back on'
                             % (self.boosts, boots if back else 'boots NOT'))
        finally:
            self.busy = False

    def frame(self):
        '''retry a refused swap-back; give up if anything changed meanwhile'''
        if not self.pending:
            return
        pc, worn_slot, bag_slot, worn, swap = self.pending
        if self.n.player() != pc or self.n.get_item(pc, worn_slot) != swap or self.n.get_item(pc, bag_slot) != worn:
            self.pending = None
            return
        if self.n.buffer() < INPUT_COST:
            return
        self.n.move(bag_slot, worn_slot)
        if self.n.get_item(pc, worn_slot) == worn:
            self.pending = None
            self.log('ironclad_swap: worn item restored')


class Plugin(PluginBase):
    def onInit(self):
        self.config.option('berserker', True, 'bool')
        # bag item to swing in (the name only has to contain this)
        self.config.option('berserker_name', 'Frenzied Berserker')
        self.config.option('boosters', True, 'bool')
        # bag boots to boost with; "Boosters" also matches Overloaded Boosters
        self.config.option('booster_name', 'Boosters')
        # input buffer left spare after the two inventory moves (75 each)
        self.config.option('buffer_reserve', 75, 'int')
        self.native = Native(self)
        self.swapper = Swapper(self.native, self.config, logging.info)
        self.active = False
        error = self.native.install({'hammer': self.hammer_hook, 'plain': self.plain_hook})
        if error:
            logging.error('ironclad_swap: off, %s', error)
            return
        self.active = True
        logging.info('ironclad_swap: on (Ironclad only; other characters untouched). Berserker=%s Boosters=%s reserve=%d',
                     self.config.berserker_name, self.config.booster_name, self.config.buffer_reserve)

    def _hook(self, handler, fn, this, use):
        '''A Python error must never eat a swing or a jump: the game's own
        call still runs, once.'''
        called = []

        def original(item, u):
            called.append(1)
            return self.native.use(fn, item, u)
        try:
            if not self.active:
                return original(this, use)
            return handler(this, use, original)
        except Exception:
            logging.exception('ironclad_swap: hook error')
            return 0 if called else self.native.use(fn, this, use)

    def hammer_hook(self, this, use):
        return self._hook(self.swapper.on_hammer, HAMMER_DOUSE, this, use)

    def plain_hook(self, this, use):
        return self._hook(self.swapper.on_plain, ITEM_DOUSE, this, use)

    def beforeUpdate(self):
        if self.active:
            try:
                self.swapper.frame()
            except Exception:
                logging.exception('ironclad_swap: frame error')

    def __del__(self):
        # calls go straight to the game again until a reloaded copy takes over
        if getattr(self, 'active', False):
            self.native.release()
