# MeshToys

Small tools that turn a [Meshtastic](https://meshtastic.org) node into a remote-control and information gateway for a PC.

## mesh_bridge.py

A Heltec node (tested target: Heltec V4) is connected to a PC over USB or WiFi. When a text message arriving over the mesh contains a command in square brackets, such as `[temperature]`, the PC runs the matching handler and sends the result back to the sender as a direct message.

### Built-in commands

| Command | Reply |
|---|---|
| `[temperature]` | Current temperature and "feels like" in °C at your base location, plus the same for the caller's location if their node has shared a position. From [Open-Meteo](https://open-meteo.com) (no API key needed). Example: `Home 12.3C (feels 10.1C) \| You 8.8C (feels 6.8C)` |
| `[weather]` | Combined reply: the `[temperature]` line (base, plus caller if known) then the `[forecast]` line. If one lookup fails the other is still sent |
| `[forecast]` | Today and tomorrow at your base: high/low in °C and chance of rain. Example: `Today 11/5C rain 40% \| Tmrw 9/3C rain 70%` |
| `[signal]` | Radio quality of the caller's message as your node received it: SNR, RSSI and hop count. For a relayed message this is the last hop's link, not the whole path |
| `[nodes]` | How many nodes your Heltec has heard in the last hour, how many it knows in total, and the best SNR with that node's short name |
| `[ping]` | `pong` and the PC's time |
| `[help]` | List of available commands |

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
- `--announce TEXT`: periodically send TEXT as a public message so other mesh users know the node offers these commands. `{id}` is replaced with this node's ID. Off by default. Example: `--announce "Home node {id}: DM me [weather] or [help]"`
- `--announce-every MIN`: minutes between announcements. Default 120, minimum 30. The first one is sent after one full interval, so restarting the script doesn't re-announce each time.
- `--announce-channel N`: channel index to announce on. Default 0 (the primary channel).
- `--log-file PATH`: also append the log to a file.
- `-v`: verbose logging.

### Node setup for WiFi

In the Meshtastic app, open Radio Config, then Network, enable WiFi and enter your SSID and password. Give the node a fixed IP or DHCP reservation. On ESP32 nodes, enabling WiFi is expected to disable Bluetooth, so check the firmware docs for your device.

Only one program can hold the node's USB serial port at a time. The script reconnects automatically if the node reboots or the connection drops.

### Adding commands

Write a function that takes the config and a context dict (`ctx["sender"]` is the caller's node ID, `ctx["pos"]` is their `(lat, lon)` or `None`, and `ctx["packet"]`, `ctx["iface"]` and `ctx["my_id"]` give the raw packet, the Meshtastic interface and this node's ID) and returns a reply string, and register it:

```python
@command("uptime")
def cmd_uptime(cfg, ctx):
    return "up 3 days"
```

Replies are truncated to 200 bytes to fit Meshtastic's text limit.

### Public messages

Text messages that are not addressed to this node (public/channel broadcasts) are never acted on with `--dm-only`, but they are written to the log as `PUBLIC chN from !id (name): text`, so you can see replies to an announcement and general chatter. Use `--log-file` to keep a record. A reply to an announcement may arrive as a public message or as a direct message; a direct message containing a `[command]` is handled as normal.

Keep announcements infrequent and short. The channel's airtime is shared by everyone, which is why the interval has a minimum.

### Caller location

The caller's position comes from the node database on your Heltec, so it only exists if the caller's node has broadcast its position to the mesh (and the position may be old, or deliberately reduced in precision). If it isn't known the reply says so. The coordinates used for each caller are written to the log with their distance from the base, which helps when a reading looks wrong. Before the weather lookup, the caller's coordinates are rounded to 2 decimal places (about 1 km) and only those are sent to Open-Meteo.

### Security

Anyone who can reach the node over the mesh can trigger commands unless `--allow` is set. With `--dm-only`, they must send a direct message to this node; channel broadcasts are ignored. Use `--allow` before adding any command that changes something.

## Status

Initial version. Not yet tested against hardware.
