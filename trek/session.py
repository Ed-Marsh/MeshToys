#!/usr/bin/env python3
"""Per-node /trek sessions for the MeshToys bridge.

"/trek" starts (or resumes) a game and puts the node "in Trek". While it is in
Trek every direct message it sends is a game command typed without a "/"
("srs", "nav 3 1.5"; a leading "/" is ignored), and "/quit" leaves. This works
exactly like /zork (see zork/session.py) so the two feel the same, and a node can
only be in one of them at a time.

The game itself, and each node's saved game, are in trek/engine.py (TrekGames). The
game is saved after every command there, so leaving or timing out loses nothing.
"""

import logging
import re
import threading
import time

import trek

log = logging.getLogger("trek")

DEFAULT_IDLE_S = 30 * 60
VERBS = [v for v in trek.INFO if v != "trek"]
TIP = "In Trek: type srs, nav 3 1.5, help ... /quit leaves."
INFO = trek.INFO["trek"]


class TrekSessions:
    name = "trek"                       # the command that starts it
    title = "Trek"

    def __init__(self, games, idle_s=DEFAULT_IDLE_S, clock=time.time, blocked=None):
        self.games = games              # trek.TrekGames
        self.idle_s = idle_s
        self.clock = clock
        self.blocked = blocked          # callable(node) -> reason string, or None
        self.live = {}                  # node key -> last activity time
        self.notices = set()
        self.lock = threading.RLock()

    # -- queries
    def in_session(self, node):
        with self.lock:
            return node.lower() in self.live

    def has_notice(self, node):
        with self.lock:
            return node.lower() in self.notices

    def take_notice(self, node):
        with self.lock:
            if node.lower() not in self.notices:
                return []
            self.notices.discard(node.lower())
        return ["Your Trek session was closed after %d min idle (your game is saved). "
                "/trek resumes it." % round(self.idle_s / 60)]

    # -- the commands
    def handle(self, node, text):
        """Handle "/trek [args]" (to start), or, when the node is in Trek, any
        message it sends. Returns a list of messages to send."""
        key = node.lower()
        text = (text or "").strip()
        with self.lock:
            if key not in self.live:
                return self._start(node, key, text)
            self.live[key] = self.clock()
            low = text.lower()
            if low == "/quit":
                del self.live[key]
                return ["Trek saved. /trek resumes. Normal commands work again."]
            other = re.match(r"/(zork|adventure|wumpus)(\s|$)", low)
            if other:
                return ["You're in Trek. Send /quit to leave, then /%s." % other.group(1)]
            return self._play(node, key, text)

    def _start(self, node, key, text):
        args = re.sub(r"^/?trek\b", "", text, flags=re.I).strip()
        if args.lower() == "help":                       # help does not start a session
            return list(self.games.command(node, "trek", "help"))
        why = self.blocked(node) if self.blocked else None
        if why:
            return [why]
        try:
            msgs = list(self.games.command(node, "trek", args))
        except Exception:                                # noqa: BLE001
            log.exception("Trek failed to start for %s", key)
            return ["Trek hit an error and could not start."]
        self.live[key] = self.clock()
        self.notices.discard(key)
        return msgs + [TIP]

    def _play(self, node, key, text):
        line = text.lstrip("/ \t")
        if not line:
            return ["Type a Trek command (srs, nav 3 1.5, help) or /quit to leave."]
        verb, _, rest = line.partition(" ")
        verb, rest = verb.lower(), rest.strip()
        if verb == "help":
            if rest and rest.lower().lstrip("/") in trek.INFO:
                return [trek.INFO[rest.lower().lstrip("/")]]
            verb, rest = "trek", "help"
        elif verb == "new":
            verb, rest = "trek", "new"
        if verb != "trek" and verb not in VERBS:
            return ["Unknown Trek command '%s'. Try: %s. /quit leaves." % (
                verb[:12], " ".join(VERBS))]
        try:
            msgs = list(self.games.command(node, verb, rest))
        except Exception:                                # noqa: BLE001
            log.exception("Trek failed for %s", key)
            del self.live[key]
            return ["Trek hit an error and has closed. /trek resumes your last save."]
        game = self.games.games.get(key)
        if game is not None and game.over:               # won or lost: leave the session
            del self.live[key]
        return msgs

    def reap(self, now=None):
        """Close sessions idle longer than idle_s, quietly (the game is already
        saved). The node is told on its next message. Returns the nodes closed."""
        now = self.clock() if now is None else now
        closed = []
        with self.lock:
            for key, last in list(self.live.items()):
                if now - last > self.idle_s:
                    del self.live[key]
                    self.notices.add(key)
                    closed.append(key)
                    log.info("Trek session for %s closed after idle", key)
        return closed
