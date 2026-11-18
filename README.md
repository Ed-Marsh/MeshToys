# MeshToys

Small tools that turn a [Meshtastic](https://meshtastic.org) node into a remote-control and information gateway for a PC.

- [`mesh_bridge.py`](#mesh_bridgepy): the bridge. Runs commands sent over the mesh and replies by direct message.
- [`trek.py`](#super-star-trek-trekpy): Super Star Trek, a game you play over the mesh through the bridge (sessions are in `trek_session.py`). Tests are in `test_trek.py` and `test_trek_session.py`.
- [`zork_session.py`](#zork-zork_sessionpy-and-zork): `/zork`, the classic text adventure (Dungeon, the mainframe Zork) played over the mesh. Tests are in `test_zork.py` and `test_dungeon.py`. See [`NOTICE`](NOTICE) for its origin and licence.

## mesh_bridge.py

A Heltec node (tested target: Heltec V4) is connected to a PC over USB or WiFi. When a text message arriving over the mesh starts with a command prefixed with `/`, such as `/temperature`, the PC runs the matching handler and sends the result back to the sender as a direct message.

### Sending commands

- The command must be the very first thing in the message: `/calc 10/2` works, but `what is 10 /x` and `@calc 10` are ignored. This keeps a `/` used for division inside text from triggering anything.
- Command names are not case sensitive. Everything after the name is passed to the command as its arguments.
- Send `/help` to list the commands and `/help name` (for example `/help calc`) for a one-line description of one. The list has one command per line.
- Admin commands (`/announce`, `/say`) are hidden from `/help` unless the sender is in `--admin`, and `/help say` from anyone else answers `Unknown /say`.

### Built-in commands

| Command | Reply |
|---|---|
| `/temperature` | Current temperature and "feels like" in °C at your base location, plus the same for the caller's location if their node has shared a position. From [Open-Meteo](https://open-meteo.com) (no API key needed). Example: `Home 12.3C (feels 10.1C) \| You 8.8C (feels 6.8C)` |
| `/weather` | Combined reply: the `/temperature` line (base, plus caller if known) then the `/forecast` line. If one lookup fails the other is still sent |
| `/forecast` | Today and tomorrow at your base: high/low in °C and chance of rain. Example: `Today 11/5C rain 40% \| Tmrw 9/3C rain 70%` |
| `/signal` | Radio quality of the caller's message as your node received it: SNR, RSSI and hop count. For a relayed message this is the last hop's link, not the whole path |
| `/nodes` | How many nodes your Heltec has heard in the last hour, how many it knows in total, and the best SNR with that node's short name |
| `/announce` | **Admin only.** Broadcasts the `--announce` preset on the public channel and replies `Sent publicly` |
| `/say text` | **Admin only.** Broadcasts `text` on the public channel. Example: `/say Net tonight 8pm` |
| `/calc expression` | Calculator. Supports `+ - * / // % ** ^` (`^` is power), brackets and `!` for factorial (0 to 170). Examples: `/calc 23 * 5 / (10 * 2)` gives `5.75`, `/calc 4!` gives `24`. Very large results are shown in scientific notation, and oversized powers are refused |
| `/ping` | `pong` and the PC's time |
| `/help` | List of available commands, one per line (admin commands only for admin nodes). `/help name` describes one command, e.g. `/help calc` |
| `/trek` | The Super Star Trek game. `/trek` starts or resumes your game and keeps you in it until `/quit`; inside, you type the game's commands without a `/` (`srs`, `nav 3 1.5`). See [Super Star Trek](#super-star-trek-trekpy) below |
| `/zork` | A text adventure. While you are in it, every message you send is a game command. See [Zork](#zork-zork_sessionpy-and-zork) below |

### Install

```
pip install meshtastic pypubsub requests
```

### Run

```
# WiFi (TCP) connection to the node
python mesh_bridge.py --lat 51.50 --lon -0.12 --host 192.168.1.50

# USB, specific port
python mesh_bridge.py --lat 51.50 --lon -0.12 --port COM5

# USB, auto-detect
python mesh_bridge.py --lat 51.50 --lon -0.12

# Recommended for an open service: WiFi, direct messages only
python mesh_bridge.py --lat 51.50 --lon -0.12 --host 192.168.1.50 --dm-only
```

Options:

- `--lat`, `--lon` (required): location used for the weather lookup.
- `--host`: node IP or hostname for a WiFi connection. Overrides `--port`.
- `--port`: serial port. Default is auto-detect.
- `--allow '!a1b2c3d4' ...`: only obey messages from these node IDs. Default is anyone on the mesh.
- `--dm-only`: only act on direct messages addressed to this node. Public/channel broadcasts are ignored, and the reply goes back as a direct message to the sender only.
- `--near-km`: a caller whose known position is within this many km of the base is treated as being at home and gets `You: near home` instead of a second lookup. Default 2.
- `--admin '!id' ...`: node IDs allowed to run admin commands (`/announce`, `/say`). Admin commands are disabled unless this is set. Other nodes are ignored silently and the attempt is logged.
- `--announce TEXT`: the preset text sent by `/announce`. `{name}` is replaced with this node's long name, `{short}` with its short name and `{id}` with its ID (for example `!b3725444`).
- `--announce-channel N`: channel index for public sends. Default 0 (the primary channel).
- `--reply-gap SECS`: pause between the parts of a multi-message reply (Trek and Zork). Default 3.
- `--session-idle-min MIN` (also `--session-idle-min`): close a Zork or Trek session after this many idle minutes. The game is saved. Default 30.
- `--zork-max-chunks N`: most messages sent for one Zork reply. Default 6.
- `--zork-save-dir DIR`: where Zork saves go, one file per node. Default `zork_saves/` next to the script (ignored by git).
- `--log-file PATH`: also append the log to a file.
- `-v`: verbose logging.

### Node setup for WiFi

In the Meshtastic app, open Radio Config, then Network, enable WiFi and enter your SSID and password. Give the node a fixed IP or DHCP reservation. On ESP32 nodes, enabling WiFi is expected to disable Bluetooth, so check the firmware docs for your device.

Only one program can hold the node's USB serial port at a time. The script reconnects automatically if the node reboots or the connection drops.

### Adding commands

Write a function that takes the config and a context dict (`ctx["sender"]` is the caller's node ID, `ctx["pos"]` is their `(lat, lon)` or `None`, and `ctx["packet"]`, `ctx["iface"]` and `ctx["my_id"]` give the raw packet, the Meshtastic interface and this node's ID) and returns a reply string, and register it. The optional `info` text is what `/help name` shows (if omitted, the function's docstring is used), and `admin=True` restricts the command to `--admin` nodes:

```python
@command("uptime", info="How long the PC has been up")
def cmd_uptime(cfg, ctx):
    return "up 3 days"
```

Replies are truncated to 200 bytes to fit Meshtastic's text limit. A `\n` in a reply starts a new line on the receiving client. Keep `info` short enough that `/name` plus the description stays under that limit.

### Public messages

Text messages that are not addressed to this node (public/channel broadcasts) are never acted on with `--dm-only`, but they are written to the log as `PUBLIC chN from !id (name): text`, so you can see replies and general chatter. Use `--log-file` to keep a record. Direct messages to this node that don't start with a `/command` are logged as `DM from !id (name): text`, so private replies to an announcement aren't lost. Admin commands sent publicly are ignored.

To broadcast from the bridge, send `/announce` or `/say your text` as a direct message from a node listed in `--admin`. There is a 60 second cooldown between public sends and a 200 byte limit. Nothing is sent on a timer. Keep public messages infrequent and short, because the channel's airtime is shared by everyone.

Example:

```
python mesh_bridge.py --lat 51.50 --lon -0.12 --host 192.168.1.50 --dm-only --admin '!b3725444' --announce "Home node {name}: DM me /weather or /help" --log-file mesh.log
```

### Caller location

The caller's position comes from the node database on your Heltec, so it only exists if the caller's node has broadcast its position to the mesh (and the position may be old, or deliberately reduced in precision). If it isn't known the reply says so. The coordinates used for each caller are written to the log with their distance from the base, which helps when a reading looks wrong. Before the weather lookup, the caller's coordinates are rounded to 2 decimal places (about 1 km) and only those are sent to Open-Meteo.

### Security

Anyone who can reach the node over the mesh can trigger commands unless `--allow` is set. With `--dm-only`, they must send a direct message to this node; channel broadcasts are ignored. Use `--allow` before adding any command that changes something. `/announce` and `/say` are restricted to nodes in `--admin`, since a node ID is what identifies the sender.

## Super Star Trek (trek.py)

A text version of the classic game, played by direct message to the bridge. Each node ID gets its own saved game (in `trek_games.json` next to the script), so people can play at the same time and carry on after a restart. It needs nothing beyond the bridge itself. Use `--dm-only` so games don't clutter the public channel.

Your mission: destroy every Klingon before the stardate deadline, resupplying at starbases when you need to. Send `/trek` to start (or resume). That puts you *in Trek*: from then on **every** direct message you send is a game command, typed without a `/` (a leading `/` is ignored), until you send `/quit`. `/trek new` (or just `new` inside the game) abandons the current game and starts another, and `help` (or `/trek help`) gives a quick guide; `help nav` explains one command.

Your game is saved after every command, so leaving with `/quit`, an idle timeout or a restart loses nothing and `/trek` picks it up again. After `--session-idle-min` minutes (default 30) without a message the session is closed quietly, and you are told the next time you message the bridge. A won or lost game also ends the session. You can only be in one game at a time: `/zork` while in Trek (or `/trek` while in Zork) says to `/quit` first. Messages sent to the public channel are never game input. The old `srs`, `nav` ... commands no longer exist outside a session.

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

On the map, `E` is you, `K` a Klingon, `B` a starbase, `S` a star and `+` an empty sector (change `DISPLAY` in `trek.py` to use others). The Meshtastic app renders Markdown, so the game never sends `_`, `*`, backticks, `~` or backslashes, which it would use as formatting and swallow. Coordinates are always row,column. Dock by sailing into any sector next to a starbase (diagonals count): energy and torpedoes are refilled, shields drop and Klingons can't hurt you. Ships get damaged by Klingon hits and by chance, and damaged devices stop working until they are repaired by time or at a starbase. Moving across a quadrant boundary costs a stardate.

Every reply is split into messages of at most 190 bytes. Longer replies (for example a scan after a fight) arrive as several messages a few seconds apart. The grid uses one character per sector, so it can look slightly uneven in apps that use a proportional font.

The map symbols were chosen by testing in the Meshtastic Android app, which uses a proportional font and renders Markdown. `.` and `-` are too narrow and make the rows drift, `_` is eaten as italics, and Markdown tables are not rendered (the `|` characters show as text). `+` and `S` have similar widths and line up. If you use a different client, change `DISPLAY` near the top of `trek.py`.

This is a rewrite of a B4J port of the 1978 BASIC game rather than a straight translation. 

Run the tests with `python -m unittest test_trek test_trek_session -v`.

## Zork (zork_session.py and zork/)

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
- **Trek and Zork:** a node can only be in one of them. `/zork` while in Trek (or `/trek` while in Zork) replies with a short message saying to `/quit` first. Trek works the same way as Zork: `/trek` to start, free text inside, `/quit` to leave.
- **Errors:** if the game engine fails, the node gets a short error and the session is closed (the last save is kept), so nobody is left stuck in Zork.
- **Message size:** replies are cleaned (prompts, MORE markers, status lines and Markdown characters removed) and split at line, then sentence boundaries into messages of at most 190 bytes, numbered `(1/3)`. At most `--zork-max-chunks` are sent; a longer reply ends with `...(say 'again' / 'look' for more)`.

The game is **Dungeon**, the mainframe Zork (see below), running as pure Python: nothing to install or compile, and it runs the same on Windows, macOS, Linux and a Raspberry Pi (Python 3.9 or newer). Several players use one process; each game runs in its own background thread. The first `/zork` after the bridge starts takes a moment while the game loads. Typing `quit` inside the game asks "Do you wish to leave the game?" like the original and ends it for good; `/quit` just saves and leaves. In-game `save` and `restore` aren't needed (the game saves itself after every move), and the `gdt` debugging console and the `!command` shell escape of the original have been removed.

The session layer works with any engine that follows the small interface described in `zork/__init__.py`; `zork/stub.py` is a four-room stand-in used to test the session layer.

### Where the game comes from, and the licence

**This part of MeshToys is non-commercial, hobby-use only. Read [`NOTICE`](NOTICE) before you use or share it.**

*Zork*, called *Dungeon* at the time, was created at the MIT Laboratory for Computer Science in 1977 by **Tim Anderson, Marc Blank, Bruce Daniels and Dave Lebling**, in the MDL language, and inspired by *Adventure* (Will Crowther, Don Woods) and *Dungeons and Dragons* (Gary Gygax, Dave Arneson). It was translated to FORTRAN, then moved to Unix and the VAX (Randy Dietrich, Lynn Cochran, Sig Peterson, Bill Randle, John Gilmore), and then to C by **Ian Lance Taylor** in 1991 using f2c. Full credits are in `NOTICE`.

The game logic in `zork/engine_gen.py` and the data file `zork/dtextc.dat` come from that C port, whose source files all carry *"COPYRIGHT 1980, INFOCOM COMPUTERS AND COMMUNICATIONS ... ALL RIGHTS RESERVED, COMMERCIAL USAGE STRICTLY PROHIBITED"*. The port's maintainers describe the code as public domain ("released to the PD by Infocom"), but the headers say otherwise, so the position is unclear and the notice is kept. `engine_gen.py` was **machine-translated** from the C by `tools/c2py.py` (a small C-to-Python translator written for this; it understands only the C that Dungeon uses) and is not meant to be edited by hand. Zork is a trademark of its owner; this project is not affiliated with or endorsed by them.

### How it was checked

`tools/crosscheck.py` builds the original C program (you need the C source and `python -m ziglang`, i.e. `pip install ziglang`), plays the same scripted games on it and on the Python port with the same random number sequence, and compares every character of output. Games start either at the beginning or from a random room carrying random objects, so the room and object code gets exercised. Thousands of games of hundreds of moves each have matched exactly. A sample of those recorded games is kept in `test_data/dungeon_golden.json` and replayed by the tests, so the check runs without the C source.

To regenerate the engine from the C source: `python tools/c2py.py path/to/c/source zork/engine_gen.py`.

Run the tests with `python -m unittest test_zork test_dungeon -v`.

## Status

Early version. Running on a Heltec V4 node; expect rough edges.
