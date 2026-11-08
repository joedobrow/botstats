"""Checks of the match page's draft pool layout (store.draft_pool):
python3 tests/test_draft_board.py

No database: the pool is built from picks and the unpicked ids alone.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DISCORD_TOKEN", "test")

from web import store    # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if not ok and detail else ""))
    if not ok:
        failures.append(label)


def own(hero_id):
    """A hero's AD abilities from the table: (basics in order, ultimates)."""
    mine = sorted(((a, v) for a, v in store._AD.items() if v["hero"] == hero_id), key=lambda x: x[1]["order"])
    return [a for a, v in mine if not v["ult"]], [a for a, v in mine if v["ult"]]


def main() -> int:
    import json
    heroes = {v["name"]: int(k) for k, v in json.loads((store._STATIC / "heroes.json").read_text()).items()}
    names = ["Abaddon", "Alchemist", "Axe", "Bane", "Bristleback", "Clinkz",
             "Crystal Maiden", "Morphling", "Lich", "Lion", "Sven", "Tiny"]
    ids = [heroes[n] for n in names]
    pool = []
    for h in ids:
        b, u = own(h)
        pool += [-h] + b[:3] + u[:1]
    # Fill Morphling's and Crystal Maiden's gaps with abilities of heroes not
    # in the pool: two basics and one ultimate.
    lina_b, lina_u = own(heroes["Lina"])
    zeus_b, _ = own(heroes["Zeus"])
    pool += [lina_b[0], zeus_b[0], lina_u[0]]
    check("a full pool is 60", len(pool) == 60, str(len(pool)))

    picks = [{"ability_id": a} for a in pool[:50]]
    grid = store.draft_pool(picks, pool[50:])
    check("eight rows of eight", len(grid) == 8 and all(len(r) == 8 for r in grid))
    cells = [c for r in grid for c in r if c]
    check("every pool item placed once", sorted(c["id"] for c in cells if "id" in c) == sorted(pool))
    check("nothing unknown in a full pool", not any(c.get("unknown") for c in cells))
    check("corners of the ultimate rows are empty",
          all(grid[r][c] is None for r in (0, 1) for c in (0, 7)))
    top = {c["id"] for r in grid[:2] for c in r if c}
    check("the ultimates are on top", lina_u[0] in top and own(heroes["Sven"])[1][0] in top)
    alpha = sorted(names)
    check("heroes down the left, then the right, by name",
          [grid[2 + k][0]["id"] for k in range(6)] == [-heroes[n] for n in alpha[:6]]
          and [grid[2 + k][7]["id"] for k in range(6)] == [-heroes[n] for n in alpha[6:]])
    morph_row = 2 + alpha.index("Morphling") - 6
    morph = [c["id"] for c in grid[morph_row][4:7]]
    check("Morphling's own two first, a borrowed ability third",
          morph[:2] == own(heroes["Morphling"])[0][:2] and morph[2] in (lina_b[0], zeus_b[0]), str(morph))
    check("taken picks carry their pick number, the rest none",
          all((c["n"] is None) == (c["id"] in pool[50:]) for c in cells if "id" in c))

    short = store.draft_pool(picks[:40], pool[50:52])
    unknown = sum(1 for r in short for c in r if c and c.get("unknown"))
    check("each item missing from the pool is a question mark", unknown == 60 - 42, str(unknown))
    check("no pool stored, no board", store.draft_pool(picks, []) is None)

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
