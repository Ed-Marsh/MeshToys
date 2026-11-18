"""Game engines for the /zork command.

An engine is a class with this interface (zork_session.py relies on nothing else):

    Engine.new()            -> a fresh game
    Engine.from_dict(d)     -> a game restored from to_dict() (raise on bad data)
    engine.to_dict()        -> JSON-safe dict of the whole game state
    engine.intro()          -> opening text for a new game (including the first room)
    engine.look()           -> description of the current room
    engine.command(line)    -> the game's reply to one typed command
    engine.finished         -> True once the game is over (won, or dead for good)

Engine is currently the small stand-in in stub.py. The port of the original
MIT "Dungeon" (the mainframe Zork) will replace it behind this same interface.
"""

from .stub import StubEngine as Engine

__all__ = ["Engine"]
