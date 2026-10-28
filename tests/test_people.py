"""Checks that a player's accounts are one player: python3 tests/test_people.py

Runs against a throwaway database in a temp directory, so it never touches
real data. Somebody who changes Steam account mid-season is the hard case
this is all for — their games are split across two account ids in the
match table, and every count that matters has to put them back together.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="people-test-")
os.environ["DB_PATH"] = os.path.join(TMP, "test.db")
os.environ.setdefault("DISCORD_TOKEN", "dev")

import db                  # noqa: E402
from web import store      # noqa: E402

SEASON = 55001
DAY = 86400
START = 1_750_000_000

# One person, two accounts: the old one they played the first half on and
# the new one they finished the season on.
OLD, NEW = 9001, 9002
MATE, OPP = 9003, 9004          # a team-mate and a whole enemy team
OTHERS = [9005, 9006, 9007, 9008, 9009]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if not ok and detail else ""))
    if not ok:
        failures.append(label)


def player_row(match_id: int, slot: int, account_id: int, name: str, side: str,
               won: int, **over) -> dict:
    row = {"match_id": match_id, "account_id": account_id, "name": name, "hero_id": 1,
           "team_side": side, "role_position": 2, "kills": 10, "deaths": 2, "assists": 5,
           "gpm": 500, "xpm": 600, "last_hits": 200, "denies": 10, "hero_damage": 20000,
           "hero_healing": 0, "gold_spent": 15000, "duration": 2000, "won": won,
           "obs_placed": 0, "sen_placed": 0, "observer_kills": 0, "sentry_kills": 0,
           "tower_kills": 1, "roshans_killed": 0, "firstblood_claimed": 0,
           "teamfight_participation": 0.5, "stuns": 5, "camps_stacked": 0, "rune_pickups": 3,
           "tormentor_kills": 0, "watcher_captures": 0, "team_first_tormentor_time": 0,
           "obs_uptime_total": 0, "obs_uptime_count": 0,
           "player_slot": slot, "defensive_item_uses": 0, "building_damage": 1000}
    row.update(over)
    return row


def add_match(match_id: int, day: int, hero_account: int, hero_name: str, won: int) -> None:
    """One league game: the person on one side with a team-mate, five on the
    other. Everything about it is the same every time except who played and
    whether they won, so a difference in the numbers is a difference in the
    counting."""
    db.upsert_match({"match_id": match_id, "division_id": DIVISION, "league_id": SEASON,
                     "start_time": START + day * DAY, "duration": 2000, "game_mode": 18,
                     "cluster": 111, "radiant_win": won, "radiant_score": 30, "dire_score": 20,
                     "fetched_at": "2026-01-01T00:00:00+00:00", "replay_salt": 0})
    rows = [player_row(match_id, 0, hero_account, hero_name, "radiant", won),
            player_row(match_id, 1, MATE, "mate", "radiant", won, kills=1, assists=1)]
    rows += [player_row(match_id, 128 + i, aid, f"opp{i}", "dire", 1 - won, kills=2)
             for i, aid in enumerate([OPP] + OTHERS[:4])]
    db.upsert_players(rows)


def setup() -> None:
    global DIVISION
    db.init_db()
    store.init_site_tables()
    DIVISION = db.create_division("Test", "test", "us_east", "ability_draft", None, SEASON)
    import datetime
    db.upsert_season(SEASON, datetime.datetime.utcfromtimestamp(START).strftime("%Y-%m-%d"),
                     "Test Season")
    db.add_season_division(SEASON, DIVISION)
    # Three games on the old account in week 1, two on the new one in week 2
    # — the half-and-half season this is all about. The weeks are spelled
    # out rather than left to the calendar, so the test says what it means.
    for i in range(3):
        add_match(7001 + i, i, OLD, "oldname", won=1)
    for i in range(2):
        add_match(7101 + i, 10 + i, NEW, "newname", won=0)
    db.upsert_season_week(SEASON, DIVISION, 1, START - DAY, START + 7 * DAY)
    db.upsert_season_week(SEASON, DIVISION, 2, START + 7 * DAY + 1, START + 14 * DAY)
    db.upsert_skill_override(account_id=NEW, windrun_rating=None, rank_tier=None,
                             leaderboard_rank=None, note=None, alt_account_for=OLD,
                             set_by_user_id=None, set_by_name="test")
    # A team, so the draft-relative numbers have something to measure against.
    db.upsert_season_teams(SEASON, DIVISION, [{"captain": "cap", "team_name": "Cap's Team"}])
    db.upsert_player_costs(SEASON, DIVISION, [
        {"account_id": NEW, "cost": 100, "captain": "cap"},
        {"account_id": MATE, "cost": 90, "captain": "cap"}])


def main() -> int:
    setup()
    season = {"id": SEASON, "division_id": DIVISION}

    rows = db.get_all_time_stats(DIVISION, SEASON)
    by_aid = {r["account_id"]: r for r in rows}
    person, alt = by_aid.get(OLD), by_aid.get(NEW)
    check("the person's games are counted once, all of them",
          person and person["games_played"] == 5, str(person and person["games_played"]))
    check("and so are their wins", person and person["wins"] == 3,
          str(person and person["wins"]))
    check("the record is filed under their main account", person and not person.get("alt_of"))
    check("looking them up by the other account finds the same record",
          alt and alt["games_played"] == 5 and alt["alt_of"] == OLD, str(alt and alt.get("alt_of")))
    check("the copy keeps the name that account played under",
          alt and alt["name"] == "newname", str(alt and alt["name"]))
    check("a ranking lists them once",
          [r["account_id"] for r in db.ranked(rows)].count(OLD) == 1
          and NEW not in {r["account_id"] for r in db.ranked(rows)})
    check("nobody else was folded in", by_aid[MATE]["games_played"] == 5)

    total = sum(r["games_played"] for r in db.ranked(rows))
    check("no game is counted twice or lost", total == 5 * 7, str(total))

    # A week that only the old account played in, and one only the new one did.
    first = db.ranked(db.get_stats_for_season_week(SEASON, DIVISION, 1))
    check("a week they played on the old account is theirs",
          next((r["games_played"] for r in first if r["account_id"] == OLD), None) == 3,
          str([(r["account_id"], r["games_played"]) for r in first]))
    later = db.get_stats_for_season_week(SEASON, DIVISION, 2)
    check("a week they played on the new one is theirs too",
          next((r["games_played"] for r in later if r["account_id"] == OLD), None) == 2)
    check("and looking that week up by the new account still answers",
          next((r["games_played"] for r in later if r["account_id"] == NEW), None) == 2)
    check("a week's ranking lists them once",
          [r["account_id"] for r in db.ranked(later)].count(OLD) == 1
          and NEW not in {r["account_id"] for r in db.ranked(later)})

    # Attendance is "what share of your team's season did you play": the team
    # played five, and they were there for all five — on one account or the
    # other.
    check("attendance counts the nights they turned out on either account",
          person and person["attendance"] == 1.0, str(person and person["attendance"]))
    check("their cost follows them from the account they were drafted on",
          person and person["cost"] == 100, str(person and person.get("cost")))

    # The site.
    check("the site's player index has one row for them",
          len([p for p in store.search_players() if p["account_id"] in (OLD, NEW)]) == 1)
    found = store.search_players("newname")
    check("searching the name they play under now finds the person",
          len(found) == 1 and found[0]["account_id"] == OLD, str(found))
    check("searching the name they used to play under finds the same one",
          [p["account_id"] for p in store.search_players("oldname")] == [OLD])
    check("searching the old account id finds them",
          [p["account_id"] for p in store.search_players(str(NEW))] == [OLD])
    check("the index counts all of their games",
          next(p["games"] for p in store.search_players("newname")) == 5)
    check("their match list is every game, on either account",
          len(store.player_matches(NEW)) == 5 and len(store.player_matches(OLD)) == 5,
          f"{len(store.player_matches(NEW))} / {len(store.player_matches(OLD))}")
    check("the matches page narrows to the person, not the account",
          store.search_matches(player="newname")["total"] == 5,
          str(store.search_matches(player="newname")["total"]))
    check("either account asks the same team",
          (store.team_of(season, NEW) or {}).get("captain") == "cap"
          and (store.team_of(season, OLD) or {}).get("captain") == "cap")
    check("the team's roster lists them once",
          [p["account_id"] for p in store.season_teams(season)[0]["roster"]].count(NEW)
          + [p["account_id"] for p in store.season_teams(season)[0]["roster"]].count(OLD) == 1,
          str(store.season_teams(season)[0]["roster"]))

    # The helpers the rest of the code reaches for.
    check("person_of sends either account to the same person",
          db.person_of(NEW) == OLD and db.person_of(OLD) == OLD)
    check("person_of leaves everyone else alone", db.person_of(MATE) == MATE)
    check("accounts_of_people finds both from either",
          db.accounts_of_people([NEW]) == {OLD, NEW} == db.accounts_of_people([OLD]))
    check("one_per_person keeps a roster the size it really is",
          db.one_per_person([OLD, NEW, MATE]) == [OLD, MATE],
          str(db.one_per_person([OLD, NEW, MATE])))

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
