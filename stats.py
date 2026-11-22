"""Lifetime usage statistics for the bridge, shown by the admin /stats command.

Counters live in memory and are written to a small JSON file at most once per
flush interval, and only if something changed since the last write (the bridge
may run from an SD card, and usage is light, so there is no need to write on
every command). A final write happens when the bridge shuts down.
"""

import json
import logging
import os
import threading
import time

log = logging.getLogger("mesh_bridge.stats")

DEFAULT_FLUSH_S = 15 * 60
DAY = 86400


class Stats:
    def __init__(self, path, flush_s=DEFAULT_FLUSH_S, clock=time.time):
        self.path = path
        self.flush_s = flush_s
        self.clock = clock
        self.lock = threading.Lock()
        self.started = clock()           # this run (uptime); "since" below is lifetime
        self.dirty = False
        self.last_flush = clock()
        self.data = self._load() or self._fresh()

    def _fresh(self):
        return {"v": 1, "since": self.clock(), "commands": {}, "game_msgs": {},
                "nodes": {}, "unknown": 0, "errors": 0, "refused": 0}

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("v") != 1 or not isinstance(data.get("nodes"), dict):
                raise ValueError("unexpected layout")
            fresh = self._fresh()
            fresh.update(data)
            return fresh
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            bad = self.path + ".bad"
            log.warning("Stats file unreadable (%s); keeping it as %s and starting afresh",
                        e, bad)
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            return None

    # --- recording (called from the worker thread) -------------------------
    def seen(self, node, name="?"):
        """Note that a node did something, and count it as a visitor."""
        now = self.clock()
        with self.lock:
            n = self.data["nodes"].setdefault(
                node, {"first": now, "last": now, "count": 0, "name": "?"})
            n["last"] = now
            n["count"] += 1
            if name and name != "?":
                n["name"] = name
            self.dirty = True

    def command(self, cmd):
        with self.lock:
            self.data["commands"][cmd] = self.data["commands"].get(cmd, 0) + 1
            self.dirty = True

    def game_message(self, game):
        with self.lock:
            self.data["game_msgs"][game] = self.data["game_msgs"].get(game, 0) + 1
            self.dirty = True

    def bump(self, field):
        """field is one of unknown, errors, refused."""
        with self.lock:
            self.data[field] += 1
            self.dirty = True

    # --- persistence --------------------------------------------------------
    def maybe_flush(self):
        """Write the file if something changed and the interval has passed."""
        if self.dirty and self.clock() - self.last_flush >= self.flush_s:
            self.flush()

    def flush(self):
        with self.lock:
            if not self.dirty:
                return
            text = json.dumps(self.data)
            self.dirty = False
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
            self.last_flush = self.clock()
        except OSError:
            log.exception("Could not write stats")
            self.dirty = True

    # --- reporting ----------------------------------------------------------
    def report(self, game_names, now=None):
        """The /stats text as a list of lines. game_names says which command
        names are games, so they can be listed apart from the utilities."""
        now = self.clock() if now is None else now
        with self.lock:
            d = json.loads(json.dumps(self.data))      # a consistent snapshot
        nodes = d["nodes"]
        cmds = d["commands"]
        total = sum(cmds.values())

        def active(days):
            return sum(1 for n in nodes.values() if now - n["last"] < days * DAY)

        def top(items, limit):
            ranked = sorted(items, key=lambda kv: (-kv[1], kv[0]))[:limit]
            return " ".join("%s %d" % (k, v) for k, v in ranked)

        up = int(now - self.started)
        lines = [
            "Stats since %s, up %dd %dh" % (
                time.strftime("%Y-%m-%d", time.localtime(d["since"])), up // DAY,
                up % DAY // 3600),
            "Visitors: %d total, %d in 24h, %d in 7d" % (len(nodes), active(1), active(7)),
            "Commands: %d (unknown %d, errors %d, refused %d)" % (
                total, d["unknown"], d["errors"], d["refused"]),
        ]
        utils = [("/" + c, n) for c, n in cmds.items() if c not in game_names]
        games = [("/" + c, n) for c, n in cmds.items() if c in game_names]
        if utils:
            lines.append("Top: " + top(utils, 8))
        if games:
            lines.append("Games played: " + top(games, 8))
        if d["game_msgs"]:
            lines.append("Game moves: " + top(d["game_msgs"].items(), 8))
        busiest = [("%s(%s)" % (n["name"], k[-4:]), n["count"]) for k, n in nodes.items()]
        if busiest:
            lines.append("Busiest: " + top(busiest, 5))
        return lines
