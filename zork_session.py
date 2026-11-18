#!/usr/bin/env python3
"""Per-node /zork sessions for the MeshToys bridge.

While a node is "in Zork" every message it sends (even one starting with "/")
is typed into its own game. "/quit" saves and leaves; "/zork" shows the room
again. Each node ID has its own game, saved to <save_dir>/<node>.json after
every command, so a restart or crash loses nothing.

The machinery is in game_session.py (shared with /adventure); this module only
says what is particular to Zork: its name, intro text, the noise to strip from
the Dungeon engine's output and which engine to load (see zork/__init__.py for
the engine interface). It can be tested without the radio (see test_zork.py).
"""

import re

import game_session
from game_session import (DEFAULT_IDLE_S, DEFAULT_MAX_CHUNKS, MD_TABLE,  # noqa: F401
                          MORE_NOTE, MORE_RE, GameSessions, chunk_reply)

INTRO = ("ZORK (Dungeon, the original mainframe version, not Zork I). Type commands like "
         "'open mailbox' or 'n'. /zork shows the room again, /quit saves and leaves.")
INFO = {"zork": "Text adventure. /zork starts or resumes your game, /zork new restarts. "
                "Then every message is a game command; /quit saves and leaves"}

STATUS_RE = re.compile(r"\bScore:\s*-?\d+.*\bMoves:\s*\d+\s*$", re.I)
BANNER_RE = re.compile(r"^(Welcome to Dungeon|This version created|Copyright \(c\)|"
                       r"ZORK I:|Revision \d+)", re.I)


def _noise(line):
    return bool(STATUS_RE.search(line) or BANNER_RE.match(line))


def clean_output(text):
    """Drop prompts, MORE markers, status lines and banners, squeeze blank lines
    and remove characters the phone app would treat as Markdown."""
    return game_session.clean_output(text, _noise)


class ZorkSessions(GameSessions):
    """Live Zork games (in memory) plus saved games (one JSON file per node)."""

    name = "zork"                       # the command that starts it
    title = "Zork"
    intro = INTRO
    others = ("trek", "adventure")      # games a player here must /quit first

    in_zork = GameSessions.in_session

    def default_engine(self):
        from zork import default_engine
        return default_engine()

    def clean(self, text):
        return clean_output(text)
