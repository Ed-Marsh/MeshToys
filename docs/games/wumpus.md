# Hunt the Wumpus (wumpus_session.py and wumpus/)

Send `/wumpus` by direct message to start a game, or resume your saved one. It works exactly like [Zork](zork.md): while you are in it, **every** direct message you send is a game command, including ones starting with `/`. Only these are special:

| Message | What it does |
|---|---|
| `/quit` (or `quit`) | Saves and leaves. Normal commands work again |
| `/wumpus` | Shows your cave again (does not use a turn) |
| `/wumpus new` | Abandons your game and starts a fresh one |

You are in a cave system of 20 caves, each joined to 3 others (the corners of a dodecahedron). A reply looks like `Cave 5. Tunnels: 1 4 6. You feel a draft. Arrows 5.` Caves next to a hazard warn you: `You smell a Wumpus.`, `You feel a draft.` (a bottomless pit), `Bats rustle nearby.`

| You type | What it does |
|---|---|
| `M 5`, `move 5`, `go to 5` | Move to cave 5, if a tunnel leads there |
| `S 3 7 12`, `shoot 3 7 12` | Shoot an arrow through up to 5 caves. Where the next cave is not joined to the arrow's current one it flies a random way. You cannot name the cave you named two steps ago (arrows are not that crooked) |
| `M` or `S` alone | The game asks for the cave(s) and your next message is the answer. `cancel` withdraws it |
| `5` | A single bare number is a move |
| `look`, `help` | Show the cave; explain the rules |

Upper or lower case, extra words like `to` and `cave` and a leading `/` are all fine. Anything else gets "Didn't understand" and the game carries on.

The rules, as in the original: two caves have bottomless pits (you lose), two have super bats (they drop you in a random cave, which may be dangerous too), and the Wumpus sleeps until you walk into its cave or shoot and miss, when it wakes and moves to a neighbouring cave 3 times in 4. If it ends up with you, or your arrow comes back round to you, you lose. Hit it with an arrow and you win. You have 5 arrows and lose when they run out. When a game ends the reply says so and `/wumpus` starts another. One difference from the original: it asked "same set-up?" for a replay; here a new game always has a new random layout.

Details: one game and one save file per node (`wumpus_saves/<node>.json`, ignored by git), saved after every command and holding the whole board, the arrows left and the state of the random number generator, so a resumed game is exact. Idle timeout, one-game-at-a-time, errors and direct-messages-only are as for Zork. Replies are normally one message of well under 190 bytes. The game is written as a state machine (the original waits for typed input in the middle of a turn; here each message advances the game exactly one step), in `wumpus/engine.py`; tests are in `test_wumpus.py` (`python -m unittest test_wumpus -v`).

## Where the game comes from

*Hunt the Wumpus* was written by **Gregory Yob** (1945-2005) in BASIC in 1973 and published in *Creative Computing* in 1975. The source listing was read from [RayKV423/original-hunt-the-wumpus](https://github.com/RayKV423/original-hunt-the-wumpus) (`wumpus1973basic`, which that repository's author dedicates to the public domain under the Unlicense). It was used as a **specification only**: the cave map, the hazards and how the Wumpus, bats, pits and arrows behave are the game's rules. The code and every message here were written fresh, and no BASIC code or text was copied. The messages are short, re-worded descriptions of the same events.
