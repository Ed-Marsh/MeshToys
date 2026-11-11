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
import math
import queue
import re
import threading
import time

import requests
import meshtastic.serial_interface
import meshtastic.tcp_interface
from pubsub import pub

log = logging.getLogger("mesh_bridge")

PUBLIC_COOLDOWN_S = 60         # minimum gap between public sends
MAX_REPLY_BYTES = 200          # Meshtastic text payload limit is ~233 bytes
COMMAND_RE = re.compile(r"\[\s*([A-Za-z0-9_]+)(?:\s+([^\]]*))?\s*\]")

# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------
COMMANDS = {}
ADMIN_COMMANDS = set()   # names that only --admin nodes may run


def command(name, admin=False):
    """Register a function as the handler for [name]. It receives the
    runtime config (argparse namespace) and a context dict and returns a
    reply string, or None for no reply. Context keys: sender (node id),
    pos ((lat, lon) or None), args (text after the command name inside
    the brackets, "" if none), packet (the received packet dict),
    iface (the meshtastic interface), my_id (this node's id)."""
    def deco(fn):
        COMMANDS[name.lower()] = fn
        if admin:
            ADMIN_COMMANDS.add(name.lower())
        return fn
    return deco


def distance_km(lat1, lon1, lat2, lon2):
    """Great-circle distance (haversine) in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


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
    dist = distance_km(cfg.lat, cfg.lon, pos[0], pos[1])
    log.info("Caller %s position %.5f,%.5f (%.2f km from base)",
             ctx.get("sender"), pos[0], pos[1], dist)
    if dist <= getattr(cfg, "near_km", 2.0):
        return reply + " | You: near home"
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


@command("weather")
def cmd_weather(cfg, ctx):
    """Combined reply: current temperature (home, plus caller if known)
    and the two-day outlook. Either half is dropped if its lookup fails."""
    parts = []
    for fn in (cmd_temperature, cmd_forecast):
        try:
            parts.append(fn(cfg, ctx))
        except Exception:  # noqa: BLE001
            log.exception("[weather] part %s failed", fn.__name__)
    return "\n".join(parts) if parts else "Weather lookup failed"


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


@command("announce", admin=True)
def cmd_announce(cfg, ctx):
    if not cfg.announce:
        return "No preset: start with --announce TEXT, or use [say text]"
    return ctx["bridge"].send_public(cfg.announce.replace("{id}", ctx["my_id"]))


@command("say", admin=True)
def cmd_say(cfg, ctx):
    if not ctx["args"]:
        return "Usage: [say your text]"
    return ctx["bridge"].send_public(ctx["args"])


@command("ping")
def cmd_ping(cfg, ctx):
    return "pong " + time.strftime("%H:%M:%S")


@command("help")
def cmd_help(cfg, ctx):
    is_admin = ctx.get("is_admin")
    names = [c for c in sorted(COMMANDS) if is_admin or c not in ADMIN_COMMANDS]
    return "Commands: " + " ".join(f"[{c}]" for c in names)


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
        self.admins = {a.lower() for a in (cfg.admin or [])}
        self.last_public = 0.0

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

    def node_name(self, node_id):
        try:
            return (self.iface.nodes[node_id].get("user") or {}).get("shortName") or "?"
        except (KeyError, AttributeError, TypeError):
            return "?"

    def log_public(self, packet, sender, text):
        log.info("PUBLIC ch%s from %s (%s): %s", packet.get("channel", 0),
                 sender, self.node_name(sender), text[:200])

    # Called on the meshtastic library's thread: keep it quick.
    def on_receive(self, packet, interface):
        try:
            text = packet["decoded"]["text"]
            sender = packet.get("fromId")
        except KeyError:
            return
        if not sender or sender == self.my_id:
            return
        if packet.get("toId") != self.my_id:
            self.log_public(packet, sender, text)
        elif not COMMAND_RE.search(text):
            # A direct message with no [command]: nothing to run, but log it.
            log.info("DM from %s (%s): %s", sender, self.node_name(sender), text[:200])
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
        args = (m.group(2) or "").strip()
        if packet.get("toId") != self.my_id and name in ADMIN_COMMANDS:
            log.warning("Ignoring admin command [%s] sent publicly by %s", name, sender)
            return
        log.info("%s -> [%s]%s", sender, name, f" {args}" if args else "")
        self.jobs.put((sender, name, args, packet))

    # Runs handlers off the library thread so slow HTTP can't block the radio.
    def worker(self):
        while True:
            sender, name, args, packet = self.jobs.get()
            handler = COMMANDS.get(name)
            is_admin = sender.lower() in self.admins
            if handler is not None and name in ADMIN_COMMANDS and not is_admin:
                # Don't reveal or answer admin commands to anyone else.
                log.warning("Refused admin command [%s] from %s", name, sender)
                continue
            if handler is None:
                reply = f"Unknown [{name}]. Try [help]"
            else:
                try:
                    ctx = {"sender": sender, "pos": self.sender_position(sender),
                           "packet": packet, "iface": self.iface,
                           "my_id": self.my_id, "args": args,
                           "is_admin": is_admin, "bridge": self}
                    reply = handler(self.cfg, ctx)
                except Exception as e:  # noqa: BLE001
                    log.exception("Handler %s failed", name)
                    reply = f"[{name}] failed: {type(e).__name__}"
            if reply:
                self.send(sender, reply)

    def send_public(self, text):
        """Broadcast text on the public channel. Returns a status string."""
        if time.time() - self.last_public < PUBLIC_COOLDOWN_S:
            wait = int(PUBLIC_COOLDOWN_S - (time.time() - self.last_public))
            return f"Too soon, wait {wait}s"
        if len(text.encode("utf-8")) > MAX_REPLY_BYTES:
            return f"Too long (max {MAX_REPLY_BYTES} bytes)"
        log.info("Public send on ch%d: %s", self.cfg.announce_channel, text)
        self.iface.sendText(text, channelIndex=self.cfg.announce_channel)
        self.last_public = time.time()
        return "Sent publicly"

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
    ap.add_argument("--near-km", type=float, default=2.0,
                    help="callers whose known position is within this many km of "
                         "the base are treated as being at home (default 2)")
    ap.add_argument("--admin", nargs="*", default=None, metavar="NODE_ID",
                    help="node IDs allowed to run admin commands such as [announce] "
                         "and [say] (e.g. '!b3725444'). Admin commands are disabled "
                         "if this is not given")
    ap.add_argument("--announce", default=None, metavar="TEXT",
                    help="preset text sent publicly by the [announce] admin command; "
                         "{id} is replaced with this node's ID")
    ap.add_argument("--announce-channel", type=int, default=0, metavar="N",
                    help="channel index for public sends (default 0, the primary)")
    ap.add_argument("--log-file", default=None,
                    help="also append the log (including public messages heard) to this file")
    ap.add_argument("-v", "--verbose", action="store_true")
    cfg = ap.parse_args()
    handlers = [logging.StreamHandler()]
    if cfg.log_file:
        handlers.append(logging.FileHandler(cfg.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if cfg.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)
    Bridge(cfg).run()


if __name__ == "__main__":
    main()
