# MeshToys

Small tools that turn a [Meshtastic](https://meshtastic.org) node into a remote-control and information gateway for a PC.

- [`mesh_bridge.py`](#mesh_bridgepy): the bridge. Runs commands sent over the mesh and replies by direct message.
- [`trek.py`](#super-star-trek-trekpy): Super Star Trek, a game you play over the mesh through the bridge. Tests are in `test_trek.py`.

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
| `/trek`, `/srs`, `/lrs`, `/nav`, `/pha`, `/tor`, `/shi`, `/dam`, `/cmp` | The Super Star Trek game. See [Super Star Trek](#super-star-trek-trekpy) below. These are listed in `/help` like any other command |

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

Your mission: destroy every Klingon before the stardate deadline, resupplying at starbases when you need to. Send `/trek` to start (or resume) and `/trek help` for a quick guide. `/trek new` abandons the current game and starts another.

| Command | What it does |
|---|---|
| `/trek` | Start a game, or show where you are. `/trek new` restarts, `/trek help` explains |
| `/srs` | Short range scan: your quadrant as an 8x8 map plus stardate, condition, position, energy, shields, torpedoes, Klingons and bases left |
| `/lrs` | Long range scan of the 3x3 quadrants around you. Each three-digit code is Klingons, bases, stars |
| `/nav course warp` | Move, e.g. `/nav 3 1.5`. Courses run 1 to 8.99 (1 = right, 3 = up, 5 = left, 7 = down; fractions aim between). One warp is 8 sectors, and warp 0.1 to 8 is allowed |
| `/pha units` | Fire phasers, e.g. `/pha 300`. The energy is shared between the Klingons in the quadrant and falls off with distance |
| `/tor course` | Fire a photon torpedo along a course. It destroys the first thing it hits (stars absorb it) |
| `/shi units` | Set the shield level. The energy comes from, and returns to, the ship's energy |
| `/dam` | Damage report. Docked at a starbase, `/dam` quotes the repair time and `/dam y` authorizes it (repairs cost stardates). The game reminds you when you dock with damage |
| `/cmp g` | Galactic record: what you have scanned so far (two messages, rows 1-4 and 5-8) |
| `/cmp s` | Status report: Klingons left, days left, starbases |
| `/cmp t`, `/cmp b` | Course and distance to each Klingon, or to the starbase, in your quadrant |
| `/cmp d r1 c1 r2 c2` | Course and distance from one sector to another |

On the map, `E` is you, `K` a Klingon, `B` a starbase, `S` a star and `+` an empty sector (change `DISPLAY` in `trek.py` to use others). The Meshtastic app renders Markdown, so the game never sends `_`, `*`, backticks, `~` or backslashes, which it would use as formatting and swallow. Coordinates are always row,column. Dock by sailing into any sector next to a starbase (diagonals count): energy and torpedoes are refilled, shields drop and Klingons can't hurt you. Ships get damaged by Klingon hits and by chance, and damaged devices stop working until they are repaired by time or at a starbase. Moving across a quadrant boundary costs a stardate.

Every reply is split into messages of at most 190 bytes. Longer replies (for example a scan after a fight) arrive as several messages a few seconds apart. The grid uses one character per sector, so it can look slightly uneven in apps that use a proportional font.

The map symbols were chosen by testing in the Meshtastic Android app, which uses a proportional font and renders Markdown. `.` and `-` are too narrow and make the rows drift, `_` is eaten as italics, and Markdown tables are not rendered (the `|` characters show as text). `+` and `S` have similar widths and line up. If you use a different client, change `DISPLAY` near the top of `trek.py`.

This is a rewrite of a B4J port of the 1978 BASIC game rather than a straight translation. Compared with that version it fixes: random numbers that never produced row/column 8 (so nothing ever sat in those sectors or quadrants), three-Klingon quadrants that could never occur, and starbases that were half as common as intended; phaser damage that ignored how far away the Klingon was; Klingons killed by torpedo that stayed alive; torpedoes that couldn't destroy a starbase; courses between whole numbers (and 8.5 to 8.99) being rounded wrongly; quadrant crossings charging energy and time twice; repairs that advanced the clock eight times; and the missing distance reading for some directions in the distance calculator. A perimeter stop no longer drops the ship on top of a star.

Run the tests with `python -m unittest test_trek -v`.

## Status

Early version. Running on a Heltec V4 node; expect rough edges.
