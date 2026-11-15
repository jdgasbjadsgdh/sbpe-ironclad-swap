# SBPE Ironclad gear swap (Windows)

An SBPE plugin for **StarBreak** on Windows that gives your **Ironclad** two
extra things while you keep your normal gear on:

- **Hammer swings use a Frenzied Berserker.** Every swing gets the Berserker's
  damage and crit bonuses, but you wear your normal armor (for example a
  Resilient Plating) the rest of the time.
- **Mid-air jumps use Boosters.** You keep your Stompers on, with their damage
  bonus, and still get a full boost whenever you jump in mid-air.

It only works while you play **Ironclad**. For any other character, swings and
jumps pass straight through untouched.

> **Status:** checked offline against the Steam Windows client's code and
> with the included test. It has not been run in the game on Windows yet, so
> check the log lines below the first time you play.

The action-order fix retains temporary gear until the game's original input
or time message is sent, then immediately restores it. It has offline Windows
logic and 32-bit x86 thunk coverage; this does not establish live Windows
flight or official-server damage acceptance.

## Install

1. Put `ironclad_swap.py` in the `plugins` folder of your SBPE install. That's
   the folder that also holds `pypy`, `manager.py` and `config.ini`.
2. Add the section in `config-snippet.ini` to `config.ini`.
3. Restart StarBreak through SBPE after replacing the plugin.

## Setup in game

1. Play Ironclad.
2. Wear your normal armor and your Stompers (or Spiked Stompers).
3. Keep a **Frenzied Berserker** and **Boosters** (Overloaded Boosters work
   too) anywhere in your **bag**. The stash doesn't count.

If you're wearing the Berserker or the Boosters yourself, there's nothing to
swap and the plugin does nothing.

## What you'll see

Nothing. Your inventory shows your armor and Stompers the whole time. That's
on purpose: restoration runs immediately after the native action message,
before the screen is drawn during normal processing. The SBPE log shows:

```
ironclad_swap: on (Ironclad only; other characters untouched). ...
ironclad_swap: swing #1 made in the Frenzied Berserker with Spiked Stompers on: damage bonus 90 (normally 30), ...
ironclad_swap: boost #1 started in the Boosters; Spiked Stompers restoration queued after native action
ironclad_swap: worn gear restored after native action message
```

It logs the first swing and boost, then every 25th one. Each swing line shows
the damage and crit numbers that swing actually used. If something is off, the
log says why, for example `no "Frenzied Berserker" in the bag`, or
`off, unknown client build`.

## How it works

The plugin points two entries in the game's own tables of functions at small
pieces of machine code that call the plugin first:

| Your action | Game function | What the plugin does |
|---|---|---|
| Hammer swing | `Hammer::doUse` | Equips the Berserker, starts the swing, queues armor restoration after the native action message |
| Jump in mid-air | `Item::doUse` on your plain boots | Equips the Boosters, starts the boost, queues Stompers restoration after the native action message |

The Windows client predicts the action before sending MT14 (input) or MT15
(time). Restoring inside `doUse` sent **equip → restore → action**. The sender
hook now forwards the original message exactly once, then restores the gear:
**equip → action → restore**. It does not create or rewrite input/time
messages. Acknowledgements and unrelated connections cannot trigger a restore.

Why that's enough (checked in the client's code):

- A hammer swing works out all of its damage inside `Hammer::doUse`, from your
  damage, crit chance and crit damage bonuses at that moment. Nothing reads
  them again later in the swing.
- An inventory move takes effect immediately: the game moves the item and
  recalculates your bonuses before the move returns. Inventory moves aren't
  blocked during a swing or a boost.
- A boost copies its speed and length into its own action when it starts, and
  never looks at your boots again. So you get the full boost even though the
  Boosters come off straight away.
- A mid-air jump that can't jump normally uses your boots' ability. Plain boots
  like Stompers have none, so the plugin swaps in the Boosters for that one
  call.

Each swap is two normal inventory moves, the same request the inventory screen
sends. Each move costs 75 of the client's input buffer. No other program or
DLL is needed.

## Safety checks

- **Low input buffer:** the swap is skipped, and the swing or jump happens
  normally, if your buffer is below 150 plus `buffer_reserve` and 75 for
  each outstanding restoration. A simultaneous swing and boost reserve both.
- **No wasted moves:** it only swaps when the game will actually swing or
  boost (the same "can act" check the game makes first), and only mid-air for
  boosts.
- **Capacity stats:** armor or boots with a max-HP, ammo or rocket stat are
  never swapped out. The game cuts your current HP down to a lower maximum, so
  removing that kind of item even briefly would cost health.
- **Failed swap-back:** if putting your gear back is ever refused, it retries
  every frame until it succeeds. It stops retrying if you change the items
  yourself or the world/player/connection changes. If no action message is
  emitted, the next frame restores the gear and logs that action ordering
  was unverified.
- **Python errors:** if the plugin hits an error, the game's own swing or jump
  still happens, exactly once, and the error goes to the log.
- **Reloading:** SBPE can reload the plugin while the game runs. The reloaded
  copy takes over the same hooks; an unloaded copy sends every call straight
  back to the game.
- **Sender compatibility:** the sender's original instructions are checked
  before installing its trampoline. An unknown build or another plugin that
  already owns that sender disables this plugin instead of overwriting it.

## Options (`[plugin_ironclad_swap]`)

| Option | Default | What it does |
|---|---|---|
| `berserker` | `yes` | Swing in the Berserker. |
| `berserker_name` | `Frenzied Berserker` | Bag item to swing in (the name only has to contain this). |
| `boosters` | `yes` | Boost in the Boosters. |
| `booster_name` | `Boosters` | Bag boots to boost with. `Boosters` also matches Overloaded Boosters; Gliders don't match. |
| `buffer_reserve` | `75` | Input buffer left spare after the two inventory moves. |

## Compatibility

- **Windows SBPE with its bundled 32-bit PyPy**, and the **Steam Windows
  client** build (the same one other SBPE native plugins target, loaded at its
  fixed address). Before changing anything, the plugin checks the bytes at
  every game address it uses. On any other build it logs
  `ironclad_swap: off, unknown client build` and changes nothing.
- The swing's damage is worked out by your own client. A server that takes the
  client's reported damage counts the Berserker bonus. Whether the official
  StarBreak servers accept it hasn't been tested.

## Tests

`tests/test_ironclad_swap_logic.py` runs the plugin's swap rules on any
Python 3, with the SBPE modules stubbed and a simulated game. Its simulated
moves cost 75 buffer and recalculate bonuses like the game does. It checks:

- The swing sees the Berserker plus the Stompers' bonuses, and only the
  Stompers' afterwards.
- Your gear always ends up where it started.
- Swaps are skipped for low buffer, other characters, other players' items,
  options turned off, and missing or unsafe items.
- Boosts happen only mid-air, and only with matching Boosters.
- A refused swap-back gets retried.
- A Python error still runs the game's own call exactly once.
- The machine-code hook encodes the intended instructions.
- Gear stays equipped through the original MT14/MT15 message, including a
  simultaneous swing and boost; unrelated messages do not restore it.
- Outstanding restores reserve their input budget and retain both exact item
  pairs after refused moves or Python errors.

```
python3 tests/test_ironclad_swap_logic.py
```

Optional native thunk checks execute 32-bit x86 in an emulator without running
StarBreak. With the development dependency `unicorn` installed:

```
python3 tests/test_windows_thunks.py --client /path/to/mvmmoclient.exe
```

This checks sender arguments, stack alignment/cleanup, preserved registers,
callback and unloaded pass-through paths, the replayed native prologue, and
the pinned Windows executable signatures. Omit `--client` for emulator-only
checks.

## Fair use

This changes your gear automatically to get bonuses your worn gear shouldn't
give you. Only use it where that's allowed, for example on a private server
whose owner permits it. On the official servers it is cheating and can get
your account banned.
