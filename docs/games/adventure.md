# Colossal Cave Adventure (adventure_session.py and adventure/)

Send `/adventure` by direct message to start a game, or resume your saved one. This is the original cave game by Will Crowther and Don Woods (1976-77), where text adventures began. It works exactly like [Zork](zork.md): while you are in it, **every** direct message you send goes to the game as a command, including ones starting with `/` (the leading `/` is dropped). Only these are special:

| Message | What it does |
|---|---|
| `/quit` | Saves and leaves. Normal commands work again |
| `/adventure` | Shows the current room again: its full description and the objects in it (this does not use up a turn) |
| `/adventure new` | Abandons your game, deletes its save and starts a fresh one (works inside or outside the game) |

The game understands one or two words: `in`, `get lamp`, `n`, `unlock grate`, `d`. `help` gives hints, `inventory` lists what you carry, `score` shows your score, and `back` returns to the previous place. The original begins by asking "Would you like instructions?"; that question is skipped here, the game starts as if you had said no (the lamp then lasts 330 turns), and the one-line intro says how to leave and that `help` gives hints.

Details:

- **Per node:** each node ID has its own game and its own save file (`adventure_saves/<node>.json`, ignored by git), so several people can play at once. The game is saved after every command, so a restart or crash loses nothing. The save holds exactly the variables the original's `suspend` writes, plus the state of the random number generator, so a resumed game does not replay the same dice (the dwarves and the pirate).
- **Direct messages only, idle timeout, one game at a time, errors:** as for Zork. Starting `/adventure` while in Trek or Zork (or the reverse) says to `/quit` first.
- **Questions:** when the game asks a yes/no question ("Do you want me to try to reincarnate you?", "Do you really want to quit now?") the next message you send is the answer. As in the original, anything that does not start with `n` means yes. If you `/quit` while a question is open, your game resumes just before the command that led to it.
- **Ending:** typing `quit` asks to confirm and then shows your score and ends the game for good; dying three times does too. `suspend`, `save`, `restore` and `load` only say that the game saves itself. `help help` (or `? ?`) prints the word list a page at a time: send any message to see the next page. Typing `log` followed by an object ends the session with `Fatal error number 39`, exactly as the C game does (the verb has no message); the save is kept.
- **Message size:** as for Zork, but a reply may use up to 10 messages (`--game-max-chunks`) because the `help` text and the longest room description need 9 or 10. A reply that is still too long ends with `...(cut short)`. The game's own wording and line breaks are kept; only the `> ` prompts, the bell, and the padding of the score table and word list are removed.

## How it is built

The game runs as pure Python, with nothing to install or compile, on Windows, macOS, Linux and a Raspberry Pi Zero W (Python 3.9 or newer). The engine, `adventure/engine.py`, is a **hand-written port** of the C source (troglobit/adventure) rather than a machine translation as for Zork. That source is about 3,000 lines of plain C with one `goto` and no pointer tricks, so a function-by-function port that keeps the C structure is readable and easy to compare, and the Dungeon translator was written for a different style of C. The text, the map, the vocabulary and the starting state are not typed in: `tools/gen_adventure_data.py` extracts them from the C headers into `adventure/data.py`.

All the game's state is in one `Game` object, so any number of games run in one process. The C program reads its commands with `fgets()`, so, as for Dungeon, each game runs in its own thread that waits for the next line and hands back what the game printed; the state is captured each time the game is waiting for a command. The engine follows the same small interface as the Zork engine (`zork/__init__.py`), and `game_session.py`, shared with `/zork`, does the per-node saves, idle timeout, splitting for the mesh and error handling.

## Where the game comes from, and the licence

*Colossal Cave Adventure* was written by **Will Crowther** (1975-77, PDP-10), with major features added by **Don Woods** (1977). It was converted to BDS C by **J. R. Jaeger** (1981), standardised for Unix by **Jerry D. Pohl** (1990), ported to QNX by **James Lummel** (1993) and to Linux and cleaned up by **Joachim Wiberg** (troglobit/adventure, the version this one follows). Full credits are in [`NOTICE`](../../NOTICE), and the same credits are at the top of `adventure/data.py`.

The licence position is not as clean as it could be, so read [`NOTICE`](../../NOTICE): the C port's repository dedicates the code to the public domain (the Unlicense, so there is no non-commercial restriction as there is for Dungeon), but its README adds that "the actual license of this program is unknown, although most people agree that it is likely to be considered to be under the public domain". The original authors' own licence has not been seen.

## What differs from the C game

- The start-up question is skipped (see above), and `suspend`/`restore` and the `~/.adventure` save file are replaced by the automatic save. The game never touches files or the environment.
- The random numbers come from a small generator whose state is saved with the game (the C program always seeded `rand()` with 511, so every game rolled the same dice).
- Input lines are cut at 79 characters and the rest is dropped (the C program would have used it as the next command).
- Four defects of the C source that end or corrupt a game are fixed, listed in `NOTICE` and at the top of `adventure/engine.py`: room 140 (the maze "Dead end" with the vending machine) could be entered but not left, because the C code printed `Fatal error number 42` at the next move; `back` was fatal after many moves (for example going down through the grate and then `back`) and used a room number as an array index; and `look` next to a dwarf left the game waiting at location 0. Everywhere else the port prints exactly what the C game prints, including its oddities (for example `say xyzzy` only answers "Okay." when the keys are at hand).

## How it was checked

`tools/crosscheck_adventure.py` builds the original C program (you need the C source and `python -m ziglang`, i.e. `pip install ziglang`) with the same random number generator as the Python engine, and then:

1. dumps every data table (descriptions, messages, travel table, vocabulary, starting state) as the C compiler sees it and compares them with `adventure/data.py`: they are identical;
2. plays thousands of random scripted games on both and compares every character of output. Games start at the beginning, or from random states (random place, carried objects, dwarves, treasure counts, clocks and lamp) so that the dwarves, the pirate, death and reincarnation, the lamp running out and the closing of the cave are reached; the commands are chosen from the room's real exits and the objects present as well as at random. The C copy used for this carries only the four bug fixes above and a hook to set the starting state. The final run was 8,000 games and 911,924 commands, with no difference in any character of output.

Recorded games from the C program (a walk through the opening, a dwarf attack, death and reincarnation, the pirate, the cave closing and closed, the lamp dimming, the final score) are kept in `test_data/adventure_golden.json` and replayed by the tests, so the check runs without the C source. The unit tests also cover the engine on its own, saving and resuming (including the random state), the session layer with two nodes at once, idle timeouts, engine failures, the exclusion of Trek and Zork, and public messages never reaching the game.

Everything above ran on Windows with Python 3.14. Since then the bridge has also been run with a real radio and node (a Heltec V4) on a Raspberry Pi Zero W; see [Running on a Raspberry Pi Zero W](../raspberry-pi-zero-w.md) for what was checked there.

To regenerate the data from the C source: `python tools/gen_adventure_data.py path/to/colossal/src`. To repeat the comparison: `python tools/crosscheck_adventure.py path/to/colossal/src --games 2000`.

Run the tests with `python -m unittest test_adventure -v` (all of them: `python -m unittest test_trek test_zork test_trek_session test_dungeon test_adventure`).
