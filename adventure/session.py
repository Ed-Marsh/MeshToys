#!/usr/bin/env python3
"""Per-node /adventure sessions for the MeshToys bridge (Colossal Cave).

Works exactly like /zork (see game_session.py and zork/session.py): "/adventure"
starts or resumes your game, every direct message is then a game command (even one
starting with "/"), "/quit" saves and leaves, "/adventure" shows the room again and
"/adventure new" starts over. The game itself is in adventure/engine.py.
"""

import re

import game_session
from game_session import GameSessions

INTRO = ("COLOSSAL CAVE ADVENTURE. Type one or two words, like 'in', 'get lamp' or 'n'. "
         "'help' gives hints. /adventure shows the room again, /quit saves and leaves.")
DEFAULT_MAX_CHUNKS = 10                 # the longest texts (help, one room) need 9 or 10 messages
MORE_NOTE = "...(cut short)"
INFO = {"adventure": "Colossal Cave. /adventure starts or resumes your game, /adventure new "
                     "restarts. Then every message is a game command; /quit saves and leaves"}


def clean_output(text):
    """Drop the "> " prompts, the bell and the padding of the word list and score table,
    and remove characters the phone app would treat as Markdown. The game's own wording
    and line breaks are kept."""
    text = text.replace("\x07", "")
    text = game_session.clean_output(text)
    return re.sub(r" {3,}", "  ", text)


class AdventureSessions(GameSessions):
    """Live Colossal Cave games (in memory) plus saved games (one JSON file per node)."""

    name = "adventure"                  # the command that starts it
    title = "Adventure"
    intro = INTRO
    others = ("trek", "zork", "wumpus")           # games a player here must /quit first
    more_note = MORE_NOTE

    def __init__(self, save_dir, *args, max_chunks=DEFAULT_MAX_CHUNKS, **kw):
        super().__init__(save_dir, *args, max_chunks=max_chunks, **kw)

    def default_engine(self):
        from adventure import default_engine
        return default_engine()

    def clean(self, text):
        return clean_output(text)
