#!/usr/bin/env python3
"""
Meshtastic <-> PC command bridge.

A Heltec node is plugged into this PC over USB. When a text message arriving
over the mesh contains a command in square brackets, e.g. "[temperature]",
the matching handler runs on the PC and its return value is sent back to the
sender as a direct message.

Setup:
    pip install meshtastic pypubsub requests

Run:
    python mesh_bridge.py --lat 51.50 --lon -0.12 --host 192.168.1.50   # WiFi
    python mesh_bridge.py --lat 51.50 --lon -0.12 --port COM5          # USB
    python mesh_bridge.py --lat 51.50 --lon -0.12                      # USB, auto-detect
    python mesh_bridge.py --lat 51.50 --lon -0.12 --host 192.168.1.50 --dm-only
    python mesh_bridge.py --lat 51.50 --lon -0.12 --allow '!a1b2c3d4'

Notes:
  * Only one program can hold the node's USB serial port at a time, so close
    any Meshtastic desktop app/web serial session first.
  * Add new commands by writing a function and decorating it with @command.
"""

import argparse
import logging
import queue
import re
import threading
import time

import requests
import meshtastic.serial_interface
import meshtastic.tcp_interface
from pubsub import pub

log = logging.getLogger("mesh_bridge")

MAX_REPLY_BYTES = 200          # Meshtastic text payload limit is ~233 bytes
COMMAND_RE = re.compile(r"\[\s*([A-Za-z0-9_]+)\s*\]")

# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------
COMMANDS = {}


def command(name):
    """Register a function as the handler for [name]. It receives the
    runtime config (argparse namespace) and a context dict and returns a
    reply string, or None for no reply. Context keys: sender (node id),
    pos ((lat, lon) or None), packet (the received packet dict),
    iface (the meshtastic interface), my_id (this node's id)."""
    def deco(fn):
        COMMANDS[name.lower()] = fn
        return fn
    return deco


def fetch_temp(lat, lon):
    """Return (temperature C, feels-like C) for a point via Open-Meteo
    (free, no API key). Coordinates are rounded to 2 dp (~1 km) before being
    sent, so a caller's precise position is never passed on."""
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": round(lat, 2),
            "longitude": round(lon, 2),
            "current": "temperature_2m,apparent_temperature",
            "timezone": "auto",
        },
        timeout=10,
    )
    r.raise_for_status()
    cur = r.json()["current"]
    return cur["temperature_2m"], cur["apparent_temperature"]


@command("temperature")
def cmd_temperature(cfg, ctx):
    t, f = fetch_temp(cfg.lat, cfg.lon)
    reply = f"Home {t:.1f}C (feels {f:.1f}C)"
    pos = ctx.get("pos")
    if pos is None:
        return reply + " | You: no position known"
    try:
        yt, yf = fetch_temp(*pos)
        return reply + f" | You {yt:.1f}C (feels {yf:.1f}C)"
    except Exception:  # noqa: BLE001
        log.exception("Caller weather lookup failed")
        return reply + " | You: lookup failed"


@command("forecast")
def cmd_forecast(cfg, ctx):
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": round(cfg.lat, 2),
            "longitude": round(cfg.lon, 2),
            "daily": "temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max",
            "forecast_days": 2,
            "timezone": "auto",
        },
        timeout=10,
    )
    r.raise_for_status()
    d = r.json()["daily"]

    def day(i, label):
        rain = d["precipitation_probability_max"][i]
        rain = "?" if rain is None else f"{rain:.0f}%"
        return (f"{label} {d['temperature_2m_max'][i]:.0f}/"
                f"{d['temperature_2m_min'][i]:.0f}C rain {rain}")
    return day(0, "Today") + " | " + day(1, "Tmrw")


@command("signal")
def cmd_signal(cfg, ctx):
    pkt = ctx["packet"]
    snr, rssi = pkt.get("rxSnr"), pkt.get("rxRssi")
    if snr is None and rssi is None:
        return "No radio signal data for that message"
    parts = []
    if snr is not None:
        parts.append(f"SNR {snr:.1f}dB")
    if rssi is not None:
        parts.append(f"RSSI {rssi}dBm")
    start, limit = pkt.get("hopStart"), pkt.get("hopLimit")
    if start is not None and limit is not None:
        hops = start - limit
        parts.append("direct" if hops == 0 else f"{hops} hop(s), last-hop link")
    return "Link: " + ", ".join(parts)


@command("nodes")
def cmd_nodes(cfg, ctx):
    nodes = ctx["iface"].nodes or {}
    now = time.time()
    others = {k: v for k, v in nodes.items() if k != ctx["my_id"]}
    recent = [v for v in others.values()
              if v.get("lastHeard") and now - v["lastHeard"] < 3600]
    reply = f"Heard {len(recent)} node(s) in last hour, {len(others)} known"
    with_snr = [v for v in recent if v.get("snr") is not None]
    if with_snr:
        best = max(with_snr, key=lambda v: v["snr"])
        name = (best.get("user") or {}).get("shortName") or "?"
        reply += f" | best SNR {best['snr']:.1f}dB ({name})"
    return reply


@command("ping")
def cmd_ping(cfg, ctx):
    return "pong " + time.strftime("%H:%M:%S")


@command("help")
def cmd_help(cfg, ctx):
    return "Commands: " + " ".join(f"[{c}]" for c in sorted(COMMANDS))


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------
class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.iface = None
        self.my_id = None
        self.jobs = queue.Queue()
        self.allow = {a.lower() for a in cfg.allow} if cfg.allow else None
        self.lost = threading.Event()

    def connect(self):
        if self.cfg.host:
            self.iface = meshtastic.tcp_interface.TCPInterface(
                hostname=self.cfg.host)
        else:
            self.iface = meshtastic.serial_interface.SerialInterface(
                devPath=self.cfg.port)  # None = auto-detect
        self.my_id = self.iface.getMyNodeInfo()["user"]["id"]
        self.lost.clear()
        log.info("Connected to node %s", self.my_id)

    def on_lost(self, interface=None):
        log.warning("Connection to node lost")
        self.lost.set()

    def start(self):
        pub.subscribe(self.on_receive, "meshtastic.receive.text")
        pub.subscribe(self.on_lost, "meshtastic.connection.lost")
        self.connect()
        threading.Thread(target=self.worker, daemon=True).start()

    def sender_position(self, node_id):
        """Last known (lat, lon) for a node from the local node database, or
        None. Only available if that node shares its position."""
        try:
            pos = self.iface.nodes[node_id].get("position") or {}
        except (KeyError, AttributeError, TypeError):
            return None
        lat, lon = pos.get("latitude"), pos.get("longitude")
        if lat is None and pos.get("latitudeI") is not None:
            lat, lon = pos["latitudeI"] / 1e7, pos["longitudeI"] / 1e7
        if lat is None or lon is None or (lat == 0 and lon == 0):
            return None
        return (lat, lon)

    # Called on the meshtastic library's thread: keep it quick.
    def on_receive(self, packet, interface):
        try:
            text = packet["decoded"]["text"]
            sender = packet.get("fromId")
        except KeyError:
            return
        if not sender or sender == self.my_id:
            return
        if self.cfg.dm_only and packet.get("toId") != self.my_id:
            log.debug("Ignoring non-DM from %s (to %s)", sender, packet.get("toId"))
            return
        if self.allow is not None and sender.lower() not in self.allow:
            log.info("Ignoring %s (not in allow list)", sender)
            return
        m = COMMAND_RE.search(text)
        if not m:
            return
        name = m.group(1).lower()
        log.info("%s -> [%s]", sender, name)
        self.jobs.put((sender, name, packet))

    # Runs handlers off the library thread so slow HTTP can't block the radio.
    def worker(self):
        while True:
            sender, name, packet = self.jobs.get()
            handler = COMMANDS.get(name)
            if handler is None:
                reply = f"Unknown [{name}]. Try [help]"
            else:
                try:
                    ctx = {"sender": sender, "pos": self.sender_position(sender),
                           "packet": packet, "iface": self.iface,
                           "my_id": self.my_id}
                    reply = handler(self.cfg, ctx)
                except Exception as e:  # noqa: BLE001
                    log.exception("Handler %s failed", name)
                    reply = f"[{name}] failed: {type(e).__name__}"
            if reply:
                self.send(sender, reply)

    def send(self, dest, text):
        data = text.encode("utf-8")[:MAX_REPLY_BYTES].decode("utf-8", "ignore")
        log.info("Reply to %s: %s", dest, data)
        try:
            self.iface.sendText(data, destinationId=dest, wantAck=True)
        except Exception:  # noqa: BLE001
            log.exception("Send failed")
            self.lost.set()

    def run(self):
        self.start()
        try:
            while True:
                time.sleep(1)
                if self.lost.is_set():
                    self.reconnect()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                self.iface.close()
            except Exception:  # noqa: BLE001
                pass

    def reconnect(self):
        try:
            self.iface.close()
        except Exception:  # noqa: BLE001
            pass
        while True:
            time.sleep(5)
            try:
                self.connect()
                return
            except Exception as e:  # noqa: BLE001
                log.warning("Reconnect failed: %s", e)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True, help="latitude for weather")
    ap.add_argument("--lon", type=float, required=True, help="longitude for weather")
    ap.add_argument("--host", default=None,
                    help="node IP/hostname for WiFi (TCP) connection; overrides --port")
    ap.add_argument("--port", default=None,
                    help="serial port (e.g. COM5 or /dev/ttyACM0); default auto-detect")
    ap.add_argument("--allow", nargs="*", default=None,
                    help="only obey these node IDs (e.g. '!a1b2c3d4'); default: anyone")
    ap.add_argument("--dm-only", action="store_true",
                    help="only act on direct messages addressed to this node; "
                         "ignore channel/public broadcasts")
    ap.add_argument("-v", "--verbose", action="store_true")
    cfg = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if cfg.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    Bridge(cfg).run()


if __name__ == "__main__":
    main()
