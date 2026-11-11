# MeshToys

Small tools that turn a [Meshtastic](https://meshtastic.org) node into a remote-control and information gateway for a PC.

## mesh_bridge.py

A Heltec node (tested target: Heltec V4) is connected to a PC over USB or WiFi. When a text message arriving over the mesh contains a command in square brackets, such as `[temperature]`, the PC runs the matching handler and sends the result back to the sender as a direct message.

### Built-in commands

| Command | Reply |
|---|---|
| `[temperature]` | Current temperature and "feels like" in °C, from [Open-Meteo](https://open-meteo.com) (no API key needed) |
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
- `-v`: verbose logging.

### Node setup for WiFi

In the Meshtastic app, open Radio Config, then Network, enable WiFi and enter your SSID and password. Give the node a fixed IP or DHCP reservation. On ESP32 nodes, enabling WiFi is expected to disable Bluetooth, so check the firmware docs for your device.

Only one program can hold the node's USB serial port at a time. The script reconnects automatically if the node reboots or the connection drops.

### Adding commands

Write a function that takes the config and returns a reply string, and register it:

```python
@command("uptime")
def cmd_uptime(cfg):
    return "up 3 days"
```

Replies are truncated to 200 bytes to fit Meshtastic's text limit.

### Security

Anyone who can reach the node over the mesh can trigger commands unless `--allow` is set. With `--dm-only`, they must send a direct message to this node; channel broadcasts are ignored. Use `--allow` before adding any command that changes something.

## Status

Initial version. Not yet tested against hardware.
