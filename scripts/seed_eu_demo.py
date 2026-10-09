"""Fake data for the EU division, so the multi-division pages can be looked at.

There is only one real league, so nothing on the site has ever been seen with
two divisions playing at once. This fills the EU division with a plausible
season — eight teams, a draft, five weeks, four of them played — using real
stat lines sampled from NA games so the numbers have the right shape.

Everything it writes is reversible and marked: fake accounts are numbered
from 4_200_000_000 (near the top of the 32-bit account space, where Steam has
not reached) and fake matches from 90_000_000_000 (ten times today's real
match ids). `--remove` deletes exactly those rows and nothing else.

    DB_PATH=... python3 scripts/seed_eu_demo.py --seed
    DB_PATH=... python3 scripts/seed_eu_demo.py --remove
"""
import argparse
import random
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

SEASON = 20056          # the EU division plays the same league as NA
DIVISION = 3
NA_DIVISION = 2
ACCOUNT_BASE = 4_200_000_000
MATCH_BASE = 90_000_000_000

# (captain handle, team name, four drafted players)
TEAMS = [
    ("Kveld",     "Nordlys",             ["sondre", "hvalross", "tromso andy", "Fjordmancer"]),
    ("Pimmel",    "Bratwurst Enjoyers",  ["schnitzelboy", "Kartoffel", "autobahn", "Weisswurst"]),
    ("Tomasz",    "Pierogi Gaming",      ["zurek", "Kielbasa", "wujek janusz", "oscypek"]),
    ("Mikkel",    "Hygge Esports",       ["Smorrebrod", "kanelsnegl", "frikadelle", "Hyggelig"]),
    ("Zbyszko",   "Wisla Wolves",        ["bigos", "Pan Tadeusz", "krakow mike", "zapiekanka"]),
    ("Aurelien",  "Baguette Brigade",    ["croissant", "Le Tilt", "mont blanc", "ratatouille"]),
    ("Dimitris",  "Souvlaki Squad",      ["feta", "Olympus", "tzatziki", "meltemi"]),
    ("Fiachra",   "Shamrock Shufflers",  ["sláinte", "Boxty", "donegal", "craic dealer"]),
]
WEEKS_PLAYED = (1, 2, 3, 4)
GAMES_PER_PAIR = 2

# Four weeks of fixtures, each a list of (home, away) team indexes: everyone
# plays everyone once over the first seven weeks, which is what a real Swiss
# season looks like four weeks in.
FIXTURES = {
    1: [(0, 1), (2, 3), (4, 5), (6, 7)],
    2: [(0, 2), (1, 3), (4, 6), (5, 7)],
    3: [(0, 4), (1, 5), (2, 6), (3, 7)],
    4: [(0, 3), (1, 2), (4, 7), (5, 6)],
}


def accounts() -> list[tuple[int, str, int]]:
    """(account_id, handle, team index) for every fake player, captains first
    within each team so seat 0 is the captain."""
    out = []
    for ti, (captain, _name, roster) in enumerate(TEAMS):
        for handle in (captain, *roster):
            out.append((ACCOUNT_BASE + len(out) + 1, handle, ti))
    return out


def seed(conn: sqlite3.Connection) -> None:
    rng = random.Random(39)
    now = datetime.now(timezone.utc).isoformat()
    people = accounts()
    by_team: dict[int, list[tuple[int, str, int]]] = {}
    for p in people:
        by_team.setdefault(p[2], []).append(p)

    conn.execute("INSERT OR IGNORE INTO season_divisions VALUES (?, ?)", (SEASON, DIVISION))
    conn.execute("INSERT OR IGNORE INTO site_season_settings (season_id, division_id, status) "
                 "VALUES (?, ?, 'active')", (SEASON, DIVISION))
    conn.execute("UPDATE divisions SET current_season_id = ? WHERE id = ?", (SEASON, DIVISION))

    # The same calendar as NA, so "this week" means the same thing in both.
    conn.execute("INSERT OR IGNORE INTO season_weeks SELECT season_id, ?, week_number, start_ts, end_ts "
                 "FROM season_weeks WHERE season_id = ? AND division_id = ?",
                 (DIVISION, SEASON, NA_DIVISION))

    for ti, (captain, name, _roster) in enumerate(TEAMS):
        conn.execute("INSERT OR IGNORE INTO season_teams (season_id, division_id, captain, team_name) "
                     "VALUES (?, ?, ?, ?)", (SEASON, DIVISION, captain.lower(), name))
        # player_costs is the draft: the four bought players, not the captain.
        for aid, handle, _ in by_team[ti][1:]:
            conn.execute("INSERT OR IGNORE INTO player_costs "
                         "(season_id, division_id, account_id, cost, captain, mmr) VALUES (?, ?, ?, ?, ?, ?)",
                         (SEASON, DIVISION, aid, rng.choice([39, 47, 58, 73, 94, 112, 131, 168, 205, 254]),
                          captain, rng.randrange(3000, 8000, 100)))
    for aid, _handle, _ti in people:
        conn.execute("INSERT OR IGNORE INTO division_roster (division_id, account_id, added_at) "
                     "VALUES (?, ?, ?)", (DIVISION, aid, now))

    # Real stat lines, so the fantasy table and the team pages have numbers
    # with the right distribution rather than numbers I made up.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(players)")
            if r[1] not in ("id", "match_id", "account_id", "name", "team_side",
                            "won", "player_slot", "duration", "hero_id", "role_position")]
    pool = conn.execute(
        f"SELECT {','.join(cols)} FROM players p JOIN matches m USING (match_id) "
        f"WHERE m.division_id = ? AND m.league_id = ?", (NA_DIVISION, SEASON)).fetchall()
    if not pool:
        raise SystemExit("no NA player rows to sample stat lines from")
    heroes = [r[0] for r in conn.execute("SELECT DISTINCT hero_id FROM players WHERE hero_id > 0")]
    weeks = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT week_number, start_ts, end_ts FROM season_weeks WHERE season_id = ? AND division_id = ?",
        (SEASON, DIVISION))}

    mid = MATCH_BASE
    for week in WEEKS_PLAYED:
        # The same Wednesday as NA, at an hour that is the evening in Europe
        # rather than the evening on the east coast.
        start = weeks[week][0] + 2 * 86400 + 18 * 3600
        for gi, (home, away) in enumerate(FIXTURES[week]):
            for game in range(GAMES_PER_PAIR):
                mid += 1
                radiant_win = rng.random() < 0.5
                duration = rng.randrange(1700, 3300)
                kills = sorted((rng.randrange(14, 55), rng.randrange(14, 55)), reverse=radiant_win)
                conn.execute(
                    "INSERT OR REPLACE INTO matches (match_id, division_id, league_id, start_time, duration, "
                    "game_mode, cluster, radiant_win, radiant_score, dire_score, fetched_at, replay_salt) "
                    "VALUES (?, ?, ?, ?, ?, 18, 142, ?, ?, ?, ?, 0)",
                    (mid, DIVISION, SEASON, start + gi * 600 + game * (duration + 900), duration,
                     int(radiant_win), kills[0], kills[1], now))
                picks = rng.sample(heroes, 10)
                for side_i, ti in enumerate((home, away)):
                    side = "radiant" if side_i == 0 else "dire"
                    won = int(radiant_win == (side_i == 0))
                    for seat, (aid, handle, _) in enumerate(by_team[ti]):
                        stats = dict(zip(cols, rng.choice(pool)))
                        conn.execute(
                            f"INSERT OR REPLACE INTO players (match_id, account_id, name, hero_id, team_side, "
                            f"won, player_slot, duration, role_position, {','.join(cols)}) "
                            f"VALUES ({','.join('?' * (9 + len(cols)))})",
                            [mid, aid, handle, picks[side_i * 5 + seat], side, won,
                             side_i * 128 + seat, duration, seat + 1, *stats.values()])
    print(f"seeded {mid - MATCH_BASE} matches, {len(people)} players, {len(TEAMS)} teams in division {DIVISION}")


def remove(conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT COUNT(*) FROM players WHERE match_id >= ?", (MATCH_BASE,)).fetchone()[0]
    m = conn.execute("SELECT COUNT(*) FROM matches WHERE match_id >= ?", (MATCH_BASE,)).fetchone()[0]
    conn.execute("DELETE FROM players WHERE match_id >= ?", (MATCH_BASE,))
    conn.execute("DELETE FROM matches WHERE match_id >= ?", (MATCH_BASE,))
    for table, col in (("player_costs", "account_id"), ("division_roster", "account_id"),
                       ("site_signups", "account_id")):
        conn.execute(f"DELETE FROM {table} WHERE {col} >= ?", (ACCOUNT_BASE,))
    for table in ("season_teams", "season_weeks", "site_season_settings", "season_divisions",
                  "playoff_brackets", "playoff_seeds", "site_season_champions"):
        conn.execute(f"DELETE FROM {table} WHERE division_id = ?", (DIVISION,))
    conn.execute("UPDATE divisions SET current_season_id = NULL WHERE id = ?", (DIVISION,))
    print(f"removed {m} matches and {n} player rows, and everything in division {DIVISION}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args()
    if args.seed == args.remove:
        ap.error("pass exactly one of --seed / --remove")
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    with conn:
        (seed if args.seed else remove)(conn)
