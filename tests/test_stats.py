"""Tests for stats.py and the /stats admin command (run: python -m unittest tests.test_stats -v)."""

import json
import os
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import trek
from stats import DAY, Stats
from tests.test_zork import blen, import_bridge


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "stats.json")
        self.clock = Clock()
        self.s = Stats(self.path, flush_s=900, clock=self.clock)

    def test_no_write_until_something_happens(self):
        self.clock.t += 5000
        self.s.maybe_flush()
        self.assertFalse(os.path.exists(self.path))

    def test_writes_only_after_interval_and_only_when_dirty(self):
        self.s.seen("!aaaa", "AAA")
        self.clock.t += 899
        self.s.maybe_flush()
        self.assertFalse(os.path.exists(self.path))      # too soon
        self.clock.t += 2
        self.s.maybe_flush()
        self.assertTrue(os.path.exists(self.path))
        mtime = os.path.getmtime(self.path)
        os.remove(self.path)
        self.clock.t += 5000
        self.s.maybe_flush()                             # nothing new: no write
        self.assertFalse(os.path.exists(self.path))
        self.assertGreater(mtime, 0)

    def test_final_flush_writes_regardless_of_interval(self):
        self.s.command("ping")
        self.s.flush()
        with open(self.path) as fh:
            self.assertEqual(json.load(fh)["commands"], {"ping": 1})

    def test_lifetime_totals_survive_restart(self):
        self.s.seen("!aaaa", "AAA")
        self.s.command("ping")
        self.s.flush()
        again = Stats(self.path, clock=self.clock)
        again.seen("!aaaa", "AAA")
        again.seen("!bbbb", "BBB")
        again.command("ping")
        self.assertEqual(again.data["commands"]["ping"], 2)
        self.assertEqual(len(again.data["nodes"]), 2)
        self.assertEqual(again.data["since"], self.s.data["since"])

    def test_corrupt_file_is_set_aside(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        s = Stats(self.path, clock=self.clock)
        self.assertEqual(s.data["nodes"], {})
        self.assertTrue(os.path.exists(self.path + ".bad"))

    def test_report_counts(self):
        s = self.s
        s.seen("!aaaa", "AAA")
        s.seen("!bbbb", "BBB")
        s.seen("!bbbb", "BBB")
        for c in ("ping", "ping", "zork"):
            s.command(c)
        s.game_message("zork")
        s.bump("unknown")
        s.bump("refused")
        s.bump("errors")
        self.clock.t += 2 * DAY                          # bbbb comes back, aaaa doesn't
        s.seen("!bbbb", "BBB")
        text = "\n".join(s.report({"zork"}))
        self.assertIn("Visitors: 2 total, 1 in 24h, 2 in 7d", text)
        self.assertIn("Commands: 3 (unknown 1, errors 1, refused 1)", text)
        self.assertIn("Top: /ping 2", text)
        self.assertIn("Games played: /zork 1", text)
        self.assertIn("Game moves: zork 1", text)
        self.assertIn("BBB(bbbb) 3", text)
        self.assertNotIn("Top: /ping 2 /zork", text)     # games listed apart

    def test_unknown_name_does_not_overwrite_known_name(self):
        self.s.seen("!aaaa", "AAA")
        self.s.seen("!aaaa", "?")
        self.assertEqual(self.s.data["nodes"]["!aaaa"]["name"], "AAA")


class BridgeStatsTests(unittest.TestCase):
    def setUp(self):
        self.mb = import_bridge()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(self.mb, "TREK", trek.TrekGames(
            os.path.join(self.tmp.name, "trek.json")))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = types.SimpleNamespace(
            allow=None, admin=["!boss"], dm_only=False, reply_gap=0, session_idle_min=30,
            zork_max_chunks=None, zork_save_dir=os.path.join(self.tmp.name, "z"),
            adventure_save_dir=os.path.join(self.tmp.name, "a"),
            wumpus_save_dir=os.path.join(self.tmp.name, "w"),
            stats_file=os.path.join(self.tmp.name, "stats.json"),
            lat=0, lon=0, near_km=2, announce=None, announce_channel=0)
        self.bridge = self.mb.Bridge(cfg)
        self.bridge.my_id = "!me"
        self.sent = []
        self.bridge.send = lambda dest, text: self.sent.append((dest, text))
        self.bridge.sender_position = lambda node: None
        self.bridge.node_name = lambda node: node[1:5].upper()
        threading.Thread(target=self.bridge.worker, daemon=True).start()

    def msg(self, sender, text, expect=1):
        before = len(self.sent)
        self.bridge.on_receive({"decoded": {"text": text}, "fromId": sender,
                                "toId": "!me"}, None)
        deadline = time.time() + 5
        while len(self.sent) < before + expect and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        return [t for _, t in self.sent[before:]]

    def test_counts_commands_unknowns_and_visitors(self):
        self.msg("!aaaa", "/ping")
        self.msg("!aaaa", "/ping")
        self.msg("!bbbb", "/nonsense")
        d = self.bridge.stats.data
        self.assertEqual(d["commands"], {"ping": 2})
        self.assertEqual(d["unknown"], 1)
        self.assertEqual(set(d["nodes"]), {"!aaaa", "!bbbb"})

    def test_admin_sees_stats_and_they_fit_in_one_message_each(self):
        self.msg("!aaaa", "/ping")
        out = self.msg("!boss", "/stats", expect=1)
        self.assertTrue(out)
        self.assertIn("Visitors: 2 total", "\n".join(out))
        for part in out:
            self.assertLessEqual(blen(part), 200)

    def test_non_admin_gets_no_answer_and_is_counted_as_refused(self):
        out = self.msg("!aaaa", "/stats", expect=0)
        self.assertEqual(out, [])
        self.assertEqual(self.bridge.stats.data["refused"], 1)
        self.assertNotIn("stats", self.bridge.stats.data["commands"])

    def test_game_commands_and_moves_are_counted(self):
        self.msg("!aaaa", "/wumpus", expect=1)
        self.msg("!aaaa", "look", expect=1)
        d = self.bridge.stats.data
        self.assertEqual(d["commands"].get("wumpus"), 1)
        self.assertEqual(d["game_msgs"].get("wumpus"), 1)
        self.assertNotIn("\x00session", d["commands"])


if __name__ == "__main__":
    unittest.main()
