"""Tests for trek_session.py and the /trek session hooks in mesh_bridge.py
(run: python -m unittest tests.test_trek_session -v)."""

import os
import random
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import trek
from tests.test_zork import import_bridge
from trek_session import TrekSessions


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.games = trek.TrekGames(os.path.join(self.tmp.name, "g.json"), random.Random(5))
        self.t = TrekSessions(self.games, clock=self.clock)

    def say(self, node, text):
        return "\n".join(self.t.handle(node, text))

    def test_start_play_quit_resume(self):
        out = self.say("!aaaa", "/trek")
        self.assertIn("Klingons", out)
        self.assertIn("/quit", out)
        self.assertTrue(self.t.in_session("!aaaa"))
        scan = self.say("!aaaa", "srs")
        self.assertIn(" 12345678", scan)
        self.assertIn("Sec", scan)
        before = self.games.games["!aaaa"].to_dict()
        self.say("!aaaa", "nav 1 0.3")
        self.assertNotEqual(self.games.games["!aaaa"].to_dict(), before)
        self.assertIn("saved", self.say("!aaaa", "/quit"))
        self.assertFalse(self.t.in_session("!aaaa"))
        again = self.say("!aaaa", "/trek")
        self.assertIn("Mission in progress", again)             # resumed, not restarted

    def test_leading_slash_is_ignored(self):
        self.say("!aaaa", "/trek")
        self.assertEqual(self.say("!aaaa", "/srs"), self.say("!aaaa", "srs"))

    def test_free_text_commands_reach_the_game(self):
        self.say("!aaaa", "/trek")
        g = self.games.games["!aaaa"]
        e = g.energy
        self.say("!aaaa", "shi 100")
        self.assertEqual(g.shields, 100)
        self.assertEqual(g.energy, e - 100)

    def test_unknown_command_is_explained(self):
        self.say("!aaaa", "/trek")
        out = self.say("!aaaa", "xyzzy")
        self.assertIn("Unknown Trek command", out)
        self.assertIn("srs", out)
        self.assertTrue(self.t.in_session("!aaaa"))

    def test_help_inside_the_session(self):
        self.say("!aaaa", "/trek")
        self.assertIn("Navigate", self.say("!aaaa", "help nav"))
        self.assertIn("destroy every Klingon", self.say("!aaaa", "help"))
        self.assertTrue(self.t.in_session("!aaaa"))

    def test_trek_help_does_not_start_a_session(self):
        out = self.say("!aaaa", "/trek help")
        self.assertIn("destroy every Klingon", out)
        self.assertFalse(self.t.in_session("!aaaa"))

    def test_new_restarts_inside_and_outside(self):
        self.say("!aaaa", "/trek")
        self.say("!aaaa", "nav 1 0.3")
        out = self.say("!aaaa", "new")
        self.assertIn("Destroy", out)
        self.assertTrue(self.t.in_session("!aaaa"))
        self.say("!aaaa", "/quit")
        self.assertIn("Destroy", self.say("!aaaa", "/trek new"))

    def test_nodes_are_independent(self):
        self.say("!aaaa", "/trek")
        self.say("!bbbb", "/trek")
        self.say("!aaaa", "nav 1 0.3")
        self.say("!bbbb", "/quit")
        self.assertTrue(self.t.in_session("!aaaa"))
        self.assertFalse(self.t.in_session("!bbbb"))
        self.assertNotEqual(self.games.games["!aaaa"].to_dict(),
                            self.games.games["!bbbb"].to_dict())

    def test_game_over_ends_the_session(self):
        self.say("!aaaa", "/trek")
        self.games.games["!aaaa"].over = "lost"
        self.say("!aaaa", "srs")
        self.assertFalse(self.t.in_session("!aaaa"))
        self.assertIn("Destroy", self.say("!aaaa", "/trek"))    # a fresh game starts

    def test_idle_timeout_then_notice(self):
        self.say("!aaaa", "/trek")
        self.clock.t += 29 * 60
        self.assertEqual(self.t.reap(), [])
        self.clock.t += 2 * 60
        self.assertEqual(self.t.reap(), ["!aaaa"])
        self.assertFalse(self.t.in_session("!aaaa"))
        note = " ".join(self.t.take_notice("!aaaa"))
        self.assertIn("/trek resumes", note)
        self.assertEqual(self.t.take_notice("!aaaa"), [])
        self.assertIn("Mission in progress", self.say("!aaaa", "/trek"))

    def test_activity_resets_idle_timer(self):
        self.say("!aaaa", "/trek")
        for _ in range(4):
            self.clock.t += 20 * 60
            self.say("!aaaa", "srs")
            self.assertEqual(self.t.reap(), [])

    def test_blocked_by_zork(self):
        t = TrekSessions(self.games, blocked=lambda node: "You're in Zork.")
        self.assertEqual(t.handle("!aaaa", "/trek"), ["You're in Zork."])
        self.assertFalse(t.in_session("!aaaa"))

    def test_zork_while_in_trek(self):
        self.say("!aaaa", "/trek")
        self.assertIn("/quit", self.say("!aaaa", "/zork"))
        self.assertTrue(self.t.in_session("!aaaa"))

    def test_engine_error_closes_the_session(self):
        self.say("!aaaa", "/trek")
        with mock.patch.object(self.games, "command", side_effect=RuntimeError("boom")):
            with self.assertLogs("trek", "ERROR"):
                out = self.say("!aaaa", "srs")
        self.assertIn("error", out)
        self.assertFalse(self.t.in_session("!aaaa"))

    def test_messages_fit_the_radio_limit(self):
        self.say("!aaaa", "/trek")
        for cmd in ["srs", "lrs", "cmp g", "cmp s", "help", "dam", "xyzzy"]:
            for m in self.t.handle("!aaaa", cmd):
                self.assertLessEqual(len(m.encode("utf-8")), trek.MSG_LIMIT, (cmd, m))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.mb = import_bridge()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(self.mb, "TREK", trek.TrekGames(
            os.path.join(self.tmp.name, "trek.json"), random.Random(3)))   # never the real file
        patch.start()
        self.addCleanup(patch.stop)
        cfg = types.SimpleNamespace(
            allow=None, admin=None, dm_only=False, reply_gap=0, session_idle_min=30,
            zork_max_chunks=6, zork_save_dir=os.path.join(self.tmp.name, "z"),
            lat=0, lon=0, near_km=2, announce=None, announce_channel=0)
        self.bridge = self.mb.Bridge(cfg)
        self.bridge.my_id = "!me"
        from zork.stub import StubEngine
        self.bridge.zork._engine = StubEngine
        self.sent = []
        self.bridge.send = lambda dest, text: self.sent.append((dest, text))
        self.bridge.sender_position = lambda node: None
        threading.Thread(target=self.bridge.worker, daemon=True).start()

    def msg(self, sender, text, to="!me", expect=1):
        before = len(self.sent)
        self.bridge.on_receive({"decoded": {"text": text}, "fromId": sender, "toId": to}, None)
        deadline = time.time() + 5
        while len(self.sent) < before + expect and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        return " ".join(t for _, t in self.sent[before:])

    def test_trek_session_end_to_end(self):
        self.assertIn("Klingons", self.msg("!aaaa", "/trek", expect=3))
        self.assertTrue(self.bridge.trek.in_session("!aaaa"))
        self.assertIn("Sec", self.msg("!aaaa", "srs", expect=1))
        out = self.msg("!aaaa", "/weather")                   # typed into the game, not run
        self.assertIn("Unknown Trek command", out)
        self.assertIn("saved", self.msg("!aaaa", "/quit"))
        self.assertTrue(self.msg("!aaaa", "/ping").startswith("pong"))      # normal again

    def test_old_slash_commands_are_gone_outside_a_session(self):
        self.assertIn("Unknown /srs", self.msg("!aaaa", "/srs"))
        self.assertIn("Unknown /nav", self.msg("!aaaa", "/nav 3 1"))

    def test_trek_blocks_zork_and_back(self):
        self.msg("!aaaa", "/trek", expect=3)
        self.assertIn("You're in Trek", self.msg("!aaaa", "/zork"))
        self.assertFalse(self.bridge.zork.in_session("!aaaa"))
        self.msg("!aaaa", "/quit")
        self.msg("!aaaa", "/zork")
        self.assertTrue(self.bridge.zork.in_session("!aaaa"))
        self.assertIn("/quit", self.msg("!aaaa", "/trek"))                   # typed into Zork
        self.assertFalse(self.bridge.trek.in_session("!aaaa"))

    def test_public_messages_are_not_game_input(self):
        self.msg("!aaaa", "/trek", expect=3)
        sent = len(self.sent)
        self.bridge.on_receive({"decoded": {"text": "srs"}, "fromId": "!aaaa",
                                "toId": "^all"}, None)
        time.sleep(0.2)
        self.assertEqual(len(self.sent), sent)

    def test_idle_notice_arrives_with_next_message(self):
        self.msg("!aaaa", "/trek", expect=3)
        self.bridge.trek.idle_s = -1
        self.bridge.trek.reap()
        out = self.msg("!aaaa", "/ping", expect=2)
        self.assertIn("closed", out)
        self.assertIn("/trek resumes", out)
        self.assertIn("pong", out)
        self.assertNotIn("closed", self.msg("!aaaa", "/ping"))

    def test_two_nodes_two_games(self):
        self.msg("!aaaa", "/trek", expect=3)
        self.msg("!bbbb", "/zork")
        self.assertTrue(self.bridge.trek.in_session("!aaaa"))
        self.assertTrue(self.bridge.zork.in_session("!bbbb"))
        self.assertIn("North of House", self.msg("!bbbb", "n"))
        self.assertIn("Unknown Trek command", self.msg("!aaaa", "n"))


if __name__ == "__main__":
    unittest.main()
