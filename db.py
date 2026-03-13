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
            -- Division config per Discord server
            CREATE TABLE IF NOT EXISTS divisions (
                guild_id        INTEGER PRIMARY KEY,  -- Discord server ID
                league_id       INTEGER NOT NULL,
                region          TEXT NOT NULL,        -- 'us_west' or 'us_east'
                game_mode       TEXT NOT NULL,        -- 'cm' or 'ad'
                season_start    TEXT NOT NULL,        -- 'YYYY-MM-DD'
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS matches (
                match_id        INTEGER PRIMARY KEY,
                guild_id        INTEGER NOT NULL DEFAULT 0,  -- Links to division
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
        """)

        # --- Migration: add guild_id column to existing matches table if missing ---
        cursor = conn.execute("PRAGMA table_info(matches)")
        columns = [row[1] for row in cursor.fetchall()]
        if "guild_id" not in columns:
            logger.info("Migrating: adding guild_id column to matches table")
            conn.execute("ALTER TABLE matches ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")

        # --- Migration: add scold_channel_id column to divisions if missing ---
        cursor = conn.execute("PRAGMA table_info(divisions)")
        div_columns = [row[1] for row in cursor.fetchall()]
        if "scold_channel_id" not in div_columns:
            logger.info("Migrating: adding scold_channel_id column to divisions table")
            conn.execute("ALTER TABLE divisions ADD COLUMN scold_channel_id INTEGER DEFAULT NULL")

        conn.executescript("""

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
            CREATE INDEX IF NOT EXISTS idx_matches_guild   ON matches(guild_id);

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
# Division management
# ---------------------------------------------------------------------------

def get_division(guild_id: int) -> dict | None:
    """Get division config for a guild, or None if not configured."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM divisions WHERE guild_id = ?", (guild_id,)
        ).fetchone()
    return dict(row) if row else None


def upsert_division(guild_id: int, league_id: int, region: str, game_mode: str, season_start: str, scold_channel_id: int | None = None):
    """Create or update a division config."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO divisions (guild_id, league_id, region, game_mode, season_start, scold_channel_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(guild_id) DO UPDATE SET
                league_id = excluded.league_id,
                region = excluded.region,
                game_mode = excluded.game_mode,
                season_start = excluded.season_start,
                scold_channel_id = excluded.scold_channel_id
        """, (guild_id, league_id, region, game_mode, season_start, scold_channel_id))


def get_all_divisions() -> list[dict]:
    """Get all configured divisions."""
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM divisions").fetchall()
    return [dict(r) for r in rows]


def get_scold_channel(guild_id: int) -> int | None:
    """Get the scold channel ID for a guild, or None if not set."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT scold_channel_id FROM divisions WHERE guild_id = ?", (guild_id,)
        ).fetchone()
    if row and row["scold_channel_id"]:
        return row["scold_channel_id"]
    return None


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def upsert_match(match: dict):
    """Insert a match row, skip if match_id already exists."""
    with _conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO matches
                (match_id, guild_id, league_id, start_time, duration, game_mode, cluster,
                 radiant_win, radiant_score, dire_score, fetched_at)
            VALUES (:match_id, :guild_id, :league_id, :start_time, :duration, :game_mode,
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


def get_random_quote(guild_id: int) -> dict | None:
    """Return a random chat message from the database, filtering out boring ones."""
    with _conn() as conn:
        # Fetch a batch of random candidates and filter in Python
        # (SQLite doesn't have good regex support for normalization)
        rows = conn.execute("""
            SELECT cm.message, cm.player_name, cm.time, cm.match_id
            FROM chat_messages cm
            JOIN matches m ON cm.match_id = m.match_id
            WHERE m.guild_id = ?
              AND LENGTH(TRIM(cm.message)) > 2
            ORDER BY RANDOM()
            LIMIT 100
        """, (guild_id,)).fetchall()

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


def _season_week_start(week_number: int, season_start_date: str) -> tuple[int, int]:
    """Return (start_unix, end_unix) for a specific season week.

    week_number=1 means the first week of the season.
    season_start_date is a string in 'YYYY-MM-DD' format.
    """
    # Parse season start date
    season_start = datetime.strptime(season_start_date, "%Y-%m-%d")
    season_start = season_start.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)

    # Find the Monday of the week containing season start
    season_monday = season_start - timedelta(days=season_start.weekday())

    # Calculate the start of the requested week (week_number is 1-indexed)
    target_monday = season_monday + timedelta(weeks=week_number - 1)
    target_sunday = target_monday + timedelta(days=7) - timedelta(seconds=1)

    return int(target_monday.timestamp()), int(target_sunday.timestamp())


def get_latest_week_stats(guild_id: int, week_offset: int = 0) -> list[dict]:
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
            WHERE m.guild_id = :guild_id
              AND m.start_time BETWEEN :start AND :end
            GROUP BY p.account_id
            ORDER BY gpm DESC
        """, {"guild_id": guild_id, "start": start, "end": end}).fetchall()

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


def get_stats_for_season_week(guild_id: int, week_number: int, season_start_date: str) -> list[dict]:
    """Return aggregated per-player stats for a specific season week."""
    start, end = _season_week_start(week_number, season_start_date)
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
            WHERE m.guild_id = :guild_id
              AND m.start_time BETWEEN :start AND :end
            GROUP BY p.account_id
            ORDER BY gpm DESC
        """, {"guild_id": guild_id, "start": start, "end": end}).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        d["kda"] = round((d["total_kills"] + d["total_assists"]) / max(d["total_deaths"], 1), 2)
        from fantasy import calculate_fantasy_points
        d["fantasy_points"] = calculate_fantasy_points(d)
        results.append(d)
    return results


def get_all_time_stats(guild_id: int) -> list[dict]:
    """Return aggregated per-player stats across all matches for a division."""
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
            WHERE m.guild_id = ?
            GROUP BY p.account_id
            ORDER BY gpm DESC
        """, (guild_id,)).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        d["kda"] = round((d["total_kills"] + d["total_assists"]) / max(d["total_deaths"], 1), 2)
        d["fantasy_points"] = calculate_fantasy_points(d)
        results.append(d)
    return results


def get_all_weeks(guild_id: int) -> list[dict]:
    """Return a list of distinct weeks that have data, with match counts."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                strftime('%Y-%m-%d', start_time, 'unixepoch') AS match_date,
                COUNT(*) AS match_count
            FROM matches
            WHERE guild_id = ?
            GROUP BY date(start_time, 'unixepoch', 'weekday 1')
            ORDER BY match_date DESC
        """, (guild_id,)).fetchall()
    return [dict(r) for r in rows]


def get_latest_season_week(guild_id: int, season_start_date: str) -> int | None:
    """Return the latest season week number that has data, or None if no data."""
    with _conn() as conn:
        row = conn.execute("""
            SELECT MAX(start_time) AS latest_time FROM matches WHERE guild_id = ?
        """, (guild_id,)).fetchone()

    if not row or not row["latest_time"]:
        return None

    # Calculate which season week this timestamp falls into
    season_start = datetime.strptime(season_start_date, "%Y-%m-%d")
    season_start = season_start.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    season_monday = season_start - timedelta(days=season_start.weekday())

    latest_dt = datetime.fromtimestamp(row["latest_time"], tz=timezone.utc)
    days_since_start = (latest_dt - season_monday).days
    week_number = (days_since_start // 7) + 1

    return max(1, week_number)


def get_matches_for_week(guild_id: int, week_offset: int = 0) -> list[dict]:
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
            WHERE guild_id = :guild_id
              AND start_time BETWEEN :start AND :end
            ORDER BY start_time DESC
        """, {"guild_id": guild_id, "start": start, "end": end}).fetchall()
    return [dict(r) for r in rows]


def get_matches_for_season_week(guild_id: int, week_number: int, season_start_date: str) -> list[dict]:
    """Return all matches for a specific season week (1-indexed)."""
    start, end = _season_week_start(week_number, season_start_date)
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
            WHERE guild_id = :guild_id
              AND start_time BETWEEN :start AND :end
            ORDER BY start_time DESC
        """, {"guild_id": guild_id, "start": start, "end": end}).fetchall()
    return [dict(r) for r in rows]


def get_latest_matches(guild_id: int) -> list[dict]:
    """Return all matches from the most recent week that has data for a division."""
    with _conn() as conn:
        # Find the most recent match for this guild
        latest = conn.execute(
            "SELECT MAX(start_time) as max_time FROM matches WHERE guild_id = ?",
            (guild_id,)
        ).fetchone()
        if not latest or not latest["max_time"]:
            return []

        latest_time = latest["max_time"]
        # Find the Monday of the week containing that match
        dt = datetime.fromtimestamp(latest_time, tz=timezone.utc)
        monday = dt - timedelta(days=dt.weekday())
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        sunday = monday + timedelta(days=7) - timedelta(seconds=1)

        # Get all matches in that week for this guild
        rows = conn.execute("""
            SELECT
                match_id,
                start_time,
                duration,
                radiant_win,
                radiant_score,
                dire_score
            FROM matches
            WHERE guild_id = :guild_id
              AND start_time BETWEEN :start AND :end
            ORDER BY start_time DESC
        """, {"guild_id": guild_id, "start": int(monday.timestamp()), "end": int(sunday.timestamp())}).fetchall()
    return [dict(r) for r in rows]


def get_all_matches(guild_id: int) -> list[dict]:
    """Return all matches for a division."""
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
            WHERE guild_id = ?
            ORDER BY start_time DESC
        """, (guild_id,)).fetchall()
    return [dict(r) for r in rows]


def nuke_data(guild_id: int):
    """Delete all match/player/chat data for a specific division."""
    with _conn() as conn:
        # Get match IDs for this guild to delete related data
        match_ids = conn.execute(
            "SELECT match_id FROM matches WHERE guild_id = ?", (guild_id,)
        ).fetchall()
        match_id_list = [r["match_id"] for r in match_ids]

        if match_id_list:
            placeholders = ",".join("?" for _ in match_id_list)
            conn.execute(f"DELETE FROM players WHERE match_id IN ({placeholders})", match_id_list)
            conn.execute(f"DELETE FROM chat_messages WHERE match_id IN ({placeholders})", match_id_list)

        conn.execute("DELETE FROM matches WHERE guild_id = ?", (guild_id,))
    logger.info("Data nuked for guild %d", guild_id)


def match_exists(guild_id: int, match_id: int) -> bool:
    """Check if a match exists for a specific guild."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM matches WHERE guild_id = ? AND match_id = ?",
            (guild_id, match_id)
        ).fetchone()
    return row is not None
