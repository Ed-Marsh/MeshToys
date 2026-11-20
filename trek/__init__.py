"""Super Star Trek for the /trek command.

The game itself is in engine.py; the per-node sessions are in ../trek_session.py.
Everything the rest of MeshToys uses is re-exported here, so `import trek` and
`trek.TrekGames` work as before.
"""

from .engine import *  # noqa: F401,F403
from .engine import DISPLAY, MSG_LIMIT, Game, TrekGames, course_vector, direction, pack  # noqa: F401
