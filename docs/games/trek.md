# Super Star Trek (trek/engine.py)

A text version of the classic game, played by direct message to the bridge. Each node ID gets its own saved game (in `trek_games.json` next to the script), so people can play at the same time and carry on after a restart. It needs nothing beyond the bridge itself. Use `--dm-only` so games don't clutter the public channel.

Your mission: destroy every Klingon before the stardate deadline, resupplying at starbases when you need to. Send `/trek` to start (or resume). That puts you *in Trek*: from then on **every** direct message you send is a game command, typed without a `/` (a leading `/` is ignored), until you send `/quit`. `/trek new` (or just `new` inside the game) abandons the current game and starts another, and `help` (or `/trek help`) gives a quick guide; `help nav` explains one command.

Your game is saved after every command, so leaving with `/quit`, an idle timeout or a restart loses nothing and `/trek` picks it up again. After `--session-idle-min` minutes (default 30) without a message the session is closed quietly, and you are told the next time you message the bridge. A won or lost game also ends the session. You can only be in one game at a time: starting `/zork`, `/adventure` or `/wumpus` while in Trek (or the reverse) says to `/quit` first. Messages sent to the public channel are never game input. The old `srs`, `nav` ... commands no longer exist outside a session.

| Command | What it does |
|---|---|
| `/trek` | Start a game or resume yours and enter Trek. `/trek new` restarts, `/trek help` explains (without entering) |
| `srs` | Short range scan: your quadrant as an 8x8 map plus stardate, condition, position, energy, shields, torpedoes, Klingons and bases left |
| `lrs` | Long range scan of the 3x3 quadrants around you. Each three-digit code is Klingons, bases, stars |
| `nav course warp` | Move, e.g. `nav 3 1.5`. Courses run 1 to 8.99 (1 = right, 3 = up, 5 = left, 7 = down; fractions aim between). One warp is 8 sectors, and warp 0.1 to 8 is allowed |
| `pha units` | Fire phasers, e.g. `pha 300`. The energy is shared between the Klingons in the quadrant and falls off with distance |
| `tor course` | Fire a photon torpedo along a course. It destroys the first thing it hits (stars absorb it) |
| `shi units` | Set the shield level. The energy comes from, and returns to, the ship's energy |
| `dam` | Damage report. Docked at a starbase, `dam` quotes the repair time and `dam y` authorizes it (repairs cost stardates). The game reminds you when you dock with damage |
| `cmp g` | Galactic record: what you have scanned so far (two messages, rows 1-4 and 5-8) |
| `cmp s` | Status report: Klingons left, days left, starbases |
| `cmp t`, `cmp b` | Course and distance to each Klingon, or to the starbase, in your quadrant |
| `cmp d r1 c1 r2 c2` | Course and distance from one sector to another |

On the map, `E` is you, `K` a Klingon, `B` a starbase, `S` a star and `+` an empty sector (change `DISPLAY` in `trek/engine.py` to use others). The Meshtastic app renders Markdown, so the game never sends `_`, `*`, backticks, `~` or backslashes, which it would use as formatting and swallow. Coordinates are always row,column. Dock by sailing into any sector next to a starbase (diagonals count): energy and torpedoes are refilled, shields drop and Klingons can't hurt you. Ships get damaged by Klingon hits and by chance, and damaged devices stop working until they are repaired by time or at a starbase. Moving across a quadrant boundary costs a stardate.

Every reply is split into messages of at most 190 bytes. Longer replies (for example a scan after a fight) arrive as several messages a few seconds apart. The grid uses one character per sector, so it can look slightly uneven in apps that use a proportional font.

The map symbols were chosen by testing in the Meshtastic Android app, which uses a proportional font and renders Markdown. `.` and `-` are too narrow and make the rows drift, `_` is eaten as italics, and Markdown tables are not rendered (the `|` characters show as text). `+` and `S` have similar widths and line up. If you use a different client, change `DISPLAY` near the top of `trek/engine.py`.

This is a rewrite of a B4J port of the 1978 BASIC game rather than a straight translation. 

Run the tests with `python -m unittest test_trek test_trek_session -v`.
