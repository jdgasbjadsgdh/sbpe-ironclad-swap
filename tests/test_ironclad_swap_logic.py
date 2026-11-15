'''Offline test of the swap rules in ironclad_swap.py.

Runs on any Python 3 (no game, no cffi): the SBPE modules are stubbed so the
plugin imports, and Swapper is driven through a simulated game. Its moves
cost 75 input buffer, swap the two slots and recalculate the damage, crit
chance and crit damage bonuses from the worn armor and boots, like
PlayerCharacter::moveItem does.

    python3 tests/test_ironclad_swap_logic.py
'''
import os
import sys
import types
from types import SimpleNamespace as NS

# --- stub the SBPE environment so the plugin module imports ----------------
stubs = {
    '_remote': NS(ffi=NS(NULL=None, cast=lambda t, v: v), lib=NS()),
    'manager': NS(PluginBase=object),
    'util': NS(getClassName=lambda p: '', getstr=lambda s: ''),
    'cffi': NS(FFI=lambda: NS(cdef=lambda s: None, NULL=None)),
}
for name, attrs in stubs.items():
    mod = types.ModuleType(name)
    mod.__dict__.update(vars(attrs))
    sys.modules[name] = mod
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import ironclad_swap as iron  # noqa: E402

BOOSTER_FN = 0xb0057e5


class Game(object):
    '''A simulated Ironclad: items are ints, slots map to items.'''

    def __init__(self):
        self.pc = 0x1000
        self.cls = 'Ironclad'
        self.buffer_ = 1000
        self.acting = True
        self.terrain = 0
        self.refuse = 0
        self.moves = 0
        self.events = []
        self.world = 0x3000
        self.connection = 0x4000
        self.items = {}
        self.slots = {}
        self.calls = []

    def add(self, item, name, slot_pos, desc_slot, stats=(), cls='Item', user=None):
        self.items[item] = {'name': name, 'desc_slot': desc_slot, 'stats': list(stats), 'cls': cls,
                            'user': self.pc if user is None else user}
        self.slots[slot_pos] = item

    # --- the Native interface Swapper uses ---
    def player(self):
        return self.pc

    def class_name(self, addr):
        return self.cls if addr == self.pc else self.items[addr]['cls']

    def slot_of(self, item):
        for slot, it in self.slots.items():
            if it == item:
                return slot
        return -1

    def item(self, addr):
        it = self.items[addr]
        return {'user': it['user'], 'slot_pos': self.slot_of(addr), 'name': it['name'], 'desc_slot': it['desc_slot'],
                'stats': [s for s, v in it['stats']], 'cls': it['cls']}

    def get_item(self, pc, slot):
        assert pc == self.pc
        return self.slots.get(slot, 0)

    def bag(self, pc):
        return {s: i for s, i in self.slots.items() if iron.SLOT_INV <= s < iron.SLOT_INV + 0x100}

    def buffer(self):
        return self.buffer_

    def identity(self):
        return (0x2000, self.world, self.connection)

    def can_act(self, pc):
        return self.acting

    def airborne(self, pc):
        return self.terrain == 0

    def move(self, frm, to):
        self.moves += 1
        if self.refuse:
            self.refuse -= 1
            return
        if self.buffer_ < iron.INPUT_COST:
            return  # ClientWorld::addModifyInv refuses
        self.buffer_ -= iron.INPUT_COST
        a, b = self.slots.get(frm), self.slots.get(to)
        self.slots.pop(frm, None)
        self.slots.pop(to, None)
        if a:
            self.slots[to] = a
        if b:
            self.slots[frm] = b
        self.events.append(18)

    def send(self, connection, kind, message=None):
        self.events.append(kind)

    def bonuses(self, pc):
        damage, crit, crit_damage = 0, 75, 150
        for slot in (iron.SLOT_BODY, iron.SLOT_MOB):
            for stat, val in self.items.get(self.slots.get(slot), {}).get('stats', []):
                damage += val if stat == 18 else 0
                crit += val if stat == 23 else 0
                crit_damage += val if stat == 20 else 0
        return damage, crit, crit_damage

    def use(self, fn, item, use):
        if fn == iron.HAMMER_DOUSE:
            self.calls.append(('swing', self.slots.get(iron.SLOT_BODY), self.bonuses(self.pc)))
            return 0x1234
        if fn == BOOSTER_FN:
            self.calls.append(('boost', self.slots.get(iron.SLOT_MOB), use))
            return 7
        if fn == iron.ITEM_DOUSE:
            self.calls.append(('plain', item, use))
            return 0
        raise AssertionError('unexpected call %#x' % fn)

    def item_use_fn(self, item):
        return BOOSTER_FN if self.items[item]['cls'] == 'Booster' else iron.ITEM_DOUSE


HAMMER, PLATING, BERSERKER, STOMPERS, BOOSTERS, GLIDERS, FILLER = range(1, 8)


def setup(**config):
    g = Game()
    g.add(HAMMER, 'Adjudicator', iron.SLOT_MAIN, iron.SLOT_MAIN, cls='Hammer')
    g.add(PLATING, 'Resilient Plating', iron.SLOT_BODY, iron.SLOT_BODY, [(10, 40), (11, -4)])
    g.add(STOMPERS, 'Spiked Stompers', iron.SLOT_MOB, iron.SLOT_MOB, [(18, 30), (10, 4), (23, 100), (20, 25)])
    g.add(FILLER, 'Bandolier', iron.SLOT_INV, iron.SLOT_BODY, [(27, 1)])
    g.add(BERSERKER, 'Frenzied Berserker', iron.SLOT_INV + 2, iron.SLOT_BODY, [(18, 60), (10, -30), (23, 300), (20, 100)])
    g.add(GLIDERS, 'Gliders', iron.SLOT_INV + 4, iron.SLOT_MOB, cls='Booster')
    g.add(BOOSTERS, 'Overloaded Boosters', iron.SLOT_INV + 5, iron.SLOT_MOB, cls='Booster')
    cfg = dict(berserker=True, berserker_name='Frenzied Berserker', boosters=True, booster_name='Boosters',
               buffer_reserve=75)
    cfg.update(config)
    logs = []
    s = iron.Swapper(g, NS(**cfg), logs.append)
    return g, s, logs


def native_action(g, s, kind=iron.MT_PLAYER_INPUT):
    g.send(g.connection, kind)
    s.sent(g.connection, kind)


def swing(g, s, send=True):
    result = s.on_hammer(HAMMER, 1, lambda item, u: g.use(iron.HAMMER_DOUSE, item, u))
    if send:
        native_action(g, s)
    return result


def jump(g, s, item=STOMPERS, use=iron.AIR_USE, send=True):
    result = s.on_plain(item, use, lambda it, u: g.use(iron.ITEM_DOUSE, it, u))
    if send:
        native_action(g, s)
    return result


def start_state(g):
    return (g.slots[iron.SLOT_BODY], g.slots[iron.SLOT_MOB], g.slots[iron.SLOT_INV + 2], g.slots[iron.SLOT_INV + 5])


def refuse_second_move(g):
    '''the swap-back (second move) is refused once'''
    real_move, n = g.move, [0]

    def move(frm, to):
        n[0] += 1
        if n[0] == 2:
            g.moves += 1
            return
        real_move(frm, to)
    return move


def no_swap(g, s):
    assert swing(g, s) == 0x1234
    assert g.calls == [('swing', PLATING, (30, 175, 175))], g.calls
    assert g.moves == 0 and g.slots[iron.SLOT_BODY] == PLATING


def main():
    # A swing is made in the Berserker with the Stompers on; armor back after.
    g, s, logs = setup()
    assert swing(g, s) == 0x1234
    assert g.calls == [('swing', BERSERKER, (30 + 60, 75 + 100 + 300, 150 + 25 + 100))], g.calls
    assert start_state(g) == (PLATING, STOMPERS, BERSERKER, BOOSTERS) and g.moves == 2 and g.buffer_ == 850
    assert g.bonuses(g.pc) == (30, 175, 175)
    assert any('swing #1 made in the Frenzied Berserker with Spiked Stompers on: damage bonus 90 (normally 30)' in m
               for m in logs), logs
    # Repeated swings never drift.
    for _ in range(5):
        swing(g, s)
    assert start_state(g) == (PLATING, STOMPERS, BERSERKER, BOOSTERS) and g.moves == 12 and g.buffer_ == 100
    assert all(c[1] == BERSERKER for c in g.calls)
    # Buffer below 150 + reserve: the swing goes out normally.
    g, s, logs = setup(); g.buffer_ = 224; no_swap(g, s)
    assert any('input buffer below 225' in m for m in logs)
    g, s, _ = setup(); g.buffer_ = 225; swing(g, s); assert g.calls[0][1] == BERSERKER
    g, s, _ = setup(buffer_reserve=0); g.buffer_ = 150; swing(g, s); assert g.calls[0][1] == BERSERKER
    # The swing won't happen, not Ironclad, someone else's hammer, wrong slot, option off.
    g, s, _ = setup(); g.acting = False; no_swap(g, s)
    g, s, _ = setup(); g.cls = 'Wildfire'; no_swap(g, s)
    g, s, _ = setup(); g.items[HAMMER]['user'] = 0x2000; no_swap(g, s)
    g, s, _ = setup(); g.slots[0x101] = g.slots.pop(iron.SLOT_MAIN); no_swap(g, s)
    g, s, _ = setup(berserker=False); no_swap(g, s)
    # No Berserker in the bag; already wearing it.
    g, s, logs = setup(); del g.slots[iron.SLOT_INV + 2]; no_swap(g, s)
    assert any('no "Frenzied Berserker" in the bag' in m for m in logs)
    g, s, _ = setup()
    g.slots[iron.SLOT_BODY], g.slots[iron.SLOT_INV + 2] = BERSERKER, PLATING
    assert swing(g, s) == 0x1234 and g.calls[0][1] == BERSERKER and g.moves == 0
    # Armor with a max-HP stat would lose HP on the way out: never swapped.
    g, s, logs = setup(); g.items[PLATING]['stats'].append((2, 50)); no_swap(g, s)
    assert any('capacity stat' in m for m in logs)
    # A refused swap-back is retried by frame() until the armor is back.
    g, s, logs = setup()
    real_move = g.move
    g.move = refuse_second_move(g)
    swing(g, s)
    assert g.slots[iron.SLOT_BODY] == BERSERKER and s.pending
    g.move = real_move
    g.calls = []; moves = g.moves
    assert swing(g, s) == 0x1234 and g.moves == moves  # no new swaps while pending
    g.buffer_ = 50; s.frame(); assert g.slots[iron.SLOT_BODY] == BERSERKER and s.pending
    g.buffer_ = 80; s.frame(); assert g.slots[iron.SLOT_BODY] == PLATING and g.slots[iron.SLOT_INV + 2] == BERSERKER
    assert not s.pending and any('worn gear restored on retry' in m for m in logs)
    # The retry gives up if the items changed meanwhile.
    g, s, _ = setup()
    real_move = g.move
    g.move = refuse_second_move(g)
    swing(g, s); assert s.pending
    g.move = real_move
    g.slots[iron.SLOT_BODY], g.slots[iron.SLOT_INV] = FILLER, BERSERKER
    s.frame(); assert not s.pending and g.slots[iron.SLOT_BODY] == FILLER

    # A mid-air jump with Stompers on boosts in the Boosters; Stompers back after.
    g, s, logs = setup()
    assert jump(g, s) == 7
    assert g.calls == [('boost', BOOSTERS, iron.AIR_USE)], g.calls
    assert start_state(g) == (PLATING, STOMPERS, BERSERKER, BOOSTERS) and g.moves == 2 and g.buffer_ == 850
    assert g.slots[iron.SLOT_INV + 4] == GLIDERS  # Gliders don't match "Boosters"
    assert any('boost #1 started in the Boosters; Spiked Stompers restoration queued after native action' in m for m in logs), logs
    # On the ground, other uses, other slots, other players, can't act, low buffer, option off: untouched.
    for change in (lambda g: setattr(g, 'terrain', 1), lambda g: setattr(g, 'acting', False),
                   lambda g: setattr(g, 'buffer_', 200), lambda g: g.items[STOMPERS].update(user=0x2000),
                   lambda g: setattr(g, 'cls', 'Duskwing')):
        g, s, _ = setup(); change(g)
        assert jump(g, s) == 0 and g.calls == [('plain', STOMPERS, iron.AIR_USE)] and g.moves == 0
    g, s, _ = setup(); assert jump(g, s, use=0x20) == 0 and g.calls == [('plain', STOMPERS, 0x20)] and g.moves == 0
    g, s, _ = setup(); assert jump(g, s, item=PLATING) == 0 and g.calls[0][0] == 'plain' and g.moves == 0
    g, s, _ = setup(boosters=False); assert jump(g, s) == 0 and g.moves == 0
    # Only Gliders in the bag: no swap; booster_name can pick them instead.
    g, s, logs = setup(); del g.slots[iron.SLOT_INV + 5]
    assert jump(g, s) == 0 and g.moves == 0 and any('no "Boosters" in the bag' in m for m in logs)
    g, s, _ = setup(booster_name='Gliders')
    assert jump(g, s) == 7 and g.calls == [('boost', GLIDERS, iron.AIR_USE)] and g.slots[iron.SLOT_MOB] == STOMPERS
    # Boots with a capacity stat are never taken off.
    g, s, _ = setup(); g.items[STOMPERS]['stats'].append((4, 2)); assert jump(g, s) == 0 and g.moves == 0
    # A swing and a boost in a row leave everything where it started.
    g, s, _ = setup(); swing(g, s); jump(g, s)
    assert start_state(g) == (PLATING, STOMPERS, BERSERKER, BOOSTERS) and g.moves == 4 and g.buffer_ == 700

    # Do not restore before the original native input or simulation message.
    for kind in (iron.MT_PLAYER_INPUT, iron.MT_TIME_PASSES):
        g, s, _ = setup()
        assert swing(g, s, send=False) == 0x1234
        assert g.slots[iron.SLOT_BODY] == BERSERKER and g.events == [18]
        s.sent(g.connection, 13)  # an acknowledgement is not the action
        s.sent(g.connection + 1, kind)  # nor another connection's message
        assert g.moves == 1 and len(s.deferred) == 1
        native_action(g, s, kind)
        assert g.events == [18, kind, 18] and g.slots[iron.SLOT_BODY] == PLATING
        assert not s.deferred and not s.pending
    # Both swaps in one input restore only after that one message.
    g, s, _ = setup(); swing(g, s, send=False); jump(g, s, send=False)
    assert g.slots[iron.SLOT_BODY] == BERSERKER and g.slots[iron.SLOT_MOB] == BOOSTERS
    assert g.events == [18, 18] and len(s.deferred) == 2
    native_action(g, s)
    assert g.events == [18, 18, 14, 18, 18] and start_state(g) == (PLATING, STOMPERS, BERSERKER, BOOSTERS)
    # Budget must reserve the first unpaid restore before starting the second.
    g, s, _ = setup(); g.buffer_ = 299; swing(g, s, send=False); jump(g, s, send=False)
    assert g.moves == 1 and g.calls[-1][0] == 'plain'
    native_action(g, s); assert g.slots[iron.SLOT_BODY] == PLATING
    g, s, _ = setup(); g.buffer_ = 375; swing(g, s, send=False); jump(g, s, send=False)
    assert len(s.deferred) == 2
    native_action(g, s); assert g.buffer_ == 75
    # A world change with the same character pointer cancels old restores.
    g, s, _ = setup(); swing(g, s, send=False); g.world += 1
    native_action(g, s); assert g.moves == 1 and not s.deferred
    # Refused simultaneous restores retain BOTH exact pairs for recovery.
    g, s, _ = setup(); swing(g, s, send=False); jump(g, s, send=False); g.refuse = 2
    native_action(g, s); assert len(s.pending) == 2
    s.frame(); assert not s.pending and start_state(g) == (PLATING, STOMPERS, BERSERKER, BOOSTERS)
    # If no normal action is emitted, the next frame restores instead.
    g, s, logs = setup(); swing(g, s, send=False); s.frame()
    assert g.slots[iron.SLOT_BODY] == PLATING and any('frame fallback' in m for m in logs)
    # User-selected gear is never toggled during deferred restoration.
    g, s, _ = setup(); swing(g, s, send=False); g.slots[iron.SLOT_BODY] = FILLER
    native_action(g, s); assert g.moves == 1 and g.slots[iron.SLOT_BODY] == FILLER
    # A transient restore exception keeps the plan until a later frame.
    g, s, _ = setup(); swing(g, s, send=False)
    real_move = g.move
    def broken_move(frm, to):
        raise RuntimeError('restore temporarily unavailable')
    g.move = broken_move; native_action(g, s); assert len(s.pending) == 1
    g.move = real_move; s.frame(); assert not s.pending and g.slots[iron.SLOT_BODY] == PLATING

    # Plugin._hook: a Python error never eats the game's own call, and never runs it twice.
    g, _, _ = setup()
    plugin = object.__new__(iron.Plugin)
    plugin.native = g
    plugin.active = True

    def boom_before(item, use, original):
        raise RuntimeError('before')

    def boom_after(item, use, original):
        original(item, use)
        raise RuntimeError('after')
    plugin.swapper = NS(on_hammer=boom_before)
    iron.logging.disable(iron.logging.CRITICAL)
    assert plugin._hook(boom_before, iron.HAMMER_DOUSE, HAMMER, 1) == 0x1234 and len(g.calls) == 1
    g.calls = []
    plugin._hook(boom_after, iron.HAMMER_DOUSE, HAMMER, 1); assert len(g.calls) == 1
    g.calls = []; plugin.active = False
    assert plugin._hook(boom_before, iron.HAMMER_DOUSE, HAMMER, 1) == 0x1234 and len(g.calls) == 1
    # The entry thunk: mov eax,[cb]; test; jz +5; pop edx; push ecx; push edx; jmp eax; jmp [orig]
    t = iron.entry_thunk(0x10000020, 0x10000024)
    assert t == bytes.fromhex('a120000010' '85c0' '7405' '5a5152' 'ffe0' 'ff2524000010'), t.hex()
    # Sender callback forwards exactly once, before restoration, and never
    # swallows the native message when restoration raises.
    plugin.active = True
    def restore_boom(connection, kind):
        raise RuntimeError('restore')
    plugin.swapper = NS(sent=restore_boom)
    g.events = []; plugin.sent_hook(g.connection, 14, 0x123)
    assert g.events == [14]
    plugin.active = False; plugin.sent_hook(g.connection, 15, 0x456)
    assert g.events == [14, 15]
    patch = iron.sender_patch(0x10000000)
    assert len(patch) == len(iron.SEND_MESSAGE_SIG) and patch[5:] == b'\x90' * 5
    assert (iron.SEND_MESSAGE + 5 + int.from_bytes(patch[1:5], 'little')) & 0xffffffff == 0x10000000 + iron.SEND_AT
    assert iron.SEND_CALL_THUNK == bytes.fromhex('5589e583e4f083ec088b4d088b450cff7514ff7510ffd089ec5dc3')
    print('ironclad swap logic tests passed')


if __name__ == '__main__':
    main()
