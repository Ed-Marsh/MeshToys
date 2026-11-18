#!/usr/bin/env python3
"""Check the Python port of Colossal Cave against the original C program.

Builds the C game (needs the C sources and `python -m ziglang`), then
  1. dumps the game's data tables as the C compiler sees them and compares them
     with adventure/data.py;
  2. plays the same scripted games on both, with the same random number generator,
     and compares every character of output.

The C program is the troglobit/adventure source, unchanged except for what a test
needs: a stub <err.h>, rand() replaced by the generator the Python engine uses, and a
way to set the starting state from an environment variable (so the dwarves, the
pirate, the closing of the cave and so on can be reached without playing for hours).
Characters are unsigned (as on ARM; the C code stores message numbers up to 199 in a
char). It also carries the bug fixes the Python port makes on purpose (see the top of
adventure/engine.py): room 140 exists, "back" no longer asks for the travel table of a
message or special exit, and "look" next to a dwarf no longer leaves the player at
location 0.

Usage:  python tools/crosscheck_adventure.py SRC_DIR [--games N] [--moves N] [--seed N]
                                              [--record DIR] [--keep DIR]
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from adventure import data as D                      # noqa: E402
from adventure.engine import AdventureEngine, Bug    # noqa: E402
from adventure.testkit import apply_scenario, scenario_text   # noqa: E402

REFRAND = """
static unsigned long s = 1;
int ref_rand(void) { s = (s * 1103515245UL + 12345UL) & 0x7fffffffUL; return (int)((s >> 16) & 0x7fff); }
void ref_srand(unsigned int seed) { s = seed & 0x7fffffffUL; }
"""
ERR_H = """#include <stdio.h>
#include <stdlib.h>
#define err(code, ...) (fprintf(stderr, __VA_ARGS__), exit(code))
#define warn(...) fprintf(stderr, __VA_ARGS__)
"""
SCALARS = ("turns loc oldloc oldloc2 newloc tally tally2 limit lmwarn wzdark closing closed "
           "holding detail knfloc clock1 clock2 panic dflag daltloc dkill chloc chloc2 bonus "
           "numdie object1 gaveup foobar").split()
ARRAYS = "cond place fixed visited prop dloc dseen odloc".split()

SCENARIO_C = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "advent.h"
#include "advdec.h"
/* ADV_SCENARIO="name=value;name[index]=value;..." sets starting state (tests only) */
static struct { const char *name; int *p; int array; } vars[] = {
%s
};
void scenario(void)
{
    char *env = getenv("ADV_SCENARIO"), *buf, *item, *save = NULL;
    if (!env) return;
    buf = strdup(env);
    for (item = strtok_r(buf, ";", &save); item; item = strtok_r(NULL, ";", &save)) {
        char name[32]; int idx = 0, val = 0, i, found = 0;
        if (sscanf(item, "%%31[a-z0-9][%%d]=%%d", name, &idx, &val) == 3) {
            for (i = 0; vars[i].name; i++)
                if (vars[i].array && !strcmp(vars[i].name, name)) { vars[i].p[idx] = val; found = 1; }
        } else if (sscanf(item, "%%31[a-z0-9]=%%d", name, &val) == 2) {
            for (i = 0; vars[i].name; i++)
                if (!vars[i].array && !strcmp(vars[i].name, name)) { *vars[i].p = val; found = 1; }
        }
        if (!found) { fprintf(stderr, "bad scenario item %%s\n", item); exit(3); }
    }
    free(buf);
}
""" % ",\n".join(['    {"%s", &%s, 0}' % (n, n) for n in SCALARS]
                 + ['    {"%s", %s, 1}' % (n, n) for n in ARRAYS] + ["    {0, 0, 0}"])

DUMP_C = r"""
#include <stdio.h>
#include <stdlib.h>
#include "advent.h"
#include "advdec.h"
extern const char *adventtxt1[], *adventtxt2[], *adventtxt3[], *adventtxt4[];
static void esc(const char *tag, int i, const char *s)
{
    printf("%s %d ", tag, i);
    if (!s) { printf("NULL\n"); return; }
    for (; *s; s++) {
        if (*s == '\n') printf("\\n");
        else if (*s == '\\') printf("\\\\");
        else putchar(*s);
    }
    printf("\n");
}
int main(void)
{
    int i;
    initplay();
    for (i = 0; i < 140; i++) { esc("L1", i, adventtxt1[i]); esc("L2", i, adventtxt2[i]); esc("CV", i, cave[i]); }
    for (i = 0; i < 64; i++) esc("L3", i, adventtxt3[i]);
    for (i = 0; i < 201; i++) esc("L4", i, adventtxt4[i]);
    for (i = 0; i < MAXWC; i++) printf("WC %d %s %d\n", i, wc[i].aword, wc[i].acode);
    for (i = 0; i < MAXLOC; i++) printf("IC %d %d\n", i, cond[i]);
    for (i = 0; i < MAXOBJ; i++) printf("IP %d %d %d %d\n", i, place[i], fixed[i], prop[i]);
    for (i = 0; i < 32; i++) printf("AM %d %d\n", i, actmsg[i]);
    for (i = 0; i < DWARFMAX; i++) printf("DL %d %d %d %d\n", i, dloc[i], odloc[i], dseen[i]);
    printf("SC %d %d %d %d %d %d %d %d %d %d %d\n", loc, oldloc, oldloc2, newloc, tally, tally2,
           limit, clock1, clock2, daltloc, chloc);
    printf("SD %d %d %d %d %d %d %d %d\n", chloc2, dflag, holding, closing, closed, numdie, bonus, foobar);
    return 0;
}
"""


def esc(s):
    return "NULL" if s is None else s.replace("\\", "\\\\").replace("\n", "\\n")


def build(src, out):
    work = os.path.join(out, "src")
    os.makedirs(work, exist_ok=True)
    for f in os.listdir(src):
        if f.endswith((".c", ".h")) and f != "advent0.c":
            with open(os.path.join(src, f), "rb") as a, open(os.path.join(work, f), "wb") as b:
                b.write(a.read())

    def put(name, text):
        with open(os.path.join(work, name), "w", newline="\n") as fh:
            fh.write(text)

    put("config.h", "#define BUILTIN 1\n")
    put("err.h", ERR_H)
    put("refrand.c", REFRAND)
    put("scenario.c", SCENARIO_C)
    put("dump.c", DUMP_C)
    # hook the scenario in after the start-up question, just before srand()
    with open(os.path.join(work, "advent.c"), newline="") as fh:
        text = fh.read().replace("\r\n", "\n")
    assert "\tsaveflg = 0;\n\tsrand(511);" in text
    text = text.replace("\tsaveflg = 0;\n\tsrand(511);",
                        "\tsaveflg = 0;\n\tscenario();\n\tsrand(511);")
    text = text.replace("int main(int argc", "void scenario(void);\nint main(int argc")
    put("advent.c", text)
    # The one deliberate difference from the unmodified C source: room 140 exists (see the
    # docstring of adventure/engine.py). gettrav() refused it and cond[]/visited[] had no
    # room for it. Patch the reference the same way so everything else compares like for like.
    for name, old, new in (
            ("database.c", "if (loc <= 0 || loc >= (int)cavesz)", "if (loc <= 0 || loc > (int)cavesz)"),
            ("advdef.h", "int cond[MAXLOC];", "int cond[MAXLOC + 1];"),
            ("advdef.h", "int visited[MAXLOC];", "int visited[MAXLOC + 1];")):
        with open(os.path.join(work, name), newline="") as fh:
            body = fh.read().replace("\r\n", "\n")
        assert old in body, (name, old)
        put(name, body.replace(old, new))
    # goback(): never ask for the travel table of a message or special exit (it was fatal), and
    # remember the exit's index, not a room number (the C code then read past travel[])
    with open(os.path.join(work, "turn.c"), newline="") as fh:
        text = fh.read().replace("\r\n", "\n")
    a = ("\t\t\ttemp = travel[kk].tdest;\n\t\t\tgettrav(temp);\n"
         "\t\t\tif (forced(temp) && travel[0].tdest == want)\n\t\t\t\tk2 = temp;\n"
         "\t\t\tcopytrv(strav, travel);\n")
    b = ("\t\t\ttemp = travel[kk].tdest;\n\t\t\tif (temp > 0 && temp <= (int)cavesz) {\n"
         "\t\t\tgettrav(temp);\n"
         "\t\t\tif (forced(temp) && travel[0].tdest == want)\n\t\t\t\tk2 = kk;\n"
         "\t\t\tcopytrv(strav, travel);\n\t\t\t}\n")
    assert a in text
    text = text.replace(a, b)
    # "look" parks the player at location 0; the dwarf check must not then set newloc to 0
    a = "\tif (newloc != loc && !forced(loc) && (cond[loc] & NOPIRAT) == 0) {"
    assert a in text
    text = text.replace(a, "\tif (newloc != loc && loc != 0 && !forced(loc) && (cond[loc] & NOPIRAT) == 0) {")
    put("turn.c", text)
    common = ["-std=gnu99", "-w", "-funsigned-char", "-DBUILTIN", "-Drand=ref_rand",
              "-Dsrand=ref_srand", "-I" + work]
    srcs = [os.path.join(work, f) for f in ("database.c", "english.c", "itverb.c", "turn.c",
                                            "verb.c", "refrand.c", "scenario.c")]
    exe = os.path.join(out, "ref_adventure.exe")
    subprocess.run([sys.executable, "-m", "ziglang", "cc", *common, "-o", exe,
                    os.path.join(work, "advent.c"), *srcs], check=True)
    dump = os.path.join(out, "dump_data.exe")
    subprocess.run([sys.executable, "-m", "ziglang", "cc", *common, "-Dmain=adv_main", "-c",
                    "-o", os.path.join(out, "advent_dump.o"), os.path.join(work, "advent.c")],
                   check=True)
    subprocess.run([sys.executable, "-m", "ziglang", "cc", *common, "-o", dump,
                    os.path.join(out, "advent_dump.o"), os.path.join(work, "dump.c"), *srcs],
                   check=True)
    return exe, dump, work


# --------------------------------------------------------------------- the data
def check_data(dump_exe):
    res = subprocess.run([dump_exe], capture_output=True, text=True, encoding="latin-1")
    rows = [ln for ln in res.stdout.replace("\r", "").split("\n") if ln]
    got = {}
    for ln in rows:
        tag, rest = ln.split(" ", 1)
        got.setdefault(tag, []).append(rest)
    bad = []

    def cmp(tag, want):
        have = [r.split(" ", 1)[1] if tag in ("L1", "L2", "L3", "L4", "CV") else r for r in got[tag]]
        if have != want:
            for i, (h, w) in enumerate(zip(have, want)):
                if h != w:
                    bad.append("%s %d: C %r, Python %r" % (tag, i, h[:60], w[:60]))
                    break
            if len(have) != len(want):
                bad.append("%s: %d entries in C, %d in Python" % (tag, len(have), len(want)))

    cmp("L1", [esc(x) for x in D.ROOM_LONG])
    cmp("L2", [esc(x) for x in D.ROOM_SHORT])
    cmp("L3", [esc(x) for x in D.ITEM_TEXT])
    cmp("L4", [esc(x) for x in D.MESSAGES])
    cmp("CV", [esc(x) for x in D.TRAVEL])
    cmp("WC", ["%d %s %d" % (i, w, c) for i, (w, c) in enumerate(D.VOCAB)])
    cmp("IC", ["%d %d" % (i, v) for i, v in enumerate(D.INIT_COND)])
    cmp("IP", ["%d %d %d %d" % (i, D.INIT_PLACE[i], D.INIT_FIXED[i], -1 if i >= 50 else 0)
               for i in range(D.MAXOBJ)])
    cmp("AM", ["%d %d" % (i, v) for i, v in enumerate(D.ACTMSG)])
    cmp("DL", ["%d %d 0 0" % (i, v) for i, v in enumerate(D.INIT_DLOC)])
    from adventure.engine import Game, LCG
    g = Game(LCG(1), lambda: "")
    want_sc = ["%d %d %d %d %d %d %d %d %d %d %d" % (g.loc, g.oldloc, g.oldloc2, g.newloc, g.tally,
                                                       g.tally2, g.limit, g.clock1, g.clock2,
                                                       g.daltloc, g.chloc)]
    cmp("SC", want_sc)
    cmp("SD", ["%d %d %d %d %d %d %d %d" % (g.chloc2, g.dflag, g.holding, g.closing, g.closed,
                                               g.numdie, g.bonus, g.foobar)])
    return bad


# ------------------------------------------------------------------ the games
MOTION_WORDS = {}
OBJECT_WORDS = []
VERB_WORDS = []
SPECIAL_WORDS = []
SKIP = {2030, 2033}             # suspend/save/pause and restore/load work differently on purpose
for _w, _c in D.VOCAB:
    if " " in _w or _c in SKIP:
        continue
    if _c < 1000:
        MOTION_WORDS.setdefault(_c, _w)
    elif _c < 2000:
        OBJECT_WORDS.append(_w)
    elif _c < 3000:
        VERB_WORDS.append(_w)
    else:
        SPECIAL_WORDS.append(_w)
ALL_MOTIONS = sorted(set(MOTION_WORDS.values()))


def room_exits(loc):
    """Motion words that lead somewhere from a room, from the travel table."""
    words = []
    if 1 <= loc <= len(D.TRAVEL):
        for e in D.TRAVEL[loc - 1].split(","):
            if e:
                verb = (int(e) // 1000) % 1000
                if verb in MOTION_WORDS:
                    words.append(MOTION_WORDS[verb])
    return words


def make_scenario(rng):
    """A random starting state (as "name=value;..." for both programs) that is consistent
    enough for the C program to behave: carried objects match 'holding'."""
    items = {}
    arrays = {}
    # a player waits for a command only in rooms that are not forced moves (cond == 2)
    rooms = [r for r in range(1, 140) if D.INIT_COND[r] != 2]
    loc = rng.choice(rooms)
    if rng.random() < 0.15:
        loc = rng.choice([3, 8, 15, 33, 64, 96, 99, 100, 108, 115, 116, 117, 119, 120, 130])
    items["loc"] = items["newloc"] = loc
    items["oldloc"] = items["oldloc2"] = rng.randrange(1, 140)
    carried = rng.sample([o for o in range(1, 65) if D.INIT_FIXED[o] == 0
                          and D.ITEM_TEXT[o - 1] is not None and o not in (D.WATER, D.OIL, D.BIRD, D.DRAGON, D.TROLL, D.TROLL2)],
                         rng.randrange(0, 6))
    if rng.random() < 0.8 and D.LAMP not in carried:
        carried.append(D.LAMP)
        arrays[("prop", D.LAMP)] = 1 if rng.random() < 0.85 else 0
    for o in carried:
        arrays[("place", o)] = -1
    for o in rng.sample(range(1, 65), rng.randrange(0, 5)):
        if D.INIT_FIXED[o] == 0 and o not in carried and o not in (D.WATER, D.OIL):
            arrays[("place", o)] = loc
    for o in range(50, 65):                         # treasures seen/unseen
        if rng.random() < 0.3 and o in carried:
            arrays[("prop", o)] = 0
    items["holding"] = len(carried)
    items["dflag"] = rng.choice([0, 1, 2, 2, 3, 3])
    for i in range(1, 7):
        if rng.random() < 0.5:
            arrays[("dloc", i)] = rng.choice([loc, loc, rng.randrange(15, 120), 0])
            arrays[("dseen", i)] = rng.randrange(2)
    if rng.random() < 0.35:
        items["tally"] = rng.choice([0, 1, 2])
        if rng.random() < 0.6:
            items["clock1"] = rng.choice([1, 2, 3, 30])
            items["clock2"] = rng.choice([1, 2, 5, 50])
    if rng.random() < 0.25:
        items["limit"] = rng.choice([2, 31, 35, 40])
    if rng.random() < 0.2:
        items["numdie"] = rng.choice([1, 2])
    if rng.random() < 0.15:
        items["knfloc"] = rng.choice([0, loc, -1])
    if rng.random() < 0.1:
        items["foobar"] = rng.choice([1, 2, 3])
    if rng.random() < 0.1:
        items["detail"] = rng.randrange(0, 5)
    return items, arrays



def pick_command(rng, g):
    """The next command, chosen from the state of the Python game."""
    r = rng.random()
    if r < 0.30:
        ex = room_exits(g.loc)
        if ex:
            return rng.choice(ex)
    if r < 0.42:
        return rng.choice(ALL_MOTIONS)
    here = [o for o in range(1, 65) if g.place[o] == g.loc or g.place[o] == -1
            or g.fixed[o] == g.loc]
    here_words = [w for w, c in D.VOCAB if 1000 < c < 2000 and (c - 1000) in here and " " not in w]
    if r < 0.72:
        verb = rng.choice(VERB_WORDS)
        pool = here_words if here_words and rng.random() < 0.8 else OBJECT_WORDS
        noun = rng.choice(pool)
        return "%s %s" % ((verb, noun) if rng.random() < 0.9 else (noun, verb))
    if r < 0.80:
        return rng.choice(VERB_WORDS)
    if r < 0.86:
        return rng.choice(["back", "back", "back", "return", "in", "out", "get lamp", "get keys", "on", "off", "look", "inventory",
                           "take all", "get food", "get bottle", "fill bottle", "drink water",
                           "score", "open grate", "unlock grate", "wave rod", "get rod",
                           "get all", "xyzzy", "plugh", "plover", "fee fie foe foo", "foo", "throw axe",
                           "kill dwarf", "feed bear", "read tablet", "feed bird", "free bear"])
    if r < 0.92:
        return rng.choice(["y", "n", "yes", "no", ""])
    if r < 0.95:
        return rng.choice(SPECIAL_WORDS)
    if r < 0.97:
        return rng.choice(["  %s   %s  " % (rng.choice(VERB_WORDS), rng.choice(OBJECT_WORDS)),
                           "ABCDEFGHIJKLMNOPQRSTUVWXYZ get", "get   ", "a b c d", "THROW AXE",
                           "say hello", "say xyzzy", "say", "help help", "? ?"])
    return rng.choice(ALL_MOTIONS + OBJECT_WORDS + VERB_WORDS)


OPENINGS = [
    [], [],
    ["in", "get lamp", "get keys", "get food", "get bottle", "out", "s", "s", "s", "unlock grate",
     "open grate", "d", "on", "w", "get cage", "w", "get rod", "w", "w"],
    ["in", "get lamp", "get keys", "out", "s", "s", "s", "unlock grate", "d", "on", "w", "w", "w",
     "get rod", "w", "d", "d", "w"],
    ["in", "get lamp", "get bottle", "out", "s", "s", "s", "d", "w", "w", "w", "get bird"],
]


# Events worth a golden transcript: (name, text that shows it in the output)
GOLDEN_EVENTS = [
    ("dwarf-attack", "One sharp, nasty knife is thrown at you"),
    ("death-and-reincarnation", "POOF"),
    ("pirate", "bearded pirate"),
    ("cave-closing-soon", "Cave closing soon"),
    ("cave-closed", "The cave is now closed"),
    ("lamp-getting-dim", "Your lamp is getting dim"),
    ("score-at-the-end", "Treasures:"),
]
WALK = ["in", "get lamp", "get keys", "get food", "get bottle", "out", "s", "s", "s", "unlock grate",
        "open grate", "d", "on", "inventory", "w", "get cage", "w", "get rod", "back", "xyzzy", "help",
        "score", "quit", "y"]


def run_python(seed, scenario, moves, rng):
    """Play a random game on the Python engine, recording the commands."""
    def setup(g):
        if scenario:
            apply_scenario(g, *scenario)
    eng = AdventureEngine.new(seed=seed, setup=setup)
    lines, out = [], [eng.intro()]
    script = list(rng.choice(OPENINGS)) if not scenario else []
    try:
        i = 0
        while i < moves and not eng.finished:
            cmd = script.pop(0) if script else pick_command(rng, eng.g)
            lines.append(cmd)
            try:
                out.append(eng.command(cmd))
            except TimeoutError:
                print("PYTHON ENGINE HUNG on %r after %r (scenario %r)" % (
                    cmd, lines[-6:-1], scenario_text(*scenario) if scenario else ""))
                import traceback
                for tid, fr in sys._current_frames().items():
                    if tid == eng.thread.ident:
                        traceback.print_stack(fr)
                raise
            except RuntimeError as e:
                if "game is over" in str(e):
                    lines.pop()
                    break
                if not isinstance(eng.error, Bug):
                    out.append("\n*** PYTHON EXCEPTION: %s\n" % eng.tb)
                    break
                out.append(eng.last_text)           # a fatal error: the C text is in the buffer
                break
            i += 1
    finally:
        eng.close()
    return lines, "".join(out)


def run_c(exe, work, lines, scenario_str):
    env = dict(os.environ)
    env["HOME"] = work                              # never touch a real ~/.adventure
    if scenario_str:
        env["ADV_SCENARIO"] = scenario_str
    res = subprocess.run([exe], input=("n\n" + "\n".join(lines) + "\n").encode("latin-1"),
                         capture_output=True, cwd=work, timeout=60, env=env)
    return (res.stdout.decode("latin-1").replace("\r", ""),
            res.stderr.decode("latin-1").replace("\r", ""), res.returncode)


def first_diff(a, b):
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n if len(a) != len(b) else -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--games", type=int, default=300)
    ap.add_argument("--moves", type=int, default=120)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--record", default=None, metavar="DIR", help="write failing games here")
    ap.add_argument("--keep", default=None, metavar="DIR", help="build here and keep the C program")
    ap.add_argument("--golden", default=None, metavar="FILE",
                    help="write the shortest matching game for each event, with the C "
                         "program's output, as golden transcripts for the unit tests")
    args = ap.parse_args()

    tmp = None
    if args.keep:
        out = args.keep
        os.makedirs(out, exist_ok=True)
    else:
        tmp = tempfile.TemporaryDirectory()
        out = tmp.name
    exe, dump, work = build(args.src, out)

    bad = check_data(dump)
    print("data tables: %s" % ("identical to the C program's" if not bad else "DIFFERENCES"))
    for b in bad:
        print("   ", b)

    rng = random.Random(args.seed)
    fails = ended = 0
    steps = 0
    stats = {"death": 0, "closing": 0, "closed": 0, "dwarf": 0, "pirate": 0, "score": 0,
             "fatal": 0}
    c_preamble = None
    fatal_kinds = {}
    golden = {}
    for n in range(args.games):
        scenario = None
        if n % 4 != 0:
            scenario = make_scenario(rng)
        seed = 511
        lines, py = run_python(seed, scenario, args.moves, rng)
        c_out, c_err, rc = run_c(exe, work, lines, scenario_text(*scenario) if scenario else "")
        if c_preamble is None:
            c_preamble = D.MESSAGES[64] + "> "
        assert c_out.startswith(c_preamble), c_out[:100]
        c_out = c_out[len(c_preamble):]
        steps += len(lines)
        if "Score:" in py:
            stats["score"] += 1
        for m in re.findall(r"Fatal error number (\d+)", py):
            fatal_kinds[m] = fatal_kinds.get(m, 0) + 1
        for key, needle in (("death", "reincarnate"), ("closing", "closing"), ("closed", "Closed"),
                            ("dwarf", "dwarf"), ("pirate", "pirate"), ("fatal", "Fatal error")):
            if needle.lower() in py.lower():
                stats[key] += 1
        ended += 1 if "Score:" in py else 0
        pos = first_diff(py, c_out)
        if pos < 0 and args.golden and "Fatal error" not in c_out:
            for tag, needle in GOLDEN_EVENTS:
                if needle in c_out and (tag not in golden or len(c_out) < len(golden[tag]["output"])):
                    golden[tag] = {"name": tag, "seed": seed, "lines": lines, "output": c_out,
                                   "scenario": scenario_text(*scenario) if scenario else ""}
        if pos >= 0:
            fails += 1
            print("MISMATCH in game %d (scenario %r, %d commands) at char %d:" % (
                n, scenario_text(*scenario) if scenario else "", len(lines), pos))
            print("   C     : %r" % c_out[max(0, pos - 80):pos + 120])
            print("   Python: %r" % py[max(0, pos - 80):pos + 120])
            if c_err.strip():
                print("   C stderr:", c_err.strip()[:200])
            if args.record:
                os.makedirs(args.record, exist_ok=True)
                with open(os.path.join(args.record, "fail_%d.json" % n), "w") as fh:
                    json.dump({"scenario": scenario_text(*scenario) if scenario else "",
                               "lines": lines}, fh)
    print("%d games, %d commands: %d mismatches" % (args.games, steps, fails))
    print("coverage (games whose output mentions): %s" % ", ".join(
        "%s %d" % kv for kv in stats.items()))
    print("fatal errors by number: %s" % fatal_kinds)
    if args.golden:
        entries = [golden[tag] for tag, _ in GOLDEN_EVENTS if tag in golden]
        c_out, _, _ = run_c(exe, work, WALK, "")
        entries.insert(0, {"name": "walk", "seed": 511, "lines": WALK, "scenario": "",
                           "output": c_out[len(c_preamble):]})
        with open(args.golden, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(entries, fh, indent=0)
        print("golden transcripts: %s" % ", ".join(
            "%s (%d commands)" % (e["name"], len(e["lines"])) for e in entries))
    if tmp:
        tmp.cleanup()
    return 1 if fails or bad else 0


if __name__ == "__main__":
    sys.exit(main())
