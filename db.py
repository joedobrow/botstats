"""
Lightweight SQLite database.  SQLite ships with Python, needs no external
service, and the file persists on disk on Railway/Render (or wherever you host).

Schema
------
matches      – one row per match (match_id, start_time, duration, etc.)
players      – one row per player per match (all the stat fields)

We derive "week" from the match start_time at query time so we never have to
update rows when weeks change.
"""

import sqlite3
import os
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/dota_stats.db" if os.path.exists("/data") else "dota_stats.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    """Create tables if they don't exist. Call once at startup."""
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS matches (
                match_id        INTEGER PRIMARY KEY,
                league_id       INTEGER NOT NULL,
                start_time      INTEGER NOT NULL,   -- unix timestamp
                duration        INTEGER NOT NULL,   -- seconds
                game_mode       INTEGER NOT NULL,
                cluster         INTEGER NOT NULL,
                radiant_win     INTEGER NOT NULL,   -- 1 or 0
                radiant_score   INTEGER NOT NULL,
                dire_score      INTEGER NOT NULL,
                fetched_at      TEXT NOT NULL        -- ISO timestamp of when we stored it
            );

            CREATE TABLE IF NOT EXISTS players (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id        INTEGER NOT NULL REFERENCES matches(match_id),
                account_id      INTEGER NOT NULL,
                name            TEXT NOT NULL,
                hero_id         INTEGER NOT NULL,
                team_side       TEXT NOT NULL,       -- 'radiant' or 'dire'
                role_position   INTEGER,             -- 1-5 positional role
                kills           INTEGER DEFAULT 0,
                deaths          INTEGER DEFAULT 0,
                assists         INTEGER DEFAULT 0,
                gpm             REAL DEFAULT 0,
                xpm             REAL DEFAULT 0,
                last_hits       INTEGER DEFAULT 0,
                denies          INTEGER DEFAULT 0,
                hero_damage     INTEGER DEFAULT 0,
                hero_healing    INTEGER DEFAULT 0,
                gold_spent      INTEGER DEFAULT 0,
                duration        INTEGER DEFAULT 0,  -- match duration (seconds), duplicated for convenience
                won             INTEGER DEFAULT 0   -- 1 if this player's team won
            );

            CREATE INDEX IF NOT EXISTS idx_players_match   ON players(match_id);
            CREATE INDEX IF NOT EXISTS idx_players_account ON players(account_id);
            CREATE INDEX IF NOT EXISTS idx_matches_start   ON matches(start_time);

            CREATE TABLE IF NOT EXISTS chat_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id        INTEGER NOT NULL REFERENCES matches(match_id),
                player_slot     INTEGER NOT NULL,
                player_name     TEXT,
                time            INTEGER NOT NULL,   -- seconds into match (can be negative for pre-game)
                message         TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chat_match ON chat_messages(match_id);
        """)
    logger.info("Database initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def upsert_match(match: dict):
    """Insert a match row, skip if match_id already exists."""
    with _conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO matches
                (match_id, league_id, start_time, duration, game_mode, cluster,
                 radiant_win, radiant_score, dire_score, fetched_at)
            VALUES (:match_id, :league_id, :start_time, :duration, :game_mode,
                    :cluster, :radiant_win, :radiant_score, :dire_score, :fetched_at)
        """, match)


def upsert_players(players: list[dict]):
    """Insert player rows. Uses INSERT OR IGNORE keyed on (match_id, account_id)
    to avoid duplicates if we re-fetch a match."""
    with _conn() as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO players
                (match_id, account_id, name, hero_id, team_side, role_position,
                 kills, deaths, assists, gpm, xpm, last_hits, denies,
                 hero_damage, hero_healing, gold_spent, duration, won)
            VALUES
                (:match_id, :account_id, :name, :hero_id, :team_side, :role_position,
                 :kills, :deaths, :assists, :gpm, :xpm, :last_hits, :denies,
                 :hero_damage, :hero_healing, :gold_spent, :duration, :won)
        """, players)


def upsert_chat_messages(messages: list[dict]):
    """Insert chat messages. Uses INSERT OR IGNORE to avoid duplicates."""
    if not messages:
        return
    with _conn() as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO chat_messages
                (match_id, player_slot, player_name, time, message)
            VALUES
                (:match_id, :player_slot, :player_name, :time, :message)
        """, messages)


# Common boring messages to filter out from /quote (lowercase, no punctuation)
BORING_MESSAGES = {
    # Pre-game pleasantries
    "gl", "hf", "glhf", "gl hf", "hfhf", "hf hf", "gg hf", "glgl", "gl gl",
    "good luck", "have fun", "good luck have fun", "gwr",
    # Post-game
    "gg", "ggwp", "gg wp", "gege", "ge", "bg", "ggs",
    # Pause-related
    "g", "go", "rdy", "ready", "r", "sec", "1 sec", "wait", "w8", "pause", "unpause",
    "1", "2", "3", "4", "5",
    # Single characters / short stuff
    "ok", "k", "ty", "thx", "thanks", "np", "mb", "my bad", "lol", "lmao", "xd",
    "yes", "no", "y", "n", "hi", "hey", "hello", "bye", "cya",
}


def _normalize_message(msg: str) -> str:
    """Normalize a message for comparison: lowercase, strip punctuation/emotes."""
    import re
    # Lowercase
    msg = msg.lower().strip()
    # Remove common punctuation and emotes
    msg = re.sub(r'[!?.,;:\'"()]+', '', msg)  # punctuation
    msg = re.sub(r'[:;][dDpP3)(\]\[]+', '', msg)  # text emotes like :D :P ;) etc
    msg = re.sub(r'[xX]+[dD]+', '', msg)  # xD, XD, xd variations
    msg = re.sub(r'\s+', ' ', msg).strip()  # collapse whitespace
    return msg


def get_random_quote() -> dict | None:
    """Return a random chat message from the database, filtering out boring ones."""
    with _conn() as conn:
        # Fetch a batch of random candidates and filter in Python
        # (SQLite doesn't have good regex support for normalization)
        rows = conn.execute("""
            SELECT cm.message, cm.player_name, cm.time, cm.match_id
            FROM chat_messages cm
            WHERE LENGTH(TRIM(cm.message)) > 2
            ORDER BY RANDOM()
            LIMIT 100
        """).fetchall()

    for row in rows:
        normalized = _normalize_message(row["message"])
        if normalized and normalized not in BORING_MESSAGES and len(normalized) > 2:
            return dict(row)

    return None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def _week_start(week_offset: int = 0) -> tuple[int, int]:
    """Return (start_unix, end_unix) for a given week.

    week_offset=0 means the current week (Mon 00:00 UTC → Sun 23:59 UTC).
    week_offset=1 means the previous week, etc.
    """
    now = datetime.now(timezone.utc)
    # Monday of the current week
    monday = now - timedelta(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    # Shift back by week_offset weeks
    monday -= timedelta(weeks=week_offset)
    sunday_end = monday + timedelta(days=7) - timedelta(seconds=1)
    return int(monday.timestamp()), int(sunday_end.timestamp())


def _season_week_start(week_number: int) -> tuple[int, int]:
    """Return (start_unix, end_unix) for a specific season week.
    
    week_number=1 means the first week of the season (starting from SEASON_START_DATE).
    """
    from config import SEASON_START_DATE
    from datetime import datetime
    
    # Parse season start date
    season_start = datetime.strptime(SEASON_START_DATE, "%Y-%m-%d")
    season_start = season_start.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    
    # Find the Monday of the week containing season start
    season_monday = season_start - timedelta(days=season_start.weekday())
    
    # Calculate the start of the requested week (week_number is 1-indexed)
    target_monday = season_monday + timedelta(weeks=week_number - 1)
    target_sunday = target_monday + timedelta(days=7) - timedelta(seconds=1)
    
    return int(target_monday.timestamp()), int(target_sunday.timestamp())


def get_latest_week_stats(week_offset: int = 0) -> list[dict]:
    """Return aggregated per-player stats for the given week.

    Each dict contains all raw totals plus computed kda and fantasy_points.
    """
    start, end = _week_start(week_offset)
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                p.account_id,
                p.name,
                p.role_position,
                COUNT(*)                        AS games_played,
                SUM(p.kills)                    AS total_kills,
                SUM(p.deaths)                   AS total_deaths,
                SUM(p.assists)                  AS total_assists,
                AVG(p.gpm)                      AS gpm,
                AVG(p.xpm)                      AS xpm,
                AVG(p.last_hits)                AS last_hits,
                AVG(p.denies)                   AS denies,
                AVG(p.hero_damage)              AS hero_damage,
                AVG(p.hero_healing)             AS hero_healing,
                SUM(p.won)                      AS wins
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.start_time BETWEEN :start AND :end
            GROUP BY p.account_id
            ORDER BY gpm DESC
        """, {"start": start, "end": end}).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        # KDA = (kills + assists) / max(deaths, 1)
        d["kda"] = round((d["total_kills"] + d["total_assists"]) / max(d["total_deaths"], 1), 2)
        # Fantasy points (see fantasy.py for the formula)
        from fantasy import calculate_fantasy_points
        d["fantasy_points"] = calculate_fantasy_points(d)
        results.append(d)
    return results


def get_stats_for_season_week(week_number: int) -> list[dict]:
    """Return aggregated per-player stats for a specific season week."""
    start, end = _season_week_start(week_number)
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                p.account_id,
                p.name,
                p.role_position,
                COUNT(*)                        AS games_played,
                SUM(p.kills)                    AS total_kills,
                SUM(p.deaths)                   AS total_deaths,
                SUM(p.assists)                  AS total_assists,
                AVG(p.gpm)                      AS gpm,
                AVG(p.xpm)                      AS xpm,
                AVG(p.last_hits)                AS last_hits,
                AVG(p.denies)                   AS denies,
                AVG(p.hero_damage)              AS hero_damage,
                AVG(p.hero_healing)             AS hero_healing,
                SUM(p.won)                      AS wins
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.start_time BETWEEN :start AND :end
            GROUP BY p.account_id
            ORDER BY gpm DESC
        """, {"start": start, "end": end}).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        d["kda"] = round((d["total_kills"] + d["total_assists"]) / max(d["total_deaths"], 1), 2)
        from fantasy import calculate_fantasy_points
        d["fantasy_points"] = calculate_fantasy_points(d)
        results.append(d)
    return results


def get_all_time_stats() -> list[dict]:
    """Return aggregated per-player stats across all matches in the season."""
    from fantasy import calculate_fantasy_points
    
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                p.account_id,
                p.name,
                p.role_position,
                COUNT(*)                        AS games_played,
                SUM(p.kills)                    AS total_kills,
                SUM(p.deaths)                   AS total_deaths,
                SUM(p.assists)                  AS total_assists,
                AVG(p.gpm)                      AS gpm,
                AVG(p.xpm)                      AS xpm,
                AVG(p.last_hits)                AS last_hits,
                AVG(p.denies)                   AS denies,
                AVG(p.hero_damage)              AS hero_damage,
                AVG(p.hero_healing)             AS hero_healing,
                SUM(p.won)                      AS wins
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            GROUP BY p.account_id
            ORDER BY gpm DESC
        """).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        d["kda"] = round((d["total_kills"] + d["total_assists"]) / max(d["total_deaths"], 1), 2)
        d["fantasy_points"] = calculate_fantasy_points(d)
        results.append(d)
    return results


def get_latest_week_stats_new() -> list[dict]:
    """Return stats from the most recent week that has data."""
    from fantasy import calculate_fantasy_points
    
    with _conn() as conn:
        # Find the most recent match
        latest = conn.execute("SELECT MAX(start_time) as max_time FROM matches").fetchone()
        if not latest or not latest["max_time"]:
            return []
        
        latest_time = latest["max_time"]
        # Find the Monday of the week containing that match
        from datetime import datetime
        dt = datetime.fromtimestamp(latest_time, tz=timezone.utc)
        monday = dt - timedelta(days=dt.weekday())
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        sunday = monday + timedelta(days=7) - timedelta(seconds=1)
        
        start_ts = int(monday.timestamp())
        end_ts = int(sunday.timestamp())
        
        # Get stats for that week
        rows = conn.execute("""
            SELECT
                p.account_id,
                p.name,
                p.role_position,
                COUNT(*)                        AS games_played,
                SUM(p.kills)                    AS total_kills,
                SUM(p.deaths)                   AS total_deaths,
                SUM(p.assists)                  AS total_assists,
                AVG(p.gpm)                      AS gpm,
                AVG(p.xpm)                      AS xpm,
                AVG(p.last_hits)                AS last_hits,
                AVG(p.denies)                   AS denies,
                AVG(p.hero_damage)              AS hero_damage,
                AVG(p.hero_healing)             AS hero_healing,
                SUM(p.won)                      AS wins
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.start_time BETWEEN ? AND ?
            GROUP BY p.account_id
            ORDER BY gpm DESC
        """, (start_ts, end_ts)).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        d["kda"] = round((d["total_kills"] + d["total_assists"]) / max(d["total_deaths"], 1), 2)
        d["fantasy_points"] = calculate_fantasy_points(d)
        results.append(d)
    return results


def get_all_weeks() -> list[dict]:
    """Return a list of distinct weeks that have data, with match counts."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                strftime('%Y-%m-%d', start_time, 'unixepoch') AS match_date,
                COUNT(*) AS match_count
            FROM matches
            GROUP BY date(start_time, 'unixepoch', 'weekday 1')
            ORDER BY match_date DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_latest_season_week() -> int | None:
    """Return the latest season week number that has data, or None if no data."""
    from config import SEASON_START_DATE
    from datetime import datetime

    with _conn() as conn:
        row = conn.execute("""
            SELECT MAX(start_time) AS latest_time FROM matches
        """).fetchone()

    if not row or not row["latest_time"]:
        return None

    # Calculate which season week this timestamp falls into
    season_start = datetime.strptime(SEASON_START_DATE, "%Y-%m-%d")
    season_start = season_start.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    season_monday = season_start - timedelta(days=season_start.weekday())

    latest_dt = datetime.fromtimestamp(row["latest_time"], tz=timezone.utc)
    days_since_start = (latest_dt - season_monday).days
    week_number = (days_since_start // 7) + 1

    return max(1, week_number)


def get_matches_for_week(week_offset: int = 0) -> list[dict]:
    """Return all matches for a given week with basic info."""
    start, end = _week_start(week_offset)
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                match_id,
                start_time,
                duration,
                radiant_win,
                radiant_score,
                dire_score
            FROM matches
            WHERE start_time BETWEEN :start AND :end
            ORDER BY start_time DESC
        """, {"start": start, "end": end}).fetchall()
    return [dict(r) for r in rows]


def get_matches_for_season_week(week_number: int) -> list[dict]:
    """Return all matches for a specific season week (1-indexed)."""
    start, end = _season_week_start(week_number)
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                match_id,
                start_time,
                duration,
                radiant_win,
                radiant_score,
                dire_score
            FROM matches
            WHERE start_time BETWEEN :start AND :end
            ORDER BY start_time DESC
        """, {"start": start, "end": end}).fetchall()
    return [dict(r) for r in rows]


def get_latest_matches() -> list[dict]:
    """Return all matches from the most recent week that has data."""
    with _conn() as conn:
        # Find the most recent match
        latest = conn.execute("SELECT MAX(start_time) as max_time FROM matches").fetchone()
        if not latest or not latest["max_time"]:
            return []
        
        latest_time = latest["max_time"]
        # Find the Monday of the week containing that match
        from datetime import datetime
        dt = datetime.fromtimestamp(latest_time, tz=timezone.utc)
        monday = dt - timedelta(days=dt.weekday())
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        sunday = monday + timedelta(days=7) - timedelta(seconds=1)
        
        # Get all matches in that week
        rows = conn.execute("""
            SELECT
                match_id,
                start_time,
                duration,
                radiant_win,
                radiant_score,
                dire_score
            FROM matches
            WHERE start_time BETWEEN :start AND :end
            ORDER BY start_time DESC
        """, {"start": int(monday.timestamp()), "end": int(sunday.timestamp())}).fetchall()
    return [dict(r) for r in rows]


def get_all_matches() -> list[dict]:
    """Return all matches across the entire season."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                match_id,
                start_time,
                duration,
                radiant_win,
                radiant_score,
                dire_score
            FROM matches
            ORDER BY start_time DESC
        """).fetchall()
    return [dict(r) for r in rows]


def nuke_data():
    """Delete all rows from players and matches tables."""
    with _conn() as conn:
        conn.execute("DELETE FROM players")
        conn.execute("DELETE FROM matches")
    logger.info("All data nuked from players and matches tables.")


def match_exists(match_id: int) -> bool:
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    return row is not None
