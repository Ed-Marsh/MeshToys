"""Tests for the Colossal Cave engine (adventure/), adventure_session.py and the
/adventure hooks in mesh_bridge.py (run: python -m unittest tests.test_adventure -v).

The golden transcripts in tests/test_data/adventure_golden.json were recorded from the
original C program by tools/crosscheck_adventure.py; replaying them checks that the
Python engine still prints exactly what the C game printed.
"""

import json
import os
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import adventure_session
import trek
from adventure import data as D
from adventure.engine import AdventureEngine, Bug, LCG
from adventure.testkit import parse_scenario, scenario_text, setup_from_text
from adventure_session import AdventureSessions, clean_output
from game_session import chunk_reply
from tests.test_zork import Clock, blen, import_bridge
from zork.stub import StubEngine

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_data",
                      "adventure_golden.json")


def play(lines, scenario="", seed=511):
    """Run commands on a fresh game; returns (the engine, all the output)."""
    eng = AdventureEngine.new(seed=seed, setup=setup_from_text(scenario))
    out = [eng.intro()]
    for line in lines:
        try:
            out.append(eng.command(line))
        except RuntimeError as e:
            if "game is over" in str(e):
                break
            out.append(eng.last_text)
            break
    return eng, "".join(out)


class EngineCase(unittest.TestCase):
    def engine(self, scenario="", seed=511):
        eng = AdventureEngine.new(seed=seed, setup=setup_from_text(scenario))
        self.addCleanup(eng.close)
        return eng


class GoldenTests(EngineCase):
    """Replays of games played on the C program."""

    def test_golden_transcripts(self):
        with open(GOLDEN, encoding="utf-8") as fh:
            games = json.load(fh)
        self.assertGreaterEqual(len(games), 6)
        for g in games:
            with self.subTest(g["name"]):
                eng, out = play(g["lines"], g["scenario"], g["seed"])
                eng.close()
                self.assertEqual(out, g["output"])

    def test_golden_covers_the_interesting_events(self):
        with open(GOLDEN, encoding="utf-8") as fh:
            names = {g["name"] for g in json.load(fh)}
        for want in ("walk", "dwarf-attack", "death-and-reincarnation", "pirate",
                     "cave-closing-soon", "cave-closed", "score-at-the-end"):
            self.assertIn(want, names)


class EngineTests(EngineCase):
    def test_new_game_starts_at_the_road_without_the_instructions_question(self):
        eng = self.engine()
        self.assertTrue(eng.intro().startswith("You are standing at the end of a road"))
        self.assertNotIn("instructions", eng.intro())
        self.assertEqual(eng.g.limit, 330)
        self.assertFalse(eng.finished)

    def test_first_moves(self):
        eng = self.engine()
        self.assertIn("well house", eng.command("in"))
        self.assertIn("keys", eng.command("look"))
        self.assertEqual(eng.command("get lamp").strip(), "OK\n>".strip())
        self.assertIn("Brass lantern", eng.command("inventory"))

    def test_parser(self):
        eng = self.engine()
        self.assertIn("What?", eng.command("hello") + eng.command("hello") + eng.command("hello")
                      + "What?")                    # one of three replies; never a crash
        self.assertEqual(eng.command("   ").strip(), ">")        # blank line: asks again
        self.assertIn("bad grammar", eng.command("in out"))
        eng.command("in")                   # (like the C game, "say" needs the keys at hand)
        self.assertIn("Okay.\nxyzzy", eng.command("say xyzzy"))
        # a word is cut at 19 characters; the rest is a second word (as sscanf does)
        from adventure.engine import _scan_words
        self.assertEqual(_scan_words("a" * 25 + " b"), ["a" * 19, "a" * 6])
        self.assertEqual(_scan_words("  get \t lamp  extra"), ["get", "lamp"])
        self.assertEqual(_scan_words("   "), [])
        self.assertIn("get what?", eng.command("GET").lower())

    def test_quit_asks_and_the_score_ends_the_game(self):
        eng = self.engine()
        out = eng.command("quit")
        self.assertIn("really want to quit", out)
        self.assertFalse(eng.finished)
        self.assertIn("OK", eng.command("n"))
        out = eng.command("quit") + eng.command("y")
        self.assertIn("Score:", out)
        self.assertTrue(eng.finished)
        with self.assertRaises(RuntimeError):
            eng.command("look")

    def test_suspend_and_restore_only_say_the_game_saves_itself(self):
        eng = self.engine()
        for word in ("suspend", "save", "pause", "restore", "load"):
            out = eng.command(word)
            self.assertIn("saved automatically", out, word)
        self.assertFalse(eng.finished)
        self.assertIn("road", eng.command("look"))

    def test_log_with_an_object_is_the_c_games_fatal_error_39(self):
        eng = self.engine()
        eng.command("in")
        with self.assertRaises(RuntimeError):
            eng.command("log lamp")
        self.assertIsInstance(eng.error, Bug)
        self.assertIn("Fatal error number 39", eng.last_text)

    def test_the_word_list_pages_with_a_return_prompt(self):
        eng = self.engine()
        out = eng.command("help help")
        self.assertIn("Enter <RETURN> to continue", out)
        self.assertIn("north", out)
        self.assertFalse(eng.finished)
        self.assertIn("road", eng.command("look"))

    def test_look_does_not_change_the_game(self):
        eng = self.engine()
        eng.command("in")
        before = json.dumps(eng.g.to_state(), sort_keys=True)
        text = eng.look()
        self.assertIn("well house", text)
        self.assertIn("keys", text)
        self.assertEqual(json.dumps(eng.g.to_state(), sort_keys=True), before)
        eng.command("get lamp")
        eng.command("out")
        self.assertIn("road", eng.look())

    def test_look_in_the_dark(self):
        eng = self.engine("loc=15;newloc=15;prop[2]=0")
        self.assertIn("pitch dark", eng.look())

    def test_games_do_not_share_state(self):
        a, b = self.engine(), self.engine()
        a.command("in")
        a.command("get keys")
        self.assertIn("road", b.look())
        self.assertNotIn("Set of keys", b.command("inventory"))
        self.assertIn("Set of keys", a.command("inventory"))

    def test_close_stops_the_game_thread(self):
        eng = AdventureEngine.new(seed=1)
        eng.close()
        eng.thread.join(5)
        self.assertFalse(eng.thread.is_alive())
        with self.assertRaises(RuntimeError):
            eng.command("look")


class SaveTests(EngineCase):
    DANGER = ("loc=64;newloc=64;oldloc=64;oldloc2=64;holding=2;dflag=3;prop[2]=1;place[2]=-1;"
              "place[50]=-1;dloc[1]=64;dseen[1]=1;dloc[2]=64;dseen[2]=1;dloc[3]=99;dloc[4]=100;"
              "dloc[5]=101;dloc[6]=102;tally=3;numdie=0")
    MOVES = ["n", "s", "w", "e", "look", "d", "u", "get all", "throw axe", "kill dwarf", "in",
             "out", "hello", "inventory", "w", "e", "s", "n", "open", "get lamp"] * 4

    def test_a_resumed_game_goes_on_exactly_like_the_original(self):
        """Including the dice: the saved state holds the random generator."""
        a = self.engine(self.DANGER, seed=7)
        for line in self.MOVES[:25]:
            try:
                a.command(line)
            except RuntimeError:
                self.skipTest("the scripted game ended too early")
        saved = json.loads(json.dumps(a.to_dict()))         # as the session layer stores it
        b = AdventureEngine.from_dict(saved)
        self.addCleanup(b.close)
        self.assertEqual(b.g.rng.state, a.g.rng.state)
        self.assertEqual(b.g.to_state(), a.g.to_state())
        for line in self.MOVES[25:60]:
            if a.finished:
                break
            try:
                want = a.command(line)
            except RuntimeError:
                with self.assertRaises(RuntimeError):
                    b.command(line)
                break
            self.assertEqual(b.command(line), want, line)

    def test_the_random_state_is_saved_and_changes_as_the_game_is_played(self):
        eng = self.engine("dflag=3;dloc[1]=1;dseen[1]=1", seed=99)
        before = eng.to_dict()["state"]["rng"]
        eng.command("hello")                                # an unknown word rolls the dice
        eng.command("hello")
        self.assertNotEqual(eng.to_dict()["state"]["rng"], before)

    def test_saved_state_is_json_and_covers_what_the_c_game_saves(self):
        eng = self.engine()
        eng.command("in")
        state = json.loads(json.dumps(eng.to_dict()))["state"]
        for name in ("turns", "loc", "oldloc", "oldloc2", "newloc", "cond", "place", "fixed",
                     "visited", "prop", "tally", "tally2", "limit", "lmwarn", "wzdark", "closing",
                     "closed", "holding", "detail", "knfloc", "clock1", "clock2", "panic", "dloc",
                     "dflag", "dseen", "odloc", "daltloc", "dkill", "chloc", "chloc2", "bonus",
                     "numdie", "object1", "gaveup", "foobar", "rng"):
            self.assertIn(name, state)
        self.assertEqual(state["loc"], 3)

    def test_bad_saves_are_refused(self):
        eng = self.engine()
        good = json.loads(json.dumps(eng.to_dict()))
        bad = []
        bad.append({"v": 2, "state": good["state"]})
        bad.append({"state": good["state"]})
        s = dict(good["state"]); del s["prop"]; bad.append({"v": 1, "state": s})
        s = dict(good["state"]); s["prop"] = s["prop"][:5]; bad.append({"v": 1, "state": s})
        s = dict(good["state"]); s["loc"] = "3"; bad.append({"v": 1, "state": s})
        s = dict(good["state"]); s["loc"] = 0; bad.append({"v": 1, "state": s})
        s = dict(good["state"]); s["place"] = ["x"] * 100; bad.append({"v": 1, "state": s})
        s = dict(good["state"]); s["rng"] = None; bad.append({"v": 1, "state": s})
        for d in bad:
            with self.assertRaises((ValueError, KeyError, TypeError)):
                AdventureEngine.from_dict(d)

    def test_resuming_asks_the_player_nothing_and_waits_for_a_command(self):
        eng = self.engine()
        eng.command("in")
        eng.command("get lamp")
        b = AdventureEngine.from_dict(json.loads(json.dumps(eng.to_dict())))
        self.addCleanup(b.close)
        self.assertIn("lantern", b.command("inventory").lower())
        self.assertIn("well house", b.look())


class FixedBugTests(EngineCase):
    """Things the C program got wrong, which the port fixes (see NOTICE)."""

    def test_room_140_can_be_left(self):
        eng = self.engine("loc=140;newloc=140;oldloc=112;oldloc2=112;prop[2]=1;place[2]=-1;holding=1")
        self.assertIn("Dead end", eng.look())
        word = [w for w, c in D.VOCAB if c == 45][0]
        out = eng.command(word)
        self.assertNotIn("Fatal error", out)
        self.assertEqual(eng.g.loc, 112)

    def test_back_after_going_down_through_the_grate(self):
        eng, out = play(["in", "get lamp", "get keys", "out", "s", "s", "s", "unlock grate",
                         "d", "back", "back"])
        eng.close()
        self.assertNotIn("Fatal error", out)

    def test_back_after_a_magic_word(self):
        eng, out = play(["in", "get lamp", "xyzzy", "back", "back"])
        eng.close()
        self.assertNotIn("Fatal error", out)

    def test_look_next_to_a_dwarf_never_leaves_the_game_at_location_zero(self):
        scenario = ("loc=102;newloc=102;oldloc=55;oldloc2=55;holding=5;dflag=3;tally=2;clock1=3;"
                    "clock2=1;limit=31;numdie=2;prop[2]=0;place[55]=-1;place[18]=-1;place[5]=-1;"
                    "place[46]=-1;place[2]=-1;place[14]=102;dloc[2]=102;dseen[2]=0;dloc[4]=97;"
                    "dseen[4]=0;dloc[5]=0;dseen[5]=0;dloc[6]=119;dseen[6]=1")
        eng = self.engine(scenario)
        for line in ["shatter lamp", "d", "proceed rod", "travel", "i rod", "d", "unlock machine",
                     "fum lantern", "d", "detonate lamp", "dome", "peruse lamp", "look", "rod attack",
                     "light eggs", "in", "on", "look", "look"]:
            if eng.finished:
                break
            out = eng.command(line)
            self.assertNotIn("Fatal error", out)
            self.assertNotEqual(eng.g.loc, 0, line)

    def test_saved_games_never_hold_location_zero(self):
        """load_state refuses it, so a save made at a bad moment must not exist."""
        eng = self.engine()
        eng.command("look")
        self.assertGreater(eng.to_dict()["state"]["loc"], 0)


class ScenarioKitTests(unittest.TestCase):
    def test_round_trip(self):
        text = "loc=5;newloc=5;place[2]=-1;dloc[3]=64"
        items, arrays = parse_scenario(text)
        self.assertEqual(items, {"loc": 5, "newloc": 5})
        self.assertEqual(arrays, {("place", 2): -1, ("dloc", 3): 64})
        self.assertEqual(scenario_text(items, arrays), text)
        with self.assertRaises(ValueError):
            parse_scenario("loc=x")

    def test_lcg_matches_the_c_reference_generator(self):
        rng = LCG(511)
        self.assertEqual([rng() for _ in range(3)], [(s >> 16) & 0x7fff for s in _lcg_states(511, 3)])


def _lcg_states(seed, n):
    s = seed
    for _ in range(n):
        s = (s * 1103515245 + 12345) & 0x7fffffff
        yield s


class CleanTests(unittest.TestCase):
    def test_prompts_bell_and_padding_go(self):
        raw = ("You are in a maze.  Twisty.\n> \nthe        words      here   \n\x07Enter <RETURN>\n\n"
               "Score:              36\n>")
        out = clean_output(raw)
        self.assertEqual(out, "You are in a maze.  Twisty.\nthe  words  here\nEnter <RETURN>\n"
                              "Score:  36")

    def test_no_markdown_characters_survive(self):
        for ch in "_*`~\\":
            self.assertNotIn(ch, clean_output("a%sb %s c" % (ch, ch)))

    def test_every_text_the_game_can_print_fits_the_mesh(self):
        texts = D.MESSAGES + D.ROOM_LONG + D.ROOM_SHORT
        for text in texts:
            msgs = chunk_reply(clean_output(text), max_chunks=99)
            for m in msgs:
                self.assertLessEqual(blen(m), trek.MSG_LIMIT)
            self.assertLessEqual(len(msgs), adventure_session.DEFAULT_MAX_CHUNKS, text[:40])


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.a = self.make()
        self.addCleanup(self.close_all)

    def make(self, **kw):
        kw.setdefault("clock", self.clock)
        return AdventureSessions(os.path.join(self.tmp.name, "saves"), **kw)

    def close_all(self):
        for s in getattr(self, "_made", [self.a]):
            for eng, _ in list(s.live.values()):
                eng.close()

    def say(self, node, text):
        return "\n".join(self.a.handle(node, text))

    def test_start_play_quit_and_resume(self):
        out = self.say("!aaaa", "/adventure")
        self.assertIn("COLOSSAL CAVE", out)
        self.assertIn("end of a road", out)
        self.assertTrue(self.a.in_session("!aaaa"))
        self.assertIn("well house", self.say("!aaaa", "in"))
        self.assertEqual(self.say("!aaaa", "/get lamp"), "OK")          # "/" is ignored
        self.assertIn("Goodbye", self.say("!aaaa", "/quit"))
        self.assertFalse(self.a.in_session("!aaaa"))
        again = self.say("!aaaa", "/adventure")
        self.assertIn("Resuming", again)
        self.assertIn("well house", again)
        self.assertIn("Brass lantern", self.say("!aaaa", "inventory"))

    def test_adventure_while_playing_shows_the_room_again(self):
        self.say("!aaaa", "/adventure")
        self.say("!aaaa", "in")
        out = self.say("!aaaa", "/adventure")
        self.assertIn("well house", out)
        self.assertIn("keys", out)
        self.assertNotIn("COLOSSAL", out)

    def test_every_message_goes_to_the_game_even_a_slash_command(self):
        self.say("!aaaa", "/adventure")
        self.assertNotIn("pong", self.say("!aaaa", "/ping"))
        self.assertIn("Say what", self.say("!aaaa", "/"))

    def test_two_nodes_are_independent(self):
        self.say("!aaaa", "/adventure")
        self.say("!bbbb", "/adventure")
        self.say("!aaaa", "in")
        self.assertIn("well house", self.say("!aaaa", "/adventure"))
        self.assertIn("end of a road", self.say("!bbbb", "/adventure"))
        self.assertTrue(self.a.in_session("!BBBB"))                      # ids are case-insensitive

    def test_new_restarts_inside_and_outside(self):
        self.say("!aaaa", "/adventure")
        self.say("!aaaa", "in")
        self.say("!aaaa", "get keys")
        out = self.say("!aaaa", "/adventure new")                        # inside
        self.assertIn("end of a road", out)
        self.assertNotIn("Resuming", out)
        self.assertNotIn("Set of keys", self.say("!aaaa", "inventory"))
        self.say("!aaaa", "in")
        self.say("!aaaa", "/quit")
        out = self.say("!aaaa", "/adventure new")                        # outside
        self.assertIn("end of a road", out)
        self.say("!aaaa", "/quit")
        self.assertIn("end of a road", self.say("!aaaa", "/adventure"))  # the old save is gone

    def test_survives_a_restart_of_the_bridge(self):
        self.say("!aaaa", "/adventure")
        self.say("!aaaa", "in")
        self.say("!aaaa", "get lamp")
        for eng, _ in self.a.live.values():
            eng.close()
        b = self.make()
        self.a = b
        self.assertIn("Resuming", self.say("!aaaa", "/adventure"))
        self.assertIn("Brass lantern", self.say("!aaaa", "inventory"))

    def test_the_random_state_survives_leaving_and_resuming(self):
        self.say("!aaaa", "/adventure")
        for _ in range(5):
            self.say("!aaaa", "hello")                                   # each one rolls the dice
        with open(self.a._path("!aaaa"), encoding="utf-8") as fh:
            saved = json.load(fh)["state"]["state"]["rng"]
        live = self.a.live["!aaaa"][0].g.rng.state
        self.assertEqual(saved, live)
        self.say("!aaaa", "/quit")
        self.say("!aaaa", "/adventure")
        self.assertEqual(self.a.live["!aaaa"][0].g.rng.state, saved)

    def test_idle_timeout_saves_quietly_then_tells_the_player(self):
        self.say("!aaaa", "/adventure")
        self.say("!aaaa", "in")
        self.clock.t += 31 * 60
        self.assertEqual(self.a.reap(), ["!aaaa"])
        self.assertFalse(self.a.in_session("!aaaa"))
        self.assertTrue(self.a.has_notice("!aaaa"))
        note = self.a.take_notice("!aaaa")[0]
        self.assertIn("closed after 30 min idle", note)
        self.assertIn("/adventure resumes it", note)
        self.assertEqual(self.a.take_notice("!aaaa"), [])
        self.assertIn("well house", self.say("!aaaa", "/adventure"))

    def test_game_over_ends_the_session_and_clears_the_save(self):
        self.say("!aaaa", "/adventure")
        self.assertIn("really want to quit", self.say("!aaaa", "quit"))
        out = self.say("!aaaa", "y")
        self.assertIn("Score:", out)
        self.assertIn("Game over. /adventure starts a new game.", out)
        self.assertFalse(self.a.in_session("!aaaa"))
        self.assertFalse(os.path.exists(self.a._path("!aaaa")))

    def test_leaving_in_the_middle_of_a_question_resumes_just_before_it(self):
        self.say("!aaaa", "/adventure")
        self.say("!aaaa", "in")
        self.assertIn("really want to quit", self.say("!aaaa", "quit"))
        self.assertIn("Goodbye", self.say("!aaaa", "/quit"))            # "/quit" is never an answer
        again = self.say("!aaaa", "/adventure")
        self.assertIn("Resuming", again)
        self.assertIn("well house", again)
        self.assertNotIn("really want to quit", again)
        self.assertIn("Set of keys", self.say("!aaaa", "get keys") + self.say("!aaaa", "inventory"))
        self.assertTrue(self.a.in_session("!aaaa"))

    def test_a_fatal_error_closes_the_session_but_keeps_the_save(self):
        self.say("!aaaa", "/adventure")
        self.say("!aaaa", "in")
        with self.assertLogs("adventure", "ERROR"):
            out = self.say("!aaaa", "log lamp")
        self.assertIn("hit an error and has closed", out)
        self.assertIn("/adventure resumes your last save", out)
        self.assertFalse(self.a.in_session("!aaaa"))
        again = self.say("!aaaa", "/adventure")
        self.assertIn("Resuming", again)
        self.assertIn("well house", again)

    def test_engine_failure_and_failure_to_start_end_cleanly(self):
        class Exploding(StubEngine):
            def command(self, line):
                raise RuntimeError("boom")

        class NoStart(StubEngine):
            def intro(self):
                raise RuntimeError("cannot start")
        z = self.make(engine=Exploding)
        z.handle("!aaaa", "/adventure")
        with self.assertLogs("adventure", "ERROR"):
            out = z.handle("!aaaa", "n")
        self.assertIn("hit an error", out[0])
        z = self.make(engine=NoStart)
        with self.assertLogs("adventure", "ERROR"):
            self.assertEqual(z.handle("!zzzz", "/adventure"),
                             ["Adventure hit an error and could not start."])

    def test_corrupt_save_is_set_aside(self):
        self.say("!aaaa", "/adventure")
        self.say("!aaaa", "/quit")
        with open(self.a._path("!aaaa"), "w") as fh:
            fh.write('{"v": 1, "node": "!aaaa", "state": {"v": 1, "state": {"loc": 3}}}')
        with self.assertLogs("adventure", "ERROR"):
            out = self.say("!aaaa", "/adventure")
        self.assertIn("unreadable", out)
        self.assertIn("end of a road", out)
        self.assertTrue(os.path.exists(self.a._path("!aaaa") + ".bad"))

    def test_other_games_are_blocked_in_both_directions(self):
        self.say("!aaaa", "/adventure")
        self.assertEqual(self.say("!aaaa", "/zork"),
                         "You're in Adventure. Send /quit to leave, then /zork.")
        self.assertEqual(self.say("!aaaa", "/trek"),
                         "You're in Adventure. Send /quit to leave, then /trek.")
        blocked = self.make(blocked=lambda node: "You're in Zork. Send /quit to leave, "
                            "then /adventure." if node == "!bbbb" else None)
        self.assertEqual(blocked.handle("!bbbb", "/adventure"),
                         ["You're in Zork. Send /quit to leave, then /adventure."])
        self.assertFalse(blocked.in_session("!bbbb"))
        self.assertTrue(blocked.handle("!cccc", "/adventure"))
        for eng, _ in blocked.live.values():
            eng.close()

    def test_the_longest_replies_are_sent_whole(self):
        self.say("!aaaa", "/adventure")
        msgs = self.a.handle("!aaaa", "help")
        self.assertGreaterEqual(len(msgs), 8)
        self.assertLessEqual(len(msgs), adventure_session.DEFAULT_MAX_CHUNKS)
        self.assertNotIn(adventure_session.MORE_NOTE, msgs[-1])
        self.assertTrue(all(blen(m) <= trek.MSG_LIMIT for m in msgs))
        self.assertTrue(msgs[0].startswith("(1/%d) " % len(msgs)))

    def test_a_cut_reply_says_so(self):
        class Wordy(StubEngine):
            def command(self, line):
                return " ".join("Blah%d." % i for i in range(500))
        z = self.make(engine=Wordy, max_chunks=2)
        z.handle("!aaaa", "/adventure")
        out = z.handle("!aaaa", "talk")
        self.assertEqual(len(out), 2)
        self.assertTrue(out[-1].endswith("...(cut short)"))
        self.assertTrue(all(blen(m) <= trek.MSG_LIMIT for m in out))

    def test_save_files_are_json_keyed_by_node_and_git_ignored(self):
        self.say("!aaaa", "/adventure")
        with open(self.a._path("!aaaa"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["node"], "!aaaa")
        self.assertEqual(data["state"]["v"], 1)
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(os.path.dirname(here), ".gitignore"), encoding="utf-8") as fh:
            self.assertIn("adventure_saves/", fh.read())

    def test_replies_have_no_prompts_or_markdown(self):
        self.say("!aaaa", "/adventure")
        for word in ("in", "get lamp", "inventory", "score", "help", "?"):
            for m in self.a.handle("!aaaa", word):
                self.assertNotIn("\n>", m)
                self.assertFalse(m.rstrip().endswith(">"), m)
                for ch in "_*`~\\\x07":
                    self.assertNotIn(ch, m)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.mb = import_bridge()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(self.mb, "TREK", trek.TrekGames(
            os.path.join(self.tmp.name, "trek.json")))
        patch.start()
        self.addCleanup(patch.stop)
        cfg = types.SimpleNamespace(
            allow=None, admin=None, dm_only=False, reply_gap=0, session_idle_min=30,
            zork_max_chunks=None, zork_save_dir=os.path.join(self.tmp.name, "z"),
            adventure_save_dir=os.path.join(self.tmp.name, "a"),
            lat=0, lon=0, near_km=2, announce=None, announce_channel=0)
        self.bridge = self.mb.Bridge(cfg)
        self.bridge.my_id = "!me"
        self.bridge.zork._engine = StubEngine           # the four-room stand-in game
        self.sent = []
        self.bridge.send = lambda dest, text: self.sent.append((dest, text))
        self.bridge.sender_position = lambda node: None
        threading.Thread(target=self.bridge.worker, daemon=True).start()
        self.addCleanup(self.close_games)

    def close_games(self):
        for s in (self.bridge.adventure,):
            for eng, _ in list(s.live.values()):
                eng.close()

    def msg(self, sender, text, to="!me", expect=1):
        before = len(self.sent)
        self.bridge.on_receive({"decoded": {"text": text}, "fromId": sender,
                                "toId": to}, None)
        deadline = time.time() + 5
        while len(self.sent) < before + expect and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        return [t for _, t in self.sent[before:]]

    def test_the_command_and_its_help_exist(self):
        self.assertIn("adventure", self.mb.COMMANDS)
        self.assertIn("Colossal Cave", " ".join(self.msg("!aaaa", "/help adventure")))

    def test_chunk_defaults(self):
        self.assertEqual(self.bridge.zork.max_chunks, 6)
        self.assertEqual(self.bridge.adventure.max_chunks, 10)
        cfg = types.SimpleNamespace(
            allow=None, admin=None, dm_only=False, reply_gap=0, session_idle_min=30,
            zork_max_chunks=4, zork_save_dir=os.path.join(self.tmp.name, "z2"),
            adventure_save_dir=os.path.join(self.tmp.name, "a2"))
        b = self.mb.Bridge(cfg)
        self.assertEqual((b.zork.max_chunks, b.adventure.max_chunks), (4, 4))
        self.assertEqual([m.name for m in b.modes], ["zork", "adventure", "wumpus", "trek"])

    def test_dm_session_end_to_end(self):
        out = self.msg("!aaaa", "/adventure")
        self.assertIn("end of a road", " ".join(out))
        self.assertIn("well house", " ".join(self.msg("!aaaa", "in")))
        self.assertNotIn("pong", " ".join(self.msg("!aaaa", "/ping")))     # typed into the game
        self.assertIn("Goodbye", " ".join(self.msg("!aaaa", "/quit")))
        self.assertTrue(self.msg("!aaaa", "/ping")[0].startswith("pong"))  # normal again
        self.assertIn("Resuming", " ".join(self.msg("!aaaa", "/adventure")))

    def test_two_nodes_at_once(self):
        self.msg("!aaaa", "/adventure")
        self.msg("!bbbb", "/adventure")
        self.msg("!aaaa", "in")
        self.assertIn("well house", " ".join(self.msg("!aaaa", "look")))
        self.assertIn("road", " ".join(self.msg("!bbbb", "look")))
        self.assertTrue(all(dest in ("!aaaa", "!bbbb") for dest, _ in self.sent))

    def test_public_messages_are_not_game_input(self):
        self.msg("!aaaa", "/adventure")
        sent = len(self.sent)
        self.bridge.on_receive({"decoded": {"text": "in"}, "fromId": "!aaaa",
                                "toId": "^all"}, None)
        time.sleep(0.2)
        self.assertEqual(len(self.sent), sent)
        self.assertIn("road", " ".join(self.msg("!aaaa", "look")))

    def test_idle_notice_comes_with_the_next_message(self):
        self.msg("!aaaa", "/adventure")
        self.bridge.adventure.idle_s = -1
        self.bridge.adventure.reap()
        out = self.msg("!aaaa", "/ping", expect=2)
        self.assertIn("closed", out[0])
        self.assertIn("/adventure resumes", out[0])
        self.assertTrue(out[1].startswith("pong"))
        self.assertEqual(len(self.msg("!aaaa", "/ping")), 1)

    def test_adventure_blocks_zork_and_trek_and_is_blocked_by_them(self):
        self.msg("!aaaa", "/adventure")
        self.assertIn("You're in Adventure", " ".join(self.msg("!aaaa", "/zork", to="^all")))
        self.assertIn("You're in Adventure", " ".join(self.msg("!aaaa", "/trek", to="^all")))
        self.assertIn("/quit", " ".join(self.msg("!aaaa", "/zork")))        # typed into Adventure
        self.assertFalse(self.bridge.zork.in_session("!aaaa"))
        self.assertFalse(self.bridge.trek.in_session("!aaaa"))
        self.msg("!aaaa", "/quit")
        self.msg("!aaaa", "/zork")
        self.assertIn("You're in Zork. Send /quit to leave, then /adventure.",
                      " ".join(self.msg("!aaaa", "/adventure", to="^all")))
        self.assertIn("You're in Zork. Send /quit to leave, then /adventure.",
                      " ".join(self.msg("!aaaa", "/adventure")))             # typed into Zork
        self.assertFalse(self.bridge.adventure.in_session("!aaaa"))
        self.msg("!aaaa", "/quit")
        self.msg("!aaaa", "/trek")
        self.assertIn("You're in Trek. Send /quit to leave, then /adventure.",
                      " ".join(self.msg("!aaaa", "/adventure", to="^all")))
        self.assertIn("/quit", " ".join(self.msg("!aaaa", "/adventure")))   # typed into Trek
        self.assertFalse(self.bridge.adventure.in_session("!aaaa"))


if __name__ == "__main__":
    unittest.main()
