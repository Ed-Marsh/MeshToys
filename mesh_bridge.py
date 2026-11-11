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
    runtime config (argparse namespace) and returns a reply string, or None
    for no reply."""
    def deco(fn):
        COMMANDS[name.lower()] = fn
        return fn
    return deco


@command("temperature")
def cmd_temperature(cfg):
    # Open-Meteo: free, no API key. Temperatures in Celsius by default.
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": cfg.lat,
            "longitude": cfg.lon,
            "current": "temperature_2m,apparent_temperature",
            "timezone": "auto",
        },
        timeout=10,
    )
    r.raise_for_status()
    cur = r.json()["current"]
    return (f"Temp {cur['temperature_2m']:.1f}C "
            f"(feels {cur['apparent_temperature']:.1f}C)")


@command("ping")
def cmd_ping(cfg):
    return "pong " + time.strftime("%H:%M:%S")


@command("help")
def cmd_help(cfg):
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
        self.jobs.put((sender, name))

    # Runs handlers off the library thread so slow HTTP can't block the radio.
    def worker(self):
        while True:
            sender, name = self.jobs.get()
            handler = COMMANDS.get(name)
            if handler is None:
                reply = f"Unknown [{name}]. Try [help]"
            else:
                try:
                    reply = handler(self.cfg)
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
