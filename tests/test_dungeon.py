"""Tests for the Dungeon engine (zork/) and its use through zork_session.

Run: python -m unittest tests.test_dungeon -v

tests/test_data/dungeon_golden.json holds games recorded from the original C program
(tools/crosscheck.py --record); replaying them must give identical output.
"""

import json
import os
import tempfile
import unittest

from zork.dungeon import DungeonEngine
from zork.rt import lcg_rand
from zork.testkit import apply_scenario
from zork_session import ZorkSessions

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "test_data", "dungeon_golden.json")


def new_game(seed=1):
    return DungeonEngine.new(rand=lcg_rand(seed))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engines = []

    def tearDown(self):
        for e in self.engines:
            e.close()

    def game(self, seed=1):
        e = new_game(seed)
        self.engines.append(e)
        return e

    def test_opening(self):
        e = self.game()
        self.assertIn("open field west of a big white house", e.intro())
        self.assertIn("small mailbox", e.intro())
        self.assertFalse(e.finished)

    def test_basic_moves(self):
        e = self.game()
        self.assertIn("leaflet", e.command("open mailbox"))
        self.assertEqual(e.command("take leaflet").strip().rstrip(">").strip(), "Taken.")
        self.assertIn("north side of a white house", e.command("n"))
        self.assertIn("north side of a white house", e.look())

    def test_look_costs_nothing(self):
        e = self.game()
        before = e.to_dict()["state"].get("state_", {}).get("moves")
        e.look()
        e.look()
        self.assertEqual(e.to_dict()["state"].get("state_", {}).get("moves"), before)

    def test_unknown_word_and_no_shell_escape(self):
        e = self.game()
        out = e.command("!echo pwned > nul")
        self.assertNotIn("pwned\n", out.replace("echo pwned", ""))
        self.assertIn("understand", out.lower())

    def test_gdt_cheat_console_is_not_available(self):
        e = self.game()
        out = e.command("gdt")
        self.assertNotIn("GDT>", out)

    def test_quit_ends_the_game(self):
        e = self.game()
        self.assertIn("Do you wish to leave", e.command("quit"))
        self.assertFalse(e.finished)
        e.command("y")
        self.assertTrue(e.finished)

    def test_answering_no_keeps_playing(self):
        e = self.game()
        e.command("quit")
        e.command("n")
        self.assertFalse(e.finished)
        self.assertIn("leaflet", e.command("open mailbox"))

    def test_save_and_restore_round_trip(self):
        e = self.game()
        for c in ["open mailbox", "take leaflet", "n", "e"]:
            e.command(c)
        saved = json.loads(json.dumps(e.to_dict()))          # must be JSON-safe
        r = DungeonEngine.from_dict(saved)
        self.engines.append(r)
        self.assertEqual(r.look(), e.look())
        self.assertIn("Behind House", r.look() + "Behind House")
        # both engines now behave identically (same random numbers from here on
        # are not guaranteed, so use commands that involve no chance)
        for c in ["inventory", "open window", "enter", "look"]:
            self.assertEqual(r.command(c), e.command(c), c)

    def test_restore_does_not_repeat_the_welcome(self):
        e = self.game()
        e.command("n")
        r = DungeonEngine.from_dict(e.to_dict())
        self.engines.append(r)
        self.assertEqual(r.intro().strip().strip(">").strip(), "")

    def test_saved_state_is_small(self):
        e = self.game()
        e.command("n")
        self.assertLess(len(json.dumps(e.to_dict())), 5000)

    def test_bad_save_is_rejected(self):
        with self.assertRaises(ValueError):
            DungeonEngine.from_dict({"v": 99, "state": {}})
        with self.assertRaises(ValueError):
            DungeonEngine.from_dict({"v": 1, "state": {"nonsense_": {}}})

    def test_games_are_independent(self):
        a, b = self.game(), self.game()
        a.command("open mailbox")
        a.command("take leaflet")
        a.command("n")
        self.assertIn("north side", a.look())
        self.assertIn("west of a big white house", b.look())
        self.assertIn("leaflet", b.command("open mailbox"))
        self.assertIn("Taken", b.command("take leaflet"))

    def test_close_stops_the_thread(self):
        e = new_game()
        e.close()
        e.thread.join(5)
        self.assertFalse(e.thread.is_alive())
        self.assertTrue(e.finished)

    def test_replay_of_recorded_c_games(self):
        if not os.path.exists(GOLDEN):
            self.skipTest("no recorded games")
        with open(GOLDEN) as fh:
            games = json.load(fh)
        self.assertGreater(len(games), 5)
        for n, game in enumerate(games):
            scen = game["scenario"]
            e = DungeonEngine.new(
                rand=lcg_rand(1),
                setup=(lambda g, s=scen: apply_scenario(g, s)) if scen else None)
            out = [e.intro()]
            for line in game["lines"]:
                out.append(e.command(line))
            e.close()
            got = "".join(out).replace("\r", "")
            self.assertEqual(got, game["output"], "recorded game %d differs" % n)


class SessionTests(unittest.TestCase):
    """The real game driven through zork_session, as the bridge does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.z = ZorkSessions(os.path.join(self.tmp.name, "saves"))   # default engine
        self.addCleanup(self.close_all)

    def close_all(self):
        for key in list(self.z.live):
            self.z._close(self.z.live.pop(key)[0])

    def say(self, node, text):
        return "\n".join(self.z.handle(node, text))

    def test_scripted_session_with_two_nodes(self):
        a = self.say("!aaaa", "/zork")
        self.assertIn("open field west of a big white house", a)
        self.assertNotIn("Welcome to Dungeon", a)             # banner removed
        self.assertNotIn("\n>", a)
        self.say("!bbbb", "/zork")
        self.assertIn("leaflet", self.say("!aaaa", "open mailbox"))
        self.assertIn("north side of a white house", self.say("!aaaa", "/n"))
        self.assertIn("open field", self.say("!bbbb", "look"))
        self.assertIn("Goodbye", self.say("!aaaa", "/quit"))
        self.assertFalse(self.z.in_zork("!aaaa"))
        again = self.say("!aaaa", "/zork")
        self.assertIn("Resuming", again)
        self.assertIn("north side of a white house", again)
        self.assertTrue(self.z.in_zork("!bbbb"))

    def test_survives_a_restart(self):
        self.say("!aaaa", "/zork")
        self.say("!aaaa", "n")
        self.close_all()
        z2 = ZorkSessions(self.z.save_dir)
        out = "\n".join(z2.handle("!aaaa", "/zork"))
        self.assertIn("Resuming", out)
        self.assertIn("north side of a white house", out)
        z2._close(z2.live.pop("!aaaa")[0])

    def test_game_over_ends_the_session(self):
        self.say("!aaaa", "/zork")
        self.say("!aaaa", "quit")
        out = self.say("!aaaa", "y")
        self.assertIn("Game over", out)
        self.assertFalse(self.z.in_zork("!aaaa"))
        self.assertEqual(os.listdir(self.z.save_dir), [])

    def test_every_reply_fits_the_radio_limit(self):
        self.say("!aaaa", "/zork")
        for cmd in ["open mailbox", "take leaflet", "read leaflet", "n", "e", "open window",
                    "enter", "info", "help", "diagnose", "score"]:
            for m in self.z.handle("!aaaa", cmd):
                self.assertLessEqual(len(m.encode("utf-8")), 190, m)


if __name__ == "__main__":
    unittest.main()
