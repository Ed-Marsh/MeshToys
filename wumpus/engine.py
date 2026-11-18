"""Hunt the Wumpus (Gregory Yob, 1973), written from its rules as a state machine.

The original program stops and waits for typed input in the middle of a turn. Here
the game is an explicit state (self.state) and command(line) advances it by exactly
one message, so nothing ever blocks:

    ACTION  waiting for "M <cave>" or "S <caves>"
    MOVE    "M" was sent alone: waiting for the cave to move to
    SHOOT   "S" was sent alone: waiting for the arrow's path
    (finished is True once the game is won or lost)

Rules (the facts of the game, not its text): 20 caves joined as the corners of a
dodecahedron, 3 tunnels each. One Wumpus, two bottomless pits, two bat colonies and
the player all start in six different caves. The player has 5 arrows. Moving into a
pit loses; into a bat cave means being dropped in a random cave (which may be
dangerous too); into the Wumpus's cave wakes it. An awake Wumpus moves to a
neighbouring cave 3 times in 4 and otherwise stays; if it ends up with the player,
the player is eaten. Shooting wakes the Wumpus unless the arrow hits it. An arrow
flies through up to 5 caves; where the next cave is not joined to the arrow's
current one it flies to a random neighbour. It can hit the Wumpus (win) or the
player (lose). Running out of arrows loses.

Randomness: every random choice is rng.randint(a, b), so a test can supply a stand-in
generator, and a real game is repeatable from a seed. The generator's state is saved
with the game, so a resumed game continues exactly where it left off.
"""

import random
import re

ROOMS = 20
ARROWS = 5
MAX_PATH = 5

# Tunnels of each cave (index 0 is cave 1). These are the edges of a dodecahedron.
TUNNELS = (
    (2, 5, 8), (1, 3, 10), (2, 4, 12), (3, 5, 14), (1, 4, 6),
    (5, 7, 15), (6, 8, 17), (1, 7, 9), (8, 10, 18), (2, 9, 11),
    (10, 12, 19), (3, 11, 13), (12, 14, 20), (4, 13, 15), (6, 14, 16),
    (15, 17, 20), (7, 16, 18), (9, 17, 19), (11, 18, 20), (13, 16, 19),
)

ACTION, MOVE, SHOOT = "action", "move", "shoot"
STATES = (ACTION, MOVE, SHOOT)

HELP = ("Find the Wumpus in 20 caves. M 5 moves to cave 5. S 3 7 12 shoots an arrow "
        "through up to 5 caves (it bends: a wrong cave sends it a random way). You have "
        "5 arrows. Pits and bats are in other caves; the Wumpus sleeps until you bump it "
        "or miss. /quit leaves.")

MOVE_WORDS = {"m", "move", "go", "walk", "goto", "run"}
SHOOT_WORDS = {"s", "shoot", "fire", "arrow", "aim"}
HELP_WORDS = {"help", "h", "?", "rules", "instructions"}
LOOK_WORDS = {"look", "l", "where", "cave", "status"}
CANCEL_WORDS = {"cancel", "back", "c", "no"}
FILLER = {"to", "room", "rooms", "cave", "caves", "through", "the", "at", "then"}
TOKEN_RE = re.compile(r"\d+|[a-z]+|\?")


class WumpusEngine:
    def __init__(self, you, wumpus, pits, bats, arrows=ARROWS, state=ACTION,
                 finished=False, rng=None):
        self.you, self.wumpus = you, wumpus
        self.pits, self.bats = list(pits), list(bats)
        self.arrows = arrows
        self.state = state
        self.finished = finished
        self.rng = rng or random.Random()
        self.check()

    # -- construction and saving
    @classmethod
    def new(cls, seed=None, layout=None, rng=None):
        """A fresh game. seed makes it repeatable; layout, if given, is a dict
        with you, wumpus, pits and bats (for tests); rng replaces the generator."""
        rng = rng or random.Random(seed)
        if layout is None:
            rooms = rng.sample(range(1, ROOMS + 1), 6)
            layout = {"you": rooms[0], "wumpus": rooms[1],
                      "pits": rooms[2:4], "bats": rooms[4:6]}
        if len({layout["you"], layout["wumpus"], *layout["pits"], *layout["bats"]}) != 6:
            raise ValueError("the six starting caves must differ")
        return cls(layout["you"], layout["wumpus"], layout["pits"], layout["bats"], rng=rng)

    def check(self):
        """Raise ValueError if the state is not a legal game."""
        places = [self.you, self.wumpus] + self.pits + self.bats
        if (len(self.pits) != 2 or len(self.bats) != 2
                or not all(type(p) is int and 1 <= p <= ROOMS for p in places)
                or len(set(self.pits + self.bats)) != 4):
            raise ValueError("bad wumpus layout")
        # The Wumpus is not bothered by hazards, so once it has moved it may share a
        # cave with a pit or bats. A live player is never in a pit, and is only with
        # the Wumpus when it has just eaten them.
        if not self.finished and (self.you == self.wumpus or self.you in self.pits):
            raise ValueError("bad wumpus layout")
        if (type(self.arrows) is not int or not 0 <= self.arrows <= ARROWS
                or self.state not in STATES):
            raise ValueError("bad wumpus state")

    @classmethod
    def from_dict(cls, d):
        rng = random.Random()
        ver, internal, gauss = d["rng"]
        rng.setstate((ver, tuple(internal), gauss))
        return cls(d["you"], d["wumpus"], d["pits"], d["bats"], d["arrows"], d["state"],
                   bool(d["finished"]), rng)

    def to_dict(self):
        ver, internal, gauss = self.rng.getstate()
        return {"you": self.you, "wumpus": self.wumpus, "pits": self.pits,
                "bats": self.bats, "arrows": self.arrows, "state": self.state,
                "finished": self.finished, "rng": [ver, list(internal), gauss]}

    def close(self):
        pass

    # -- what the player sees
    def intro(self):
        return self.look()

    def look(self):
        tun = TUNNELS[self.you - 1]
        parts = ["Cave %d. Tunnels: %s." % (self.you, " ".join(map(str, tun)))]
        near = set(tun)
        if self.wumpus in near:
            parts.append("You smell a Wumpus.")
        if near & set(self.pits):
            parts.append("You feel a draft.")
        if near & set(self.bats):
            parts.append("Bats rustle nearby.")
        parts.append("Arrows %d." % self.arrows)
        if self.state == MOVE:
            parts.append("Move to which cave?")
        elif self.state == SHOOT:
            parts.append("Shoot through which caves (1-5)?")
        return " ".join(parts)

    # -- one message in, one step of the game
    def command(self, line):
        if self.finished:
            raise RuntimeError("game is over")
        toks = TOKEN_RE.findall((line or "").lower())
        if not toks:
            return "Say M <cave> to move or S <caves> to shoot. help explains."
        word = None if toks[0].isdigit() else toks[0]
        rest = [t for t in (toks[1:] if word else toks) if t not in FILLER]
        if word in HELP_WORDS:
            return HELP
        if word in LOOK_WORDS and not rest:
            return self.look()
        if word in CANCEL_WORDS and not rest:
            self.state = ACTION
            return self.look()
        if word is None or word in MOVE_WORDS or word in SHOOT_WORDS:
            if all(t.isdigit() for t in rest):
                nums = [int(t) for t in rest]
                if word in SHOOT_WORDS:
                    return self._start_shoot(nums)
                if word in MOVE_WORDS:
                    return self._start_move(nums)
                # Bare numbers answer the open question; with none open, one number
                # is taken as a move.
                if self.state == SHOOT:
                    return self._start_shoot(nums)
                if self.state == MOVE or len(nums) == 1:
                    return self._start_move(nums)
        return self._huh()

    def _huh(self):
        return ("Didn't understand. M 5 moves, S 3 7 12 shoots, help explains. "
                + self.look())

    # -- moving
    def _start_move(self, nums):
        if not nums:
            self.state = MOVE
            return self.look()
        if len(nums) != 1:
            return "Move to one cave, e.g. M 5. " + self.look()
        dest = nums[0]
        if dest not in TUNNELS[self.you - 1]:
            if not 1 <= dest <= ROOMS:
                return "Caves are numbered 1-20. " + self.look()
            return "No tunnel to cave %d. " % dest + self.look()
        self.state = ACTION
        out = []
        self.you = dest
        self._arrive(out)
        if not self.finished:
            out.append(self.look())
        return " ".join(out)

    def _arrive(self, out):
        """Enter self.you's cave and deal with whatever is there. Bats can carry
        the player somewhere else with its own hazards, hence the loop."""
        while True:
            if self.you == self.wumpus:
                out.append("You bumped the Wumpus!")
                self._wumpus_moves(out)
                if self.finished:
                    return
            if self.you in self.pits:
                out.append("You fall into a bottomless pit. You lose.")
                self.finished = True
                return
            if self.you in self.bats:
                self.you = self.rng.randint(1, ROOMS)
                out.append("Super bats carry you off to cave %d!" % self.you)
                continue
            return

    def _wumpus_moves(self, out):
        """The woken Wumpus moves to a neighbour (3 in 4) or stays; eats you if
        it is then in your cave."""
        k = self.rng.randint(1, 4)
        if k < 4:
            self.wumpus = TUNNELS[self.wumpus - 1][k - 1]
        if self.wumpus == self.you:
            out.append("The Wumpus got you. You lose.")
            self.finished = True

    # -- shooting
    def _start_shoot(self, nums):
        if not nums:
            self.state = SHOOT
            return self.look()
        if len(nums) > MAX_PATH:
            return "An arrow flies through 1-5 caves, e.g. S 3 7 12. " + self.look()
        if any(not 1 <= n <= ROOMS for n in nums):
            return "Caves are numbered 1-20. " + self.look()
        for i in range(2, len(nums)):
            if nums[i] == nums[i - 2]:
                return "Arrows aren't that crooked. Pick other caves. " + self.look()
        self.state = ACTION
        out = []
        self._fly(nums, out)
        if not self.finished:
            out.append(self.look())
        return " ".join(out)

    def _fly(self, path, out):
        at = self.you
        for want in path:
            tun = TUNNELS[at - 1]
            at = want if want in tun else tun[self.rng.randint(1, 3) - 1]
            if at == self.wumpus:
                out.append("Your arrow hit the Wumpus. You win!")
                self.finished = True
                return
            if at == self.you:
                out.append("Your arrow came back and hit you. You lose.")
                self.finished = True
                return
        out.append("Missed.")
        self._wumpus_moves(out)             # the noise wakes it
        self.arrows -= 1
        if self.arrows <= 0 and not self.finished:
            out.append("Out of arrows. You lose.")
            self.finished = True
