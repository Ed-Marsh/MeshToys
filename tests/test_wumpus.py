"""Tests for the Wumpus engine (wumpus/), wumpus/session.py and the /wumpus hooks in
mesh_bridge.py (run: python -m unittest tests.test_wumpus -v).

Games are made deterministic two ways: a fixed starting layout, and a FakeRng that
hands out the random numbers the test wants (every random choice in the engine is
rng.randint, so the script says exactly which die rolls happen).
"""

import json
import os
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import trek
from tests.test_zork import Clock, blen, import_bridge
from wumpus import engine as E
from wumpus.engine import ACTION, MOVE, SHOOT, TUNNELS, WumpusEngine
from wumpus.session import WumpusSessions


class FakeRng:
    """Stands in for random.Random: randint returns the scripted values in order."""

    def __init__(self, *rolls):
        self.rolls = list(rolls)

    def randint(self, a, b):
        v = self.rolls.pop(0)
        assert a <= v <= b, "scripted roll %d outside %d-%d" % (v, a, b)
        return v

    def getstate(self):
        return (3, tuple([0] * 625), None)


def game(you=1, wumpus=12, pits=(17, 18), bats=(19, 20), rolls=()):
    """A game with the given layout. Defaults are far apart: cave 1 has tunnels
    2 5 8 and none of the hazards is next to it."""
    return WumpusEngine.new(layout={"you": you, "wumpus": wumpus, "pits": list(pits),
                                    "bats": list(bats)}, rng=FakeRng(*rolls))


class FixedEngine(WumpusEngine):
    """Engine whose new games always start the same way (for the session tests)."""

    @classmethod
    def new(cls, seed=None, layout=None, rng=None):
        return super().new(layout={"you": 1, "wumpus": 12, "pits": [17, 18],
                                   "bats": [19, 20]}, rng=rng or __import__("random").Random(5))


class CaveTests(unittest.TestCase):
    def test_the_cave_is_a_dodecahedron(self):
        self.assertEqual(len(TUNNELS), 20)
        edges = set()
        for room, tun in enumerate(TUNNELS, 1):
            self.assertEqual(len(set(tun)), 3)
            for other in tun:
                self.assertNotEqual(room, other)
                self.assertIn(room, TUNNELS[other - 1], "tunnel %d-%d is one way" % (room, other))
                edges.add(frozenset((room, other)))
        self.assertEqual(len(edges), 30)
        seen, todo = {1}, [1]
        while todo:
            for n in TUNNELS[todo.pop() - 1]:
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        self.assertEqual(len(seen), 20)

    def test_a_new_game_has_six_different_caves_and_five_arrows(self):
        for seed in range(50):
            g = WumpusEngine.new(seed=seed)
            self.assertEqual(len({g.you, g.wumpus, *g.pits, *g.bats}), 6)
            self.assertEqual(g.arrows, 5)
            self.assertFalse(g.finished)

    def test_the_same_seed_gives_the_same_game(self):
        self.assertEqual(WumpusEngine.new(seed=7).to_dict(), WumpusEngine.new(seed=7).to_dict())
        self.assertNotEqual(WumpusEngine.new(seed=7).to_dict(), WumpusEngine.new(seed=8).to_dict())


class LookTests(unittest.TestCase):
    def test_plain_cave_is_compact(self):
        self.assertEqual(game().look(), "Cave 1. Tunnels: 2 5 8. Arrows 5.")

    def test_each_warning(self):
        self.assertIn("You smell a Wumpus.", game(wumpus=2).look())
        self.assertIn("You feel a draft.", game(pits=(5, 17)).look())
        self.assertIn("Bats rustle nearby.", game(bats=(8, 19)).look())
        both = game(wumpus=2, pits=(5, 17), bats=(8, 19)).look()
        for w in ("Wumpus", "draft", "Bats"):
            self.assertIn(w, both)

    def test_a_hazard_two_tunnels_away_gives_no_warning(self):
        self.assertNotIn("Wumpus", game(wumpus=3).look())


class MoveTests(unittest.TestCase):
    def test_move_to_a_joined_cave(self):
        g = game()
        out = g.command("M 5")
        self.assertEqual(g.you, 5)
        self.assertTrue(out.startswith("Cave 5. Tunnels: 1 4 6."), out)

    def test_forgiving_phrasing(self):
        for line in ("m 5", "M5", "move 5", "MOVE 5", "go to 5", "  move   to cave 5 ", "/m 5".lstrip("/")):
            g = game()
            g.command(line)
            self.assertEqual(g.you, 5, line)

    def test_no_tunnel_or_bad_number_keeps_the_game_going(self):
        g = game()
        self.assertIn("No tunnel to cave 7", g.command("M 7"))
        self.assertIn("1-20", g.command("M 99"))
        self.assertIn("one cave", g.command("M 2 5"))
        self.assertIn("No tunnel to cave 1", g.command("M 1"))     # staying put is not a move
        self.assertEqual((g.you, g.finished), (1, False))

    def test_pit_loses(self):
        g = game(pits=(2, 17))
        self.assertIn("pit", g.command("M 2"))
        self.assertTrue(g.finished)

    def test_bumping_the_wumpus_it_wanders_off(self):
        g = game(wumpus=2, rolls=[2])               # roll 2: to its second tunnel (cave 3)
        out = g.command("M 2")
        self.assertIn("bumped", out)
        self.assertEqual((g.wumpus, g.finished, g.you), (3, False, 2))
        self.assertIn("You smell a Wumpus.", out)   # it is next door now

    def test_bumping_the_wumpus_it_stays_and_eats_you(self):
        g = game(wumpus=2, rolls=[4])               # roll 4: it does not move
        out = g.command("M 2")
        self.assertIn("Wumpus got you", out)
        self.assertTrue(g.finished)

    def test_the_woken_wumpus_can_use_any_of_its_three_tunnels(self):
        for roll, where in ((1, 1), (2, 3), (3, 10)):
            g = game(wumpus=2, you=5, rolls=[roll])
            g.you = 1                                   # step into its cave from cave 1
            g.command("M 2")
            self.assertEqual(g.wumpus, where)
            self.assertEqual(g.finished, where == 2)

    def test_bats_carry_you_elsewhere(self):
        g = game(bats=(2, 19), rolls=[7])
        out = g.command("M 2")
        self.assertIn("bats carry you off to cave 7", out)
        self.assertEqual(g.you, 7)
        self.assertIn("Cave 7. Tunnels: 6 8 17.", out)

    def test_bats_can_drop_you_in_a_pit(self):
        g = game(bats=(2, 19), rolls=[17])
        out = g.command("M 2")
        self.assertIn("carry you off to cave 17", out)
        self.assertIn("pit", out)
        self.assertTrue(g.finished)

    def test_bats_can_drop_you_on_the_wumpus(self):
        g = game(bats=(2, 19), wumpus=9, rolls=[9, 4])    # dropped on it; it stays; eaten
        out = g.command("M 2")
        self.assertIn("bumped", out)
        self.assertIn("Wumpus got you", out)
        self.assertTrue(g.finished)

    def test_bats_can_drop_you_with_more_bats(self):
        g = game(bats=(2, 19), rolls=[19, 11])         # first drop is the other colony
        g.command("M 2")
        self.assertEqual(g.you, 11)


class ShootTests(unittest.TestCase):
    def test_arrow_down_a_straight_path_wins(self):
        g = game(wumpus=3)
        out = g.command("S 2 3")
        self.assertIn("You win", out)
        self.assertTrue(g.finished)

    def test_a_wrong_cave_sends_the_arrow_a_random_way(self):
        g = game(wumpus=2, rolls=[1])                  # 7 isn't joined to 1; roll 1 = first tunnel (2)
        self.assertIn("You win", g.command("S 7"))

    def test_the_arrow_can_come_back_and_hit_you(self):
        g = game()
        out = g.command("S 2 1")
        self.assertIn("hit you", out)
        self.assertTrue(g.finished)

    def test_arrows_are_not_that_crooked(self):
        g = game()
        self.assertIn("crooked", g.command("S 2 1 2"))
        self.assertIn("crooked", g.command("S 2 3 2"))
        self.assertEqual((g.arrows, g.finished), (5, False))
        # the same cave twice in a row is allowed (the arrow flies off at random)
        g = game(rolls=[2, 4])                         # hop 2 -> 3, then the Wumpus stays
        g.command("S 2 2")
        self.assertEqual(g.arrows, 4)

    def test_path_length_and_range_checked(self):
        g = game()
        self.assertIn("1-5", g.command("S 2 3 4 3 4 3"))
        self.assertIn("1-20", g.command("S 21"))
        self.assertIn("1-20", g.command("S 0"))
        self.assertEqual((g.arrows, g.finished), (5, False))

    def test_a_miss_costs_an_arrow_and_wakes_the_wumpus(self):
        g = game(wumpus=12, rolls=[1])                # wumpus 12's first tunnel is 3
        out = g.command("S 2")
        self.assertIn("Missed.", out)
        self.assertEqual((g.arrows, g.wumpus), (4, 3))

    def test_a_missed_arrow_can_wake_the_wumpus_into_your_cave(self):
        g = game(wumpus=2, rolls=[1])                 # you are in 1; arrow goes elsewhere
        g.rng.rolls.insert(0, 3)                       # the arrow's random hop (1 -> 8)
        out = g.command("S 7")
        self.assertIn("Wumpus got you", out)
        self.assertTrue(g.finished)

    def test_out_of_arrows_loses(self):
        g = game(wumpus=12, rolls=[4] * 5)            # the Wumpus never moves
        for i in range(4):
            self.assertNotIn("Out of arrows", g.command("S 2"))
            self.assertEqual(g.arrows, 4 - i)
        out = g.command("S 2")
        self.assertIn("Out of arrows", out)
        self.assertTrue(g.finished)

    def test_a_hit_on_the_last_arrow_still_wins(self):
        g = game(wumpus=2)
        g.arrows = 1
        self.assertIn("You win", g.command("S 2"))


class StateMachineTests(unittest.TestCase):
    def test_move_then_cave_in_a_second_message(self):
        g = game()
        self.assertIn("Move to which cave?", g.command("m"))
        self.assertEqual(g.state, MOVE)
        g.command("5")
        self.assertEqual((g.you, g.state), (5, ACTION))

    def test_shoot_then_path_in_a_second_message(self):
        g = game(wumpus=3)
        self.assertIn("Shoot through which caves", g.command("s"))
        self.assertEqual(g.state, SHOOT)
        self.assertIn("You win", g.command("2 3"))

    def test_a_bad_answer_keeps_the_question_open(self):
        g = game()
        g.command("m")
        self.assertIn("No tunnel", g.command("7"))
        self.assertEqual(g.state, MOVE)
        self.assertEqual(g.command("cancel"), g.look())
        self.assertEqual(g.state, ACTION)

    def test_a_full_command_overrides_an_open_question(self):
        g = game()
        g.command("s")
        g.command("m 5")
        self.assertEqual((g.you, g.state), (5, ACTION))

    def test_a_bare_number_moves(self):
        g = game()
        g.command("5")
        self.assertEqual(g.you, 5)

    def test_unknown_input_never_ends_or_changes_the_game(self):
        g = game()
        before = g.to_dict()
        for junk in ("xyzzy", "", "   ", "m banana", "s 2 x", "1 2 3", "????", "😀", "m -3"):
            out = g.command(junk)
            self.assertTrue(out, junk)
            self.assertFalse(g.finished, junk)
        self.assertIn("Didn't understand", g.command("dance"))
        self.assertEqual(g.to_dict(), before)

    def test_help_and_look(self):
        g = game()
        self.assertIn("S 3 7 12", g.command("help"))
        self.assertIn("S 3 7 12", g.command("?"))
        self.assertEqual(g.command("look"), g.look())

    def test_commands_after_the_end_raise(self):
        g = game(pits=(2, 17))
        g.command("m 2")
        with self.assertRaises(RuntimeError):
            g.command("m 1")


class FuzzTests(unittest.TestCase):
    def test_random_play_always_ends_cleanly(self):
        import random
        pick = random.Random(99)
        words = ["m", "s", "go", "shoot", "help", "look", "cancel", "x", "?", ""]
        for seed in range(200):
            g = WumpusEngine.new(seed=seed)
            for _ in range(300):
                if g.finished:
                    break
                line = " ".join([pick.choice(words)] + [str(pick.randint(0, 22))
                                                       for _ in range(pick.randint(0, 6))])
                out = g.command(line)
                self.assertTrue(out)
                self.assertLessEqual(blen(out), 400, line)
                self.assertTrue(1 <= g.you <= 20 and 0 <= g.arrows <= 5)
                g.check()
            if not g.finished:                          # a sensible player always ends it
                for _ in range(40):
                    if g.finished:
                        break
                    g.command("m %d" % pick.choice(TUNNELS[g.you - 1]))


class SaveTests(unittest.TestCase):
    def test_a_saved_game_resumes_exactly(self):
        a = WumpusEngine.new(seed=3)
        a.command("m %d" % TUNNELS[a.you - 1][0])
        b = WumpusEngine.from_dict(json.loads(json.dumps(a.to_dict())))
        self.assertEqual(a.to_dict(), b.to_dict())
        for line in ("s 1 2 3", "m 4", "s 5", "m 6", "s 7 8"):     # same random events
            if a.finished:
                break
            self.assertEqual(a.command(line), b.command(line))
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_a_wumpus_that_wandered_onto_a_hazard_still_saves_and_loads(self):
        g = game(wumpus=3, pits=(4, 17), rolls=[3])     # cave 3's third tunnel is cave 12; use 2nd
        g.wumpus = 4                                     # it has walked into a pit cave
        self.assertEqual(WumpusEngine.from_dict(json.loads(json.dumps(g.to_dict()))).wumpus, 4)

    def test_overlapping_start_is_refused(self):
        with self.assertRaises(ValueError):
            game(you=1, wumpus=1)

    def test_state_is_saved(self):
        g = game()
        g.command("s")
        self.assertEqual(WumpusEngine.from_dict(json.loads(json.dumps(g.to_dict()))).state, SHOOT)

    def test_bad_saves_are_refused(self):
        good = WumpusEngine.new(seed=1).to_dict()
        for key, bad in (("you", 99), ("you", good["wumpus"]), ("arrows", 9),
                         ("state", "dancing"), ("pits", [1]), ("you", "3")):
            d = dict(good, **{key: bad})
            with self.assertRaises((ValueError, KeyError, TypeError), msg=key):
                WumpusEngine.from_dict(d)
        with self.assertRaises(KeyError):
            WumpusEngine.from_dict({})


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.s = WumpusSessions(self.tmp.name, engine=FixedEngine, clock=self.clock)

    def say(self, node, text):
        return "\n".join(self.s.handle(node, text))


class SessionTests(SessionCase):
    def test_start_replies_with_intro_cave_and_tunnels(self):
        msgs = self.s.handle("!aaaa", "/wumpus")
        text = "\n".join(msgs)
        self.assertIn("HUNT THE WUMPUS", text)
        self.assertIn("Gregory Yob", text)
        self.assertIn("Cave 1. Tunnels: 2 5 8.", text)
        self.assertTrue(self.s.in_session("!aaaa"))
        self.assertTrue(all(blen(m) <= trek.MSG_LIMIT for m in msgs))

    def test_every_reply_fits_one_packet(self):
        self.say("!aaaa", "/wumpus")
        for line in ("help", "m 5", "m 99", "s 2 3 4", "dance", "look", "s", "cancel"):
            msgs = self.s.handle("!aaaa", line)
            self.assertTrue(msgs, line)
            self.assertTrue(all(blen(m) <= trek.MSG_LIMIT for m in msgs), line)
        self.assertEqual(len(self.s.handle("!aaaa", "m 5")), 1)      # the usual reply is one message

    def test_everything_is_game_input_even_with_a_slash(self):
        self.say("!aaaa", "/wumpus")
        self.assertIn("Cave 5", self.say("!aaaa", "/m 5"))
        self.assertIn("Didn't understand", self.say("!aaaa", "/ping"))
        self.assertTrue(self.s.in_session("!aaaa"))

    def test_slash_wumpus_resends_the_cave(self):
        self.say("!aaaa", "/wumpus")
        self.say("!aaaa", "m 5")
        self.assertEqual(self.say("!aaaa", "/wumpus"), "Cave 5. Tunnels: 1 4 6. Arrows 5.")

    def test_quit_mid_game_then_resume_is_exact(self):
        self.say("!aaaa", "/wumpus")
        self.say("!aaaa", "m 5")
        self.say("!aaaa", "s 6 7")                     # a miss: an arrow is gone
        before = self.s.live["!aaaa"][0].to_dict()
        out = self.say("!aaaa", "/quit")
        self.assertIn("saved", out)
        self.assertFalse(self.s.in_session("!aaaa"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "_aaaa.json")) or
                        os.listdir(self.tmp.name))
        out = self.say("!aaaa", "/wumpus")
        self.assertIn("Resuming", out)
        self.assertEqual(self.s.live["!aaaa"][0].to_dict(), before)
        self.assertIn("Arrows 4", out)

    def test_a_new_session_object_resumes_from_disk(self):
        self.say("!aaaa", "/wumpus")
        self.say("!aaaa", "m 5")
        again = WumpusSessions(self.tmp.name, engine=FixedEngine, clock=self.clock)
        self.assertIn("Cave 5", "\n".join(again.handle("!aaaa", "/wumpus")))

    def test_bare_quit_works(self):
        self.say("!aaaa", "/wumpus")
        self.assertIn("saved", self.say("!aaaa", "quit"))
        self.assertFalse(self.s.in_session("!aaaa"))

    def test_wumpus_new_starts_over(self):
        self.say("!aaaa", "/wumpus")
        self.say("!aaaa", "m 5")
        self.assertIn("Cave 1.", self.say("!aaaa", "/wumpus new"))

    def test_win_ends_the_game_and_offers_a_new_one(self):
        self.s = WumpusSessions(self.tmp.name, clock=self.clock, engine=type(
            "Easy", (WumpusEngine,), {"new": classmethod(lambda cls, **k: WumpusEngine(
                1, 3, [17, 18], [19, 20], rng=FakeRng()))}))
        self.say("!aaaa", "/wumpus")
        out = self.say("!aaaa", "s 2 3")
        self.assertIn("You win", out)
        self.assertIn("/wumpus starts a new game", out)
        self.assertFalse(self.s.in_session("!aaaa"))
        self.assertEqual(os.listdir(self.tmp.name), [])            # nothing stale to resume
        self.assertIn("Cave 1.", self.say("!aaaa", "/wumpus"))

    def test_loss_ends_the_game_too(self):
        self.say("!aaaa", "/wumpus")
        self.say("!aaaa", "m 5")
        self.say("!aaaa", "m 6")
        self.say("!aaaa", "m 15")
        self.say("!aaaa", "m 16")
        out = self.say("!aaaa", "m 17")                            # the pit
        self.assertIn("pit", out)
        self.assertIn("/wumpus starts a new game", out)
        self.assertFalse(self.s.in_session("!aaaa"))

    def test_two_nodes_are_independent(self):
        self.say("!aaaa", "/wumpus")
        self.say("!bbbb", "/wumpus")
        self.say("!aaaa", "m 5")
        self.assertIn("Cave 5", self.say("!aaaa", "look"))
        self.assertIn("Cave 1", self.say("!bbbb", "look"))
        self.say("!aaaa", "/quit")
        self.assertTrue(self.s.in_session("!bbbb"))

    def test_node_ids_are_case_insensitive(self):
        self.say("!AAAA", "/wumpus")
        self.assertTrue(self.s.in_session("!aaaa"))

    def test_starting_while_in_another_game_says_how_to_leave(self):
        self.s.blocked = lambda node: "You're in Zork. Send /quit to leave, then /wumpus."
        self.assertIn("You're in Zork", self.say("!aaaa", "/wumpus"))
        self.assertFalse(self.s.in_session("!aaaa"))

    def test_other_games_are_refused_while_playing(self):
        self.say("!aaaa", "/wumpus")
        for other in ("trek", "zork", "adventure"):
            self.assertEqual(self.say("!aaaa", "/" + other),
                             "You're in Wumpus. Send /quit to leave, then /%s." % other)
        self.assertTrue(self.s.in_session("!aaaa"))

    def test_idle_timeout_saves_and_ends_quietly(self):
        self.say("!aaaa", "/wumpus")
        self.say("!aaaa", "m 5")
        self.clock.t += 31 * 60
        self.assertEqual(self.s.reap(), ["!aaaa"])
        self.assertFalse(self.s.in_session("!aaaa"))
        self.assertTrue(self.s.has_notice("!aaaa"))
        self.assertIn("closed after 30 min", self.s.take_notice("!aaaa")[0])
        self.assertIn("Cave 5", self.say("!aaaa", "/wumpus"))      # resumes

    def test_idle_is_configurable_and_activity_resets_it(self):
        self.s.idle_s = 60
        self.say("!aaaa", "/wumpus")
        self.clock.t += 50
        self.say("!aaaa", "look")
        self.clock.t += 50
        self.assertEqual(self.s.reap(), [])
        self.clock.t += 20
        self.assertEqual(self.s.reap(), ["!aaaa"])

    def test_an_engine_exception_ends_the_session_cleanly(self):
        class Boom(FixedEngine):
            def command(self, line):
                raise RuntimeError("bug")
        s = WumpusSessions(self.tmp.name, engine=Boom, clock=self.clock)
        s.handle("!aaaa", "/wumpus")
        out = "\n".join(s.handle("!aaaa", "m 5"))
        self.assertIn("error", out)
        self.assertFalse(s.in_session("!aaaa"))                    # never stuck "in game"

    def test_a_corrupt_save_is_set_aside_and_a_new_game_starts(self):
        with open(os.path.join(self.tmp.name, "_aaaa.json"), "w") as fh:
            fh.write('{"v": 1, "state": {"you": 99}}')
        out = self.say("!aaaa", "/wumpus")
        self.assertIn("unreadable", out)
        self.assertIn("Cave 1.", out)
        self.assertTrue(self.s.in_session("!aaaa"))


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
            wumpus_save_dir=os.path.join(self.tmp.name, "w"),
            lat=0, lon=0, near_km=2, announce=None, announce_channel=0)
        self.bridge = self.mb.Bridge(cfg)
        self.bridge.my_id = "!me"
        self.bridge.wumpus._engine = FixedEngine
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

    def test_the_command_and_its_help_exist(self):
        self.assertIn("wumpus", self.mb.COMMANDS)
        self.assertIn("Wumpus", " ".join(self.msg("!aaaa", "/help wumpus")))

    def test_dm_session_end_to_end(self):
        self.assertIn("Cave 1", " ".join(self.msg("!aaaa", "/wumpus")))
        self.assertIn("Cave 5", " ".join(self.msg("!aaaa", "m 5")))
        self.assertNotIn("pong", " ".join(self.msg("!aaaa", "/ping")))     # typed into the game
        self.assertIn("saved", " ".join(self.msg("!aaaa", "/quit")))
        self.assertTrue(self.msg("!aaaa", "/ping")[0].startswith("pong"))  # normal again
        self.assertIn("Resuming", " ".join(self.msg("!aaaa", "/wumpus")))

    def test_two_nodes_at_once(self):
        self.msg("!aaaa", "/wumpus")
        self.msg("!bbbb", "/wumpus")
        self.msg("!aaaa", "m 5")
        self.assertIn("Cave 5", " ".join(self.msg("!aaaa", "look")))
        self.assertIn("Cave 1", " ".join(self.msg("!bbbb", "look")))

    def test_public_messages_are_not_game_input(self):
        self.msg("!aaaa", "/wumpus")
        sent = len(self.sent)
        self.bridge.on_receive({"decoded": {"text": "m 5"}, "fromId": "!aaaa",
                                "toId": "^all"}, None)
        time.sleep(0.2)
        self.assertEqual(len(self.sent), sent)
        self.assertIn("Cave 1", " ".join(self.msg("!aaaa", "look")))

    def test_one_game_at_a_time_in_both_directions(self):
        self.msg("!aaaa", "/wumpus")
        for other in ("zork", "adventure", "trek"):
            self.assertIn("You're in Wumpus", " ".join(self.msg("!aaaa", "/" + other)))
            self.assertIn("You're in Wumpus", " ".join(self.msg("!aaaa", "/" + other, to="^all")))
            self.assertFalse(getattr(self.bridge, other).in_session("!aaaa"))
        self.msg("!aaaa", "/quit")
        self.msg("!aaaa", "/trek")
        self.assertIn("You're in Trek. Send /quit to leave, then /wumpus.",
                      " ".join(self.msg("!aaaa", "/wumpus")))
        self.assertFalse(self.bridge.wumpus.in_session("!aaaa"))
        self.msg("!aaaa", "/quit")
        self.msg("!aaaa", "/zork")
        self.assertIn("You're in Zork. Send /quit to leave, then /wumpus.",
                      " ".join(self.msg("!aaaa", "/wumpus")))
        self.assertFalse(self.bridge.wumpus.in_session("!aaaa"))


if __name__ == "__main__":
    unittest.main()
