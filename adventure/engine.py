"""Colossal Cave Adventure (Crowther and Woods) in pure Python.

A line-by-line port of troglobit/adventure, the BDS C to Unix version of the game
(see NOTICE for its origin and the licence position). The structure follows the C
source (turn.c, verb.c, itverb.c, english.c, database.c) so the two can be
compared; the text and tables come from adventure/data.py, which is extracted from
the C headers by tools/gen_adventure_data.py. The port was checked against the C
program with tools/crosscheck_adventure.py.

Differences from the C program, on purpose:
  * two off-by-one slips of the C source are fixed: room 140 (the "Dead end" of the
    maze, with the vending machine) exists, but gettrav() refused it, so the C game
    printed "Fatal error number 42" at the first move out of it, and cond[] and
    visited[] had no entry for it;
  * "back" (goback) crashed the C game with "Fatal error number 42" whenever it
    scanned an exit that leads to a message or special move instead of a room (for
    instance "d" through the grate, then "back"); it also used a room number as an
    index into the 17-entry travel array. It now skips such exits and uses the
    exit's index, which gives the same result as the C code wherever that did not
    crash;
  * after "look" the C code parks the player at location 0 for one turn so the room is
    described again; if a dwarf that had seen the player stood where the player was
    (the "dwarf blocks your way" check), newloc became 0 too and the game waited for a
    command at location 0, where the next description read outside its arrays and the
    next move was fatal. That check now leaves the look alone;
  * all game state lives in one Game object (the C program used globals), so any
    number of games can run in one process;
  * the game never touches files or the environment. "suspend"/"save" and
    "restore"/"resume" just say that the game saves itself (the host saves it after
    every command), and the start-up question "Would you like instructions?" is
    skipped (the game starts as if the answer was no: 330 turns of lamp);
  * random numbers come from a small generator whose state is part of the saved
    game, so a resumed game does not replay the same dice;
  * exit() becomes an exception; a "Fatal error number N" ends the game's session
    without erasing the player's save;
  * input lines are cut at 79 characters and anything after is dropped (the C
    program would have used the rest as the next command).

The C program reads its input with fgets(), so each game runs in its own thread and
blocks whenever it wants a line; AdventureEngine hands it one and waits until it
asks again. The state is saved each time the game is waiting for a command.
"""

import queue
import random
import threading
import traceback

from . import data as D
from .data import *  # noqa: F401,F403 - the #define constants of advent.h

MAXTRAV = 17            # max number of travel directions from a location + terminator
WORDSIZE = 20           # (kept for reference: words are cut at WORDSIZE - 1)
SAVE_VERSION = 1
TIMEOUT = 30            # seconds the game program may take to work on one command

SAVED_SCALARS = ("turns", "loc", "oldloc", "oldloc2", "newloc", "tally", "tally2",
                 "limit", "lmwarn", "wzdark", "closing", "closed", "holding", "detail",
                 "knfloc", "clock1", "clock2", "panic", "dflag", "daltloc", "dkill",
                 "chloc", "chloc2", "bonus", "numdie", "object1", "gaveup", "foobar")
SAVED_ARRAYS = ("cond", "place", "fixed", "visited", "prop", "dloc", "dseen", "odloc")
# exactly the variables saveadv() writes in advent.c


class GameExit(Exception):
    """The program would have called exit(): the game is over or was closed."""


class Bug(Exception):
    """The C program's bug(n): "Fatal error number n"."""


class LCG:
    """The game's random numbers (rand() % n in the C source): a linear congruential
    generator with one integer of state, easy to save. tools/crosscheck_adventure.py
    gives the C program the same generator."""

    def __init__(self, seed=1):
        self.state = seed & 0x7fffffff

    def __call__(self):
        self.state = (self.state * 1103515245 + 12345) & 0x7fffffff
        return (self.state >> 16) & 0x7fff


def _scan_words(line):
    """sscanf(words, "%19s %19s", word1, word2): up to two whitespace-separated
    tokens, each at most 19 characters (a longer word is split)."""
    ws = " \t\n\v\f\r"
    out, i, n = [], 0, len(line)
    for _ in range(2):
        while i < n and line[i] in ws:
            i += 1
        if i >= n:
            break
        j = i
        while j < n and line[j] not in ws and j - i < 19:
            j += 1
        out.append(line[i:j])
        i = j
    return out


class Game:
    """The whole state of one game and the game logic (the C functions)."""

    def __init__(self, rng, read_line, on_prompt=None):
        self.rng = rng
        self.read_line = read_line      # callable() -> str; raises GameExit
        self.on_prompt = on_prompt      # called when the game is about to wait for a command
        self.setup = None               # test hook: called with the game before play starts
        self.out = []
        self.finished = False
        self.travel = [[0, 0, 0] for _ in range(MAXTRAV)]      # [tdest, tverb, tcond]
        self.verb = self.object = self.motion = 0
        self.word1 = self.word2 = ""
        self.initplay()

    # ------------------------------------------------------------ output
    def write(self, text):
        self.out.append(text)

    def take(self):
        text = "".join(self.out)
        self.out.clear()
        return text

    def rand(self):
        return self.rng()

    # ------------------------------------------------------------ set-up
    def initplay(self):
        """initplay() of advent.c (the starting values come from adventure/data.py)."""
        self.turns = 0
        self.cond = list(D.INIT_COND) + [0]         # +1: room 140 exists (see gettrav)
        self.place = list(D.INIT_PLACE)
        self.fixed = list(D.INIT_FIXED)
        self.visited = [0] * (MAXLOC + 1)
        self.prop = [0] * MAXOBJ
        for i in range(50, MAXOBJ):
            self.prop[i] = -1
        self.wzdark = self.closed = self.closing = self.holding = self.detail = 0
        self.limit = 100
        self.tally = 15
        self.tally2 = 0
        self.newloc = 1
        self.loc = self.oldloc = self.oldloc2 = 3
        self.knfloc = 0
        self.chloc = 114
        self.chloc2 = 140
        self.dloc = list(D.INIT_DLOC)
        self.odloc = [0] * DWARFMAX
        self.dkill = 0
        self.dseen = [0] * DWARFMAX
        self.clock1 = 30
        self.clock2 = 50
        self.panic = 0
        self.bonus = 0
        self.numdie = 0
        self.daltloc = 18
        self.lmwarn = 0
        self.foobar = 0
        self.dflag = 0
        self.gaveup = 0
        self.object1 = 0

    def to_state(self):
        """JSON-safe copy of exactly what saveadv() writes, plus the random state."""
        d = {k: getattr(self, k) for k in SAVED_SCALARS}
        for k in SAVED_ARRAYS:
            d[k] = list(getattr(self, k))
        d["rng"] = self.rng.state
        return d

    def load_state(self, d):
        """Restore from to_state(); raises ValueError on anything malformed."""
        sizes = {"cond": MAXLOC + 1, "place": MAXOBJ, "fixed": MAXOBJ, "visited": MAXLOC + 1,
                 "prop": MAXOBJ, "dloc": DWARFMAX, "dseen": DWARFMAX, "odloc": DWARFMAX}
        new = {}
        for k in SAVED_SCALARS:
            v = d[k]
            if type(v) is not int:
                raise ValueError("bad value for %s" % k)
            new[k] = v
        for k in SAVED_ARRAYS:
            v = d[k]
            if (not isinstance(v, list) or len(v) != sizes[k]
                    or any(type(x) is not int for x in v)):
                raise ValueError("bad array %s" % k)
            new[k] = list(v)
        if type(d["rng"]) is not int:
            raise ValueError("bad rng")
        top = MAXLOC + 1
        if not (1 <= new["loc"] < top and 0 <= new["newloc"] < top
                and 0 <= new["oldloc"] < top and 0 <= new["oldloc2"] < top):
            raise ValueError("location out of range")
        for k, v in new.items():
            setattr(self, k, v)
        self.rng.state = d["rng"] & 0x7fffffff

    # ------------------------------------------------------------ running
    def start(self):
        """A new game: main() of advent.c without the instructions question."""
        self.limit = 330
        if self.setup:
            self.setup(self)
        self.run()

    def run(self, resumed=False):
        """The main loop: while (!saveflg) turn(). A resumed game is waiting for a
        command, so it skips the first half of the turn. It ends by raising GameExit
        (the game is over or was closed) or Bug."""
        if resumed:
            self.turn_command()
        while True:
            self.turn()

    def yes(self, msg1, msg2, msg3):
        """Ask a yes/no question; anything not starting with n is yes."""
        if msg1:
            self.rspeak(msg1)
        self.write("> ")
        answer = self.read_line()
        if answer[:1].lower() == "n":
            if msg3:
                self.rspeak(msg3)
            return 0
        if msg2:
            self.rspeak(msg2)
        return 1

    def normend(self):
        self.score()
        self.finished = True
        raise GameExit()

    def bug(self, n):
        self.write("Fatal error number %d\n" % n)
        raise Bug(n)

    # ------------------------------------------------------------ database.c
    def gettrav(self, loc):
        """Fill the travel array for a location."""
        if loc <= 0 or loc > len(D.TRAVEL):     # the C code says >=, which makes room 140 fatal
            self.bug(42)
        entries = [e for e in D.TRAVEL[loc - 1].split(",") if e != ""]
        if len(entries) > MAXTRAV - 1:
            self.bug(33)
        for i, e in enumerate(entries):
            t = int(e)
            self.travel[i][2] = t % 1000
            t //= 1000
            self.travel[i][1] = t % 1000
            t //= 1000
            self.travel[i][0] = t % 1000
        self.travel[len(entries)][0] = -1

    def rspeak(self, msg):
        if not 0 < msg <= len(D.MESSAGES):      # (the C code would read outside its array)
            self.bug(44)
        self.write(D.MESSAGES[msg - 1])

    def pspeak(self, item, state):
        """Print the message of an object for a given state (advent3.h)."""
        if item > 64:
            self.bug(40)
        text = D.ITEM_TEXT[item - 1]
        if text is None:
            self.bug(31)
        n = state + 2
        segs = text.split("/")
        if n > len(segs) - 1:
            self.bug(32)
        self.write(segs[n])

    def desclg(self, loc):
        if not 0 < loc <= len(D.ROOM_LONG):
            self.bug(45)
        self.write(D.ROOM_LONG[loc - 1])

    def descsh(self, loc):
        if not 0 < loc <= len(D.ROOM_SHORT):
            self.bug(45)
        self.write(D.ROOM_SHORT[loc - 1])

    @staticmethod
    def binary(w, maxwc):
        lo, hi = 0, maxwc - 1
        table = D.VOCAB
        while lo <= hi:
            mid = (lo + hi) // 2
            word = table[mid][0]
            if w < word:
                hi = mid - 1
            elif w > word:
                lo = mid + 1
            else:
                return mid
        return -1

    def vocab(self, word, val):
        v1 = self.binary(word, MAXWC)
        if v1 < 0:
            return -1
        v2 = self.binary(word, MAXWC - 1)
        if v2 < 0:
            v2 = v1
        c1, c2 = D.VOCAB[v1][1], D.VOCAB[v2][1]
        if not val:
            return c1 if c1 < c2 else c2
        if val <= c1:
            return c1 % 1000
        if val <= c2:
            return c2 % 1000
        return -1

    def dark(self):
        return not (self.cond[self.loc] & LIGHT) and (not self.prop[LAMP] or not self.here(LAMP))

    def here(self, item):
        return self.place[item] == self.loc or self.toting(item)

    def toting(self, item):
        return self.place[item] == -1

    def forced(self, atloc):
        return self.cond[atloc] == 2

    def pct(self, x):
        return self.rand() % 100 < x

    def at(self, item):
        return self.place[item] == self.loc or self.fixed[item] == self.loc

    def dstroy(self, obj):
        self.move(obj, 0)

    def move(self, obj, where):
        frm = self.place[obj] if obj < MAXOBJ else self.fixed[obj - MAXOBJ]
        if 0 < frm <= 300:
            self.carry(obj, frm)
        self.drop(obj, where)

    def juggle(self, loc):
        pass

    def carry(self, obj, where):
        if obj < MAXOBJ:
            if self.place[obj] == -1:
                return
            self.place[obj] = -1
            self.holding += 1

    def drop(self, obj, where):
        if obj < MAXOBJ:
            if self.place[obj] == -1:
                self.holding -= 1
            self.place[obj] = where
        else:
            self.fixed[obj - MAXOBJ] = where

    def put(self, obj, where, pval):
        self.move(obj, where)
        return (-1) - pval

    def dcheck(self):
        for i in range(1, DWARFMAX - 1):
            if self.dloc[i] == self.loc:
                return i
        return 0

    def liq(self):
        i = self.prop[BOTTLE]
        j = -1 - i
        return self.liq2(i if i > j else j)

    def liqloc(self, loc):
        if self.cond[loc] & LIQUID:
            return self.liq2(self.cond[loc] & WATOIL)
        return self.liq2(1)

    @staticmethod
    def liq2(pbottle):
        return (1 - pbottle) * WATER + (pbottle >> 1) * (WATER + OIL)

    # ------------------------------------------------------------ english.c
    def english(self):
        """Analyze a two word sentence; returns 1 if there is something to do."""
        self.verb = self.object = self.motion = 0
        type2 = val2 = -1
        type1 = val1 = -1
        msg = "bad grammar...\n"

        self.getwords()

        if not self.word1:
            return 0                            # ignore whitespace
        ok, type1, val1 = self.analyze(self.word1)
        if not ok:
            return 0                            # didn't know it
        if type1 == 2 and val1 == SAY:
            self.verb = SAY                     # repeat word & act upon if..
            self.object = 1
            return 1
        if self.word2:
            ok, type2, val2 = self.analyze(self.word2)
            if not ok:
                return 0

        if type1 == 3 and type2 == 3 and val1 == 51 and val2 == 51:
            self.outwords()
            return 0
        elif type1 == 3:
            self.rspeak(val1)
            return 0
        elif type2 == 3:
            self.rspeak(val2)
            return 0
        elif type1 == 0:
            if type2 == 0:
                self.write(msg)
                return 0
            self.motion = val1
        elif type2 == 0:
            self.motion = val2
        elif type1 == 1:
            self.object = val1
            if type2 == 2:
                self.verb = val2
            if type2 == 1:
                self.write(msg)
                return 0
        elif type1 == 2:
            self.verb = val1
            if type2 == 1:
                self.object = val2
            if type2 == 2:
                self.write(msg)
                return 0
        else:
            self.bug(36)
        return 1

    def analyze(self, word):
        """Returns (understood, type, value)."""
        wordval = self.vocab(word, 0)
        if wordval == -1:
            r = self.rand() % 3
            self.rspeak(60 if r == 0 else 61 if r == 1 else 13)
            return 0, -1, -1
        return 1, wordval // 1000, wordval % 1000

    def getwords(self):
        """Prompt for and read a line; the first two words go to word1 and word2."""
        self.write("> ")
        self.word1 = self.word2 = ""
        if self.on_prompt:
            self.on_prompt()
        line = self.read_line()
        line = line.partition("\n")[0].split("\0")[0][:79]
        line = "".join(ch for ch in line if ord(ch) < 128).lower()
        toks = _scan_words(line)
        if toks:
            self.word1 = toks[0]
        if len(toks) > 1:
            self.word2 = toks[1]

    def outwords(self):
        """The list of motion and verb words, six to a line, 20 lines to a page."""
        j = line = 0
        for i in range(MAXWC):
            word, code = D.VOCAB[i]
            if code < 1000 or 1999 < code < 3000:
                self.write("%-12s" % word)
                j += 1
                if j == 6 or i == MAXWC - 1:
                    j = 0
                    self.write("\n")
                    line += 1
                    if line == 20:
                        line = 0
                        self.write("\n\007Enter <RETURN> to continue\n\n")
                        self.read_line()

    # ------------------------------------------------------------ turn.c
    def turn(self):
        """Take one turn."""
        if self.turn_start():
            self.turn_command()

    def turn_start(self):
        """Everything in a turn up to asking for the player's command. Returns False
        if the turn is over without one."""
        # if closing, then he can't leave except via the main office.
        if self.newloc < 9 and self.newloc != 0 and self.closing:
            self.rspeak(130)
            self.newloc = self.loc
            if not self.panic:
                self.clock2 = 15
            self.panic = 1
        # see if a dwarf has seen him and has come from where he wants to go.
        if (self.newloc != self.loc and self.loc != 0 and not self.forced(self.loc)
                and (self.cond[self.loc] & NOPIRAT) == 0):
            for i in range(1, DWARFMAX - 1):
                if self.odloc[i] == self.newloc and self.dseen[i]:
                    self.newloc = self.loc
                    self.rspeak(2)
                    break

        self.dwarves()          # & special dwarf (pirate who steals)

        if self.loc != self.newloc:
            self.turns += 1
            self.loc = self.newloc
            # check for death
            if self.loc == 0:
                self.death()
                return False
            # check for forced move
            if self.forced(self.loc):
                self.describe()
                self.domove()
                return False
            # check for wandering in dark
            if self.wzdark and self.dark() and self.pct(35):
                self.rspeak(23)
                self.oldloc2 = self.loc
                self.death()
                return False
            # describe his situation
            self.describe()
            if not self.dark():
                self.visited[self.loc] += 1
                self.descitem()

        if self.closed:
            if self.prop[OYSTER] < 0 and self.toting(OYSTER):
                self.pspeak(OYSTER, 1)
            for i in range(1, MAXOBJ):
                if self.toting(i) and self.prop[i] < 0:
                    self.prop[i] = -1 - self.prop[i]

        self.wzdark = 1 if self.dark() else 0
        if self.knfloc > 0 and self.knfloc != self.loc:
            self.knfloc = 0

        if self.stimer():       # as the grains of sand slip by
            return False
        return True

    def turn_command(self):
        """Read the player's instruction and carry it out."""
        while not self.english():
            pass
        if self.motion:
            self.domove()
        elif self.object:
            self.doobj()
        else:
            self.itverb()

    def describe(self):
        """Describe the current location."""
        if self.toting(BEAR):
            self.rspeak(141)
        if self.dark():
            self.rspeak(16)
        elif self.visited[self.loc]:
            self.descsh(self.loc)
        else:
            self.desclg(self.loc)
        if self.loc == 33 and self.pct(25) and not self.closing:
            self.rspeak(8)

    def descitem(self):
        """Describe the visible items."""
        for i in range(1, MAXOBJ):
            if self.at(i):
                if i == STEPS and self.toting(NUGGET):
                    continue
                if self.prop[i] < 0:
                    if self.closed:
                        continue
                    self.prop[i] = 0
                    if i == RUG or i == CHAIN:
                        self.prop[i] += 1
                    self.tally -= 1
                if i == STEPS and self.loc == self.fixed[STEPS]:
                    state = 1
                else:
                    state = self.prop[i]
                self.pspeak(i, state)
        if self.tally == self.tally2 and self.tally != 0 and self.limit > 35:
            self.limit = 35

    def domove(self):
        """Handle motion requests."""
        self.gettrav(self.loc)
        m = self.motion
        if m == NULLX:
            pass
        elif m == BACK:
            self.goback()
        elif m == LOOK:
            if self.detail < 3:
                self.rspeak(15)
            self.detail += 1
            self.wzdark = 0
            self.visited[self.loc] = 0
            self.newloc = self.loc
            self.loc = 0
        elif m == CAVE:
            if self.loc < 8:
                self.rspeak(57)
            else:
                self.rspeak(58)
        else:
            self.oldloc2 = self.oldloc
            self.oldloc = self.loc
            self.dotrav()

    def goback(self):
        """Return from whence we came!"""
        if self.forced(self.oldloc):
            want = self.oldloc2
        else:
            want = self.oldloc
        self.oldloc2 = self.oldloc
        self.oldloc = self.loc
        k2 = 0
        if want == self.loc:
            self.rspeak(91)
            return
        strav = [[0, 0, 0] for _ in range(MAXTRAV)]
        self.copytrv(self.travel, strav)
        kk = 0
        while self.travel[kk][0] != -1:
            if not self.travel[kk][2] and self.travel[kk][0] == want:
                self.motion = self.travel[kk][1]
                self.dotrav()
                return
            if not self.travel[kk][2]:
                k2 = kk
                temp = self.travel[kk][0]
                if 0 < temp <= len(D.TRAVEL):   # only rooms have a travel table; the C code
                    self.gettrav(temp)          # asked for one for every exit, so "back" after
                    if self.forced(temp) and self.travel[0][0] == want:     # some moves was
                        k2 = kk                 # fatal (error 42); and it said k2 = temp, a
                    self.copytrv(strav, self.travel)    # room number, then read travel[k2]
            kk += 1
        if k2:
            self.motion = self.travel[k2][1]
            self.dotrav()
        else:
            self.rspeak(140)

    @staticmethod
    def copytrv(trav1, trav2):
        """Copy a travel array. The C function never advances its pointers, so it
        copies only the first entry, MAXTRAV times; that is kept as it is."""
        for _ in range(MAXTRAV):
            trav2[0][0] = trav1[0][0]
            trav2[0][1] = trav1[0][1]
            trav2[0][2] = trav1[0][2]

    def dotrav(self):
        """Figure out a new location given the current location and a motion."""
        self.newloc = self.loc
        mvflag = hitflag = 0
        pctt = self.rand() % 100
        rdest = 0
        kk = 0
        while self.travel[kk][0] >= 0 and not mvflag:
            rdest, rverb, rcond = self.travel[kk]
            robject = rcond % 100
            kk += 1
            if rverb != 1 and rverb != self.motion and not hitflag:
                continue
            hitflag += 1
            c = rcond // 100
            if c == 0:
                if rcond == 0 or pctt < rcond:
                    mvflag += 1
            elif c == 1:
                if robject == 0:
                    mvflag += 1
                elif self.toting(robject):
                    mvflag += 1
            elif c == 2:
                if self.toting(robject) or self.at(robject):
                    mvflag += 1
            elif c in (3, 4, 5, 7):
                if self.prop[robject] != c - 3:
                    mvflag += 1
            else:
                self.bug(37)
        if not mvflag:
            self.badmove()
        elif rdest > 500:
            self.rspeak(rdest - 500)
        elif rdest > 300:
            self.spcmove(rdest)
        else:
            self.newloc = rdest

    def badmove(self):
        """The player tried a poor move option."""
        m = self.motion
        msg = 12
        if 43 <= m <= 50:
            msg = 9
        if m == 29 or m == 30:
            msg = 9
        if m == 7 or m == 36 or m == 37:
            msg = 10
        if m == 11 or m == 19:
            msg = 11
        if self.verb == FIND or self.verb == INVENTORY:
            msg = 59
        if m == 62 or m == 65:
            msg = 42
        if m == 17:
            msg = 80
        self.rspeak(msg)

    def spcmove(self, rdest):
        """Very special movement."""
        k = rdest - 300
        if k == 1:      # plover movement via alcove
            if not self.holding or (self.holding == 1 and self.toting(EMERALD)):
                self.newloc = (99 + 100) - self.loc
            else:
                self.rspeak(117)
        elif k == 2:    # trying to remove plover, bad route
            self.drop(EMERALD, self.loc)
        elif k == 3:    # troll bridge
            if self.prop[TROLL] == 1:
                self.pspeak(TROLL, 1)
                self.prop[TROLL] = 0
                self.move(TROLL2, 0)
                self.move(TROLL2 + MAXOBJ, 0)
                self.move(TROLL, 117)
                self.move(TROLL + MAXOBJ, 122)
                self.juggle(CHASM)
                self.newloc = self.loc
            else:
                self.newloc = 122 if self.loc == 117 else 117
                if self.prop[TROLL] == 0:
                    self.prop[TROLL] += 1
                if not self.toting(BEAR):
                    return
                self.rspeak(162)
                self.prop[CHASM] = 1
                self.prop[TROLL] = 2
                self.drop(BEAR, self.newloc)
                self.fixed[BEAR] = -1
                self.prop[BEAR] = 3
                if self.prop[SPICES] < 0:
                    self.tally2 += 1
                self.oldloc2 = self.newloc
                self.death()
        else:
            self.bug(38)

    def dwarfend(self):
        """The player's demise via waking up the dwarves..."""
        self.death()
        self.normend()

    def score(self):
        s = t = 0
        for i in range(50, MAXTRS + 1):
            if i == CHEST:
                k = 14
            elif i > CHEST:
                k = 16
            else:
                k = 12
            if self.prop[i] >= 0:
                t += 2
            if self.place[i] == 3 and self.prop[i] == 0:
                t += k - 2
        s = t
        self.write("%-20s%d\n" % ("Treasures:", t))
        t = (MAXDIE - self.numdie) * 10
        if t:
            self.write("%-20s%d\n" % ("Survival:", t))
        s += t
        if not self.gaveup:
            s += 4
        t = 25 if self.dflag else 0
        if t:
            self.write("%-20s%d\n" % ("Getting well in:", t))
        s += t
        t = 25 if self.closing else 0
        if t:
            self.write("%-20s%d\n" % ("Masters section:", t))
        s += t
        if self.closed:
            if self.bonus == 0:
                t = 10
            elif self.bonus == 135:
                t = 25
            elif self.bonus == 134:
                t = 30
            elif self.bonus == 133:
                t = 45
            self.write("%-20s%d\n" % ("Bonus:", t))
            s += t
        if self.place[MAGAZINE] == 108:
            s += 1
        s += 2
        self.write("%-20s%d\n" % ("Score:", s))

    def death(self):
        """The passing on of one of the player's incarnations..."""
        if not self.closing:
            yea = self.yes(81 + self.numdie * 2, 82 + self.numdie * 2, 54)
            self.numdie += 1
            if self.numdie >= MAXDIE or not yea:
                self.normend()
            self.place[WATER] = 0
            self.place[OIL] = 0
            if self.toting(LAMP):
                self.prop[LAMP] = 0
            for j in range(1, MAXOBJ):
                i = MAXOBJ - j
                if self.toting(i):
                    self.drop(i, 1 if i == LAMP else self.oldloc2)
            self.newloc = 3
            self.oldloc = self.loc
            return
        # closing -- no resurrection...
        self.rspeak(131)
        self.numdie += 1
        self.normend()

    def doobj(self):
        """Process an object."""
        o = self.object
        if self.fixed[o] == self.loc or self.here(o):       # is object here? transitive
            self.trobj()
        elif o == GRATE:                                    # did he give grate as destination?
            if self.loc in (1, 4, 7):
                self.motion = DEPRESSION
                self.domove()
            elif 9 < self.loc < 15:
                self.motion = ENTRANCE
                self.domove()
        elif self.dcheck() and self.dflag >= 2:             # is it a dwarf he is after?
            self.object = DWARF
            self.trobj()
        elif (self.liq() == o and self.here(BOTTLE)) or self.liqloc(self.loc) == o:
            self.trobj()                                    # trying to get/use a liquid?
        elif o == PLANT and self.at(PLANT2) and self.prop[PLANT2] == 0:
            self.object = PLANT2
            self.trobj()
        elif o == KNIFE and self.knfloc == self.loc:        # trying to grab a knife?
            self.rspeak(116)
            self.knfloc = -1
        elif o == ROD and self.here(ROD2):                  # trying to get at dynamite?
            self.object = ROD2
            self.trobj()
        else:
            self.write("I see no %s here.\n" % self.probj(self.object))

    def trobj(self):
        """Process an object being referred to."""
        if self.verb:
            self.trverb()
        else:
            self.write("What do you want to do with the %s?\n" % self.probj(self.object))

    def probj(self, obj):
        """The word the player typed for the object."""
        _, wtype, _ = self.analyze(self.word1)
        return self.word1 if wtype == 1 else self.word2

    def dwarves(self):
        """The dwarves, and the pirate."""
        # see if dwarves allowed here
        if self.newloc == 0 or self.forced(self.newloc) or self.cond[self.newloc] & NOPIRAT:
            return
        # see if dwarves are active.
        if not self.dflag:
            if self.newloc > 15:
                self.dflag += 1
            return
        # if first close encounter (of 3rd kind) kill 0, 1 or 2
        if self.dflag == 1:
            if self.newloc < 15 or self.pct(95):
                return
            self.dflag += 1
            for _ in range(1, 3):
                if self.pct(50):
                    self.dloc[self.rand() % 5 + 1] = 0
            for i in range(1, DWARFMAX - 1):
                if self.dloc[i] == self.newloc:
                    self.dloc[i] = self.daltloc
                self.odloc[i] = self.dloc[i]
            self.rspeak(3)
            self.drop(AXE, self.newloc)
            return
        dtotal = attack = stick = 0
        for i in range(1, DWARFMAX):
            if self.dloc[i] == 0:
                continue
            # move a dwarf at random
            j = 0
            for _try in range(1, 20):
                j = self.rand() % 106 + 15          # allowed area
                if (j != self.odloc[i] and j != self.dloc[i]
                        and not (i == DWARFMAX - 1 and (self.cond[j] & NOPIRAT) == NOPIRAT)):
                    break
            if j == 0:
                j = self.odloc[i]
            self.odloc[i] = self.dloc[i]
            self.dloc[i] = j
            if ((self.dseen[i] and self.newloc >= 15) or self.dloc[i] == self.newloc
                    or self.odloc[i] == self.newloc):
                self.dseen[i] = 1
            else:
                self.dseen[i] = 0
            if not self.dseen[i]:
                continue
            self.dloc[i] = self.newloc
            if i == 6:
                self.dopirate()
            else:
                dtotal += 1
                if self.odloc[i] == self.dloc[i]:
                    attack += 1
                    if self.knfloc >= 0:
                        self.knfloc = self.newloc
                    if self.rand() % 1000 < 95 * (self.dflag - 2):
                        stick += 1
        if dtotal == 0:
            return
        if dtotal > 1:
            self.write("There are %d threatening little dwarves in the room with you!\n" % dtotal)
        else:
            self.rspeak(4)
        if attack == 0:
            return
        if self.dflag == 2:
            self.dflag += 1
        if attack > 1:
            self.write("%d of them throw knives at you!!\n" % attack)
            k = 6
        else:
            self.rspeak(5)
            k = 52
        if stick <= 1:
            self.rspeak(stick + k)
            if stick == 0:
                return
        else:
            self.write("%d of them get you !!!\n" % stick)
        self.oldloc2 = self.newloc
        self.death()

    def dopirate(self):
        """The pirate."""
        if self.newloc == self.chloc or self.prop[CHEST] >= 0:
            return
        k = 0
        steal = False
        for j in range(50, MAXTRS + 1):
            if j != PYRAMID or (self.newloc != self.place[PYRAMID]
                                and self.newloc != self.place[EMERALD]):
                if self.toting(j):
                    steal = True
                    break
                if self.here(j):
                    k += 1
        if not steal:
            if (self.tally == self.tally2 + 1 and k == 0 and self.place[CHEST] == 0
                    and self.here(LAMP) and self.prop[LAMP] == 1):
                self.rspeak(186)
                self.move(CHEST, self.chloc)
                self.move(MESSAGE, self.chloc2)
                self.dloc[6] = self.chloc
                self.odloc[6] = self.chloc
                self.dseen[6] = 0
                return
            if self.odloc[6] != self.dloc[6] and self.pct(20):
                self.rspeak(127)
                return
            return
        # stealit:
        self.rspeak(128)
        if self.place[MESSAGE] == 0:
            self.move(CHEST, self.chloc)
        self.move(MESSAGE, self.chloc2)
        for j in range(50, MAXTRS + 1):
            if j == PYRAMID and (self.newloc == self.place[PYRAMID]
                                 or self.newloc == self.place[EMERALD]):
                continue
            if self.at(j) and self.fixed[j] == 0:
                self.carry(j, self.newloc)
            if self.toting(j):
                self.drop(j, self.chloc)
        self.dloc[6] = self.chloc
        self.odloc[6] = self.chloc
        self.dseen[6] = 0

    def stimer(self):
        """Special time limit stuff... Returns 1 when the cave has just closed."""
        self.foobar = -self.foobar if self.foobar > 0 else 0
        if self.tally == 0 and self.loc >= 15 and self.loc != 33:
            self.clock1 -= 1
        if self.clock1 == 0:
            # start closing the cave
            self.prop[GRATE] = 0
            self.prop[FISSURE] = 0
            for i in range(1, DWARFMAX):
                self.dseen[i] = 0
            self.move(TROLL, 0)
            self.move(TROLL + MAXOBJ, 0)
            self.move(TROLL2, 117)
            self.move(TROLL2 + MAXOBJ, 122)
            self.juggle(CHASM)
            if self.prop[BEAR] != 3:
                self.dstroy(BEAR)
            self.prop[CHAIN] = 0
            self.fixed[CHAIN] = 0
            self.prop[AXE] = 0
            self.fixed[AXE] = 0
            self.rspeak(129)
            self.clock1 = -1
            self.closing = 1
            return 0
        if self.clock1 < 0:
            self.clock2 -= 1
        if self.clock2 == 0:
            # set up storage room... and close the cave...
            self.prop[BOTTLE] = self.put(BOTTLE, 115, 1)
            self.prop[PLANT] = self.put(PLANT, 115, 0)
            self.prop[OYSTER] = self.put(OYSTER, 115, 0)
            self.prop[LAMP] = self.put(LAMP, 115, 0)
            self.prop[ROD] = self.put(ROD, 115, 0)
            self.prop[DWARF] = self.put(DWARF, 115, 0)
            self.loc = 115
            self.oldloc = 115
            self.newloc = 115
            self.put(GRATE, 116, 0)
            self.prop[SNAKE] = self.put(SNAKE, 116, 1)
            self.prop[BIRD] = self.put(BIRD, 116, 1)
            self.prop[CAGE] = self.put(CAGE, 116, 0)
            self.prop[ROD2] = self.put(ROD2, 116, 0)
            self.prop[PILLOW] = self.put(PILLOW, 116, 0)
            self.prop[MIRROR] = self.put(MIRROR, 115, 0)
            self.fixed[MIRROR] = 116
            for i in range(1, MAXOBJ):
                if self.toting(i):
                    self.dstroy(i)
            self.rspeak(132)
            self.closed = 1
            return 1
        if self.prop[LAMP] == 1:
            self.limit -= 1
        if (self.limit <= 30 and self.here(BATTERIES) and self.prop[BATTERIES] == 0
                and self.here(LAMP)):
            self.rspeak(188)
            self.prop[BATTERIES] = 1
            if self.toting(BATTERIES):
                self.drop(BATTERIES, self.loc)
            self.limit += 2500
            self.lmwarn = 0
            return 0
        if self.limit == 0:
            self.limit -= 1
            self.prop[LAMP] = 0
            if self.here(LAMP):
                self.rspeak(184)
            return 0
        if self.limit < 0 and self.loc <= 8:
            self.rspeak(185)
            self.gaveup = 1
            self.normend()
        if self.limit <= 30:
            if self.lmwarn or not self.here(LAMP):
                return 0
            self.lmwarn = 1
            i = 187
            if self.place[BATTERIES] == 0:
                i = 183
            if self.prop[BATTERIES] == 1:
                i = 189
            self.rspeak(i)
            return 0
        return 0

    # ------------------------------------------------------------ verb.c
    def trverb(self):
        """Process a transitive verb."""
        v = self.verb
        if v in (CALM, WALK, QUIT, SCORE, FOO, BRIEF, SUSPEND, HOURS, LOG):
            self.actspk(v)
        elif v == TAKE:
            self.vtake()
        elif v == DROP:
            self.vdrop()
        elif v in (OPEN, LOCK):
            self.vopen()
        elif v == SAY:
            self.vsay()
        elif v == NOTHING:
            self.rspeak(54)
        elif v == ON:
            self.von()
        elif v == OFF:
            self.voff()
        elif v == WAVE:
            self.vwave()
        elif v == KILL:
            self.vkill()
        elif v == POUR:
            self.vpour()
        elif v == EAT:
            self.veat()
        elif v == DRINK:
            self.vdrink()
        elif v == RUB:
            if self.object != LAMP:
                self.rspeak(76)
            else:
                self.actspk(RUB)
        elif v == THROW:
            self.vthrow()
        elif v == FEED:
            self.vfeed()
        elif v in (FIND, INVENTORY):
            self.vfind()
        elif v == FILL:
            self.vfill()
        elif v == READ:
            self.vread()
        elif v == BLAST:
            self.vblast()
        elif v == BREAK:
            self.vbreak()
        elif v == WAKE:
            self.vwake()
        else:
            self.write("This verb is not implemented yet.\n")

    def vtake(self):
        """CARRY, TAKE etc."""
        if self.toting(self.object):
            self.actspk(self.verb)
            return
        # special case objects and fixed objects
        msg = 25
        if self.object == PLANT and self.prop[PLANT] <= 0:
            msg = 115
        if self.object == BEAR and self.prop[BEAR] == 1:
            msg = 169
        if self.object == CHAIN and self.prop[BEAR] != 0:
            msg = 170
        if self.fixed[self.object]:
            self.rspeak(msg)
            return
        # special case for liquids
        if self.object == WATER or self.object == OIL:
            if not self.here(BOTTLE) or self.liq() != self.object:
                self.object = BOTTLE
                if self.toting(BOTTLE) and self.prop[BOTTLE] == 1:
                    self.vfill()
                    return
                if self.prop[BOTTLE] != 1:
                    msg = 105
                if not self.toting(BOTTLE):
                    msg = 104
                self.rspeak(msg)
                return
            self.object = BOTTLE
        if self.holding >= 7:
            self.rspeak(92)
            return
        # special case for bird.
        if self.object == BIRD and self.prop[BIRD] == 0:
            if self.toting(ROD):
                self.rspeak(26)
                return
            if not self.toting(CAGE):
                self.rspeak(27)
                return
            self.prop[BIRD] = 1
        if (self.object == BIRD or self.object == CAGE) and self.prop[BIRD] != 0:
            self.carry((BIRD + CAGE) - self.object, self.loc)
        self.carry(self.object, self.loc)
        # handle liquid in bottle
        i = self.liq()
        if self.object == BOTTLE and i != 0:
            self.place[i] = -1
        self.rspeak(54)

    def vdrop(self):
        """DROP etc."""
        # check for dynamite
        if self.toting(ROD2) and self.object == ROD and not self.toting(ROD):
            self.object = ROD2
        if not self.toting(self.object):
            self.actspk(self.verb)
            return
        o = self.object
        if o == BIRD and self.here(SNAKE):                  # snake and bird
            self.rspeak(30)
            if self.closed:
                self.dwarfend()
            self.dstroy(SNAKE)
            self.prop[SNAKE] = -1
        elif o == COINS and self.here(VEND):                # coins and vending machine
            self.dstroy(COINS)
            self.drop(BATTERIES, self.loc)
            self.pspeak(BATTERIES, 0)
            return
        elif o == BIRD and self.at(DRAGON) and self.prop[DRAGON] == 0:   # bird and dragon
            self.rspeak(154)
            self.dstroy(BIRD)
            self.prop[BIRD] = 0
            if self.place[SNAKE] != 0:
                self.tally2 += 1
            return
        # Bear and troll
        if o == BEAR and self.at(TROLL):
            self.rspeak(163)
            self.move(TROLL, 0)
            self.move(TROLL + MAXOBJ, 0)
            self.move(TROLL2, 117)
            self.move(TROLL2 + MAXOBJ, 122)
            self.juggle(CHASM)
            self.prop[TROLL] = 2
        elif o == VASE:                                     # vase
            if self.loc == 96:
                self.rspeak(54)
            else:
                self.prop[VASE] = 0 if self.at(PILLOW) else 2
                self.pspeak(VASE, self.prop[VASE] + 1)
                if self.prop[VASE] != 0:
                    self.fixed[VASE] = -1
        # handle liquid and bottle
        i = self.liq()
        if i == self.object:
            self.object = BOTTLE
        if self.object == BOTTLE and i != 0:
            self.place[i] = 0
        # handle bird and cage
        if self.object == CAGE and self.prop[BIRD] != 0:
            self.drop(BIRD, self.loc)
        if self.object == BIRD:
            self.prop[BIRD] = 0
        self.drop(self.object, self.loc)

    def vopen(self):
        """LOCK, UNLOCK, OPEN, CLOSE etc."""
        o = self.object
        if o in (CLAM, OYSTER):
            oyclam = 1 if o == OYSTER else 0
            if self.verb == LOCK:
                msg = 61
            elif not self.toting(TRIDENT):
                msg = 122 + oyclam
            elif self.toting(o):
                msg = 120 + oyclam
            else:
                msg = 124 + oyclam
                self.dstroy(CLAM)
                self.drop(OYSTER, self.loc)
                self.drop(PEARL, 105)
        elif o == DOOR:
            msg = 54 if self.prop[DOOR] == 1 else 111
        elif o == CAGE:
            msg = 32
        elif o == KEYS:
            msg = 55
        elif o == CHAIN:
            if not self.here(KEYS):
                msg = 31
            elif self.verb == LOCK:
                if self.prop[CHAIN] != 0:
                    msg = 34
                elif self.loc != 130:
                    msg = 173
                else:
                    self.prop[CHAIN] = 2
                    if self.toting(CHAIN):
                        self.drop(CHAIN, self.loc)
                    self.fixed[CHAIN] = -1
                    msg = 172
            else:
                if self.prop[BEAR] == 0:
                    msg = 41
                elif self.prop[CHAIN] == 0:
                    msg = 37
                else:
                    self.prop[CHAIN] = 0
                    self.fixed[CHAIN] = 0
                    if self.prop[BEAR] != 3:
                        self.prop[BEAR] = 2
                    self.fixed[BEAR] = 2 - self.prop[BEAR]
                    msg = 171
        elif o == GRATE:
            if not self.here(KEYS):
                msg = 31
            elif self.closing:
                if not self.panic:
                    self.clock2 = 15
                    self.panic += 1
                msg = 130
            else:
                msg = 34 + self.prop[GRATE]
                self.prop[GRATE] = 0 if self.verb == LOCK else 1
                msg += 2 * self.prop[GRATE]
        else:
            msg = 33
        self.rspeak(msg)

    def vsay(self):
        """SAY etc."""
        _, _, wval = self.analyze(self.word1)
        self.write("Okay.\n%s\n" % (self.word2 if wval == SAY else self.word1))

    def von(self):
        """ON etc."""
        if not self.here(LAMP):
            self.actspk(self.verb)
        elif self.limit < 0:
            self.rspeak(184)
        else:
            self.prop[LAMP] = 1
            self.rspeak(39)
            if self.wzdark:
                self.wzdark = 0
                self.describe()

    def voff(self):
        """OFF etc."""
        if not self.here(LAMP):
            self.actspk(self.verb)
        else:
            self.prop[LAMP] = 0
            self.rspeak(40)

    def vwave(self):
        """WAVE etc."""
        o = self.object
        if not self.toting(o) and (o != ROD or not self.toting(ROD2)):
            self.rspeak(29)
        elif o != ROD or not self.at(FISSURE) or not self.toting(o) or self.closing:
            self.actspk(self.verb)
        else:
            self.prop[FISSURE] = 1 - self.prop[FISSURE]
            self.pspeak(FISSURE, 2 - self.prop[FISSURE])

    def vkill(self):
        """ATTACK, KILL etc."""
        o = self.object
        if o == BIRD:
            if self.closed:
                msg = 137
            else:
                self.dstroy(BIRD)
                self.prop[BIRD] = 0
                if self.place[SNAKE] == 19:
                    self.tally2 += 1
                msg = 45
        elif o == 0:
            msg = 44
        elif o in (CLAM, OYSTER):
            msg = 150
        elif o == SNAKE:
            msg = 46
        elif o == DWARF:
            if self.closed:
                self.dwarfend()
            msg = 49
        elif o == TROLL:
            msg = 157
        elif o == BEAR:
            msg = 165 + (self.prop[BEAR] + 1) // 2
        elif o == DRAGON:
            if self.prop[DRAGON] != 0:
                msg = 167
            else:
                if not self.yes(49, 0, 0):
                    return
                self.pspeak(DRAGON, 1)
                self.prop[DRAGON] = 2
                self.prop[RUG] = 0
                self.move(DRAGON + MAXOBJ, -1)
                self.move(RUG + MAXOBJ, 0)
                self.move(DRAGON, 120)
                self.move(RUG, 120)
                for i in range(1, MAXOBJ):
                    if self.place[i] == 119 or self.place[i] == 121:
                        self.move(i, 120)
                self.newloc = 120
                return
        else:
            self.actspk(self.verb)
            return
        self.rspeak(msg)

    def vpour(self):
        """POUR"""
        if self.object == BOTTLE or self.object == 0:
            self.object = self.liq()
        if self.object == 0:
            self.needobj()
            return
        if not self.toting(self.object):
            self.actspk(self.verb)
            return
        if self.object != OIL and self.object != WATER:
            self.rspeak(78)
            return
        self.prop[BOTTLE] = 1
        self.place[self.object] = 0
        if self.at(PLANT):
            if self.object != WATER:
                self.rspeak(112)
            else:
                self.pspeak(PLANT, self.prop[PLANT] + 1)
                self.prop[PLANT] = (self.prop[PLANT] + 2) % 6
                self.prop[PLANT2] = self.prop[PLANT] // 2
                self.describe()
        elif self.at(DOOR):
            self.prop[DOOR] = 1 if self.object == OIL else 0
            self.rspeak(113 + self.prop[DOOR])
        else:
            self.rspeak(77)

    def veat(self):
        """EAT"""
        o = self.object
        if o == FOOD:
            self.dstroy(FOOD)
            msg = 72
        elif o in (BIRD, SNAKE, CLAM, OYSTER, DWARF, DRAGON, TROLL, BEAR):
            msg = 71
        else:
            self.actspk(self.verb)
            return
        self.rspeak(msg)

    def vdrink(self):
        """DRINK"""
        if self.object != WATER:
            self.rspeak(110)
        elif self.liq() != WATER or not self.here(BOTTLE):
            self.actspk(self.verb)
        else:
            self.prop[BOTTLE] = 1
            self.place[WATER] = 0
            self.rspeak(74)

    def vthrow(self):
        """THROW etc."""
        if self.toting(ROD2) and self.object == ROD and not self.toting(ROD):
            self.object = ROD2
        if not self.toting(self.object):
            self.actspk(self.verb)
            return
        # treasure to troll
        if self.at(TROLL) and 50 <= self.object < MAXOBJ:
            self.rspeak(159)
            self.drop(self.object, 0)
            self.move(TROLL, 0)
            self.move(TROLL + MAXOBJ, 0)
            self.drop(TROLL2, 117)
            self.drop(TROLL2 + MAXOBJ, 122)
            self.juggle(CHASM)
            return
        # feed the bears...
        if self.object == FOOD and self.here(BEAR):
            self.object = BEAR
            self.vfeed()
            return
        # if not axe, same as drop...
        if self.object != AXE:
            self.vdrop()
            return
        # AXE is THROWN
        i = self.dcheck()
        if i:                                               # at a dwarf...
            msg = 48
            if self.pct(33):
                self.dseen[i] = self.dloc[i] = 0
                msg = 47
                self.dkill += 1
                if self.dkill == 1:
                    msg = 149
        elif self.at(DRAGON) and self.prop[DRAGON] == 0:    # at a dragon...
            msg = 152
        elif self.at(TROLL):                                # at the troll...
            msg = 158
        elif self.here(BEAR) and self.prop[BEAR] == 0:      # at the bear...
            self.rspeak(164)
            self.drop(AXE, self.loc)
            self.fixed[AXE] = -1
            self.prop[AXE] = 1
            self.juggle(BEAR)
            return
        else:                                               # otherwise it is an attack
            self.verb = KILL
            self.object = 0
            self.itverb()
            return
        # handle the left over axe...
        self.rspeak(msg)
        self.drop(AXE, self.loc)
        self.describe()

    def vfind(self):
        """INVENTORY, FIND etc."""
        if self.toting(self.object):
            msg = 24
        elif self.closed:
            msg = 138
        elif self.dcheck() and self.dflag >= 2 and self.object == DWARF:
            msg = 94
        elif (self.at(self.object) or (self.liq() == self.object and self.here(BOTTLE))
              or self.object == self.liqloc(self.loc)):
            msg = 94
        else:
            self.actspk(self.verb)
            return
        self.rspeak(msg)

    def vfill(self):
        """FILL"""
        o = self.object
        if o == BOTTLE:
            if self.liq() != 0:
                msg = 105
            elif self.liqloc(self.loc) == 0:
                msg = 106
            else:
                self.prop[BOTTLE] = self.cond[self.loc] & WATOIL
                i = self.liq()
                if self.toting(BOTTLE):
                    self.place[i] = -1
                msg = 108 if i == OIL else 107
        elif o == VASE:
            if self.liqloc(self.loc) == 0:
                msg = 144
            elif not self.toting(VASE):
                msg = 29
            else:
                self.rspeak(145)
                self.vdrop()
                return
        else:
            msg = 29
        self.rspeak(msg)

    def vfeed(self):
        """FEED"""
        o = self.object
        if o == BIRD:
            msg = 100
        elif o == DWARF:
            if not self.here(FOOD):
                self.actspk(self.verb)
                return
            self.dflag += 1
            msg = 103
        elif o == BEAR:
            if not self.here(FOOD):
                if self.prop[BEAR] == 0:
                    msg = 102
                elif self.prop[BEAR] == 3:
                    msg = 110
                else:
                    self.actspk(self.verb)
                    return
            else:
                self.dstroy(FOOD)
                self.prop[BEAR] = 1
                self.fixed[AXE] = 0
                self.prop[AXE] = 0
                msg = 168
        elif o == DRAGON:
            msg = 110 if self.prop[DRAGON] != 0 else 102
        elif o == TROLL:
            msg = 182
        elif o == SNAKE:
            if self.closed or not self.here(BIRD):
                msg = 102
            else:
                msg = 101
                self.dstroy(BIRD)
                self.prop[BIRD] = 0
                self.tally2 += 1
        else:
            msg = 14
        self.rspeak(msg)

    def vread(self):
        """READ etc."""
        msg = 0
        if self.dark():
            self.write("I see no %s here.\n" % self.probj(self.object))
            return
        o = self.object
        if o == MAGAZINE:
            msg = 190
        elif o == TABLET:
            msg = 196
        elif o == MESSAGE:
            msg = 191
        elif o == OYSTER:
            if self.toting(OYSTER) and self.closed:
                self.yes(192, 193, 54)
                return
        if msg:
            self.rspeak(msg)
        else:
            self.actspk(self.verb)

    def vblast(self):
        """BLAST etc."""
        if self.prop[ROD2] < 0 or not self.closed:
            self.actspk(self.verb)
        else:
            self.bonus = 133
            if self.loc == 115:
                self.bonus = 134
            if self.here(ROD2):
                self.bonus = 135
            self.rspeak(self.bonus)
            self.normend()

    def vbreak(self):
        """BREAK etc."""
        if self.object == MIRROR:
            msg = 148
            if self.closed:
                self.rspeak(197)
                self.dwarfend()
        elif self.object == VASE and self.prop[VASE] == 0:
            msg = 198
            if self.toting(VASE):
                self.drop(VASE, self.loc)
            self.prop[VASE] = 2
            self.fixed[VASE] = -1
        else:
            self.actspk(self.verb)
            return
        self.rspeak(msg)

    def vwake(self):
        """WAKE etc."""
        if self.object != DWARF or not self.closed:
            self.actspk(self.verb)
        else:
            self.rspeak(199)
            self.dwarfend()

    def actspk(self, verb):
        """The default verb message."""
        if verb < 1 or verb > 31:
            self.bug(39)
        i = D.ACTMSG[verb]          # (a char in the C code: read as unsigned, as intended)
        if i:
            self.rspeak(i)

    def needobj(self):
        """No reasonable object for the verb was found."""
        _, wtype, _ = self.analyze(self.word1)
        self.write("%s what?\n" % (self.word1 if wtype == 2 else self.word2))

    # ------------------------------------------------------------ itverb.c
    def itverb(self):
        """Process an intransitive verb."""
        v = self.verb
        if v in (DROP, SAY, WAVE, CALM, RUB, THROW, FIND, FEED, BREAK, WAKE):
            self.needobj()
        elif v == TAKE:
            self.ivtake()
        elif v in (OPEN, LOCK):
            self.ivopen()
        elif v == NOTHING:
            self.rspeak(54)
        elif v in (ON, OFF, POUR):
            self.trverb()
        elif v == WALK:
            self.actspk(v)
        elif v == KILL:
            self.ivkill()
        elif v == EAT:
            self.iveat()
        elif v == DRINK:
            self.ivdrink()
        elif v == QUIT:
            self.ivquit()
        elif v == FILL:
            self.ivfill()
        elif v == BLAST:
            self.vblast()
        elif v == SCORE:
            self.score()
        elif v == FOO:
            self.ivfoo()
        elif v == SUSPEND:
            self.write("Your game is saved automatically after every command. "
                       "/quit leaves, and /adventure picks it up again.\n")
        elif v == INVENTORY:
            self.inventory()
        elif v == LOAD:
            self.write("Your game is saved automatically; /adventure picks it up again.\n")
        else:
            self.write("This intransitive not implemented yet\n")

    def ivtake(self):
        """CARRY, TAKE etc."""
        anobj = 0
        for item in range(1, MAXOBJ):
            if self.place[item] == self.loc:
                if anobj != 0:
                    self.needobj()
                    return
                anobj = item
        if anobj == 0 or (self.dcheck() and self.dflag >= 2):
            self.needobj()
            return
        self.object = anobj
        self.vtake()

    def ivopen(self):
        """OPEN, LOCK, UNLOCK"""
        if self.here(CLAM):
            self.object = CLAM
        if self.here(OYSTER):
            self.object = OYSTER
        if self.at(DOOR):
            self.object = DOOR
        if self.at(GRATE):
            self.object = GRATE
        if self.here(CHAIN):
            if self.object != 0:
                self.needobj()
                return
            self.object = CHAIN
        if self.object == 0:
            self.rspeak(28)
            return
        self.vopen()

    def ivkill(self):
        """ATTACK, KILL etc"""
        self.object1 = 0
        if self.dcheck() and self.dflag >= 2:
            self.object = DWARF
        if self.here(SNAKE):
            self.addobj(SNAKE)
        if self.at(DRAGON) and self.prop[DRAGON] == 0:
            self.addobj(DRAGON)
        if self.at(TROLL):
            self.addobj(TROLL)
        if self.here(BEAR) and self.prop[BEAR] == 0:
            self.addobj(BEAR)
        if self.object1 != 0:
            self.needobj()
            return
        if self.object != 0:
            self.vkill()
            return
        if self.here(BIRD) and self.verb != THROW:
            self.object = BIRD
        if self.here(CLAM) or self.here(OYSTER):
            self.addobj(CLAM)
        if self.object1 != 0:
            self.needobj()
            return
        self.vkill()

    def iveat(self):
        """EAT"""
        if not self.here(FOOD):
            self.needobj()
        else:
            self.object = FOOD
            self.veat()

    def ivdrink(self):
        """DRINK"""
        if self.liqloc(self.loc) != WATER and (self.liq() != WATER or not self.here(BOTTLE)):
            self.needobj()
        else:
            self.object = WATER
            self.vdrink()

    def ivquit(self):
        """QUIT"""
        self.gaveup = self.yes(22, 54, 54)
        if self.gaveup:
            self.normend()

    def ivfill(self):
        """FILL"""
        if not self.here(BOTTLE):
            self.needobj()
        else:
            self.object = BOTTLE
            self.vfill()

    def ivfoo(self):
        """Handle fee fie foe foo..."""
        k = self.vocab(self.word1, 3000)
        msg = 42
        if self.foobar != 1 - k:
            if self.foobar != 0:
                msg = 151
            self.rspeak(msg)
            return
        self.foobar = k
        if k != 4:
            return
        self.foobar = 0
        if self.place[EGGS] == 92 or (self.toting(EGGS) and self.loc == 92):
            self.rspeak(msg)
            return
        if self.place[EGGS] == 0 and self.place[TROLL] == 0 and self.prop[TROLL] == 0:
            self.prop[TROLL] = 1
        if self.here(EGGS):
            k = 1
        elif self.loc == 92:
            k = 0
        else:
            k = 2
        self.move(EGGS, 92)
        self.pspeak(EGGS, k)

    def inventory(self):
        """INVENTORY"""
        msg = 98
        for i in range(1, 65):
            if i == BEAR or not self.toting(i):
                continue
            if msg:
                self.rspeak(99)
            msg = 0
            self.pspeak(i, -1)
        if self.toting(BEAR):
            msg = 141
        if msg:
            self.rspeak(msg)

    def addobj(self, obj):
        """Ensure uniqueness as objects are searched out for an intransitive verb."""
        if self.object1 != 0:
            return
        if self.object != 0:
            self.object1 = -1
            return
        self.object = obj

    # ------------------------------------------------------------ looking
    def room_text(self):
        """The current room as the player would see it on arrival, without changing
        anything in the game (used to show the room again)."""
        out = []
        if self.toting(BEAR):
            out.append(D.MESSAGES[140])
        if self.dark():
            out.append(D.MESSAGES[15])
        else:
            out.append(D.ROOM_LONG[self.loc - 1])
            for i in range(1, MAXOBJ):
                if self.at(i):
                    if i == STEPS and self.toting(NUGGET):
                        continue
                    if self.prop[i] < 0:
                        continue            # treasure not yet seen: arriving would reveal it
                    state = 1 if i == STEPS and self.loc == self.fixed[STEPS] else self.prop[i]
                    segs = (D.ITEM_TEXT[i - 1] or "").split("/")
                    if state + 2 <= len(segs) - 1:
                        out.append(segs[state + 2])
        return "".join(out)


class AdventureEngine:
    """The engine interface of zork/__init__.py for Colossal Cave."""

    def __init__(self, seed=None, saved=None, setup=None):
        self.finished = False
        self.error = None
        self.tb = ""
        self.text = ""
        self.last_text = ""
        self.snapshot = None
        self.closed = False
        self.inq = queue.Queue()
        self.idle = threading.Event()
        self.rng = LCG(random.randrange(1, 2 ** 31) if seed is None else seed)
        self.g = Game(self.rng, self._read_line, self._on_prompt)
        self.g.setup = setup
        resumed = saved is not None
        if resumed:
            self.g.load_state(saved)
            self.snapshot = saved
        self.thread = threading.Thread(target=self._run, args=(resumed,), daemon=True,
                                       name="adventure")
        self.thread.start()
        self._wait()
        self.text = self.g.take()

    # -- the game thread
    def _read_line(self):
        self.idle.set()
        line = self.inq.get()
        self.idle.clear()
        if line is None or self.closed:
            raise GameExit()
        return line

    def _on_prompt(self):
        self.snapshot = self.g.to_state()

    def _run(self, resumed):
        try:
            if resumed:
                self.g.run(resumed=True)
            else:
                self.g.start()
        except GameExit:
            pass
        except BaseException as e:      # noqa: BLE001 - reported to the host
            self.error = e
            self.tb = traceback.format_exc()
        finally:
            self.finished = self.g.finished or self.finished
            self.idle.set()

    def _wait(self):
        if not self.idle.wait(TIMEOUT):
            raise TimeoutError("Adventure did not respond")
        if self.error is not None:
            self.last_text = self.g.take()
            raise RuntimeError("Adventure engine error: %r: %s" % (self.error, self.tb))

    # -- the engine interface (see zork/__init__.py)
    @classmethod
    def new(cls, seed=None, setup=None):
        return cls(seed=seed, setup=setup)

    @classmethod
    def from_dict(cls, d):
        if d.get("v") != SAVE_VERSION:
            raise ValueError("unsupported save version")
        return cls(saved=d["state"])

    def to_dict(self):
        return {"v": SAVE_VERSION, "state": self.snapshot}

    def intro(self):
        return self.text

    def look(self):
        return self.g.room_text()

    def command(self, line):
        if self.finished or not self.thread.is_alive():
            raise RuntimeError("the game is over")
        self.g.take()
        self.idle.clear()
        self.inq.put(line)
        self._wait()
        return self.g.take()

    def close(self):
        self.closed = True
        self.inq.put(None)
