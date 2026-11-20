#!/usr/bin/env python3
"""Per-node /wumpus sessions for the MeshToys bridge (Hunt the Wumpus).

Works exactly like /zork and /adventure (see game_session.py): "/wumpus" starts or
resumes your game, every direct message is then a game command (even one starting
with "/"), "/quit" saves and leaves, "/wumpus" shows the cave again and "/wumpus new"
starts over. The game itself is in wumpus/engine.py.
"""

from game_session import GameSessions

INTRO = ("HUNT THE WUMPUS (Gregory Yob, 1973). M 5 moves, S 3 7 12 shoots, help explains, "
         "/quit saves and leaves.")
INFO = {"wumpus": "Hunt the Wumpus. /wumpus starts or resumes your game, /wumpus new restarts. "
                  "Then M 5 moves, S 3 7 12 shoots; /quit saves and leaves"}


class WumpusSessions(GameSessions):
    """Live Wumpus games (in memory) plus saved games (one JSON file per node)."""

    name = "wumpus"                     # the command that starts it
    title = "Wumpus"
    intro = INTRO
    others = ("trek", "zork", "adventure")   # games a player here must /quit first

    def default_engine(self):
        from wumpus import default_engine
        return default_engine()

    def handle(self, node, text):
        # A bare "quit" is as good as "/quit" (easier to type on a phone).
        if (text or "").strip().lower() in ("quit", "exit") and self.in_session(node):
            text = "/quit"
        return super().handle(node, text)
