"""Test helpers shared by tools/crosscheck_adventure.py and the engine tests.

A scenario is a starting state set after the game is created, written as
"name=value;name[index]=value;..." (the same text is read by the C program's test
hook in the cross-check), so the dwarves, the pirate or the closing of the cave can
be reached without playing for hours.
"""

import re


def parse_scenario(text):
    """"loc=5;place[2]=-1" -> ({"loc": 5}, {("place", 2): -1})."""
    items, arrays = {}, {}
    for part in (text or "").split(";"):
        if not part:
            continue
        m = re.fullmatch(r"([a-z0-9]+)\[(\d+)\]=(-?\d+)", part)
        if m:
            arrays[(m.group(1), int(m.group(2)))] = int(m.group(3))
            continue
        m = re.fullmatch(r"([a-z0-9]+)=(-?\d+)", part)
        if not m:
            raise ValueError("bad scenario item %r" % part)
        items[m.group(1)] = int(m.group(2))
    return items, arrays


def scenario_text(items, arrays):
    parts = ["%s=%d" % kv for kv in items.items()]
    parts += ["%s[%d]=%d" % (n, i, v) for (n, i), v in arrays.items()]
    return ";".join(parts)


def apply_scenario(game, items, arrays):
    for k, v in items.items():
        setattr(game, k, v)
    for (n, i), v in arrays.items():
        getattr(game, n)[i] = v


def setup_from_text(text):
    """A setup callback for AdventureEngine.new(setup=...) from scenario text."""
    items, arrays = parse_scenario(text)
    return lambda game: apply_scenario(game, items, arrays)
