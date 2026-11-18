"""Hunt the Wumpus for the /wumpus command.

The engine follows the interface described in zork/__init__.py (new, from_dict,
to_dict, intro, look, command, finished); see engine.py. The game is Gregory Yob's
1973 Hunt the Wumpus; the code here is a fresh implementation of its rules (see the
README).
"""


def default_engine():
    from .engine import WumpusEngine
    return WumpusEngine
