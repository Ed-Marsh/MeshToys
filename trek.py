#!/usr/bin/env python3
"""Super Star Trek for the MeshToys bridge.

A Python rewrite of the classic "Super Star Trek", shaped for Meshtastic text
messages: every reply is split into messages of at most MSG_LIMIT bytes, the
sector map uses one character per sector, and each node that talks to the
bridge gets its own saved game.

Nothing in here talks to the radio, so the game can be tested on its own
(see test_trek.py).

Coordinates are always row,column. In a quadrant "sector 3,5" is row 3,
column 5. Course 1 is right, 3 is up, 5 is left and 7 is down; fractions such
as 1.5 aim between two courses.
"""

import json
import logging
import math
import os
import random
import tempfile

log = logging.getLogger("trek")

MSG_LIMIT = 190      # bytes per message (Meshtastic text limit is ~233)
MAX_MSGS = 5         # most messages sent for one command
# How sectors are drawn on the phone. The Meshtastic app renders Markdown, so _ and * (and
# ` ~ and backslashes) must never appear in replies: they italicise text and swallow
# characters. The fonts are also proportional, so use characters of similar width
# ("." is far too narrow). Internally the grid still uses "." and "*".
DISPLAY = {".": "+", "*": "S"}
E0 = 3000            # starting / docked energy
P0 = 10              # torpedo capacity

# Course vectors, index 1..9: (row delta, column delta). 9 is the same as 1.
COURSE = [None, (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1),
          (1, -1), (1, 0), (1, 1), (0, 1)]

DEVICES = ["", "Warp Engines", "Short Range Sensors", "Long Range Sensors",
           "Phasers", "Photon Tubes", "Damage Control", "Shield Control",
           "Library-Computer"]
SHORT = ["", "Warp", "SRS", "LRS", "Phas", "Torp", "Dmg", "Shld", "Comp"]

# One-line descriptions shown by "/help name" (the bridge registers these).
INFO = {
    "trek": "Super Star Trek. /trek starts or resumes your game, /trek new restarts, /trek help explains",
    "srs": "Short range scan of your quadrant. E=you K=Klingon B=starbase S=star +=empty. Row,column",
    "lrs": "Long range scan of the 3x3 quadrants around you. Digits: Klingons, bases, stars",
    "nav": "Navigate: /nav course warp, e.g. /nav 3 1.5. Course 1-8.9 (1=right, 3=up), warp 0-8",
    "pha": "Fire phasers: /pha units, e.g. /pha 200. Closer Klingons take more damage",
    "tor": "Fire a photon torpedo: /tor course, e.g. /tor 3",
    "shi": "Set shields: /shi units, taken from ship energy, e.g. /shi 500",
    "dam": "Damage report. When docked, /dam y pays for repairs with time",
    "cmp": "Computer: /cmp g galaxy, s status, t torpedo data, b base data, d r c r c course calc",
}


NAV_TOL = 0.05     # courses are shown to 3 decimals, so allow a little rounding slack


def snap(v, tol=1e-9):
    """Pull a position that is within tol of a whole number onto it, so float dust
    (2.9999999999999996) or a rounded course (7.333) can't land the ship one sector short."""
    r = round(v)
    return float(r) if abs(v - r) < tol else v


def num(text):
    """Parse a number from user text, or return None."""
    try:
        v = float(text)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def pack(lines, limit=MSG_LIMIT):
    """Greedily join lines with newlines into messages of at most limit bytes."""
    msgs, cur = [], ""
    for ln in lines:
        while len(ln.encode("utf-8")) > limit:      # absurdly long line
            ln = ln[:-1]
        cand = cur + "\n" + ln if cur else ln
        if len(cand.encode("utf-8")) <= limit:
            cur = cand
        else:
            if cur:
                msgs.append(cur)
            cur = ln
    if cur:
        msgs.append(cur)
    return msgs


def course_vector(course):
    """Row/column step for a course 1 <= course < 9 (interpolated)."""
    k = int(math.floor(course))
    f = course - k
    a, b = COURSE[k], COURSE[k + 1]
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


def direction(dr, dc):
    """Course (1 <= c < 9) that heads along the vector (dr, dc), or None.

    This is the exact inverse of course_vector, so steering by the course it
    returns really does arrive at the target."""
    m = max(abs(dr), abs(dc))
    if m == 0:
        return None
    ur, uc = dr / m, dc / m
    for k in range(1, 9):
        a, b = COURSE[k], COURSE[k + 1]
        d0, d1 = b[0] - a[0], b[1] - a[1]
        if d0 != 0:
            f = (ur - a[0]) / d0
            ok = abs(uc - a[1]) < 1e-9
        else:
            f = (uc - a[1]) / d1
            ok = abs(ur - a[0]) < 1e-9
        if ok and -1e-9 <= f <= 1 + 1e-9:
            c = k + f
            return 1.0 if c >= 9 - 1e-9 else c
    return None


class Game:
    FIELDS = ("q1", "q2", "s1", "s2", "grid", "kl", "base", "galaxy", "known",
              "energy", "shields", "torps", "t", "t0", "t9", "k9", "k7", "b9",
              "dmg", "docked", "over", "repair_cost")

    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.out = []     # finished messages
        self.buf = []     # event lines waiting to be packed

    # ------------------------------------------------------------ output
    def say(self, text):
        self.buf.append(text)

    def _flush(self):
        if self.buf:
            self.out.extend(pack(self.buf))
            self.buf = []

    def block(self, text):
        """Add text as a message of its own (never merged or split)."""
        self._flush()
        self.out.append(text)

    def take(self):
        self._flush()
        msgs, self.out = self.out, []
        if len(msgs) > MAX_MSGS:      # keep the last one: it is usually the map
            msgs = msgs[:MAX_MSGS - 1] + msgs[-1:]
        return msgs

    # ------------------------------------------------------- persistence
    def to_dict(self):
        d = {f: getattr(self, f) for f in self.FIELDS}
        d["grid"] = ["".join(row) for row in self.grid]
        return json.loads(json.dumps(d))

    @classmethod
    def from_dict(cls, d, rng=None):
        g = cls(rng)
        for f in cls.FIELDS:
            setattr(g, f, d[f])
        g.grid = [list(row) for row in d["grid"]]
        return g

    # ------------------------------------------------------------- setup
    @classmethod
    def new(cls, rng=None):
        g = cls(rng)
        g._setup()
        return g

    def _setup(self):
        r = self.rng
        self.t = float(r.randint(20, 39) * 100)
        self.t0 = self.t
        self.t9 = 25 + r.randint(0, 9)
        self.energy, self.shields, self.torps = E0, 0, P0
        self.dmg = [0.0] * 9
        self.docked = False
        self.over = None
        self.repair_cost = None
        self.known = [[0] * 8 for _ in range(8)]
        self.galaxy = [[0] * 8 for _ in range(8)]
        k9 = b9 = 0
        for i in range(8):
            for j in range(8):
                x = r.random()
                k = 3 if x > 0.98 else 2 if x > 0.95 else 1 if x > 0.80 else 0
                b = 1 if r.random() > 0.96 else 0
                k9 += k
                b9 += b
                self.galaxy[i][j] = k * 100 + b * 10 + r.randint(1, 8)
        if k9 == 0:                       # vanishingly rare, but keep it playable
            i, j = r.randrange(8), r.randrange(8)
            self.galaxy[i][j] += 100
            k9 = 1
        if b9 == 0:
            i, j = r.randrange(8), r.randrange(8)
            self.galaxy[i][j] += 10
            b9 = 1
        self.k9, self.k7, self.b9 = k9, k9, b9
        if self.k9 > self.t9:
            self.t9 = self.k9 + 1
        self.q1, self.q2 = r.randint(1, 8), r.randint(1, 8)
        self.s1, self.s2 = float(r.randint(1, 8)), float(r.randint(1, 8))
        self.kl, self.base, self.grid = [], None, [["."] * 8 for _ in range(8)]
        self._enter_quadrant()

    def _enter_quadrant(self):
        v = self.galaxy[self.q1 - 1][self.q2 - 1]
        k, b, stars = v // 100, (v // 10) % 10, v % 10
        self.known[self.q1 - 1][self.q2 - 1] = v
        self.grid = [["."] * 8 for _ in range(8)]
        self.kl, self.base = [], None
        self.grid[int(self.s1) - 1][int(self.s2) - 1] = "E"
        for _ in range(k):
            r, c = self._empty_cell()
            self.grid[r][c] = "K"
            self.kl.append([r + 1, c + 1, 200.0 * (0.5 + self.rng.random())])
        if b:
            r, c = self._empty_cell()
            self.grid[r][c] = "B"
            self.base = [r + 1, c + 1]
        for _ in range(stars):
            r, c = self._empty_cell()
            self.grid[r][c] = "*"

    def _empty_cell(self):
        cells = [(r, c) for r in range(8) for c in range(8) if self.grid[r][c] == "."]
        return self.rng.choice(cells)

    def _update_galaxy(self):
        v = self.galaxy[self.q1 - 1][self.q2 - 1]
        stars = v % 10
        v = len(self.kl) * 100 + (1 if self.base else 0) * 10 + stars
        self.galaxy[self.q1 - 1][self.q2 - 1] = v
        self.known[self.q1 - 1][self.q2 - 1] = v

    # ------------------------------------------------------------ helpers
    def _cell(self):
        return int(self.s1), int(self.s2)

    def _dist(self, k):
        r, c = self._cell()
        return math.hypot(k[0] - r, k[1] - c)

    def _condition(self):
        if self.docked:
            return "DOCKED"
        if self.kl:
            return "RED"
        if self.energy < E0 * 0.1:
            return "YELLOW"
        return "GREEN"

    def _days_left(self):
        return self.t0 + self.t9 - self.t

    def _check_docked(self):
        self.docked = False
        if self.base:
            r, c = self._cell()
            if abs(self.base[0] - r) <= 1 and abs(self.base[1] - c) <= 1:
                self.docked = True
                self.energy, self.torps, self.shields = E0, P0, 0

    def _srs_text(self):
        rows = [" 12345678"]
        for i in range(8):
            rows.append(str(i + 1) + "".join(DISPLAY.get(ch, ch) for ch in self.grid[i]))
        r, c = self._cell()
        rows.append("SD%.1f %s %.1fd" % (self.t, self._condition(), self._days_left()))
        rows.append("Q%d,%d Sec%d,%d" % (self.q1, self.q2, r, c))
        rows.append("En%d Sh%d Tp%d K%d B%d" % (self.energy + self.shields,
                                                 self.shields, self.torps,
                                                 self.k9, self.b9))
        return "\n".join(rows)

    def _srs(self):
        self._check_docked()
        if self.dmg[2] < 0:
            self.say("Short Range Sensors are OUT.")
        else:
            self.block(self._srs_text())

    # ------------------------------------------------------- end of game
    def _lose(self, why):
        if self.over:
            return
        self.over = "lost"
        self.say(why)
        self.say("%d Klingons remained. Game over. /trek for a new game." % self.k9)

    def _won(self):
        self.over = "won"
        eff = 1000.0 * (self.k7 / max(self.t - self.t0, 0.1)) ** 2
        self.say("Congratulations, Captain! The last Klingon is destroyed. "
                 "Efficiency rating %d. /trek for a new game." % eff)

    def _check_time(self):
        if not self.over and self.t > self.t0 + self.t9:
            self._lose("You ran out of time. It is stardate %.1f." % self.t)

    def _check_stranded(self):
        if self.over:
            return
        if self.shields + self.energy > 10 and (self.energy > 10 or self.dmg[7] >= 0):
            return
        self.say("FATAL ERROR: you've stranded the ship. Not enough energy to "
                 "maneuver and shield control can't feed the engines.")
        self._lose("The Enterprise is lost.")

    # ------------------------------------------------------------ combat
    def _klingons_move(self):
        for k in self.kl:
            self.grid[k[0] - 1][k[1] - 1] = "."
            r, c = self._empty_cell()
            k[0], k[1] = r + 1, c + 1
            self.grid[r][c] = "K"
        self._klingons_shoot()

    def _klingons_shoot(self):
        if not self.kl:
            return
        if self.docked:
            self.say("Starbase shields protect the Enterprise.")
            return
        for k in list(self.kl):
            h = int(k[2] / self._dist(k) * (2 + self.rng.random()))
            self.shields -= h
            k[2] = k[2] / (3 + self.rng.random())
            self.say("%d hit from %d,%d" % (h, k[0], k[1]))
            if self.shields <= 0:
                self._lose("The Enterprise has been destroyed. The Federation will be conquered.")
                return
            self.say("Shields %d" % self.shields)
            # damage only for big hits: h >= 20 AND a 60% roll AND h/s > 2%
            if h >= 20 and self.rng.random() <= 0.6 and h / self.shields > 0.02:
                dev = self.rng.randint(1, 8)
                self.dmg[dev] -= h / self.shields + 0.5 * self.rng.random()
                self.say("%s damaged by the hit" % DEVICES[dev])

    def _kill_klingon(self, k):
        self.kl.remove(k)
        self.grid[k[0] - 1][k[1] - 1] = "."
        self.k9 -= 1
        self._update_galaxy()
        self.say("KLINGON DESTROYED!")
        if self.k9 <= 0:
            self._won()

    def _destroy_base(self):
        r, c = self.base
        self.grid[r - 1][c - 1] = "."
        self.base = None
        self.b9 -= 1
        self.docked = False
        self._update_galaxy()
        self.say("STARBASE DESTROYED!")
        if self.b9 <= 0:
            self._lose("That does it, Captain! You are relieved of command and "
                       "sentenced to 99 stardates of hard labor on Cygnus 12.")
        else:
            self.say("Starfleet Command is reviewing your record for court martial.")

    # ------------------------------------------------------------- damage
    def _repair_damage(self, warp):
        d6 = warp / 10 if warp >= 1 else warp
        for i in range(1, 9):
            if self.dmg[i] < 0:
                self.dmg[i] += d6
                if -0.1 < self.dmg[i] < 0:
                    self.dmg[i] = -0.1
                elif self.dmg[i] >= 0:
                    self.say("%s repair completed" % DEVICES[i])
        if self.rng.random() <= 0.2:
            dev = self.rng.randint(1, 8)
            if self.rng.random() < 0.6:
                self.dmg[dev] -= self.rng.random() * 5 + 1
                self.say("%s damaged" % DEVICES[dev])
            else:
                self.dmg[dev] += self.rng.random() * 3 + 1
                self.say("%s state of repair improved" % DEVICES[dev])

    # --------------------------------------------------------- navigation
    def _maneuver_energy(self, n):
        self.energy -= n + 10
        if self.energy >= 0:
            return
        self.say("Shield Control supplies energy to complete the maneuver.")
        self.shields += self.energy
        self.energy = 0
        if self.shields <= 0:
            self.shields = 0

    def _complete_maneuver(self, warp, n):
        r, c = self._cell()
        self.grid[r - 1][c - 1] = "E"
        self._maneuver_energy(n)
        self.t += warp if warp < 1.0 else 1.0
        self._check_time()
        if not self.over:
            self._srs()

    def _leave_quadrant(self, sr, sc, n, x1, x2, prev):
        """The ship has run off the edge of its quadrant. Work out where it ends up.

        Returns True if it is now in another quadrant. If the galactic perimeter
        stops it inside the same quadrant it stays at `prev`, the last sector it
        reached (the old code dropped it on the clamped corner, even on top of a star)."""
        ar = snap((self.q1 - 1) * 8 + sr + n * x1, NAV_TOL)   # absolute galactic position
        ac = snap((self.q2 - 1) * 8 + sc + n * x2, NAV_TOL)
        nq1 = int(math.floor((ar - 1) / 8.0)) + 1
        nq2 = int(math.floor((ac - 1) / 8.0)) + 1
        edge = False
        if nq1 < 1:
            nq1, ar, edge = 1, 1.0, True
        elif nq1 > 8:
            nq1, ar, edge = 8, (nq1 - 1) * 8 + 8.0, True
        if nq2 < 1:
            nq2, ac, edge = 1, 1.0, True
        elif nq2 > 8:
            nq2, ac, edge = 8, (nq2 - 1) * 8 + 8.0, True
        nq1 = max(1, min(8, nq1))
        nq2 = max(1, min(8, nq2))
        s1 = max(1, min(8, int(math.floor(ar - (nq1 - 1) * 8))))
        s2 = max(1, min(8, int(math.floor(ac - (nq2 - 1) * 8))))
        moved = (nq1, nq2) != (self.q1, self.q2)
        if moved:
            self.q1, self.q2, self.s1, self.s2 = nq1, nq2, float(s1), float(s2)
        else:
            self.s1, self.s2 = prev
            s1, s2 = int(prev[0]), int(prev[1])
        if edge:
            self.say("Starfleet: crossing the galactic perimeter is denied. "
                     "Engines shut down at sector %d,%d of quadrant %d,%d." % (s1, s2, nq1, nq2))
        return moved

    # ----------------------------------------------------------- commands
    def do_srs(self, args):
        self._srs()
        return self.take()

    def do_lrs(self, args):
        if self.dmg[3] < 0:
            self.say("Long Range Sensors are inoperable.")
            return self.take()
        lines = ["LRS Q%d,%d" % (self.q1, self.q2)]
        for i in range(self.q1 - 1, self.q1 + 2):
            cells = []
            for j in range(self.q2 - 1, self.q2 + 2):
                if 1 <= i <= 8 and 1 <= j <= 8:
                    v = self.galaxy[i - 1][j - 1]
                    self.known[i - 1][j - 1] = v
                    cells.append("%03d" % v)
                else:
                    cells.append("XXX")
            lines.append(" ".join(cells))
        self.block("\n".join(lines))
        return self.take()

    def do_nav(self, args):
        if len(args) < 2:
            self.say("Usage: /nav course warp, e.g. /nav 3 1.5")
            return self.take()
        course, warp = num(args[0]), num(args[1])
        if course is None or warp is None:
            self.say("Usage: /nav course warp, e.g. /nav 3 1.5")
            return self.take()
        if course == 9:
            course = 1.0
        if course < 1 or course >= 9:
            self.say("Lt. Sulu: Incorrect course data, sir! Use 1 to 8.9")
            return self.take()
        if warp <= 0 or warp > 8:
            self.say("Lt. Sulu: Incorrect warp factor, sir! Use 0.1 to 8")
            return self.take()
        if self.dmg[1] < 0 and warp > 0.2:
            self.say("Warp Engines are damaged. Maximum speed is warp 0.2.")
            return self.take()
        n = int(warp * 8 + 0.5)
        if self.energy - n < 0:
            self.say("Engineering: insufficient energy for warp %g." % warp)
            if self.shields >= n and self.dmg[7] >= 0:
                self.say("Deflector Control: %d units are in the shields." % self.shields)
            return self.take()

        self._klingons_move()
        if self.over:
            return self.take()
        self._repair_damage(warp)

        r, c = self._cell()
        self.grid[r - 1][c - 1] = "."
        x1, x2 = course_vector(course)
        sr, sc = self.s1, self.s2
        for _ in range(n):
            prev = (self.s1, self.s2)
            self.s1 = snap(self.s1 + x1, NAV_TOL)
            self.s2 = snap(self.s2 + x2, NAV_TOL)
            if not (1 <= self.s1 < 9 and 1 <= self.s2 < 9):
                if self._leave_quadrant(sr, sc, n, x1, x2, prev):
                    self.t += 1
                    self._maneuver_energy(n)
                    self._enter_quadrant()
                    self._check_time()
                    if not self.over:
                        if self.kl:
                            self.say("Combat area, condition RED.")
                            if self.shields <= 200:
                                self.say("Shields dangerously low.")
                        self._srs()
                else:
                    self._complete_maneuver(warp, n)
                return self.take()
            if self.grid[int(self.s1) - 1][int(self.s2) - 1] != ".":
                self.s1 = float(max(1, math.floor(snap(self.s1 - x1, NAV_TOL))))
                self.s2 = float(max(1, math.floor(snap(self.s2 - x2, NAV_TOL))))
                self.say("Warp Engines shut down at sector %d,%d due to bad navigation."
                         % (int(self.s1), int(self.s2)))
                break
        self._complete_maneuver(warp, n)
        return self.take()

    def do_pha(self, args):
        if self.dmg[4] < 0:
            self.say("Phasers inoperative.")
            return self.take()
        if not self.kl:
            self.say("Spock: sensors show no enemy ships in this quadrant.")
            return self.take()
        x = num(args[0]) if args else None
        if x is None:
            self.say("Phasers locked on target. Energy %d. /pha units" % self.energy)
            return self.take()
        x = int(x)
        if x <= 0:
            self.say("Phasers need at least 1 unit.")
            return self.take()
        if x > self.energy:
            self.say("Not enough energy. You have %d." % self.energy)
            return self.take()
        self.energy -= x
        if self.dmg[8] < 0:
            self.say("Computer failure hampers accuracy.")
            x = int(x * self.rng.random())
        h1 = x // len(self.kl)
        for k in list(self.kl):
            h = int(h1 / self._dist(k) * (self.rng.random() + 2))
            if h <= 0.15 * k[2]:
                self.say("No damage to the Klingon at %d,%d" % (k[0], k[1]))
                continue
            k[2] -= h
            self.say("%d hit on Klingon at %d,%d" % (h, k[0], k[1]))
            if k[2] <= 0:
                self._kill_klingon(k)
                if self.over:
                    return self.take()
        self._klingons_shoot()
        return self.take()

    def do_tor(self, args):
        if self.torps <= 0:
            self.say("All photon torpedoes expended.")
            return self.take()
        if self.dmg[5] < 0:
            self.say("Photon Tubes are not operational.")
            return self.take()
        course = num(args[0]) if args else None
        if course is None:
            self.say("Usage: /tor course, e.g. /tor 3")
            return self.take()
        if course == 9:
            course = 1.0
        if course < 1 or course >= 9:
            self.say("Ensign Chekov: Incorrect course data, sir! Use 1 to 8.9")
            return self.take()
        self.energy -= 2
        self.torps -= 1
        x1, x2 = course_vector(course)
        r, c = self._cell()
        x, y = r + 0.5, c + 0.5            # start in the middle of the ship's sector
        track = []
        hit = False
        while True:
            x = snap(x + x1, 1e-6)
            y = snap(y + x2, 1e-6)
            xi, yi = int(math.floor(x)), int(math.floor(y))
            if not (1 <= xi <= 8 and 1 <= yi <= 8):
                break
            track.append("%d,%d" % (xi, yi))
            what = self.grid[xi - 1][yi - 1]
            if what == ".":
                continue
            hit = True
            self.say("Torpedo track: " + " ".join(track))
            if what == "*":
                self.say("Star at %d,%d absorbed the torpedo." % (xi, yi))
            elif what == "K":
                for k in list(self.kl):
                    if (k[0], k[1]) == (xi, yi):
                        self._kill_klingon(k)
            elif what == "B":
                self._destroy_base()
            break
        if not hit:
            self.say("Torpedo track: " + " ".join(track))
            self.say("Torpedo missed.")
        if not self.over:
            self._klingons_shoot()
        return self.take()

    def do_shi(self, args):
        if self.dmg[7] < 0:
            self.say("Shield Control is inoperable.")
            return self.take()
        x = num(args[0]) if args else None
        if x is None:
            self.say("Energy available %d. Shields %d. /shi units"
                     % (self.energy + self.shields, self.shields))
            return self.take()
        x = int(x)
        if x < 0 or x == self.shields:
            self.say("<Shields unchanged>")
        elif x > self.energy + self.shields:
            self.say("Shield Control: 'This is not the Federation Treasury.' <Shields unchanged>")
        else:
            self.energy = self.energy + self.shields - x
            self.shields = x
            self.say("Shields now at %d units." % x)
        return self.take()

    def do_dam(self, args):
        if self.dmg[6] < 0:
            self.say("Damage Control report not available.")
        else:
            cells = ["%s %.1f" % (SHORT[i], self.dmg[i]) for i in range(1, 9)]
            self.say(" ".join(cells[:4]))
            self.say(" ".join(cells[4:]))
        broken = sum(1 for i in range(1, 9) if self.dmg[i] < 0)
        if self.docked and broken:
            if args and args[0].lower().startswith("y") and self.repair_cost is not None:
                for i in range(1, 9):
                    if self.dmg[i] < 0:
                        self.dmg[i] = 0.0
                self.t += self.repair_cost + 0.1
                self.say("Repairs completed. %.1f stardates elapsed." % (self.repair_cost + 0.1))
                self.repair_cost = None
                self._check_time()
            else:
                cost = 0.1 * broken + self.rng.random() / 50
                self.repair_cost = min(cost, 0.9)
                self.say("Repairs need %.1f stardates. /dam y to authorize."
                         % (self.repair_cost + 0.1))
        return self.take()

    def do_cmp(self, args):
        if self.dmg[8] < 0:
            self.say("Library-Computer is inoperable.")
            return self.take()
        sub = args[0].lower() if args else ""
        if sub == "g":
            for part, rows in ((1, range(0, 4)), (2, range(4, 8))):
                lines = ["Galaxy rows %d-%d, cols 1-8" % (rows[0] + 1, rows[-1] + 1)]
                for i in rows:
                    vals = ["???" if self.known[i][j] == 0 else "%03d" % self.known[i][j]
                            for j in range(8)]
                    lines.append("%d %s" % (i + 1, " ".join(vals)))
                self.block("\n".join(lines))
        elif sub == "s":
            self.say("Klingons left: %d" % self.k9)
            self.say("Days left: %.1f" % self._days_left())
            if self.b9 < 1:
                self.say("You have no starbases left!")
            else:
                self.say("Starbases: %d" % self.b9)
        elif sub == "t":
            if not self.kl:
                self.say("Spock: sensors show no enemy ships in this quadrant.")
            for k in self.kl:
                self.say(self._vector_text("Klingon", k[0], k[1]))
        elif sub == "b":
            if not self.base:
                self.say("Spock: sensors show no starbase in this quadrant.")
            else:
                self.say(self._vector_text("Base", self.base[0], self.base[1]))
        elif sub == "d":
            v = [num(a) for a in args[1:5]]
            if len(v) < 4 or None in v:
                self.say("Usage: /cmp d row col row col (from, to)")
            else:
                self.say(self._vector_text("Target", v[2], v[3], v[0], v[1]))
        else:
            self.say("/cmp g galaxy, s status, t torpedo data, b base data, "
                     "d row col row col = course calc")
        return self.take()

    def _vector_text(self, label, tr, tc, fr=None, fc=None):
        if fr is None:
            fr, fc = self._cell()
        dr, dc = tr - fr, tc - fc
        crs = direction(dr, dc)
        if crs is None:
            return "%s: same position" % label
        dist = max(abs(dr), abs(dc))
        return "%s %g,%g: course %.3f dist %.1f (warp %.2f)" % (label, tr, tc, crs, dist, dist / 8.0)

    # ------------------------------------------------------ start / resume
    def orders(self):
        self.say("Destroy %d Klingons in %d days (by stardate %d). %d starbases to resupply at. "
                 "/trek help explains." % (self.k9, self.t9, self.t0 + self.t9, self.b9))
        self._srs()
        return self.take()

    def resume(self):
        self.say("Mission in progress: %d Klingons, %.1f days left." % (self.k9, self._days_left()))
        self._srs()
        return self.take()


HELP = [
    "Super Star Trek: destroy every Klingon before time runs out.\n"
    "/srs /lrs /nav course warp /pha units /tor course /shi units /dam /cmp\n"
    "/help name explains each. Row,column.",
    "Courses: 1 right, 2 up-right, 3 up, 4 up-left, 5 left, 6 down-left, 7 down, 8 down-right.\n"
    "Map: E=you K=Klingon B=base S=star +=empty. Dock by sailing next to B. 1 warp = 8 sectors.",
]


class TrekGames:
    """One saved game per node id, kept in a JSON file."""

    def __init__(self, path, rng=None):
        self.path = path
        self.rng = rng
        self.games = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("Could not read %s (%s); starting with no saved games", self.path, e)
            return
        for node, d in raw.items():
            try:
                self.games[node] = Game.from_dict(d, self.rng)
            except (KeyError, TypeError, ValueError, IndexError) as e:
                log.warning("Skipping unreadable saved game for %s: %s", node, e)

    def _save(self):
        data = {node: g.to_dict() for node, g in self.games.items()}
        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            fd, tmp = tempfile.mkstemp(prefix=".trek", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("Could not save games: %s", e)

    def command(self, sender, verb, argstr):
        """Run a game command for a node; returns a list of messages."""
        args = (argstr or "").split()
        key = sender.lower()
        g = self.games.get(key)
        if verb == "trek":
            sub = args[0].lower() if args else ""
            if sub == "help":
                return list(HELP)
            if sub == "new" or g is None or g.over:
                g = Game.new(self.rng)
                self.games[key] = g
                msgs = g.orders()
            else:
                msgs = g.resume()
            self._save()
            return msgs
        if g is None:
            return ["No game yet. Send /trek to start one."]
        if g.over:
            return ["Game over (%s). Send /trek for a new game." % g.over]
        fn = getattr(g, "do_" + verb, None)
        if fn is None:
            return ["Unknown game command /%s" % verb]
        snapshot = g.to_dict()
        try:
            msgs = fn(args)
            g._check_stranded()
            msgs += g.take()
        except Exception:
            self.games[key] = Game.from_dict(snapshot, self.rng)   # undo a half-done move
            raise
        self._save()
        return msgs
