"""Test helpers shared by tools/crosscheck.py and the engine tests."""

def apply_scenario(g, scenario):
    """Python twin of scenario_() in SCENARIO_C."""
    from . import engine_gen as E
    from .rt import flat
    player = E.aindex_.player
    for part in scenario.split(";"):
        if not part:
            continue
        kind = part[0]
        for item in part[2:].split(","):
            nums = [int(x) for x in item.split(":")] + [0, 0]
            a, b, c = nums[0], nums[1], nums[2]
            if kind == "H":
                g.play_.here = a
                g.advs_.aroom[player - 1] = a
            elif kind == "G":
                g.objcts_.oroom[a - 1] = 0
                g.objcts_.oadv[a - 1] = player
                g.objcts_.ocan[a - 1] = 0
            elif kind == "B":
                g.objcts_.oflag1[a - 1] |= b
                g.objcts_.oflag2[a - 1] |= c
            elif kind == "F":
                flat(g.findex_)[a] = b
