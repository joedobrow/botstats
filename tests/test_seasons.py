"""Checks which season a division is playing: python3 tests/test_seasons.py

Runs against a throwaway database in a temp directory, so it never touches
real data. Every Discord command's default, the weekly fetch and the site's
front page all read the current season, and when they disagreed the bot's
leaderboards spent a week on a season nobody had played yet. One rule
(db.playing_season_id) decides it now; this is that rule, case by case.
"""
import os
import sys
import tempfile
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="seasons-test-")
os.environ["DB_PATH"] = os.path.join(TMP, "test.db")
os.environ.setdefault("DISCORD_TOKEN", "dev")

import db                  # noqa: E402
from web import store      # noqa: E402

OLD, NOW, NEXT = 61001, 61002, 61003
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if not ok and detail else ""))
    if not ok:
        failures.append(label)


def day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


def main() -> int:
    db.init_db()
    store.init_site_tables()
    na = db.create_division(name="NA", slug="na", region="any", game_mode="ad", guild_id=None)
    eu = db.create_division(name="EU", slug="eu", region="any", game_mode="ad", guild_id=None)

    db.upsert_season(OLD, day(-200), label="Old")
    db.upsert_season(NOW, day(-30), label="Now")
    db.upsert_season(NEXT, day(60), label="Next")
    for s in (OLD, NOW):
        db.add_season_division(s, na)
    # Season setup run ahead of time: the pointer moves to a season that
    # hasn't started.
    db.set_current_season(na, NEXT)
    db.set_current_season(eu, NEXT)

    check("a season set up ahead doesn't take over before it starts",
          db.get_division(na)["current_season_id"] == NOW,
          str(db.get_division(na)["current_season_id"]))
    check("the pointer is still there for the weekly fetch",
          db.get_division(na)["season_pointer"] == NEXT)
    check("a division with nothing started yet follows its pointer",
          db.get_division(eu)["current_season_id"] == NEXT)
    check("the bot's get_current_season agrees",
          (db.get_current_season(na) or {}).get("id") == NOW)

    current = store.current_seasons()
    check("the site's front page shows the season being played",
          [(s["id"], s["division_id"]) for s in current] == [(NOW, na)], str(current))
    check("and leaves out a division that hasn't begun", all(s["division_id"] != eu for s in current))

    store.set_status(OLD, na, "active")
    check("an admin marking a season Active wins",
          db.get_division(na)["current_season_id"] == OLD
          and [s["id"] for s in store.current_seasons()] == [OLD])
    store.set_status(OLD, na, "completed")

    db.update_season(NEXT, season_start=day(0))
    check("on its start date the new season takes over by itself",
          db.get_division(na)["current_season_id"] == NEXT
          and db.get_division(eu)["current_season_id"] == NEXT)
    check("and the site agrees",
          sorted((s["id"], s["division_id"]) for s in store.current_seasons())
          == sorted([(NEXT, na), (NEXT, eu)]), str(store.current_seasons()))

    # The leaderboard cut every table shares.
    check("early on, half the most anyone has played", db.min_games_for(6) == 3)
    check("late on, never more than ten", db.min_games_for(30) == 10)

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
