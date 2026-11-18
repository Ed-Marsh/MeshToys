"""Tests for zork_session.py and the /zork hooks in mesh_bridge.py
(run: python -m unittest test_zork -v)."""

import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import trek
import zork_session
from zork.stub import StubEngine as Engine
from zork_session import ZorkSessions, chunk_reply, clean_output


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ExplodingEngine(Engine):
    """Stub game that raises on the command 'boom'."""

    def command(self, line):
        if line == "boom":
            raise RuntimeError("engine bug")
        return super().command(line)


class BrokenStartEngine(Engine):
    def intro(self):
        raise RuntimeError("cannot start")


def blen(s):
    return len(s.encode("utf-8"))


class CleanTests(unittest.TestCase):
    def test_strips_noise_and_markdown(self):
        raw = ("Welcome to Dungeon.   This version created 11-MAR-91.\n"
               "West of House        Score: 0   Moves: 3\n\n\n"
               "You are *outside* a_house.\n[MORE]\n  \n>\n")
        self.assertEqual(clean_output(raw), "You are outside a house.")

    def test_no_markdown_characters_survive(self):
        out = clean_output("a_b *c* `d` ~e~ f\\g")
        for ch in "_*`~\\":
            self.assertNotIn(ch, out)


class ChunkTests(unittest.TestCase):
    def test_short_text_is_one_unnumbered_message(self):
        self.assertEqual(chunk_reply("hello"), ["hello"])
        self.assertEqual(chunk_reply("  \n "), [])

    def test_splits_on_lines_and_numbers(self):
        text = "\n".join(["line %d " % i + "x" * 70 for i in range(6)])
        msgs = chunk_reply(text, max_chunks=10)
        self.assertGreater(len(msgs), 1)
        n = len(msgs)
        for i, m in enumerate(msgs, 1):
            self.assertTrue(m.startswith("(%d/%d) " % (i, n)), m)
            self.assertLessEqual(blen(m), trek.MSG_LIMIT)
        # every original line survives whole
        joined = "\n".join(msgs)
        for i in range(6):
            self.assertIn("line %d " % i + "x" * 70, joined)

    def test_long_line_splits_on_sentences(self):
        sents = ["This is sentence number %d of a very long room description." % i
                 for i in range(8)]
        msgs = chunk_reply(" ".join(sents), max_chunks=10)
        self.assertGreater(len(msgs), 1)
        body = " ".join(m.split(") ", 1)[1].replace("\n", " ") for m in msgs)
        self.assertEqual(body, " ".join(sents))
        for m in msgs:
            self.assertTrue(m.rstrip().endswith("."), m)       # cut at sentence ends

    def test_unbroken_text_is_hard_cut_within_limit(self):
        msgs = chunk_reply("y" * 1000, max_chunks=10)
        self.assertTrue(all(blen(m) <= trek.MSG_LIMIT for m in msgs))
        self.assertEqual("".join(m.split(") ", 1)[1] for m in msgs), "y" * 1000)

    def test_cap_adds_note_and_respects_limit(self):
        text = " ".join("Word%d." % i for i in range(400))
        msgs = chunk_reply(text, max_chunks=3)
        self.assertEqual(len(msgs), 3)
        self.assertTrue(msgs[0].startswith("(1/3) "))
        self.assertTrue(msgs[-1].endswith(zork_session.MORE_NOTE))
        self.assertTrue(all(blen(m) <= trek.MSG_LIMIT for m in msgs))

    def test_multibyte_text_stays_within_bytes(self):
        msgs = chunk_reply("é" * 400, max_chunks=10)
        self.assertTrue(all(blen(m) <= trek.MSG_LIMIT for m in msgs))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.z = self.make()

    def make(self, **kw):
        kw.setdefault("clock", self.clock)
        kw.setdefault("engine", Engine)
        return ZorkSessions(os.path.join(self.tmp.name, "saves"), **kw)

    def say(self, node, text):
        return "\n".join(self.z.handle(node, text))

    def test_scripted_session_quit_and_resume(self):
        out = self.say("!aaaa", "/zork")
        self.assertIn("West of House", out)
        self.assertTrue(self.z.in_zork("!aaaa"))
        self.assertIn("leaflet", self.say("!aaaa", "open mailbox"))
        self.assertEqual(self.say("!aaaa", "take leaflet"), "Taken.")
        self.assertIn("North of House", self.say("!aaaa", "/n"))   # "/" is ignored
        self.assertIn("Goodbye", self.say("!aaaa", "/quit"))
        self.assertFalse(self.z.in_zork("!aaaa"))
        again = self.say("!aaaa", "/zork")                         # resume
        self.assertIn("Resuming", again)
        self.assertIn("North of House", again)
        self.assertIn("leaflet", self.say("!aaaa", "i"))           # state survived

    def test_zork_while_playing_resends_the_room(self):
        self.say("!aaaa", "/zork")
        self.say("!aaaa", "n")
        self.assertIn("North of House", self.say("!aaaa", "/zork"))
        self.assertTrue(self.z.in_zork("!aaaa"))

    def test_slash_commands_go_to_the_game(self):
        self.say("!aaaa", "/zork")
        self.assertIn("don't know", self.say("!aaaa", "/weather"))

    def test_two_nodes_are_independent(self):
        self.say("!aaaa", "/zork")
        self.say("!bbbb", "/zork")
        self.say("!aaaa", "n")
        self.assertIn("North of House", self.say("!aaaa", "look"))
        self.assertIn("West of House", self.say("!bbbb", "look"))
        self.say("!bbbb", "/quit")
        self.assertTrue(self.z.in_zork("!aaaa"))
        self.assertFalse(self.z.in_zork("!bbbb"))
        files = sorted(os.listdir(self.z.save_dir))
        self.assertEqual(len(files), 2)

    def test_node_ids_are_case_insensitive(self):
        self.say("!AAAA", "/zork")
        self.assertTrue(self.z.in_zork("!aaaa"))

    def test_survives_restart(self):
        self.say("!aaaa", "/zork")
        self.say("!aaaa", "n")                   # autosaved, no /quit
        z2 = self.make()
        self.assertFalse(z2.in_zork("!aaaa"))
        out = "\n".join(z2.handle("!aaaa", "/zork"))
        self.assertIn("Resuming", out)
        self.assertIn("North of House", out)

    def test_idle_timeout_saves_quietly_then_tells_user(self):
        self.say("!aaaa", "/zork")
        self.say("!aaaa", "n")
        self.clock.t += 29 * 60
        self.assertEqual(self.z.reap(), [])
        self.clock.t += 2 * 60
        self.assertEqual(self.z.reap(), ["!aaaa"])
        self.assertFalse(self.z.in_zork("!aaaa"))
        self.assertTrue(self.z.has_notice("!aaaa"))
        note = " ".join(self.z.take_notice("!aaaa"))
        self.assertIn("/zork resumes", note)
        self.assertEqual(self.z.take_notice("!aaaa"), [])          # told once
        self.assertIn("North of House", self.say("!aaaa", "/zork"))

    def test_activity_resets_idle_timer(self):
        self.say("!aaaa", "/zork")
        for _ in range(4):
            self.clock.t += 20 * 60
            self.say("!aaaa", "look")
            self.assertEqual(self.z.reap(), [])

    def test_idle_timeout_is_configurable(self):
        z = self.make(idle_s=60)
        z.handle("!aaaa", "/zork")
        self.clock.t += 61
        self.assertEqual(z.reap(), ["!aaaa"])

    def test_starting_clears_pending_notice(self):
        self.say("!aaaa", "/zork")
        self.clock.t += 3600
        self.z.reap()
        self.say("!aaaa", "/zork")
        self.assertFalse(self.z.has_notice("!aaaa"))

    def test_engine_failure_ends_session_cleanly(self):
        z = self.make(engine=ExplodingEngine)
        z.handle("!aaaa", "/zork")
        z.handle("!aaaa", "n")
        with self.assertLogs("zork", "ERROR"):
            out = "\n".join(z.handle("!aaaa", "boom"))
        self.assertIn("error", out)
        self.assertFalse(z.in_zork("!aaaa"))                       # not stuck
        self.assertIn("North of House", "\n".join(z.handle("!aaaa", "/zork")))

    def test_engine_failure_on_start(self):
        z = self.make(engine=BrokenStartEngine)
        with self.assertLogs("zork", "ERROR"):
            out = "\n".join(z.handle("!aaaa", "/zork"))
        self.assertIn("error", out)
        self.assertFalse(z.in_zork("!aaaa"))

    def test_corrupt_save_is_set_aside(self):
        self.say("!aaaa", "/zork")
        self.say("!aaaa", "/quit")
        path = self.z._path("!aaaa")
        with open(path, "w") as fh:
            fh.write("{not json")
        with self.assertLogs("zork", "ERROR"):
            out = self.say("!aaaa", "/zork")
        self.assertIn("unreadable", out)
        self.assertIn("West of House", out)
        self.assertTrue(os.path.exists(path + ".bad"))

    def test_game_over_ends_session_and_clears_save(self):
        self.say("!aaaa", "/zork")
        out = self.say("!aaaa", "die")
        self.assertIn("Game over", out)
        self.assertFalse(self.z.in_zork("!aaaa"))
        self.assertEqual(os.listdir(self.z.save_dir), [])
        self.assertIn("West of House", self.say("!aaaa", "/zork"))   # fresh game

    def test_trek_and_zork_are_exclusive(self):
        self.say("!aaaa", "/zork")
        self.assertIn("/quit", self.say("!aaaa", "/trek"))
        self.assertTrue(self.z.in_zork("!aaaa"))
        z = self.make(blocked=lambda node: "You're in Trek." if node == "!bbbb" else None)
        self.assertEqual(z.handle("!bbbb", "/zork"), ["You're in Trek."])
        self.assertFalse(z.in_zork("!bbbb"))
        self.assertTrue(z.handle("!cccc", "/zork"))
        self.assertTrue(z.in_zork("!cccc"))

    def test_long_output_is_capped(self):
        class Wordy(Engine):
            def command(self, line):
                return " ".join("Blah%d." % i for i in range(500))
        z = self.make(engine=Wordy, max_chunks=2)
        z.handle("!aaaa", "/zork")
        out = z.handle("!aaaa", "talk")
        self.assertEqual(len(out), 2)
        self.assertTrue(out[-1].endswith(zork_session.MORE_NOTE))

    def test_save_files_are_json_keyed_by_node(self):
        self.say("!aaaa", "/zork")
        with open(self.z._path("!aaaa")) as fh:
            data = json.load(fh)
        self.assertEqual(data["node"], "!aaaa")
        self.assertIn("state", data)


# --- The bridge hooks, with the radio libraries replaced by stubs ----------------
def import_bridge():
    stubs = {}
    for name in ("requests", "meshtastic", "meshtastic.serial_interface",
                 "meshtastic.tcp_interface", "pubsub"):
        try:
            __import__(name)
        except ImportError:
            stubs[name] = mock.MagicMock()
    with mock.patch.dict(sys.modules, stubs):
        import mesh_bridge
    return mesh_bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.mb = import_bridge()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Never touch the real trek_games.json
        patch = mock.patch.object(self.mb, "TREK", trek.TrekGames(
            os.path.join(self.tmp.name, "trek.json")))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = types.SimpleNamespace(
            allow=None, admin=None, dm_only=False, reply_gap=0, session_idle_min=30,
            zork_max_chunks=6, zork_save_dir=os.path.join(self.tmp.name, "z"),
            lat=0, lon=0, near_km=2, announce=None, announce_channel=0)
        self.bridge = self.mb.Bridge(cfg)
        self.bridge.my_id = "!me"
        self.bridge.zork._engine = Engine           # the four-room stand-in game
        self.sent = []
        self.bridge.send = lambda dest, text: self.sent.append((dest, text))
        self.bridge.sender_position = lambda node: None
        threading.Thread(target=self.bridge.worker, daemon=True).start()

    def msg(self, sender, text, to="!me", expect=1):
        before = len(self.sent)
        self.bridge.on_receive({"decoded": {"text": text}, "fromId": sender,
                                "toId": to}, None)
        deadline = time.time() + 5
        while len(self.sent) < before + expect and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        return [t for _, t in self.sent[before:]]

    def test_dm_session_end_to_end(self):
        out = self.msg("!aaaa", "/zork")
        self.assertIn("West of House", " ".join(out))
        self.assertIn("Opening the small mailbox", " ".join(self.msg("!aaaa", "open mailbox")))
        self.assertIn("don't know", " ".join(self.msg("!aaaa", "/weather")))   # not run as a command
        self.assertIn("North of House", " ".join(self.msg("!aaaa", "n")))
        self.assertIn("Goodbye", " ".join(self.msg("!aaaa", "/quit")))
        self.assertTrue(self.msg("!aaaa", "/ping")[0].startswith("pong"))    # normal again
        self.assertIn("Resuming", " ".join(self.msg("!aaaa", "/zork")))

    def test_two_nodes_at_once(self):
        self.msg("!aaaa", "/zork")
        self.msg("!bbbb", "/zork")
        self.msg("!aaaa", "n")
        self.assertIn("North of House", " ".join(self.msg("!aaaa", "look")))
        self.assertIn("West of House", " ".join(self.msg("!bbbb", "look")))
        self.assertTrue(all(dest in ("!aaaa", "!bbbb") for dest, _ in self.sent))

    def test_public_messages_are_not_game_input(self):
        self.msg("!aaaa", "/zork")
        sent = len(self.sent)
        self.bridge.on_receive({"decoded": {"text": "n"}, "fromId": "!aaaa",
                                "toId": "^all"}, None)
        time.sleep(0.2)
        self.assertEqual(len(self.sent), sent)
        self.assertIn("West of House", " ".join(self.msg("!aaaa", "look")))

    def test_idle_notice_comes_with_next_message(self):
        self.msg("!aaaa", "/zork")
        self.bridge.zork.idle_s = -1
        self.bridge.zork.reap()
        out = self.msg("!aaaa", "/ping", expect=2)
        self.assertIn("closed", out[0])
        self.assertIn("/zork resumes", out[0])
        self.assertTrue(out[1].startswith("pong"))
        self.assertEqual(len(self.msg("!aaaa", "/ping")), 1)             # only once

    def test_zork_blocks_trek(self):
        self.msg("!aaaa", "/zork")
        self.assertIn("/quit", " ".join(self.msg("!aaaa", "/trek")))   # typed into Zork
        self.assertIn("You're in Zork", " ".join(self.msg("!aaaa", "/trek", to="^all")))
        self.assertFalse(self.bridge.trek.in_session("!aaaa"))


if __name__ == "__main__":
    unittest.main()
