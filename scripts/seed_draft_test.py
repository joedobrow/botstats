"""Sign up last season's NA league for this season's draft, to try the draft room.

Everyone who captained an RD2L Season 39 team signs up for ADCL Season 1 NA
as a captain, and everyone drafted onto one signs up as a player — the
right numbers, the right names and real ratings, without anyone having to
fill in a form. Their positions are the ones they played most last season.

Real signups are never touched: anyone already signed up is skipped, and
the accounts this adds are written to their own table, so `--remove` takes
out exactly those and nothing else. `--remove` also clears the season's
draft (its event log, and any rosters a committed test draft wrote), since a
draft run against seeded players is a test, whoever ran it.

    DB_PATH=... python3 scripts/seed_draft_test.py --seed
    DB_PATH=... python3 scripts/seed_draft_test.py --remove
"""
import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

FROM_SEASON, TO_SEASON, DIVISION = 20056, 20508, 2     # RD2L 39 -> ADCL 1, NA
MARK = "seed_draft_test"                               # the table of what was added


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def seed() -> None:
    captains = {aid for aid in db.get_captain_account_ids(FROM_SEASON, DIVISION).values() if aid}
    with connect() as conn:
        drafted = [r["account_id"] for r in conn.execute(
            "SELECT account_id FROM player_costs WHERE season_id = ? AND division_id = ?",
            (FROM_SEASON, DIVISION))]
        players = [a for a in drafted if a not in captains]
        already = {r["account_id"] for r in conn.execute(
            "SELECT account_id FROM site_signups WHERE season_id = ? AND division_id = ?",
            (TO_SEASON, DIVISION))}
        where, params = db.season_scope(FROM_SEASON, DIVISION).where("m")
        played: dict[int, Counter] = {}
        for r in conn.execute(f"""
                SELECT p.account_id, p.role_position FROM players p
                JOIN matches m ON m.match_id = p.match_id
                WHERE {where} AND p.role_position BETWEEN 1 AND 5""", params):
            played.setdefault(r["account_id"], Counter())[r["role_position"]] += 1
        discord = {r["account_id"]: r["discord"] for r in conn.execute(
            "SELECT account_id, discord FROM site_users WHERE discord IS NOT NULL")}

        conn.execute(f"CREATE TABLE IF NOT EXISTS {MARK} "
                     "(season_id INTEGER, division_id INTEGER, account_id INTEGER, "
                     "PRIMARY KEY (season_id, division_id, account_id))")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        added = {"captain": 0, "player": 0}
        for level, people in (("captain", sorted(captains)), ("player", players)):
            for aid in people:
                if aid in already:
                    print(f"  skipping {aid}: already signed up")
                    continue
                top = [pos for pos, _ in (played.get(aid) or Counter()).most_common(2)] or [1]
                conn.execute(
                    "INSERT INTO site_signups (season_id, division_id, account_id, mmr, positions, "
                    "captain, discord, statement, created_at, updated_at, level) "
                    "VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?)",
                    (TO_SEASON, DIVISION, aid, ",".join(map(str, sorted(top))),
                     "yes" if level == "captain" else "no", discord.get(aid), now, now, level))
                conn.execute(f"INSERT OR IGNORE INTO {MARK} VALUES (?, ?, ?)",
                             (TO_SEASON, DIVISION, aid))
                already.add(aid)
                added[level] += 1
    print(f"Signed up {added['captain']} captains and {added['player']} players for "
          f"season {TO_SEASON}, division {DIVISION}.")


def remove() -> None:
    with connect() as conn:
        have = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                            (MARK,)).fetchone()
        if have:
            gone = conn.execute(f"""
                DELETE FROM site_signups WHERE (season_id, division_id, account_id) IN
                    (SELECT season_id, division_id, account_id FROM {MARK})""").rowcount
            conn.execute(f"DROP TABLE {MARK}")
        else:
            gone = 0
        events = conn.execute("DELETE FROM draft_events WHERE season_id = ? AND division_id = ?",
                              (TO_SEASON, DIVISION)).rowcount
        costs = conn.execute("DELETE FROM player_costs WHERE season_id = ? AND division_id = ?",
                             (TO_SEASON, DIVISION)).rowcount
        teams = conn.execute("DELETE FROM season_teams WHERE season_id = ? AND division_id = ?",
                             (TO_SEASON, DIVISION)).rowcount
    print(f"Removed {gone} seeded signups, {events} draft events, {costs} roster rows "
          f"and {teams} teams from season {TO_SEASON}, division {DIVISION}.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--seed", action="store_true")
    g.add_argument("--remove", action="store_true")
    args = ap.parse_args()
    seed() if args.seed else remove()
