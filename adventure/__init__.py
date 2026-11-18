"""Colossal Cave Adventure for the /adventure command.

The engine follows the interface described in zork/__init__.py (new, from_dict,
to_dict, intro, look, command, finished, close); see engine.py. Its data is in
data.py, extracted from the original C source by tools/gen_adventure_data.py. See
NOTICE for the origin of the game and the licence position.
"""


def default_engine():
    from .engine import AdventureEngine
    return AdventureEngine
