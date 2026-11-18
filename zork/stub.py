"""A tiny stand-in game (four rooms) used until the full Dungeon port lands.

It exists so the mesh layer (sessions, saving, message splitting) can be built
and tested end to end. It is not part of Zork.
"""

ROOMS = {
    "west": ("West of House",
             "You are standing in an open field west of a white house, with a "
             "boarded front door. There is a small mailbox here.",
             {"n": "north", "s": "south"}),
    "north": ("North of House",
              "You are facing the north side of a white house. There is no door "
              "here, and all the windows are boarded up.",
              {"w": "west", "e": "behind"}),
    "south": ("South of House",
              "You are facing the south side of a white house. There is no door "
              "here, and all the windows are boarded.",
              {"w": "west", "e": "behind"}),
    "behind": ("Behind House",
               "You are behind the white house. In one corner of the house there "
               "is a small window which is slightly ajar.",
               {"n": "north", "s": "south"}),
}
DIRS = {"north": "n", "south": "s", "east": "e", "west": "w"}


class StubEngine:
    def __init__(self, room="west", box_open=False, leaflet="box", moves=0,
                 finished=False):
        self.room, self.box_open, self.leaflet = room, box_open, leaflet
        self.moves, self.finished = moves, finished

    @classmethod
    def new(cls):
        return cls()

    @classmethod
    def from_dict(cls, d):
        if d["room"] not in ROOMS:
            raise ValueError("bad room")
        return cls(d["room"], bool(d["box_open"]), d["leaflet"], int(d["moves"]),
                   bool(d["finished"]))

    def to_dict(self):
        return {"room": self.room, "box_open": self.box_open,
                "leaflet": self.leaflet, "moves": self.moves,
                "finished": self.finished}

    def intro(self):
        return self.look()

    def look(self):
        title, text, _ = ROOMS[self.room]
        return title + "\n" + text

    def command(self, line):
        words = line.lower().split()
        if not words:
            return "I beg your pardon?"
        self.moves += 1
        verb, rest = words[0], " ".join(words[1:])
        if verb in ("look", "l"):
            return self.look()
        verb = DIRS.get(verb, verb)
        if len(verb) == 1 and verb in "nsew":
            dest = ROOMS[self.room][2].get(verb)
            if dest is None:
                return "You can't go that way."
            self.room = dest
            return self.look()
        if verb == "open" and rest == "mailbox" and self.room == "west":
            self.box_open = True
            return "Opening the small mailbox reveals a leaflet."
        if verb == "take" and rest == "leaflet":
            if self.box_open and self.leaflet == "box" and self.room == "west":
                self.leaflet = "you"
                return "Taken."
            return "You can't see any leaflet here."
        if verb == "read" and rest == "leaflet" and self.leaflet == "you":
            return "WELCOME TO ZORK! (stand-in game)"
        if verb in ("i", "inventory"):
            return "You are carrying a leaflet." if self.leaflet == "you" \
                else "You are empty-handed."
        if verb == "die":
            self.finished = True
            return "You have died."
        return "I don't know how to do that."
