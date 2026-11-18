#!/usr/bin/env python3
"""Check the Python port against the original C program.

Builds the C game (needs the C sources and `python -m ziglang`), then plays the
same scripted games on both with the same random number generator and compares
every character of output.

Usage:  python tools/crosscheck.py SRC_DIR [--games N] [--moves N] [--seed N] [--record DIR]
"""

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from zork.dungeon import DungeonEngine          # noqa: E402
from zork.rt import lcg_rand                    # noqa: E402
from zork.testkit import apply_scenario        # noqa: E402

REFRAND = """
static unsigned long s = 1;
int ref_rand(void) { s = (s * 1103515245UL + 12345UL) & 0x7fffffffUL; return (int)((s >> 16) & 0x7fff); }
void ref_srand(unsigned int seed) { s = seed & 0x7fffffffUL; }
"""
SCENARIO_C = r"""
#include <stdlib.h>
#include <string.h>
#include "funcs.h"
#include "vars.h"
/* ZORK_SCENARIO="H=room;G=o,o,..;B=obj:mask1:mask2,..;F=idx:val,.." (test set-up) */
void scenario_(void)
{
    char *env = getenv("ZORK_SCENARIO"), *buf, *part, *item;
    char *sp1, *sp2;
    if (!env) return;
    buf = strdup(env);
    for (part = strtok_r(buf, ";", &sp1); part; part = strtok_r(NULL, ";", &sp1)) {
        char kind = part[0];
        for (item = strtok_r(part + 2, ",", &sp2); item; item = strtok_r(NULL, ",", &sp2)) {
            int a = 0, b = 0, c = 0;
            sscanf(item, "%d:%d:%d", &a, &b, &c);
            if (kind == 'H') {
                play_1.here = a; advs_1.aroom[aindex_1.player - 1] = a;
            } else if (kind == 'G') {
                objcts_1.oroom[a - 1] = 0; objcts_1.oadv[a - 1] = aindex_1.player;
                objcts_1.ocan[a - 1] = 0;
            } else if (kind == 'B') {
                objcts_1.oflag1[a - 1] |= b; objcts_1.oflag2[a - 1] |= c;
            } else if (kind == 'F') {
                flags[a] = b;
            }
        }
    }
}
"""
FILES = ("actors ballop clockr demons dgame dinit dmain dso1 dso2 dso3 dso4 dso5 dso6 dso7 "
         "dsub dverb1 dverb2 lightp local nobjs np np1 np2 np3 nrooms objcts rooms sobjs "
         "supp sverbs verbs villns").split()

VERBS = ("take drop open close look examine read inventory push pull turn light extinguish "
         "attack kill throw put give eat drink climb enter exit wait search move lift lower "
         "break burn fill pour shake touch wave wear ring tie untie unlock lock unlock "
         "say yell jump swim pray hello diagnose score brief superbrief verbose").split()
DIRS = "n s e w u d ne nw se sw north south east west up down in out".split()
NOUNS = ("mailbox leaflet window door lamp sword knife rope bottle water sack garlic "
         "trap-door rug chest egg bag canary bell book candle matches coin skull torch "
         "boat tree grating key axe troll thief cyclops all").split()
PREPS = "with to in on at from".split()

OPENINGS = [
    ["open mailbox", "take leaflet", "n", "e", "open window", "enter", "w", "take lamp",
     "take sword", "move rug", "open trap-door", "turn on lamp", "d", "n"],
    ["open mailbox", "take leaflet", "n", "e", "open window", "enter", "take sack",
     "take bottle", "open sack", "take garlic", "w", "take lamp", "take sword", "move rug",
     "open trap-door", "turn on lamp", "d", "s", "e"],
    ["n", "n", "n", "climb tree", "take egg", "d", "s", "s", "e", "open window", "enter",
     "w", "take lamp", "take sword", "move rug", "open trap-door", "turn on lamp", "d"],
    ["n", "e", "open window", "enter", "e", "u", "take lamp", "take knife", "d", "w",
     "move rug", "open trap-door", "turn on lamp", "d", "n", "kill troll with sword",
     "kill troll with sword", "kill troll with sword", "n"],
]


def make_script(rng, moves):
    lines = list(rng.choice(OPENINGS))
    while len(lines) < moves:
        r = rng.random()
        if r < 0.55:
            lines.append(rng.choice(DIRS))
        elif r < 0.7:
            lines.append("%s %s" % (rng.choice(VERBS), rng.choice(NOUNS)))
        elif r < 0.8:
            lines.append("%s %s %s %s" % (rng.choice(VERBS), rng.choice(NOUNS),
                                          rng.choice(PREPS), rng.choice(NOUNS)))
        elif r < 0.9:
            lines.append(rng.choice(["take all", "look", "inventory", "open door", "wait",
                                     "turn on lamp", "examine room", "take lamp"]))
        else:
            lines.append(rng.choice(VERBS))
    return lines[:moves]


def build_reference(src, out_dir):
    work = os.path.join(out_dir, "src")
    os.makedirs(work, exist_ok=True)
    for f in os.listdir(src):
        if f.endswith((".c", ".h", ".dat")):
            with open(os.path.join(src, f), "rb") as a, open(os.path.join(work, f), "wb") as b:
                b.write(a.read())
    with open(os.path.join(work, "refrand.c"), "w") as fh:
        fh.write(REFRAND)
    with open(os.path.join(work, "scenario.c"), "w") as fh:
        fh.write(SCENARIO_C)
    dm = os.path.join(work, "dmain.c")
    with open(dm) as fh:
        text = fh.read()
    text = text.replace("if (init_()) {", "if (init_()) {\n\tscenario_();")
    text = text.replace("void main(", "void scenario_(void);\nvoid main(")
    with open(dm, "w") as fh:
        fh.write(text)
    exe = os.path.join(out_dir, "ref_dungeon.exe")
    cmd = [sys.executable, "-m", "ziglang", "cc", "-std=gnu89", "-w", "-DMORE_NONE",
           "-Drand=ref_rand", '-DTEXTFILE="dtextc.dat"', "-o", exe,
           *[os.path.join(work, f + ".c") for f in FILES], os.path.join(work, "refrand.c"),
           os.path.join(work, "scenario.c")]
    subprocess.run(cmd, check=True)
    return exe, work


def run_c(exe, work, lines, scenario=""):
    env = dict(os.environ, ZORK_SCENARIO=scenario) if scenario else None
    res = subprocess.run([exe], input="\n".join(lines) + "\n", capture_output=True,
                         text=True, cwd=work, timeout=60, env=env)
    return res.stdout.replace("\r", "")


def make_scenario(rng):
    from zork import engine_gen as E
    room = rng.randrange(1, 191)
    objs = rng.sample(range(1, 220), rng.randrange(0, 25))
    lamp = E.oindex_.lamp
    parts = ["H=%d" % room]
    parts.append("G=" + ",".join(str(o) for o in objs + [lamp]))
    parts.append("B=%d:%d:0" % (lamp, 65))          # lamp lit (ONBT|LITEBT)
    if rng.random() < 0.5:
        parts.append("F=" + ",".join("%d:1" % rng.randrange(0, 46)
                                     for _ in range(rng.randrange(1, 6))))
    return ";".join(parts)


def run_py(lines, scenario=""):
    eng = DungeonEngine.new(
        rand=lcg_rand(1),
        setup=(lambda g: apply_scenario(g, scenario)) if scenario else None)
    out = [eng.intro()]
    fed = 0
    for ln in lines:
        if eng.finished:
            break
        out.append(eng.command(ln))
        fed += 1
    if not eng.finished:
        eng.close()
    return "".join(out).replace("\r", ""), fed


def first_diff(a, b):
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n if len(a) != len(b) else -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--games", type=int, default=20)
    ap.add_argument("--moves", type=int, default=250)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--scenarios", action="store_true",
                    help="start each game from a random room with random inventory/flags")
    ap.add_argument("--record", default=None,
                    help="write the C program's transcripts here as golden files")
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="crosscheck_")
    exe, work = build_reference(a.src, tmp)
    rng = random.Random(a.seed)
    bad = 0
    for n in range(a.games):
        lines = make_script(rng, a.moves)
        scen = make_scenario(rng) if a.scenarios else ""
        if scen:
            lines = lines[len(OPENINGS[0]):]    # the openings assume the starting house
        c_out = run_c(exe, work, lines, scen)
        p_out, fed = run_py(lines, scen)
        i = first_diff(c_out, p_out)
        if i < 0:
            print("game %2d: identical (%d chars, %d moves fed)" % (n, len(c_out), fed))
            if a.record:
                os.makedirs(a.record, exist_ok=True)
                with open(os.path.join(a.record, "game%02d.json" % n), "w") as fh:
                    json.dump({"scenario": scen, "lines": lines[:fed], "output": c_out}, fh)
        else:
            bad += 1
            print("game %2d: DIFFERS at char %d  scenario=%s" % (n, i, scen))
            print("   C : %r" % c_out[max(0, i - 80):i + 80])
            print("   Py: %r" % p_out[max(0, i - 80):i + 80])
    print("%d of %d games differ" % (bad, a.games))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
