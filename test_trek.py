"""Tests for trek/engine.py (run: python -m unittest test_trek -v)."""

import json
import os
import random
import re
import sys
import tempfile
import unittest

import trek
from trek import Game, TrekGames, course_vector, direction


def quiet_rng(seed=1, roll=0.9):
    """A Random whose random() always returns `roll` (no random damage etc.)."""
    r = random.Random(seed)
    r.random = lambda: roll
    return r


def clear_quadrant(g, keep_stars=False):
    """Empty the current quadrant so tests can place things by hand."""
    g.grid = [["."] * 8 for _ in range(8)]
    g.kl, g.base = [], None
    g.grid[int(g.s1) - 1][int(g.s2) - 1] = "E"
    v = g.galaxy[g.q1 - 1][g.q2 - 1]
    g.galaxy[g.q1 - 1][g.q2 - 1] = v % 10 if keep_stars else 0
    resync(g)


def resync(g):
    """Recount Klingons/bases after a test has edited the galaxy by hand, and make sure
    there is at least one spare Klingon and base elsewhere so a kill does not end the game."""
    cur = (g.q1 - 1, g.q2 - 1)
    spare = [(i, j) for i in range(8) for j in range(8) if (i, j) != cur]
    if sum(g.galaxy[i][j] // 100 for i, j in spare) == 0:
        i, j = spare[0]
        g.galaxy[i][j] += 100
    if sum((g.galaxy[i][j] // 10) % 10 for i, j in spare) == 0:
        i, j = spare[1]
        g.galaxy[i][j] += 10
    g.k9 = sum(v // 100 for row in g.galaxy for v in row)
    g.b9 = sum((v // 10) % 10 for row in g.galaxy for v in row)
    g.k7 = max(g.k7, g.k9)


def put(g, ch, r, c):
    g.grid[r - 1][c - 1] = ch


def add_klingon(g, r, c, energy=200.0):
    put(g, "K", r, c)
    g.kl.append([r, c, energy])
    g._update_galaxy()
    resync(g)


def add_base(g, r, c):
    put(g, "B", r, c)
    g.base = [r, c]
    g._update_galaxy()
    resync(g)


def set_ship(g, q1, q2, r, c):
    g.q1, g.q2, g.s1, g.s2 = q1, q2, float(r), float(c)
    g._enter_quadrant()
    clear_quadrant(g)


class CourseTests(unittest.TestCase):
    def test_vectors(self):
        self.assertEqual(course_vector(1), (0, 1))     # right
        self.assertEqual(course_vector(3), (-1, 0))    # up
        self.assertEqual(course_vector(5), (0, -1))    # left
        self.assertEqual(course_vector(7), (1, 0))     # down
        self.assertEqual(course_vector(1.5), (-0.5, 1.0))

    def test_every_course_in_range_works(self):
        # The B4X version rounded the course, so 8.5 to 8.99 ran off the end of its table.
        c = 1.0
        while c < 9:
            v = course_vector(c)
            self.assertAlmostEqual(max(abs(v[0]), abs(v[1])), 1.0)
            back = direction(*v)
            if c > 8.999:            # course 9 is the same heading as course 1
                back = back if back > 8 else back + 8
            self.assertAlmostEqual(back, c, places=6)
            c += 0.05

    def test_fractional_course_is_between_neighbours(self):
        # 1.6 must lean towards course 2 (up-right) from course 1 (right).
        dr, dc = course_vector(1.6)
        self.assertAlmostEqual(dr, -0.6)
        self.assertAlmostEqual(dc, 1.0)

    def test_direction_of_known_targets(self):
        self.assertEqual(direction(0, 5), 1.0)
        self.assertEqual(direction(-3, 0), 3.0)
        self.assertEqual(direction(0, -2), 5.0)
        self.assertEqual(direction(4, 0), 7.0)
        self.assertEqual(direction(-2, 2), 2.0)
        self.assertIsNone(direction(0, 0))


class GenerationTests(unittest.TestCase):
    def test_galaxy_statistics(self):
        k_hist = {0: 0, 1: 0, 2: 0, 3: 0}
        stars = set()
        bases = 0
        cells = 0
        edge_objects = 0
        for seed in range(150):
            g = Game.new(random.Random(seed))
            self.assertEqual(g.k9, sum(v // 100 for row in g.galaxy for v in row))
            self.assertEqual(g.b9, sum((v // 10) % 10 for row in g.galaxy for v in row))
            self.assertGreaterEqual(g.k9, 1)
            self.assertGreaterEqual(g.b9, 1)
            self.assertGreater(g.t9, g.k9 - 1)
            for row in g.galaxy:
                for v in row:
                    k_hist[v // 100] += 1
                    stars.add(v % 10)
                    bases += (v // 10) % 10
                    cells += 1
        self.assertIn(3, k_hist and [k for k in k_hist if k_hist[k]])   # 3-Klingon quadrants exist
        self.assertEqual(stars, set(range(1, 9)))                        # 1..8 stars, incl. 8
        self.assertTrue(0.02 < bases / cells < 0.07)                     # about 4% have a base
        self.assertTrue(0.12 < k_hist[1] / cells < 0.19)                 # about 15% have one Klingon

    def test_row_and_column_8_are_used(self):
        seen_r8 = seen_c8 = seen_q8 = seen_qc8 = seen_ship8 = False
        for seed in range(200):
            g = Game.new(random.Random(seed))
            for r in range(8):
                for c in range(8):
                    if g.grid[r][c] != "." and r == 7:
                        seen_r8 = True
                    if g.grid[r][c] != "." and c == 7:
                        seen_c8 = True
            seen_q8 |= g.q1 == 8
            seen_qc8 |= g.q2 == 8
            seen_ship8 |= g.s1 == 8 or g.s2 == 8
        self.assertTrue(seen_r8 and seen_c8 and seen_q8 and seen_qc8 and seen_ship8)

    def test_one_enterprise_and_counts_match(self):
        for seed in range(50):
            g = Game.new(random.Random(seed))
            check_invariants(self, g)


class CombatTests(unittest.TestCase):
    def setUp(self):
        self.g = Game.new(quiet_rng())
        set_ship(self.g, 4, 4, 4, 2)

    def test_torpedo_kill_removes_klingon_for_good(self):
        g = self.g
        add_klingon(g, 4, 6)
        k_before = g.k9
        g.do_tor(["1"])
        self.assertEqual(g.k9, k_before - 1)
        self.assertEqual(g.kl, [])
        self.assertEqual(g.grid[3][5], ".")
        self.assertEqual(g.galaxy[3][3] // 100, 0)
        # A dead Klingon must not come back, move or shoot.
        shields = g.shields = 1000
        g.do_nav(["5", "0.1"])
        self.assertEqual(g.kl, [])
        self.assertEqual(sum(row.count("K") for row in g.grid), 0)
        self.assertEqual(g.shields, shields)

    def test_torpedo_kills_only_the_one_hit(self):
        g = self.g
        add_klingon(g, 4, 6)
        add_klingon(g, 7, 7)
        before = g.k9
        g.do_tor(["1"])
        self.assertEqual([(k[0], k[1]) for k in g.kl], [(7, 7)])
        self.assertEqual(g.k9, before - 1)

    def test_torpedo_destroys_starbase(self):
        g = self.g
        add_base(g, 4, 5)
        b_before = g.b9
        g.do_tor(["1"])
        self.assertIsNone(g.base)
        self.assertEqual(g.b9, b_before - 1)
        self.assertEqual((g.galaxy[3][3] // 10) % 10, 0)
        self.assertEqual(g.grid[3][4], ".")

    def test_torpedo_absorbed_by_star(self):
        g = self.g
        put(g, "*", 4, 5)
        msgs = "\n".join(g.do_tor(["1"]))
        self.assertIn("absorbed", msgs)
        self.assertEqual(g.grid[3][4], "*")

    def test_torpedo_miss_costs_a_torpedo_and_energy(self):
        g = self.g
        e, p = g.energy, g.torps
        msgs = "\n".join(g.do_tor(["5"]))
        self.assertIn("missed", msgs)
        self.assertEqual((g.energy, g.torps), (e - 2, p - 1))

    def test_bad_torpedo_course_costs_nothing(self):
        g = self.g
        e, p = g.energy, g.torps
        for bad in ("0", "0.5", "9.5", "abc", "-1"):
            g.do_tor([bad])
        self.assertEqual((g.energy, g.torps), (e, p))

    def test_phaser_damage_falls_with_distance(self):
        near, far = [], []
        for pos, bucket in (((4, 3), near), ((4, 8), far)):
            g = Game.new(quiet_rng())
            set_ship(g, 4, 4, 4, 2)
            add_klingon(g, pos[0], pos[1], energy=500.0)
            g.shields = 100000
            msgs = "\n".join(g.do_pha(["300"]))
            bucket.append(int(re.search(r"(\d+) hit on Klingon", msgs).group(1)))
        self.assertGreater(near[0], far[0])        # B4X used a fixed distance: no difference

    def test_phaser_split_between_klingons(self):
        g = self.g
        add_klingon(g, 4, 3, 10000.0)
        add_klingon(g, 4, 4, 10000.0)
        g.shields = 100000
        g.do_pha(["400"])
        for k in g.kl:
            self.assertLess(k[2], 10000.0)

    def test_phaser_kill_and_win(self):
        g = self.g
        for i in range(8):
            for j in range(8):
                g.galaxy[i][j] = g.galaxy[i][j] % 100
        g.galaxy[3][3] = g.galaxy[3][3] % 10
        g.k9 = g.k7 = 0
        add_klingon(g, 4, 3, 5.0)
        g.k9 = g.k7 = 1
        msgs = "\n".join(g.do_pha(["1000"]))
        self.assertEqual(g.over, "won")
        self.assertIn("Congratulations", msgs)

    def test_phasers_need_energy_and_targets(self):
        g = self.g
        self.assertIn("no enemy", "\n".join(g.do_pha(["100"])))
        add_klingon(g, 4, 3)
        e = g.energy
        g.do_pha(["999999"])
        self.assertEqual(g.energy, e)

    def test_klingon_attack_formula(self):
        g = self.g
        add_klingon(g, 4, 4, 300.0)          # distance 2 from (4,2)
        g.shields = 1000
        g._klingons_shoot()
        # int(300 / 2 * (2 + 0.9)) = 435
        self.assertEqual(g.shields, 1000 - 435)

    def test_docked_ship_is_protected(self):
        g = self.g
        add_base(g, 5, 2)
        add_klingon(g, 8, 8, 300.0)
        g.do_srs([])
        self.assertTrue(g.docked)
        self.assertEqual(g.energy, trek.E0)
        shields = g.shields
        g._klingons_shoot()
        self.assertEqual(g.shields, shields)

    def test_destroying_the_last_starbase_ends_the_game(self):
        g = self.g
        add_base(g, 4, 5)
        g.b9 = 1                 # pretend it is the last one
        g.do_tor(["1"])
        self.assertEqual(g.over, "lost")


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.g = Game.new(quiet_rng())

    def test_moving_inside_a_quadrant(self):
        g = self.g
        set_ship(g, 4, 4, 4, 2)
        t, e = g.t, g.energy
        g.do_nav(["1", "0.5"])            # 4 sectors right
        self.assertEqual((int(g.s1), int(g.s2)), (4, 6))
        self.assertEqual(g.energy, e - 4 - 10)
        self.assertAlmostEqual(g.t, t + 0.5)
        self.assertEqual(sum(row.count("E") for row in g.grid), 1)

    def test_quadrant_crossing_charges_energy_and_time_once(self):
        g = self.g
        set_ship(g, 5, 2, 4, 2)
        t, e = g.t, g.energy
        g.do_nav(["3", "1"])              # 8 sectors up: into quadrant 4,2
        self.assertEqual((g.q1, g.q2), (4, 2))
        self.assertEqual((int(g.s1), int(g.s2)), (4, 2))
        self.assertEqual(e - g.energy, 8 + 10)       # B4X charged this twice
        self.assertAlmostEqual(g.t - t, 1.0)         # and added the extra day twice

    def test_crossing_into_each_neighbour(self):
        for course, dq in ((1, (0, 1)), (3, (-1, 0)), (5, (0, -1)), (7, (1, 0))):
            g = Game.new(quiet_rng())
            set_ship(g, 4, 4, 4, 4)
            g.do_nav([str(course), "1"])
            self.assertEqual((g.q1, g.q2), (4 + dq[0], 4 + dq[1]), course)
            check_invariants(self, g)

    def test_exact_edge_landing_stays_put(self):
        g = self.g
        set_ship(g, 4, 4, 4, 4)
        g.do_nav(["1", "0.5"])            # 4 right: lands on column 8, still quadrant 4,4
        self.assertEqual((g.q1, g.q2, int(g.s2)), (4, 4, 8))

    def test_galactic_perimeter(self):
        g = self.g
        set_ship(g, 1, 1, 1, 1)
        msgs = "\n".join(g.do_nav(["3", "1"]))
        self.assertEqual((g.q1, g.q2, int(g.s1), int(g.s2)), (1, 1, 1, 1))
        self.assertIn("perimeter", msgs)
        set_ship(g, 8, 8, 8, 8)
        g.do_nav(["7", "2"])
        self.assertEqual((g.q1, g.q2), (8, 8))
        check_invariants(self, g)

    def test_perimeter_stop_never_lands_on_something(self):
        # The original dropped the ship on the clamped corner even if a star was there.
        g = self.g
        set_ship(g, 1, 8, 1, 5)
        put(g, "*", 1, 3)
        stars = sum(row.count("*") for row in g.grid)
        g.do_nav(["4.49", "0.19"])
        self.assertEqual(sum(row.count("*") for row in g.grid), stars)
        self.assertEqual(g.grid[0][2], "*")
        self.assertEqual(sum(row.count("E") for row in g.grid), 1)
        self.assertEqual((g.q1, g.q2), (1, 8))

    def test_obstacle_stops_the_ship(self):
        g = self.g
        set_ship(g, 4, 4, 4, 2)
        put(g, "*", 4, 5)
        msgs = "\n".join(g.do_nav(["1", "0.5"]))
        self.assertIn("bad navigation", msgs)
        self.assertEqual((int(g.s1), int(g.s2)), (4, 4))

    def test_fractional_courses_do_not_crash(self):
        for course in ("1.5", "2.25", "8.5", "8.75", "8.99", "9", "1"):
            g = Game.new(quiet_rng())
            set_ship(g, 4, 4, 4, 4)
            g.do_nav([course, "0.3"])
            check_invariants(self, g)

    def test_bad_input_changes_nothing(self):
        g = self.g
        set_ship(g, 4, 4, 4, 2)
        before = (g.t, g.energy, g.s1, g.s2)
        for args in ([], ["3"], ["x", "1"], ["0", "1"], ["9.5", "1"], ["3", "0"], ["3", "9"],
                     ["3", "-1"], ["nan", "1"], ["inf", "1"]):
            g.do_nav(args)
        self.assertEqual((g.t, g.energy, g.s1, g.s2), before)

    def test_damaged_engines_limit_warp(self):
        g = self.g
        set_ship(g, 4, 4, 4, 2)
        g.dmg[1] = -1.0
        msgs = "\n".join(g.do_nav(["1", "1"]))
        self.assertIn("damaged", msgs)
        self.assertEqual(int(g.s2), 2)
        g.do_nav(["1", "0.2"])
        self.assertEqual(int(g.s2), 4)     # warp 0.2 = int(0.2 * 8 + 0.5) = 2 sectors

    def test_out_of_time_ends_the_game(self):
        g = self.g
        set_ship(g, 4, 4, 4, 2)
        g.t = g.t0 + g.t9 + 0.5
        g.do_nav(["1", "0.1"])
        self.assertEqual(g.over, "lost")

    def test_not_enough_energy(self):
        g = self.g
        set_ship(g, 4, 4, 4, 2)
        g.energy = 5
        msgs = "\n".join(g.do_nav(["1", "1"]))
        self.assertIn("insufficient", msgs)

    def test_shields_make_up_a_shortfall(self):
        g = self.g
        set_ship(g, 4, 4, 4, 2)
        g.energy, g.shields = 12, 500
        g.do_nav(["1", "0.125"])          # n = 1, cost 11: fits in energy
        self.assertEqual(g.energy, 1)
        g.do_nav(["1", "0.125"])          # needs 11, only 1: shields pay the difference
        self.assertEqual(g.energy, 0)
        self.assertEqual(g.shields, 500 - 10)


class SystemsTests(unittest.TestCase):
    def setUp(self):
        self.g = Game.new(quiet_rng())
        set_ship(self.g, 4, 4, 4, 2)

    def test_repair_takes_time_once(self):
        g = self.g
        add_base(g, 5, 2)
        g.dmg[1] = g.dmg[3] = g.dmg[5] = -2.0
        g.do_srs([])
        self.assertTrue(g.docked)
        msgs = "\n".join(g.do_dam([]))
        self.assertIn("Repairs need", msgs)
        t = g.t
        cost = g.repair_cost
        g.do_dam(["y"])
        self.assertAlmostEqual(g.t - t, cost + 0.1)      # B4X added this 8 times
        self.assertTrue(all(d >= 0 for d in g.dmg[1:]))

    def test_dock_hints_at_repairs_once(self):
        g = self.g
        add_base(g, 5, 2)
        g.dmg[3] = -1.0
        msgs = "\n".join(g.do_srs([]))
        self.assertIn("dam to repair", msgs)
        self.assertLess(g.dmg[3], 0)                     # not repaired for free
        self.assertNotIn("dam to repair", "\n".join(g.do_srs([])))

    def test_dock_without_damage_has_no_hint(self):
        g = self.g
        add_base(g, 5, 2)
        self.assertNotIn("dam to repair", "\n".join(g.do_srs([])))

    def test_no_repairs_away_from_a_starbase(self):
        g = self.g
        g.dmg[2] = -1.0
        msgs = "\n".join(g.do_dam(["y"]))
        self.assertNotIn("Repairs", msgs)
        self.assertLess(g.dmg[2], 0)

    def test_shield_control(self):
        g = self.g
        g.do_shi(["500"])
        self.assertEqual((g.shields, g.energy), (500, 2500))
        g.do_shi(["0"])
        self.assertEqual((g.shields, g.energy), (0, 3000))
        msgs = "\n".join(g.do_shi(["99999"]))
        self.assertIn("Treasury", msgs)
        g.do_shi(["-5"])
        self.assertEqual(g.shields, 0)
        g.do_shi(["3000"])                  # everything is allowed
        self.assertEqual((g.shields, g.energy), (3000, 0))

    def test_damaged_devices_refuse(self):
        g = self.g
        for dev, call, word in ((2, g.do_srs, "OUT"), (3, g.do_lrs, "inoperable"),
                                (4, g.do_pha, "inoperative"), (5, g.do_tor, "not operational"),
                                (7, g.do_shi, "inoperable"), (8, g.do_cmp, "inoperable")):
            g.dmg[dev] = -1.0
            add_klingon(g, 8, 8) if dev == 4 and not g.kl else None
            self.assertIn(word, "\n".join(call(["1"])), dev)
            g.dmg[dev] = 0.0

    def test_computer_outputs(self):
        g = self.g
        add_klingon(g, 4, 5)
        add_base(g, 8, 8)
        out = "\n".join(g.do_cmp(["t"]))
        self.assertIn("course 1.000 dist 3.0", out)
        out = "\n".join(g.do_cmp(["b"]))
        self.assertIn("Base 8,8", out)
        out = "\n".join(g.do_cmp(["s"]))
        self.assertIn("Klingons left: %d" % g.k9, out)
        for msg in g.do_cmp(["g"]):
            self.assertLessEqual(len(msg.encode()), trek.MSG_LIMIT)
        self.assertIn("course 8.000 dist 4.0", "\n".join(g.do_cmp(["d", "1", "1", "5", "5"])))
        self.assertIn("course 3.000", "\n".join(g.do_cmp(["d", "5", "5", "1", "5"])))

    def test_dist_calc_follows_navigation(self):
        # Whatever course /cmp reports must lead to the target when you navigate it.
        g = self.g
        for target in ((1, 1), (8, 8), (2, 7), (7, 3), (4, 8), (1, 5)):
            set_ship(g, 4, 4, 4, 2)
            dr, dc = target[0] - 4, target[1] - 2
            crs = direction(dr, dc)
            dist = max(abs(dr), abs(dc))
            g.dmg = [0.0] * 9
            g.do_nav(["%.3f" % crs, "%.2f" % (dist / 8.0)])
            self.assertEqual((int(g.q1), int(g.q2)), (4, 4), target)
            self.assertEqual((int(g.s1), int(g.s2)), target, target)

    def test_lrs_updates_the_galactic_record(self):
        g = self.g
        g.known = [[0] * 8 for _ in range(8)]
        g.do_lrs([])
        self.assertTrue(all(g.known[i][j] == g.galaxy[i][j]
                            for i in range(2, 5) for j in range(2, 5)))
        self.assertEqual(g.known[0][0], 0)

    def test_stranded_ship_is_lost(self):
        g = self.g
        g.energy, g.shields = 5, 3
        g._check_stranded()
        self.assertEqual(g.over, "lost")


class MessageTests(unittest.TestCase):
    def test_every_message_fits(self):
        for seed in range(40):
            g = Game.new(random.Random(seed))
            for m in g.orders():
                self.assertLessEqual(len(m.encode()), trek.MSG_LIMIT)
            for m in trek.HELP:
                self.assertLessEqual(len(m.encode()), trek.MSG_LIMIT)

    def test_pack(self):
        lines = ["x" * 100, "y" * 100, "z" * 10]
        msgs = trek.pack(lines, 150)
        self.assertEqual(msgs, ["x" * 100, "y" * 100 + "\n" + "z" * 10])
        self.assertEqual(trek.pack(["a" * 500], 50), ["a" * 50])


class PersistenceTests(unittest.TestCase):
    def test_round_trip(self):
        g = Game.new(random.Random(3))
        g.do_nav(["1", "0.5"])
        d = g.to_dict()
        json.dumps(d)
        g2 = Game.from_dict(d)
        self.assertEqual(g2.to_dict(), d)

    def test_games_survive_restart_and_are_per_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "games.json")
            games = TrekGames(path, random.Random(5))
            a1 = games.command("!aaaa", "trek", "")
            games.command("!bbbb", "trek", "")
            games.command("!aaaa", "nav", "1 0.3")
            state_a = games.games["!aaaa"].to_dict()
            games2 = TrekGames(path, random.Random(6))
            self.assertEqual(games2.games["!aaaa"].to_dict(), state_a)
            self.assertIn("!bbbb", games2.games)
            self.assertNotEqual(games2.games["!aaaa"].to_dict(), games2.games["!bbbb"].to_dict())
            self.assertIn("Mission in progress", "\n".join(games2.command("!aaaa", "trek", "")))
            self.assertTrue(a1)

    def test_commands_without_a_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            games = TrekGames(os.path.join(tmp, "g.json"))
            self.assertIn("/trek", games.command("!x", "srs", "")[0])

    def test_corrupt_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "g.json")
            with open(path, "w") as fh:
                fh.write("{not json")
            self.assertEqual(TrekGames(path).games, {})

    def test_failed_command_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            games = TrekGames(os.path.join(tmp, "g.json"), random.Random(2))
            games.command("!x", "trek", "")
            before = games.games["!x"].to_dict()

            def boom(args):
                games.games["!x"].energy = -999
                raise RuntimeError("boom")
            games.games["!x"].do_srs = boom
            with self.assertRaises(RuntimeError):
                games.command("!x", "srs", "")
            self.assertEqual(games.games["!x"].to_dict(), before)

    def test_game_over_then_new_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            games = TrekGames(os.path.join(tmp, "g.json"), random.Random(2))
            games.command("!x", "trek", "")
            games.games["!x"].over = "lost"
            self.assertIn("Game over", games.command("!x", "nav", "1 1")[0])
            msgs = games.command("!x", "trek", "")
            self.assertIn("Destroy", msgs[0])
            self.assertIsNone(games.games["!x"].over)

    def test_trek_new_restarts_midgame(self):
        with tempfile.TemporaryDirectory() as tmp:
            games = TrekGames(os.path.join(tmp, "g.json"), random.Random(2))
            games.command("!x", "trek", "")
            first = games.games["!x"]
            games.command("!x", "trek", "new")
            self.assertIsNot(games.games["!x"], first)


def check_invariants(tc, g):
    tc.assertEqual(g.k9, sum(v // 100 for row in g.galaxy for v in row))
    tc.assertEqual(g.b9, sum((v // 10) % 10 for row in g.galaxy for v in row))
    tc.assertGreaterEqual(g.energy, 0)
    tc.assertGreaterEqual(g.shields, 0)
    tc.assertTrue(0 <= g.torps <= trek.P0)
    tc.assertTrue(1 <= g.q1 <= 8 and 1 <= g.q2 <= 8)
    tc.assertTrue(1 <= g.s1 < 9 and 1 <= g.s2 < 9)
    flat = [ch for row in g.grid for ch in row]
    tc.assertEqual(flat.count("E"), 1)
    tc.assertEqual(g.grid[int(g.s1) - 1][int(g.s2) - 1], "E")
    tc.assertEqual(flat.count("K"), len(g.kl))
    for k in g.kl:
        tc.assertEqual(g.grid[k[0] - 1][k[1] - 1], "K")
        tc.assertGreater(k[2], 0)
    tc.assertEqual(flat.count("B"), 1 if g.base else 0)
    v = g.galaxy[g.q1 - 1][g.q2 - 1]
    tc.assertEqual(v // 100, len(g.kl))
    tc.assertEqual((v // 10) % 10, 1 if g.base else 0)
    tc.assertEqual(v % 10, flat.count("*"))


class FuzzTests(unittest.TestCase):
    VERBS = ["srs", "lrs", "nav", "pha", "tor", "shi", "dam", "cmp"]
    JUNK = ["", "x", "-1", "0", "1", "2.5", "9", "8.99", "100", "1e9", "nan", "inf", "3 1", "7 0.25",
            "1 8", "5 2", "y", "g", "s", "t", "b", "d 1 1 8 8", "d", "300", "99999"]

    def test_random_play(self):
        rng = random.Random(1234)
        finished = {"won": 0, "lost": 0}
        for game_no in range(120):
            g = Game.new(random.Random(game_no))
            for step in range(250):
                if g.over:
                    break
                verb = rng.choice(self.VERBS)
                if verb == "nav":
                    args = [str(round(rng.uniform(1, 8.99), 2)), str(round(rng.uniform(0.1, 3), 2))]
                    if rng.random() < 0.2:
                        args = rng.choice(self.JUNK).split()
                elif verb in ("tor",):
                    args = [str(round(rng.uniform(1, 8.99), 2))] if rng.random() < 0.8 else rng.choice(self.JUNK).split()
                elif verb == "pha":
                    args = [str(rng.choice([10, 100, 500, 1000, 5000]))] if rng.random() < 0.8 else rng.choice(self.JUNK).split()
                else:
                    args = rng.choice(self.JUNK).split()
                if rng.random() < 0.3 and g.kl:
                    verb = rng.choice(["pha", "tor"])
                    args = ["300"] if verb == "pha" else [str(round(rng.uniform(1, 8.99), 2))]
                msgs = getattr(g, "do_" + verb)(args)
                g._check_stranded()
                msgs += g.take()
                for m in msgs:
                    self.assertLessEqual(len(m.encode()), trek.MSG_LIMIT, (verb, args, m))
                if not g.over:
                    check_invariants(self, g)
                    Game.from_dict(g.to_dict())
            if g.over:
                finished[g.over] += 1
        self.assertGreater(finished["won"] + finished["lost"], 0)


class MarkdownSafetyTests(unittest.TestCase):
    """The Meshtastic app treats _ * ` ~ and backslashes as formatting, which eats characters."""
    BAD = set("_*`~\\")

    def test_no_markdown_characters_in_any_reply(self):
        for seed in range(30):
            g = Game.new(random.Random(seed))
            msgs = list(g.orders()) + list(trek.HELP)
            for verb, args in (("lrs", []), ("cmp", ["g"]), ("cmp", ["s"]), ("cmp", ["t"]), ("cmp", ["b"]),
                               ("cmp", ["x"]), ("dam", []), ("shi", []), ("pha", []), ("tor", ["3"]),
                               ("nav", ["3", "1"]), ("nav", ["1", "0.2"]), ("srs", [])):
                msgs += getattr(g, "do_" + verb)(args)
            g.dmg[2] = -1.0
            msgs += g.do_srs([])
            for m in msgs:
                self.assertFalse(self.BAD & set(m), m)
        for text in trek.INFO.values():
            self.assertFalse(self.BAD & set(text), text)

    def test_map_uses_the_display_symbols(self):
        g = Game.new(random.Random(2))
        text = g._srs_text()
        self.assertNotIn(".", text.split("SD")[0])
        self.assertIn(trek.DISPLAY["."], text)


if __name__ == "__main__":
    unittest.main()
