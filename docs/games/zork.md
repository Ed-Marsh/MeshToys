# Zork (zork/)

Send `/zork` by direct message to start a game, or resume your saved one. While you are in Zork, **every** direct message you send goes to the game as a command, including ones starting with `/` (the leading `/` is dropped, so `/look` works). Only three things are special:

| Message | What it does |
|---|---|
| `/quit` | Saves and leaves. Normal commands work again |
| `/zork` | Shows the current room again |
| `/zork new` | Abandons your game, deletes its save and starts a fresh one (works inside or outside Zork) |

Details:

- **Per node:** each node ID has its own game and its own save file (`zork_saves/<node>.json`), so several people can play at once. The game is saved after every command, so a restart or crash loses nothing. `/zork` after a restart resumes it.
- **Direct messages only:** public channel messages are never treated as game input, so a node in Zork can still chat on the channel.
- **Idle timeout:** after `--session-idle-min` minutes without a message the session is saved and closed quietly. The next time that node messages the bridge it is told, and `/zork` resumes.
- **One game at a time:** a node can only be in one of Trek, Zork, Adventure and Wumpus. Starting another one (`/trek`, `/zork`, `/adventure` or `/wumpus`) replies with a short message saying `You're in Zork. Send /quit to leave, then /trek.` and so on. Trek works the same way as Zork: `/trek` to start, free text inside, `/quit` to leave.
- **Errors:** if the game engine fails, the node gets a short error and the session is closed (the last save is kept), so nobody is left stuck in Zork.
- **Message size:** replies are cleaned (prompts, MORE markers, status lines and Markdown characters removed) and split at line, then sentence boundaries into messages of at most 190 bytes, numbered `(1/3)`. At most `--game-max-chunks` are sent; a longer reply ends with `...(say 'again' / 'look' for more)`.

The game is **Dungeon**, the mainframe Zork (see below), running as pure Python: nothing to install or compile, and it runs the same on Windows, macOS, Linux and a Raspberry Pi (Python 3.10 or newer). Several players use one process; each game runs in its own background thread. The first `/zork` after the bridge starts takes a moment while the game loads. Typing `quit` inside the game asks "Do you wish to leave the game?" like the original and ends it for good; `/quit` just saves and leaves. In-game `save` and `restore` aren't needed (the game saves itself after every move), and the `gdt` debugging console and the `!command` shell escape of the original have been removed.

The session layer works with any engine that follows the small interface described in `zork/__init__.py`; `zork/stub.py` is a four-room stand-in used to test the session layer.

## Where the game comes from, and the licence

**This part of MeshToys is non-commercial, hobby-use only. Read [`NOTICE`](../../NOTICE) before you use or share it.**

*Zork*, called *Dungeon* at the time, was created at the MIT Laboratory for Computer Science in 1977 by **Tim Anderson, Marc Blank, Bruce Daniels and Dave Lebling**, in the MDL language, and inspired by *Adventure* (Will Crowther, Don Woods) and *Dungeons and Dragons* (Gary Gygax, Dave Arneson). It was translated to FORTRAN, then moved to Unix and the VAX (Randy Dietrich, Lynn Cochran, Sig Peterson, Bill Randle, John Gilmore), and then to C by **Ian Lance Taylor** in 1991 using f2c. Full credits are in `NOTICE`.

The game logic in `zork/engine_gen.py` and the data file `zork/dtextc.dat` come from that C port, whose source files all carry *"COPYRIGHT 1980, INFOCOM COMPUTERS AND COMMUNICATIONS ... ALL RIGHTS RESERVED, COMMERCIAL USAGE STRICTLY PROHIBITED"*. The port's maintainers describe the code as public domain ("released to the PD by Infocom"), but the headers say otherwise, so the position is unclear and the notice is kept. `engine_gen.py` was **machine-translated** from the C by `tools/c2py.py` (a small C-to-Python translator written for this; it understands only the C that Dungeon uses) and is not meant to be edited by hand. Zork is a trademark of its owner; this project is not affiliated with or endorsed by them.

## How it was checked

`tools/crosscheck.py` builds the original C program (you need the C source and `python -m ziglang`, i.e. `pip install ziglang`), plays the same scripted games on it and on the Python port with the same random number sequence, and compares every character of output. Games start either at the beginning or from a random room carrying random objects, so the room and object code gets exercised. Thousands of games of hundreds of moves each have matched exactly. A sample of those recorded games is kept in `tests/test_data/dungeon_golden.json` and replayed by the tests, so the check runs without the C source.

To regenerate the engine from the C source: `python tools/c2py.py path/to/c/source zork/engine_gen.py`.

Run the tests with `python -m unittest tests.test_zork tests.test_dungeon -v`.
