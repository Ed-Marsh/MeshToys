# MeshToys

Small tools that turn a [Meshtastic](https://meshtastic.org) node into a remote-control and information gateway for a PC.

- [`mesh_bridge.py`](#mesh_bridgepy): the bridge. Runs commands sent over the mesh and replies by direct message.
- [Running on a Raspberry Pi Zero W](#running-on-a-raspberry-pi-zero-w): setting up the bridge on a Pi Zero W with the Heltec plugged in by USB, wiring, and starting it at boot.
- [`trek.py`](#super-star-trek-trekpy): Super Star Trek, a game you play over the mesh through the bridge (sessions are in `trek_session.py`). Tests are in `test_trek.py` and `test_trek_session.py`.
- [`zork_session.py`](#zork-zork_sessionpy-and-zork): `/zork`, the classic text adventure (Dungeon, the mainframe Zork) played over the mesh. Tests are in `test_zork.py` and `test_dungeon.py`. See [`NOTICE`](NOTICE) for its origin and licence.
- [`adventure_session.py`](#colossal-cave-adventure-adventure_sessionpy-and-adventure): `/adventure`, Colossal Cave Adventure (Crowther and Woods), the original cave game, played over the mesh. The engine is in `adventure/`, the shared session code (also used by Zork) is in `game_session.py`. Tests are in `test_adventure.py`. See [`NOTICE`](NOTICE) for its origin and licence.
- [`wumpus_session.py`](#hunt-the-wumpus-wumpus_sessionpy-and-wumpus): `/wumpus`, Gregory Yob's 1973 Hunt the Wumpus, played over the mesh. The engine is in `wumpus/`, the session code is the shared `game_session.py`. Tests are in `test_wumpus.py`.

## mesh_bridge.py

A Heltec node (tested target: Heltec V4) is connected to a PC over USB or WiFi. When a text message arriving over the mesh starts with a command prefixed with `/`, such as `/temperature`, the PC runs the matching handler and sends the result back to the sender as a direct message.

### Sending commands

- The command must be the very first thing in the message: `/calc 10/2` works, but `what is 10 /x` and `@calc 10` are ignored. This keeps a `/` used for division inside text from triggering anything.
- Command names are not case sensitive. Everything after the name is passed to the command as its arguments.
- Send `/help` to list the commands and `/help name` (for example `/help calc`) for a one-line description of one. The list has one command per line. The games are not in that list: send `/games` to see them.
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
| `/help` | List of available commands, one per line (admin commands only for admin nodes). The games are not listed; use `/games` for those. `/help name` describes one command, e.g. `/help calc` |
| `/games` | List of the games you can play over the mesh. `/help name` describes one, e.g. `/help zork` |

### Games

Send `/games` to list them. Each game starts with its own command, and while you are in a game, every message you send is a game command until `/quit`.

| Command | Game |
|---|---|
| `/trek` | The Super Star Trek game. `/trek` starts or resumes your game and keeps you in it until `/quit`; inside, you type the game's commands without a `/` (`srs`, `nav 3 1.5`). See [Super Star Trek](#super-star-trek-trekpy) below |
| `/zork` | A text adventure. While you are in it, every message you send is a game command. See [Zork](#zork-zork_sessionpy-and-zork) below |
| `/adventure` | Colossal Cave Adventure, the original cave game. Works like `/zork`: every message you send is a game command until `/quit`. See [Colossal Cave](#colossal-cave-adventure-adventure_sessionpy-and-adventure) below |
| `/wumpus` | Hunt the Wumpus (Gregory Yob, 1973). Works like `/zork`: every message you send is a game command until `/quit`. See [Hunt the Wumpus](#hunt-the-wumpus-wumpus_sessionpy-and-wumpus) below |

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
- `--reply-gap SECS`: pause between the parts of a multi-message reply (Trek, Zork and Adventure). Default 3.
- `--session-idle-min MIN` (also `--zork-idle-min`): close a Zork, Adventure or Trek session after this many idle minutes. The game is saved. Default 30.
- `--game-max-chunks N` (also `--zork-max-chunks`): most messages sent for one Zork or Adventure reply. Default 6 for Zork and 10 for Adventure (its `help` text and its longest room description need 9 or 10).
- `--zork-save-dir DIR`: where Zork saves go, one file per node. Default `zork_saves/` next to the script (ignored by git).
- `--adventure-save-dir DIR`: where Adventure saves go, one file per node. Default `adventure_saves/` next to the script (ignored by git).
- `--wumpus-save-dir DIR`: where Wumpus saves go, one file per node. Default `wumpus_saves/` next to the script (ignored by git).
- `--log-file PATH`: also append the log to a file.
- `-v`: verbose logging.

### Node setup for WiFi

In the Meshtastic app, open Radio Config, then Network, enable WiFi and enter your SSID and password. Give the node a fixed IP or DHCP reservation. On ESP32 nodes, enabling WiFi is expected to disable Bluetooth, so check the firmware docs for your device.

Only one program can hold the node's USB serial port at a time. The script reconnects automatically if the node reboots or the connection drops.

### Running on a Raspberry Pi Zero W

The bridge can run on a Raspberry Pi Zero W instead of a PC, with the Heltec plugged into the Pi by USB. It is pure Python, so nothing needs compiling. With a USB link, the bridge keeps working when the router or internet is down, apart from the weather commands, which need internet.

**What you need**

- Raspberry Pi Zero W, a microSD card and a 5 V micro-USB power supply
- A Heltec V4 (or another Meshtastic node that shows up as a USB serial port)
- A micro-USB OTG cable or adapter that ends in a USB-C plug for the Heltec

**1. Flash the card.** In [Raspberry Pi Imager](https://www.raspberrypi.com/software/) choose Raspberry Pi OS Lite (no desktop), then use the customisation options to set a hostname, username and password, your WiFi, and to enable SSH. The Zero W has 2.4 GHz WiFi only (802.11 b/g/n), so it will not see a 5 GHz-only network. WiFi is only needed for you to log in over SSH. The bridge itself does not use it when connected by USB.

**2. Wire it.** The Zero W has two micro-USB ports. Go by the labels printed on the board:

| Port | Use |
|---|---|
| `PWR IN` (the one nearest the corner) | Power supply only |
| `USB` (the middle one) | Data. Plug the OTG cable here, with the other end in the Heltec's USB-C port |

The Heltec's USB-C port carries both its power and its serial data, so one cable does both. Do not plug the Heltec into `PWR IN`: that port has no data connection. The Pi's `PWR IN` supply also ends up powering the Heltec. How much current a Zero W's USB port can safely supply to a transmitting Heltec V4 has not been measured here. If the node resets or the bridge drops out when it transmits, suspect power first and try a better supply.

**3. Install.** Log in with `ssh youruser@yourhostname.local` (or use the Pi's IP address), then:

```
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3-venv python3-pip
git clone https://github.com/Ed-Marsh/MeshToys.git
cd MeshToys
python3 -m venv .venv
source .venv/bin/activate
pip install meshtastic pypubsub requests
```

Each time you log in again, run `source .venv/bin/activate` from the `MeshToys` folder before running the script by hand. Your prompt starts with `(.venv)` when it is active. If you get `ModuleNotFoundError: No module named 'meshtastic'`, the environment is not active.

**4. Find the port and test.**

```
ls /dev/ttyACM* /dev/ttyUSB*
python mesh_bridge.py --lat 51.50 --lon -0.12 --port /dev/ttyACM0 --dm-only --log-file mesh.log -v
```

The V4 appeared as `/dev/ttyACM0` on the Pi this was written on. Use your own latitude and longitude (the numbers above are an example). If you get a permission error on the port, add your user to the `dialout` group with `sudo usermod -aG dialout $USER`, then log out and in again. `--host` overrides `--port`, so leave `--host` out when using USB. Only one program can hold the serial port, so stop any other copy of the bridge (for example on a PC) and close any Meshtastic app that has the node open.

**5. Start it at boot.** Create `/etc/systemd/system/meshtoys.service` with `sudo nano`, replacing `youruser`, the folder, and the coordinates, and adjusting the other options to suit:

```
[Unit]
Description=MeshToys bridge
After=multi-user.target

[Service]
User=youruser
WorkingDirectory=/home/youruser/MeshToys
ExecStart=/home/youruser/MeshToys/.venv/bin/python mesh_bridge.py --lat 51.50 --lon -0.12 --port /dev/ttyACM0 --dm-only --admin '!b3725444' --announce "{short}: DM me /help" --log-file mesh.log
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```
sudo systemctl daemon-reload
sudo systemctl enable --now meshtoys
systemctl status meshtoys
```

`status` should show `enabled` and `active (running)`. If it shows `disabled` or `inactive (dead)`, the `enable --now` step did not run. To watch the log, use `journalctl -u meshtoys -f`. The service calls the virtual environment's Python directly, so it does not need `source .venv/bin/activate`. `Restart=always` brings it back if the script crashes. If the Heltec ever shows up under a different name than `/dev/ttyACM0` (for example `ttyACM1` after being replugged), the service will not connect until you change `--port`.

**Updating.** Stop the service or leave it running, then:

```
cd ~/MeshToys
git pull
sudo systemctl restart meshtoys
```

If `git pull` complains about local changes, check `git status` first. If a new version adds a dependency, install it with the environment active.

**Status.** Checked on a Pi Zero W with a Heltec V4 over USB: the bridge connects and answers `/wumpus`, and the systemd service starts and connects, including automatically after a reboot. Not yet checked on the Pi: `/trek`, `/zork` and `/adventure`, long-term running, and the power draw with a battery or UPS.

### Adding commands

Write a function that takes the config and a context dict (`ctx["sender"]` is the caller's node ID, `ctx["pos"]` is their `(lat, lon)` or `None`, and `ctx["packet"]`, `ctx["iface"]` and `ctx["my_id"]` give the raw packet, the Meshtastic interface and this node's ID) and returns a reply string, and register it. The optional `info` text is what `/help name` shows (if omitted, the function's docstring is used), `admin=True` restricts the command to `--admin` nodes, and `game=True` lists it under `/games` instead of `/help`:

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

### Troubleshooting: a direct message gets no reply

**Symptom.** A node sends a direct message such as `/help` to the bridge's node and its app shows "Relayed, not confirmed by recipient", but no reply ever arrives. Other nodes can talk to the bridge normally, and a traceroute from the failing node to the bridge's node works.

**Check whether the bridge got it.** Send the message again, then on the machine running the bridge look for a line with the sender's node ID:

```
journalctl -u meshtoys --since "2 min ago" --no-pager | grep -E "\->|DM from"
```

Test the check first by sending a command from a node that is working, which should produce a line like `!b3725444 -> /help`. If the failing node's ID never appears, the bridge's node did not receive or decode its message, so the bridge never saw it. If you run the bridge by hand instead of as a service, look in `mesh.log` or the terminal.

**Likely cause, seen once.** Direct messages are encrypted to the recipient's public key, so the bridge's node needs the sender's current key. If the sender was reinstalled or reset after the bridge's node first heard it, it can come back with the same node ID but new keys. The Meshtastic Android app documentation says that, like the firmware, the app sticks with the first key it saw for a node and rejects a different one later. The bridge's node then keeps an old key, and may also keep the sender's old name, which is a useful clue: look at the sender's name in the bridge node's node list and see if it is out of date. This explanation fits what was seen here but was not confirmed by inspecting the keys.

**What fixed it.**

1. In the Meshtastic app, connect to the bridge's node, open the sender's entry in its node list and remove it. This clears only that node's local copy.
2. Reboot the sender so it announces itself again.
3. Wait a few minutes until the sender shows up again with its current name and a closed lock.
4. Send `/help` again.

Wait for step 3 before testing. If the sender is still missing after ten minutes or so, try **Request User Info** from the sender's app, on the bridge node's entry.

## Super Star Trek (trek.py)

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
- **One game at a time:** a node can only be in one of Trek, Zork, Adventure and Wumpus. Starting another one (`/trek`, `/zork`, `/adventure` or `/wumpus`) replies with a short message saying `You're in Zork. Send /quit to leave, then /trek.` and so on. Trek works the same way as Zork: `/trek` to start, free text inside, `/quit` to leave.
- **Errors:** if the game engine fails, the node gets a short error and the session is closed (the last save is kept), so nobody is left stuck in Zork.
- **Message size:** replies are cleaned (prompts, MORE markers, status lines and Markdown characters removed) and split at line, then sentence boundaries into messages of at most 190 bytes, numbered `(1/3)`. At most `--game-max-chunks` are sent; a longer reply ends with `...(say 'again' / 'look' for more)`.

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

## Colossal Cave Adventure (adventure_session.py and adventure/)

Send `/adventure` by direct message to start a game, or resume your saved one. This is the original cave game by Will Crowther and Don Woods (1976-77), where text adventures began. It works exactly like [Zork](#zork-zork_sessionpy-and-zork): while you are in it, **every** direct message you send goes to the game as a command, including ones starting with `/` (the leading `/` is dropped). Only these are special:

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

### How it is built

The game runs as pure Python, with nothing to install or compile, on Windows, macOS, Linux and a Raspberry Pi Zero W (Python 3.9 or newer). The engine, `adventure/engine.py`, is a **hand-written port** of the C source (troglobit/adventure) rather than a machine translation as for Zork. That source is about 3,000 lines of plain C with one `goto` and no pointer tricks, so a function-by-function port that keeps the C structure is readable and easy to compare, and the Dungeon translator was written for a different style of C. The text, the map, the vocabulary and the starting state are not typed in: `tools/gen_adventure_data.py` extracts them from the C headers into `adventure/data.py`.

All the game's state is in one `Game` object, so any number of games run in one process. The C program reads its commands with `fgets()`, so, as for Dungeon, each game runs in its own thread that waits for the next line and hands back what the game printed; the state is captured each time the game is waiting for a command. The engine follows the same small interface as the Zork engine (`zork/__init__.py`), and `game_session.py`, shared with `/zork`, does the per-node saves, idle timeout, splitting for the mesh and error handling.

### Where the game comes from, and the licence

*Colossal Cave Adventure* was written by **Will Crowther** (1975-77, PDP-10), with major features added by **Don Woods** (1977). It was converted to BDS C by **J. R. Jaeger** (1981), standardised for Unix by **Jerry D. Pohl** (1990), ported to QNX by **James Lummel** (1993) and to Linux and cleaned up by **Joachim Wiberg** (troglobit/adventure, the version this one follows). Full credits are in [`NOTICE`](NOTICE), and the same credits are at the top of `adventure/data.py`.

The licence position is not as clean as it could be, so read [`NOTICE`](NOTICE): the C port's repository dedicates the code to the public domain (the Unlicense, so there is no non-commercial restriction as there is for Dungeon), but its README adds that "the actual license of this program is unknown, although most people agree that it is likely to be considered to be under the public domain". The original authors' own licence has not been seen.

### What differs from the C game

- The start-up question is skipped (see above), and `suspend`/`restore` and the `~/.adventure` save file are replaced by the automatic save. The game never touches files or the environment.
- The random numbers come from a small generator whose state is saved with the game (the C program always seeded `rand()` with 511, so every game rolled the same dice).
- Input lines are cut at 79 characters and the rest is dropped (the C program would have used it as the next command).
- Four defects of the C source that end or corrupt a game are fixed, listed in `NOTICE` and at the top of `adventure/engine.py`: room 140 (the maze "Dead end" with the vending machine) could be entered but not left, because the C code printed `Fatal error number 42` at the next move; `back` was fatal after many moves (for example going down through the grate and then `back`) and used a room number as an array index; and `look` next to a dwarf left the game waiting at location 0. Everywhere else the port prints exactly what the C game prints, including its oddities (for example `say xyzzy` only answers "Okay." when the keys are at hand).

### How it was checked

`tools/crosscheck_adventure.py` builds the original C program (you need the C source and `python -m ziglang`, i.e. `pip install ziglang`) with the same random number generator as the Python engine, and then:

1. dumps every data table (descriptions, messages, travel table, vocabulary, starting state) as the C compiler sees it and compares them with `adventure/data.py`: they are identical;
2. plays thousands of random scripted games on both and compares every character of output. Games start at the beginning, or from random states (random place, carried objects, dwarves, treasure counts, clocks and lamp) so that the dwarves, the pirate, death and reincarnation, the lamp running out and the closing of the cave are reached; the commands are chosen from the room's real exits and the objects present as well as at random. The C copy used for this carries only the four bug fixes above and a hook to set the starting state. The final run was 8,000 games and 911,924 commands, with no difference in any character of output.

Recorded games from the C program (a walk through the opening, a dwarf attack, death and reincarnation, the pirate, the cave closing and closed, the lamp dimming, the final score) are kept in `test_data/adventure_golden.json` and replayed by the tests, so the check runs without the C source. The unit tests also cover the engine on its own, saving and resuming (including the random state), the session layer with two nodes at once, idle timeouts, engine failures, the exclusion of Trek and Zork, and public messages never reaching the game.

Everything above ran on Windows with Python 3.14. Since then the bridge has also been run with a real radio and node (a Heltec V4) on a Raspberry Pi Zero W; see [Running on a Raspberry Pi Zero W](#running-on-a-raspberry-pi-zero-w) for what was checked there.

To regenerate the data from the C source: `python tools/gen_adventure_data.py path/to/colossal/src`. To repeat the comparison: `python tools/crosscheck_adventure.py path/to/colossal/src --games 2000`.

Run the tests with `python -m unittest test_adventure -v` (all of them: `python -m unittest test_trek test_zork test_trek_session test_dungeon test_adventure`).

## Hunt the Wumpus (wumpus_session.py and wumpus/)

Send `/wumpus` by direct message to start a game, or resume your saved one. It works exactly like [Zork](#zork-zork_sessionpy-and-zork): while you are in it, **every** direct message you send is a game command, including ones starting with `/`. Only these are special:

| Message | What it does |
|---|---|
| `/quit` (or `quit`) | Saves and leaves. Normal commands work again |
| `/wumpus` | Shows your cave again (does not use a turn) |
| `/wumpus new` | Abandons your game and starts a fresh one |

You are in a cave system of 20 caves, each joined to 3 others (the corners of a dodecahedron). A reply looks like `Cave 5. Tunnels: 1 4 6. You feel a draft. Arrows 5.` Caves next to a hazard warn you: `You smell a Wumpus.`, `You feel a draft.` (a bottomless pit), `Bats rustle nearby.`

| You type | What it does |
|---|---|
| `M 5`, `move 5`, `go to 5` | Move to cave 5, if a tunnel leads there |
| `S 3 7 12`, `shoot 3 7 12` | Shoot an arrow through up to 5 caves. Where the next cave is not joined to the arrow's current one it flies a random way. You cannot name the cave you named two steps ago (arrows are not that crooked) |
| `M` or `S` alone | The game asks for the cave(s) and your next message is the answer. `cancel` withdraws it |
| `5` | A single bare number is a move |
| `look`, `help` | Show the cave; explain the rules |

Upper or lower case, extra words like `to` and `cave` and a leading `/` are all fine. Anything else gets "Didn't understand" and the game carries on.

The rules, as in the original: two caves have bottomless pits (you lose), two have super bats (they drop you in a random cave, which may be dangerous too), and the Wumpus sleeps until you walk into its cave or shoot and miss, when it wakes and moves to a neighbouring cave 3 times in 4. If it ends up with you, or your arrow comes back round to you, you lose. Hit it with an arrow and you win. You have 5 arrows and lose when they run out. When a game ends the reply says so and `/wumpus` starts another. One difference from the original: it asked "same set-up?" for a replay; here a new game always has a new random layout.

Details: one game and one save file per node (`wumpus_saves/<node>.json`, ignored by git), saved after every command and holding the whole board, the arrows left and the state of the random number generator, so a resumed game is exact. Idle timeout, one-game-at-a-time, errors and direct-messages-only are as for Zork. Replies are normally one message of well under 190 bytes. The game is written as a state machine (the original waits for typed input in the middle of a turn; here each message advances the game exactly one step), in `wumpus/engine.py`; tests are in `test_wumpus.py` (`python -m unittest test_wumpus -v`).

### Where the game comes from

*Hunt the Wumpus* was written by **Gregory Yob** (1945-2005) in BASIC in 1973 and published in *Creative Computing* in 1975. The source listing was read from [RayKV423/original-hunt-the-wumpus](https://github.com/RayKV423/original-hunt-the-wumpus) (`wumpus1973basic`, which that repository's author dedicates to the public domain under the Unlicense). It was used as a **specification only**: the cave map, the hazards and how the Wumpus, bats, pits and arrows behave are the game's rules. The code and every message here were written fresh, and no BASIC code or text was copied. The messages are short, re-worded descriptions of the same events.

## Status

Early version. Running on a Heltec V4 node; expect rough edges.
