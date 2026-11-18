"""The Dungeon (mainframe Zork) engine behind the interface in zork/__init__.py.

The game itself is engine_gen.py, translated from the C port by tools/c2py.py.
That code was written to read its commands from a terminal, so each game runs in
its own thread and blocks whenever it asks for a line; command() hands it one
and waits until it asks again. All of a game's state lives in one State object,
so any number of games can run side by side.

Saving stores only what differs from a freshly started game, taken each time the
game is waiting at its main ">" prompt.
"""

import threading
import traceback

from . import engine_gen as E
from .rt import DBFile, GameExit, GameIO, text_data

# How long to wait for the game *program* to finish working on one command (normally
# milliseconds). It does not limit how long a player takes to reply: while waiting for
# the player the game just blocks. That limit is the idle timeout in zork_session.
TIMEOUT = 30
SAVE_VERSION = 1

_BASE = {}


def baseline():
    """State of a freshly initialised game (computed once per process)."""
    if "d" not in _BASE:
        g = E.State()
        g.io = GameIO()
        if not E.init_(g):
            raise RuntimeError("Dungeon failed to initialise: " + g.io.take())
        _BASE["d"] = {n: getattr(g, n).to_dict() for n in E.STATE_NAMES}
    return _BASE["d"]


def _delta(g):
    base = baseline()
    out = {}
    for name in E.STATE_NAMES:
        cur = getattr(g, name).to_dict()
        diff = {k: v for k, v in cur.items() if v != base[name][k]}
        if diff:
            out[name] = diff
    return out


def _apply(g, full):
    for name in E.STATE_NAMES:
        getattr(g, name).load_dict(full[name])


class DungeonEngine:
    finished = False

    def __init__(self, rand=None, saved=None, setup=None):
        g = E.State()
        g.io = GameIO(rand)
        g.io.on_main_prompt = self._snap
        self.g = g
        self.setup = setup      # test hook: called after init, before play
        self.text = ""
        self.finished = False
        if saved is None:
            target = self._run_new
        else:
            full = {n: dict(d) for n, d in baseline().items()}
            for name, diff in saved.items():
                if name not in full:
                    raise ValueError("unknown state block %r" % name)
                full[name].update(diff)
            _apply(g, full)
            g.dbfile = DBFile(text_data())
            g.io.resuming = True
            g.io.snapshot = saved
            target = self._run_game
        self.thread = threading.Thread(target=target, daemon=True, name="dungeon")
        self.thread.start()
        self._wait()
        self.text = g.io.take()

    # -- the game thread
    def _run_new(self):
        try:
            if E.init_(self.g):
                if self.setup:
                    self.setup(self.g)
                E.game_(self.g)
        except GameExit:
            pass
        except BaseException as e:      # noqa: BLE001 - reported to the host
            self.g.io.error = e
            self.g.io.tb = traceback.format_exc()
        finally:
            self.finished = True
            self.g.io.idle.set()

    def _run_game(self):
        try:
            E.game_(self.g)
        except GameExit:
            pass
        except BaseException as e:      # noqa: BLE001
            self.g.io.error = e
            self.g.io.tb = traceback.format_exc()
        finally:
            self.finished = True
            self.g.io.idle.set()

    def _snap(self, g):
        g.io.snapshot = _delta(g)

    def _wait(self):
        if not self.g.io.idle.wait(TIMEOUT):
            raise TimeoutError("Dungeon did not respond")
        if self.g.io.error is not None:
            raise RuntimeError("Dungeon engine error: %r: %s" % (
                self.g.io.error, getattr(self.g.io, "tb", "")))

    # -- the engine interface (see zork/__init__.py)
    @classmethod
    def new(cls, rand=None, setup=None):
        return cls(rand=rand, setup=setup)

    @classmethod
    def from_dict(cls, d):
        if d.get("v") != SAVE_VERSION:
            raise ValueError("unsupported save version")
        return cls(saved=d["state"])

    def to_dict(self):
        snap = self.g.io.snapshot
        if snap is None:
            snap = _delta(self.g)
        return {"v": SAVE_VERSION, "state": snap}

    def intro(self):
        return self.text

    def look(self):
        io = self.g.io
        io.take()
        E.rmdesc_(self.g, 3)
        return io.take()

    def command(self, line):
        if self.finished:
            raise RuntimeError("the game is over")
        io = self.g.io
        io.take()
        io.idle.clear()
        io.inq.put(line)
        self._wait()
        return io.take()

    def close(self):
        io = self.g.io
        io.closed = True
        io.inq.put(None)
