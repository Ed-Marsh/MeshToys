#!/usr/bin/env python3
"""Per-node /zork sessions for the MeshToys bridge.

While a node is "in Zork" every message it sends (even one starting with "/")
is typed into its own game. "/quit" saves and leaves; "/zork" shows the room
again. Each node ID has its own game, saved to <save_dir>/<node>.json after
every command, so a restart or crash loses nothing.

This module knows nothing about the radio or about which game engine is used
(see zork/__init__.py for the engine interface), so it can be tested on its
own (see test_zork.py).
"""

import json
import logging
import os
import re
import tempfile
import threading
import time

import trek

log = logging.getLogger("zork")

DEFAULT_IDLE_S = 30 * 60
DEFAULT_MAX_CHUNKS = 6
MORE_NOTE = "...(say 'again' / 'look' for more)"
INTRO = ("ZORK. Type commands like 'open mailbox' or 'n'. A leading / is ignored. "
         "/zork shows the room again, /quit saves and leaves.")
INFO = {"zork": "Text adventure. /zork starts or resumes your game, /zork new restarts. "
                "Then every message is a game command; /quit saves and leaves"}

# --- Cleaning the game's output ---------------------------------------------
MORE_RE = re.compile(r"^\s*[\[(<*-]*\s*(more|press (any )?key.*|hit return.*)\s*[\])>*-]*\s*$",
                     re.I)
STATUS_RE = re.compile(r"\bScore:\s*-?\d+.*\bMoves:\s*\d+\s*$", re.I)
BANNER_RE = re.compile(r"^(Welcome to Dungeon|This version created|Copyright \(c\)|"
                       r"ZORK I:|Revision \d+)", re.I)
# The Meshtastic app renders Markdown (see trek.py), so these must never be sent.
MD_TABLE = str.maketrans({"_": " ", "*": "", "`": "'", "~": "-", "\\": "/"})


def clean_output(text):
    """Drop prompts, MORE markers, status lines and banners, squeeze blank lines
    and remove characters the phone app would treat as Markdown."""
    lines = []
    for ln in text.replace("\r", "").split("\n"):
        ln = ln.rstrip()
        if (MORE_RE.match(ln) or STATUS_RE.search(ln) or BANNER_RE.match(ln)
                or ln.strip() == ">"):
            continue
        lines.append(ln)
    text = re.sub(r"\n{2,}", "\n", "\n".join(lines))
    return text.translate(MD_TABLE).strip()


# --- Splitting for the mesh ---------------------------------------------------
def _blen(s):
    return len(s.encode("utf-8"))


def _fit_words(text, limit):
    """Split one over-long string into pieces of at most limit bytes."""
    out, cur = [], ""
    for w in text.split(" "):
        if _blen(w) > limit:                        # one enormous word: cut it
            if cur:
                out.append(cur)
            piece = ""
            for ch in w:
                if _blen(piece + ch) > limit:
                    out.append(piece)
                    piece = ""
                piece += ch
            cur = piece
            continue
        cand = cur + " " + w if cur else w
        if _blen(cand) <= limit:
            cur = cand
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out


def _split_line(line, limit):
    """A line that fits stays whole; otherwise split on sentences, then words."""
    if _blen(line) <= limit:
        return [line]
    pieces, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", line):
        for part in ([sent] if _blen(sent) <= limit else _fit_words(sent, limit)):
            cand = cur + " " + part if cur else part
            if _blen(cand) <= limit:
                cur = cand
            else:
                pieces.append(cur)
                cur = part
    if cur:
        pieces.append(cur)
    return pieces


def chunk_reply(text, max_chunks=DEFAULT_MAX_CHUNKS, limit=trek.MSG_LIMIT):
    """Split text into messages of at most limit bytes, on line then sentence
    boundaries, numbered "(1/3) ..." when there is more than one. At most
    max_chunks are returned; if text was cut, the last one ends with MORE_NOTE."""
    text = text.strip()
    if not text:
        return []
    if _blen(text) <= limit:
        return [text]
    body = limit - 8                                # room for "(10/12) "
    units = []                                      # (text, separator before it)
    for line in text.split("\n"):
        line = line.strip()
        for i, piece in enumerate(_split_line(line, body) if line else []):
            units.append((piece, "\n" if i == 0 else " "))
    chunks, cur = [], ""
    for piece, sep in units:
        cand = cur + sep + piece if cur else piece
        if _blen(cand) <= body:
            cur = cand
        else:
            chunks.append(cur)
            cur = piece
    if cur:
        chunks.append(cur)
    if len(chunks) > max_chunks:
        chunks = chunks[:max(1, max_chunks)]
        last = chunks[-1]
        while last and _blen(last + "\n" + MORE_NOTE) > body:
            parts = last.rsplit(None, 1)
            last = parts[0] if len(parts) > 1 else last[:-1]
        chunks[-1] = last + "\n" + MORE_NOTE if last else MORE_NOTE
    n = len(chunks)
    if n == 1:
        return chunks
    return ["(%d/%d) %s" % (i, n, c) for i, c in enumerate(chunks, 1)]


# --- Sessions -----------------------------------------------------------------
class ZorkSessions:
    """Live games (in memory) plus saved games (one JSON file per node)."""

    name = "zork"                       # the command that starts it

    def __init__(self, save_dir, engine=None, idle_s=DEFAULT_IDLE_S,
                 max_chunks=DEFAULT_MAX_CHUNKS, limit=trek.MSG_LIMIT,
                 clock=time.time, blocked=None):
        self.save_dir = save_dir
        self._engine = engine           # None: load zork.Engine on first use
        self.idle_s = idle_s
        self.max_chunks = max_chunks
        self.limit = limit
        self.clock = clock
        self.blocked = blocked          # callable(node) -> reason string, or None
        self.live = {}                  # key -> [engine, last_active]
        self.notices = set()            # nodes whose idle session was closed
        self.lock = threading.RLock()

    @property
    def engine(self):
        if self._engine is None:
            from zork import default_engine
            self._engine = default_engine()
        return self._engine

    @staticmethod
    def _close(eng):
        """Stop a game's background thread, if the engine has one."""
        try:
            getattr(eng, "close", lambda: None)()
        except Exception:               # noqa: BLE001
            log.exception("Closing a Zork engine failed")

    # -- queries
    def in_zork(self, node):
        with self.lock:
            return node.lower() in self.live

    in_session = in_zork

    def has_notice(self, node):
        with self.lock:
            return node.lower() in self.notices

    def take_notice(self, node):
        with self.lock:
            if node.lower() not in self.notices:
                return []
            self.notices.discard(node.lower())
        return ["Your Zork session was closed after %d min idle and saved. "
                "/zork resumes it." % round(self.idle_s / 60)]

    # -- persistence
    def _path(self, key):
        return os.path.join(self.save_dir, re.sub(r"[^A-Za-z0-9_-]", "_", key) + ".json")

    def _save(self, key, eng):
        try:
            os.makedirs(self.save_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".zork", suffix=".tmp", dir=self.save_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"v": 1, "node": key, "state": eng.to_dict()}, fh)
            os.replace(tmp, self._path(key))
        except (OSError, TypeError, ValueError) as e:
            log.warning("Could not save Zork game for %s: %s", key, e)

    def _load(self, key):
        """Saved engine for key, None if there is no save. Raises if unreadable."""
        try:
            with open(self._path(key), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        return self.engine.from_dict(data["state"])

    def _forget_save(self, key):
        try:
            os.remove(self._path(key))
        except OSError:
            pass

    # -- the commands
    def _reply(self, text):
        return chunk_reply(clean_output(text), self.max_chunks, self.limit)

    def handle(self, node, text):
        """Handle one message from a node: the /zork command, or (when the node
        is in Zork) anything it types. Returns a list of messages to send."""
        key = node.lower()
        text = (text or "").strip()
        low = text.lower()
        with self.lock:
            fresh = re.fullmatch(r"/zork\s+new", low) is not None
            s = self.live.get(key)
            if s is None:
                return self._start(node, key, fresh)
            s[1] = self.clock()
            if low == "/quit":
                return self._quit(key, s[0])
            if fresh:                           # abandon this game, start another
                self._close(self.live.pop(key)[0])
                self._forget_save(key)
                return self._start(node, key, True)
            if low == "/zork":
                return self._guard(key, lambda: s[0].look())
            if re.match(r"/trek(\s|$)", low):
                return ["You're in Zork. Send /quit to leave, then /trek."]
            line = text.lstrip("/ \t")
            if not line:
                return ["Say what? Type a game command, or /quit to leave."]
            return self._guard(key, lambda: s[0].command(line), save=True)

    def _guard(self, key, fn, save=False):
        """Run an engine call; on any failure close the session cleanly."""
        eng = self.live[key][0]
        try:
            out = fn()
            if save:
                if eng.finished:
                    self._forget_save(key)
                else:
                    self._save(key, eng)
            msgs = self._reply(out)
            if eng.finished:
                self._close(self.live.pop(key)[0])
                msgs = msgs + ["Game over. /zork starts a new game."]
            return msgs
        except Exception:                       # noqa: BLE001 - never strand the node
            log.exception("Zork engine failed for %s", key)
            if key in self.live:
                self._close(self.live.pop(key)[0])
            return ["Zork hit an error and has closed. /zork resumes your last save."]

    def _start(self, node, key, fresh=False):
        why = self.blocked(node) if self.blocked else None
        if why:
            return [why]
        note = ""
        eng, resumed = None, False
        if fresh:                               # "/zork new": drop any saved game
            self._forget_save(key)
        try:
            eng = self._load(key)
            resumed = eng is not None
        except Exception:                       # noqa: BLE001
            log.exception("Unreadable Zork save for %s; setting it aside", key)
            try:
                os.replace(self._path(key), self._path(key) + ".bad")
            except OSError:
                pass
            note = "Your saved game was unreadable; starting a new one.\n"
        try:
            if eng is None:
                eng = self.engine.new()
                body = eng.intro()
            else:
                body = eng.look()
            head = "Resuming your saved game." if resumed else INTRO
            text = note + head + "\n" + clean_output(body)
        except Exception:                       # noqa: BLE001
            log.exception("Zork engine failed to start for %s", key)
            return ["Zork hit an error and could not start."]
        self.live[key] = [eng, self.clock()]
        self.notices.discard(key)
        self._save(key, eng)
        return chunk_reply(text, self.max_chunks, self.limit)

    def _quit(self, key, eng):
        self._save(key, eng)
        self._close(self.live.pop(key)[0])
        return ["Zork saved. Goodbye! Normal commands work again; /zork resumes."]

    def reap(self, now=None):
        """Save and close sessions idle longer than idle_s, quietly. The node is
        told on its next message (take_notice). Returns the nodes closed."""
        now = self.clock() if now is None else now
        closed = []
        with self.lock:
            for key, (eng, last) in list(self.live.items()):
                if now - last > self.idle_s:
                    self._save(key, eng)
                    self._close(self.live.pop(key)[0])
                    self.notices.add(key)
                    closed.append(key)
                    log.info("Zork session for %s closed after idle", key)
        return closed
