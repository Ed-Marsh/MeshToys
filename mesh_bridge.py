#!/usr/bin/env python3
"""
Meshtastic <-> PC command bridge.

A Heltec node is plugged into this PC over USB. When a text message arriving
over the mesh starts with a command prefixed with /, e.g. "/temperature",
the matching handler runs on the PC and its return value is sent back to the
sender as a direct message. A handler may return a list of strings, which are
sent as separate messages a few seconds apart (the Super Star Trek game does).

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
import os
import queue
import re
import signal
import sys
import threading
import time

import requests
import meshtastic.serial_interface
import meshtastic.tcp_interface
from pubsub import pub

from adventure import session as adventure_session
import hostops
import stats as stats_mod
import trek
from trek import session as trek_session
from wumpus import session as wumpus_session
from zork import session as zork_session

log = logging.getLogger("mesh_bridge")

PUBLIC_COOLDOWN_S = 60         # minimum gap between public sends
MAX_REPLY_BYTES = 200          # Meshtastic text payload limit is ~233 bytes
REPLY_GAP_S = 3.0              # pause between the parts of a multi-message reply
SESSION_INPUT = "\x00session"   # internal job names (never match a typed /command)
SESSION_NOTICE = "\x00session-notice"
# "/name" at the very start of the message (so a "/" used for division in
# the middle of text never matches). Everything after the name, to the end of the
# message, is args.
COMMAND_RE = re.compile(r"^\s*/([A-Za-z0-9_]+)(?:\s+(.*))?$", re.DOTALL)

# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------
COMMANDS = {}
ADMIN_COMMANDS = set()   # names that only --admin nodes may run
GAME_COMMANDS = set()    # games: listed by /games, left out of the plain /help list
INFO = {}                # name -> one-line description shown by "/help name"


def command(name, admin=False, info="", game=False):
    """Register a function as the handler for /name. It receives the
    runtime config (argparse namespace) and a context dict and returns a
    reply string, or None for no reply. Context keys: sender (node id),
    pos ((lat, lon) or None), args (rest of the message after the command
    name, "" if none), packet (the received packet dict),
    iface (the meshtastic interface), my_id (this node's id).
    game=True marks a game: it is listed by /games instead of /help."""
    def deco(fn):
        COMMANDS[name.lower()] = fn
        INFO[name.lower()] = info or (fn.__doc__ or "").strip()
        if admin:
            ADMIN_COMMANDS.add(name.lower())
        if game:
            GAME_COMMANDS.add(name.lower())
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


@command("temperature", info="Current temperature and feels-like at the base, plus yours if your node shares a position")
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


@command("forecast", info="Today and tomorrow at the base: high/low and chance of rain")
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


@command("weather", info="Temperature plus forecast in one reply")
def cmd_weather(cfg, ctx):
    """Combined reply: current temperature (home, plus caller if known)
    and the two-day outlook. Either half is dropped if its lookup fails."""
    parts = []
    for fn in (cmd_temperature, cmd_forecast):
        try:
            parts.append(fn(cfg, ctx))
        except Exception:  # noqa: BLE001
            log.exception("/weather part %s failed", fn.__name__)
    return "\n".join(parts) if parts else "Weather lookup failed"


@command("signal", info="Signal quality (SNR, RSSI, hops) of your last message as this node received it")
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


@command("nodes", info="How many nodes this node heard in the last hour, how many it knows, and the best SNR")
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


@command("announce", admin=True, info="Admin only. Send the preset announcement to the public channel")
def cmd_announce(cfg, ctx):
    if not cfg.announce:
        return "No preset: start with --announce TEXT, or use /say text"
    b = ctx["bridge"]
    text = (cfg.announce.replace("{id}", ctx["my_id"])
                        .replace("{name}", b.my_long or ctx["my_id"])
                        .replace("{short}", b.my_short or ctx["my_id"]))
    return b.send_public(text)


@command("say", admin=True, info="Admin only. Send your text to the public channel. Usage: /say text")
def cmd_say(cfg, ctx):
    if not ctx["args"]:
        return "Usage: /say your text"
    return ctx["bridge"].send_public(ctx["args"])


@command("stats", admin=True, info="Admin only. Lifetime usage: visitors, commands, games, busiest nodes")
def cmd_stats(cfg, ctx):
    return trek.pack(ctx["bridge"].stats.report(GAME_COMMANDS))


# --- /calc: a small safe calculator (no eval) ------------------------------
CALC_MAX_LEN = 100        # longest expression accepted
CALC_MAX_FACT = 170       # largest n for n! (171! overflows a float)
CALC_MAX_POW_BITS = 20000 # refuse powers whose result would exceed this many bits
CALC_TOKEN_RE = re.compile(r"\s*(\d+\.?\d*|\.\d+|\*\*|//|[-+*/%^()!x])")


class CalcError(Exception):
    pass


def calc_tokens(text):
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = CALC_TOKEN_RE.match(text, pos)
        if not m:
            raise CalcError("bad character")
        out.append(m.group(1))
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return out


def calc_number(tok):
    if re.fullmatch(r"\d+", tok):
        return int(tok)
    return float(tok)


def calc_pow(a, b):
    if isinstance(b, float) and not b.is_integer():
        if a < 0:
            raise CalcError("complex result")
        return float(a) ** b
    b = int(b)
    if abs(b) > 10000 or (isinstance(a, int) and b > 0
                          and a.bit_length() * b > CALC_MAX_POW_BITS):
        raise CalcError("too big")
    return a ** b


def calc_eval(text):
    """Evaluate + - * / // % ** ^ ( ) and postfix ! on plain numbers.
    '^' means power; 'x' is accepted as multiply."""
    if len(text) > CALC_MAX_LEN:
        raise CalcError("too long")
    toks = calc_tokens(text)
    if not toks:
        raise CalcError("empty")
    i = 0

    def peek():
        return toks[i] if i < len(toks) else None

    def take():
        nonlocal i
        i += 1
        return toks[i - 1]

    def expr():
        v = term()
        while peek() in ("+", "-"):
            v = v + term() if take() == "+" else v - term()
        return v

    def term():
        v = unary()
        while peek() in ("*", "x", "/", "//", "%"):
            op = take()
            r = unary()
            if op in ("*", "x"):
                v = v * r
            elif r == 0:
                raise CalcError("divide by zero")
            elif op == "/":
                v = v / r
            elif op == "//":
                v = v // r
            else:
                v = v % r
        return v

    def unary():
        if peek() in ("+", "-"):
            return unary() if take() == "+" else -unary()
        return power()

    def power():
        base = postfix()
        if peek() in ("**", "^"):
            take()
            return calc_pow(base, unary())   # right-associative, allows 2^-1
        return base

    def postfix():
        v = atom()
        while peek() == "!":
            take()
            if isinstance(v, float) and not v.is_integer() or v < 0 or v > CALC_MAX_FACT:
                raise CalcError(f"! needs a whole number 0-{CALC_MAX_FACT}")
            v = math.factorial(int(v))
        return v

    def atom():
        t = peek()
        if t is None:
            raise CalcError("incomplete")
        if t == "(":
            take()
            v = expr()
            if peek() != ")":
                raise CalcError("missing )")
            take()
            return v
        if t[0].isdigit() or t[0] == ".":
            return calc_number(take())
        raise CalcError(f"unexpected {t}")

    result = expr()
    if i != len(toks):
        raise CalcError(f"unexpected {toks[i]}")
    return result


def calc_format(v):
    if isinstance(v, int):
        txt = str(v)
        if len(txt) <= 40:
            return txt
        try:
            return f"{float(v):.6g}"
        except OverflowError:
            return "too big"
    if v != v or v in (float("inf"), float("-inf")):
        return "too big"
    return f"{v:.10g}"


@command("calc", info="Calculator. Example: /calc 23 * 5 / (10 * 2). Supports + - * / // % ** ^ brackets and 4!")
def cmd_calc(cfg, ctx):
    if not ctx["args"]:
        return "Usage: /calc 23 * 5 / (10 * 2)  (also ** ^ // % and 4!)"
    try:
        return calc_format(calc_eval(ctx["args"]))
    except CalcError as e:
        return f"calc: {e}"
    except (OverflowError, ZeroDivisionError):
        return "calc: too big"


@command("ping", info="Replies pong with the PC time, to check the bridge is alive")
def cmd_ping(cfg, ctx):
    return "pong " + time.strftime("%H:%M:%S")


@command("status", info="Bridge and host uptime, plus a ping test to the router and to the internet")
def cmd_status(cfg, ctx):
    b = ctx["bridge"]
    return trek.pack(hostops.status_lines(
        b.stats.started, getattr(cfg, "status_router", "192.168.55.1"),
        getattr(cfg, "status_host", "google.com")))


@command("reboot", admin=True,
         info="Admin only. Reboot the host: /reboot yes (after a delay), /reboot now, /reboot cancel")
def cmd_reboot(cfg, ctx):
    b = ctx["bridge"]
    arg = ctx["args"].strip().lower()
    if arg == "cancel":
        return b.rebooter.cancel()
    if arg not in ("yes", "now"):
        return "This reboots the host. Send /reboot yes (delayed) or /reboot now"
    log.warning("Reboot (%s) requested by %s", arg, ctx["sender"])
    return b.rebooter.schedule(lambda msg: b.send(ctx["sender"], msg), now=(arg == "now"))


@command("help", info="List commands. Use /help name for details, e.g. /help calc")
def cmd_help(cfg, ctx):
    is_admin = ctx.get("is_admin")
    want = ctx["args"].strip().lstrip("/").lower()
    if want:
        # Admin commands are not revealed to non-admins.
        if want not in COMMANDS or (want in ADMIN_COMMANDS and not is_admin):
            return f"Unknown /{want}. Try /help"
        return f"/{want}\n{INFO.get(want) or 'no description'}"
    names = [c for c in sorted(COMMANDS)
             if c not in GAME_COMMANDS and (is_admin or c not in ADMIN_COMMANDS)]
    lines = ["Commands:"] + [f"/{c}" for c in names] + ["/help name for details"]
    return trek.pack(lines)


@command("games", info="List the games you can play over the mesh")
def cmd_games(cfg, ctx):
    lines = ["Games:"] + [f"/{c}" for c in sorted(GAME_COMMANDS)] + ["/help name for details"]
    return trek.pack(lines)


# --- Super Star Trek (see trek/engine.py) ------------------------------------------
TREK = trek.TrekGames(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "trek_games.json"))


@command("trek", info=trek_session.INFO, game=True)
def cmd_trek(cfg, ctx):
    return ctx["bridge"].trek.handle(ctx["sender"], "/trek " + ctx["args"])


# --- Zork (see zork/session.py) ---------------------------------------------
@command("zork", info=zork_session.INFO["zork"], game=True)
def cmd_zork(cfg, ctx):
    return ctx["bridge"].zork.handle(ctx["sender"], ("/zork " + ctx["args"]).strip())


# --- Colossal Cave (see adventure/session.py) -------------------------------
@command("adventure", info=adventure_session.INFO["adventure"], game=True)
def cmd_adventure(cfg, ctx):
    return ctx["bridge"].adventure.handle(ctx["sender"], ("/adventure " + ctx["args"]).strip())


# --- Hunt the Wumpus (see wumpus/session.py) --------------------------------
@command("wumpus", info=wumpus_session.INFO["wumpus"], game=True)
def cmd_wumpus(cfg, ctx):
    return ctx["bridge"].wumpus.handle(ctx["sender"], ("/wumpus " + ctx["args"]).strip())


def _session_input(cfg, ctx):
    """A node in a game (Zork, Adventure or Trek): its message is a game command."""
    for mode in ctx["bridge"].modes:
        if mode.in_session(ctx["sender"]):
            ctx["bridge"].stats.game_message(mode.name)
            return mode.handle(ctx["sender"], ctx["args"])
    return None


def _session_notice(cfg, ctx):
    """Tell a node its idle game session was closed (ctx args = which game)."""
    for mode in ctx["bridge"].modes:
        if mode.name == ctx["args"]:
            return mode.take_notice(ctx["sender"])
    return None


# Jobs the bridge queues itself, so they are not in COMMANDS (or /help).
INTERNAL = {SESSION_INPUT: _session_input, SESSION_NOTICE: _session_notice}


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------
class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.iface = None
        self.my_id = None
        self.my_long = None
        self.my_short = None
        self.jobs = queue.Queue()
        self.allow = {a.lower() for a in cfg.allow} if cfg.allow else None
        self.lost = threading.Event()
        self.admins = {a.lower() for a in (cfg.admin or [])}
        self.last_public = 0.0
        here = os.path.dirname(os.path.abspath(__file__))
        self.stats = stats_mod.Stats(
            getattr(cfg, "stats_file", None) or os.path.join(here, "stats.json"),
            flush_s=getattr(cfg, "stats_interval_min", 15) * 60)
        self.rebooter = hostops.Rebooter(
            delay_s=getattr(cfg, "reboot_delay", hostops.DEFAULT_REBOOT_DELAY_S),
            password=os.environ.get("SUDO_PASSWORD"), before=self.stats.flush)
        idle_s = getattr(cfg, "session_idle_min", 30) * 60
        # A node can be in one game at a time; each says how to leave the others.
        def blocker(me):
            def blocked(node):
                for mode in self.modes:
                    if mode.name != me and mode.in_session(node):
                        return "You're in %s. Send /quit to leave, then /%s." % (mode.title, me)
                return None
            return blocked

        chunks = getattr(cfg, "zork_max_chunks", None)      # --game-max-chunks
        self.zork = zork_session.ZorkSessions(
            getattr(cfg, "zork_save_dir", None) or os.path.join(here, "zork_saves"),
            idle_s=idle_s, max_chunks=chunks or zork_session.DEFAULT_MAX_CHUNKS,
            blocked=blocker("zork"))
        self.adventure = adventure_session.AdventureSessions(
            getattr(cfg, "adventure_save_dir", None) or os.path.join(here, "adventure_saves"),
            idle_s=idle_s, max_chunks=chunks or adventure_session.DEFAULT_MAX_CHUNKS,
            blocked=blocker("adventure"))
        self.wumpus = wumpus_session.WumpusSessions(
            getattr(cfg, "wumpus_save_dir", None) or os.path.join(here, "wumpus_saves"),
            idle_s=idle_s, blocked=blocker("wumpus"))
        self.trek = trek_session.TrekSessions(TREK, idle_s=idle_s, blocked=blocker("trek"))
        self.modes = (self.zork, self.adventure, self.wumpus, self.trek)

    def connect(self):
        if self.cfg.host:
            self.iface = meshtastic.tcp_interface.TCPInterface(
                hostname=self.cfg.host)
        else:
            self.iface = meshtastic.serial_interface.SerialInterface(
                devPath=self.cfg.port)  # None = auto-detect
        me = self.iface.getMyNodeInfo()["user"]
        self.my_id = me["id"]
        self.my_long = me.get("longName") or self.my_id
        self.my_short = me.get("shortName") or self.my_id
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
            # A direct message with no /command: nothing to run, but log it.
            log.info("DM from %s (%s): %s", sender, self.node_name(sender), text[:200])
        if self.cfg.dm_only and packet.get("toId") != self.my_id:
            log.debug("Ignoring non-DM from %s (to %s)", sender, packet.get("toId"))
            return
        if self.allow is not None and sender.lower() not in self.allow:
            log.info("Ignoring %s (not in allow list)", sender)
            return
        m = COMMAND_RE.search(text)
        if packet.get("toId") == self.my_id:
            # A node in a game (Zork, Adventure or Trek) has every direct message typed into
            # it, "/" or not. Public messages are never game input.
            for mode in self.modes:
                if mode.has_notice(sender) and not re.match(
                        r"\s*/%s\b" % mode.name, text, re.I):
                    self.jobs.put((sender, SESSION_NOTICE, mode.name, packet))
            if any(mode.in_session(sender) for mode in self.modes):
                log.info("%s -> game: %s", sender, text[:100])
                self.jobs.put((sender, SESSION_INPUT, text, packet))
                return
        if not m:
            return
        name = m.group(1).lower()
        args = (m.group(2) or "").strip()
        if packet.get("toId") != self.my_id and name in ADMIN_COMMANDS:
            log.warning("Ignoring admin command /%s sent publicly by %s", name, sender)
            return
        log.info("%s -> /%s%s", sender, name, f" {args}" if args else "")
        self.jobs.put((sender, name, args, packet))

    # Runs handlers off the library thread so slow HTTP can't block the radio.
    def worker(self):
        while True:
            sender, name, args, packet = self.jobs.get()
            handler = INTERNAL.get(name) or COMMANDS.get(name)
            is_admin = sender.lower() in self.admins
            if name != SESSION_NOTICE:
                self.stats.seen(sender, self.node_name(sender))
            if handler is not None and name in ADMIN_COMMANDS and not is_admin:
                # Don't reveal or answer admin commands to anyone else.
                log.warning("Refused admin command /%s from %s", name, sender)
                self.stats.bump("refused")
                continue
            if handler is None:
                self.stats.bump("unknown")
                reply = f"Unknown /{name}. Try /help"
            else:
                if name not in INTERNAL:
                    self.stats.command(name)
                try:
                    ctx = {"sender": sender, "pos": self.sender_position(sender),
                           "packet": packet, "iface": self.iface,
                           "my_id": self.my_id, "args": args,
                           "is_admin": is_admin, "bridge": self}
                    reply = handler(self.cfg, ctx)
                except Exception as e:  # noqa: BLE001
                    log.exception("Handler %s failed", name)
                    self.stats.bump("errors")
                    reply = f"/{name} failed: {type(e).__name__}"
            if reply:
                parts = reply if isinstance(reply, (list, tuple)) else [reply]
                for i, part in enumerate(parts):
                    if i:
                        time.sleep(self.cfg.reply_gap)
                    self.send(sender, part)

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
        last_reap = 0.0
        try:
            while True:
                time.sleep(1)
                if time.time() - last_reap > 30:
                    last_reap = time.time()
                    for mode in self.modes:
                        mode.reap()
                    self.stats.maybe_flush()
                if self.lost.is_set():
                    self.reconnect()
        except KeyboardInterrupt:
            pass
        finally:
            self.stats.flush()
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
                    help="node IDs allowed to run admin commands such as /announce "
                         "and /say (e.g. '!b3725444'). Admin commands are disabled "
                         "if this is not given")
    ap.add_argument("--announce", default=None, metavar="TEXT",
                    help="preset text sent publicly by the /announce admin command; "
                         "{name} is replaced with this node's long name, {short} with its "
                         "short name and {id} with its ID")
    ap.add_argument("--announce-channel", type=int, default=0, metavar="N",
                    help="channel index for public sends (default 0, the primary)")
    ap.add_argument("--reply-gap", type=float, default=REPLY_GAP_S, metavar="SECS",
                    help="pause between the parts of a multi-message reply (default 3)")
    ap.add_argument("--session-idle-min", "--zork-idle-min", dest="session_idle_min",
                    type=float, default=30, metavar="MIN",
                    help="close a /zork, /adventure, /wumpus or /trek session after this many idle "
                         "minutes (the game is saved). Default 30")
    ap.add_argument("--game-max-chunks", "--zork-max-chunks", dest="zork_max_chunks", type=int,
                    default=None, metavar="N",
                    help="most messages sent for one game reply (Zork and Adventure). Default "
                         "6 for Zork and 10 for Adventure, whose longest texts need that many")
    ap.add_argument("--zork-save-dir", default=None, metavar="DIR",
                    help="where Zork saves live, one file per node "
                         "(default zork_saves/ next to the script)")
    ap.add_argument("--adventure-save-dir", default=None, metavar="DIR",
                    help="where Adventure saves live, one file per node "
                         "(default adventure_saves/ next to the script)")
    ap.add_argument("--wumpus-save-dir", default=None, metavar="DIR",
                    help="where Wumpus saves live, one file per node "
                         "(default wumpus_saves/ next to the script)")
    ap.add_argument("--stats-file", default=None, metavar="FILE",
                    help="where lifetime usage stats for /stats are kept "
                         "(default stats.json next to the script)")
    ap.add_argument("--stats-interval-min", type=float, default=15, metavar="MIN",
                    help="write the stats file at most this often, and only if something "
                         "changed (also written on shutdown). Default 15")
    ap.add_argument("--status-router", default="192.168.55.1", metavar="HOST",
                    help="host pinged as the 'Router' line of /status (default 192.168.55.1)")
    ap.add_argument("--status-host", default="google.com", metavar="HOST",
                    help="host pinged as the 'Internet' line of /status (default google.com)")
    ap.add_argument("--reboot-delay", type=float, default=hostops.DEFAULT_REBOOT_DELAY_S,
                    metavar="SECS",
                    help="seconds between /reboot yes and the reboot, so the reply gets out "
                         "(default 60)")
    ap.add_argument("--log-file", default=None,
                    help="also append the log (including public messages heard) to this file")
    ap.add_argument("-v", "--verbose", action="store_true")
    cfg = ap.parse_args()
    # .env next to the script (SUDO_PASSWORD for /reboot); never committed.
    hostops.load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    handlers = [logging.StreamHandler()]
    if cfg.log_file:
        handlers.append(logging.FileHandler(cfg.log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if cfg.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers)
    # systemd stops a service with SIGTERM: exit normally so the stats are written.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Bridge(cfg).run()


if __name__ == "__main__":
    main()
