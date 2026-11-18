"""Game engines for the /zork command.

An engine is a class with this interface (zork_session.py relies on nothing else):

    Engine.new()            -> a fresh game
    Engine.from_dict(d)     -> a game restored from to_dict() (raise on bad data)
    engine.to_dict()        -> JSON-safe dict of the whole game state
    engine.intro()          -> opening text for a new game (including the first room)
    engine.look()           -> description of the current room
    engine.command(line)    -> the game's reply to one typed command
    engine.finished         -> True once the game is over (won, or dead for good)
    engine.close()          -> (optional) release resources; called when a session ends

default_engine() is the port of Dungeon in dungeon.py (see NOTICE for its origin
and licence). stub.py is a four-room stand-in used by the tests of the session layer.
"""


def default_engine():
    from .dungeon import DungeonEngine
    return DungeonEngine
