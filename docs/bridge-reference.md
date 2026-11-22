# Bridge reference

Reference for `mesh_bridge.py`: how commands are sent, every option, node setup, adding your own commands, public messages, caller location and security. The short version is in the [README](../README.md).

## Sending commands

- The command must be the very first thing in the message: `/calc 10/2` works, but `what is 10 /x` and `@calc 10` are ignored. This keeps a `/` used for division inside text from triggering anything.
- Command names are not case sensitive. Everything after the name is passed to the command as its arguments.
- Send `/help` to list the commands and `/help name` (for example `/help calc`) for a one-line description of one. The list has one command per line. The games are not in that list: send `/games` to see them.
- Admin commands (`/announce`, `/say`, `/stats`) are hidden from `/help` unless the sender is in `--admin`, and `/help say` from anyone else answers `Unknown /say`.

## Running the bridge

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
- `--admin '!id' ...`: node IDs allowed to run admin commands (`/announce`, `/say`, `/stats`). Admin commands are disabled unless this is set. Other nodes are ignored silently and the attempt is logged.
- `--announce TEXT`: the preset text sent by `/announce`. `{name}` is replaced with this node's long name, `{short}` with its short name and `{id}` with its ID (for example `!b3725444`).
- `--announce-channel N`: channel index for public sends. Default 0 (the primary channel).
- `--reply-gap SECS`: pause between the parts of a multi-message reply (Trek, Zork and Adventure). Default 3.
- `--stats-file FILE`: where the lifetime usage counters behind `/stats` are kept. Default `stats.json` next to the script. Delete it to reset the totals.
- `--stats-interval-min MIN`: write the stats file at most this often, and only if something has changed since the last write. It is also written when the bridge shuts down cleanly (Ctrl+C or `systemctl stop`), but a power cut can lose up to this long's worth of counts. Default 15.
- `--session-idle-min MIN` (also `--zork-idle-min`): close a Zork, Adventure or Trek session after this many idle minutes. The game is saved. Default 30.
- `--game-max-chunks N` (also `--zork-max-chunks`): most messages sent for one Zork or Adventure reply. Default 6 for Zork and 10 for Adventure (its `help` text and its longest room description need 9 or 10).
- `--zork-save-dir DIR`: where Zork saves go, one file per node. Default `zork_saves/` next to the script (ignored by git).
- `--adventure-save-dir DIR`: where Adventure saves go, one file per node. Default `adventure_saves/` next to the script (ignored by git).
- `--wumpus-save-dir DIR`: where Wumpus saves go, one file per node. Default `wumpus_saves/` next to the script (ignored by git).
- `--log-file PATH`: also append the log to a file.
- `-v`: verbose logging.

## Node setup for WiFi

In the Meshtastic app, open Radio Config, then Network, enable WiFi and enter your SSID and password. Give the node a fixed IP or DHCP reservation. On ESP32 nodes, enabling WiFi is expected to disable Bluetooth, so check the firmware docs for your device.

Only one program can hold the node's USB serial port at a time. The script reconnects automatically if the node reboots or the connection drops.

## Adding commands

Write a function that takes the config and a context dict (`ctx["sender"]` is the caller's node ID, `ctx["pos"]` is their `(lat, lon)` or `None`, and `ctx["packet"]`, `ctx["iface"]` and `ctx["my_id"]` give the raw packet, the Meshtastic interface and this node's ID) and returns a reply string, and register it. The optional `info` text is what `/help name` shows (if omitted, the function's docstring is used), `admin=True` restricts the command to `--admin` nodes, and `game=True` lists it under `/games` instead of `/help`:

```python
@command("uptime", info="How long the PC has been up")
def cmd_uptime(cfg, ctx):
    return "up 3 days"
```

Replies are truncated to 200 bytes to fit Meshtastic's text limit. A `\n` in a reply starts a new line on the receiving client. Keep `info` short enough that `/name` plus the description stays under that limit.

## Public messages

Text messages that are not addressed to this node (public/channel broadcasts) are never acted on with `--dm-only`, but they are written to the log as `PUBLIC chN from !id (name): text`, so you can see replies and general chatter. Use `--log-file` to keep a record. Direct messages to this node that don't start with a `/command` are logged as `DM from !id (name): text`, so private replies to an announcement aren't lost. Admin commands sent publicly are ignored.

To broadcast from the bridge, send `/announce` or `/say your text` as a direct message from a node listed in `--admin`. There is a 60 second cooldown between public sends and a 200 byte limit. Nothing is sent on a timer. Keep public messages infrequent and short, because the channel's airtime is shared by everyone.

Example:

```
python mesh_bridge.py --lat 51.50 --lon -0.12 --host 192.168.1.50 --dm-only --admin '!b3725444' --announce "Home node {name}: DM me /weather or /help" --log-file mesh.log
```

## Caller location

The caller's position comes from the node database on your Heltec, so it only exists if the caller's node has broadcast its position to the mesh (and the position may be old, or deliberately reduced in precision). If it isn't known the reply says so. The coordinates used for each caller are written to the log with their distance from the base, which helps when a reading looks wrong. Before the weather lookup, the caller's coordinates are rounded to 2 decimal places (about 1 km) and only those are sent to Open-Meteo.

## Security

Anyone who can reach the node over the mesh can trigger commands unless `--allow` is set. With `--dm-only`, they must send a direct message to this node; channel broadcasts are ignored. Use `--allow` before adding any command that changes something. `/announce` and `/say` are restricted to nodes in `--admin`, since a node ID is what identifies the sender.
