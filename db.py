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
                fetched_at      TEXT NOT NULL,       -- ISO timestamp of when we stored it
                replay_salt     INTEGER DEFAULT 0    -- from OpenDota; used to build replay download URL
            );
        """)

        # --- Migration: add columns to existing matches table if missing ---
        cursor = conn.execute("PRAGMA table_info(matches)")
        columns = [row[1] for row in cursor.fetchall()]
        if "guild_id" not in columns:
            logger.info("Migrating: adding guild_id column to matches table")
            conn.execute("ALTER TABLE matches ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")
        if "replay_salt" not in columns:
            logger.info("Migrating: adding replay_salt column to matches table")
            conn.execute("ALTER TABLE matches ADD COLUMN replay_salt INTEGER DEFAULT 0")

        # --- Migration: add stat columns to players if missing ---
        cursor = conn.execute("PRAGMA table_info(players)")
        player_columns = [row[1] for row in cursor.fetchall()]
        new_player_cols = [
            ("obs_placed",              "INTEGER DEFAULT 0"),
            ("sen_placed",              "INTEGER DEFAULT 0"),
            ("observer_kills",          "INTEGER DEFAULT 0"),
            ("sentry_kills",            "INTEGER DEFAULT 0"),
            ("tower_kills",             "INTEGER DEFAULT 0"),
            ("roshans_killed",          "INTEGER DEFAULT 0"),
            ("firstblood_claimed",      "INTEGER DEFAULT 0"),
            ("teamfight_participation", "REAL DEFAULT 0"),
            ("stuns",                   "REAL DEFAULT 0"),
            ("camps_stacked",              "INTEGER DEFAULT 0"),
            ("rune_pickups",              "INTEGER DEFAULT 0"),
            ("tormentor_kills",           "INTEGER DEFAULT 0"),
            ("watcher_captures",          "INTEGER DEFAULT 0"),
            ("team_first_tormentor_time", "INTEGER DEFAULT -1"),  # seconds; -1 = not taken
            ("player_slot",               "INTEGER DEFAULT -1"),  # 0-4 radiant, 128-132 dire
            ("defensive_item_uses",       "INTEGER DEFAULT 0"),   # sum of activations across pipe/crimson/lotus/glimmer/force/pavise/solar/halberd/sphere
        ]
        for col, col_type in new_player_cols:
            if col not in player_columns:
                logger.info("Migrating: adding %s column to players table", col)
                conn.execute(f"ALTER TABLE players ADD COLUMN {col} {col_type}")

        # --- Migration: add scold_channel_id column to divisions if missing ---
        cursor = conn.execute("PRAGMA table_info(divisions)")
        div_columns = [row[1] for row in cursor.fetchall()]
        if "scold_channel_id" not in div_columns:
            logger.info("Migrating: adding scold_channel_id column to divisions table")
            conn.execute("ALTER TABLE divisions ADD COLUMN scold_channel_id INTEGER DEFAULT NULL")

        # --- Migration: add ability_name column to draft_picks if missing ---
        cursor = conn.execute("PRAGMA table_info(draft_picks)")
        draft_columns = [row[1] for row in cursor.fetchall()]
        if "ability_name" not in draft_columns:
            logger.info("Migrating: adding ability_name column to draft_picks table")
            conn.execute("ALTER TABLE draft_picks ADD COLUMN ability_name TEXT DEFAULT ''")

        # --- Migration: add pick_type column ('ability' or 'model') ---
        if "pick_type" not in draft_columns:
            logger.info("Migrating: adding pick_type column to draft_picks table")
            conn.execute("ALTER TABLE draft_picks ADD COLUMN pick_type TEXT DEFAULT 'ability'")

        # --- Migration: add m_n_ability_id (opaque Source 2 pick ID) so we
        #     can retroactively resolve ability_name from windrun bootstrap. ---
        if "m_n_ability_id" not in draft_columns:
            logger.info("Migrating: adding m_n_ability_id column to draft_picks table")
            conn.execute("ALTER TABLE draft_picks ADD COLUMN m_n_ability_id INTEGER DEFAULT 0")

        conn.executescript("""

            CREATE TABLE IF NOT EXISTS draft_picks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id      INTEGER NOT NULL,
                pick_order    INTEGER NOT NULL,   -- 0-indexed sequence across the whole draft
                ability_id    INTEGER NOT NULL DEFAULT 0,  -- legacy; 0 for replay-parsed picks
                ability_name  TEXT    NOT NULL DEFAULT '',  -- e.g. "morphling_waveform" or "death_prophet" for models
                pick_type     TEXT    NOT NULL DEFAULT 'ability',  -- 'ability' or 'model'
                m_n_ability_id INTEGER NOT NULL DEFAULT 0,  -- opaque per-pick ID for backfill lookups
                player_id     INTEGER NOT NULL,   -- draft seat 0-9 (0-4 radiant, 5-9 dire)
                UNIQUE(match_id, pick_order)
            );

            CREATE INDEX IF NOT EXISTS idx_draft_match ON draft_picks(match_id);

            -- Bootstrap lookup: m_nAbilityID (opaque per-pick ID from Source 2
            -- demo) → ability name + hero_id (for models). Populated by
            -- cross-referencing with windrun.io's per-match API. Stable across
            -- matches — once we have enough rows, no further bootstrap needed.
            CREATE TABLE IF NOT EXISTS ability_id_mapping (
                m_n_ability_id INTEGER PRIMARY KEY,
                pick_type      TEXT NOT NULL,    -- 'ability' or 'model'
                ability_name   TEXT NOT NULL DEFAULT '',
                hero_id        INTEGER NOT NULL DEFAULT 0
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
                duration                INTEGER DEFAULT 0,  -- match duration (seconds), duplicated for convenience
                won                     INTEGER DEFAULT 0,  -- 1 if this player's team won
                obs_placed              INTEGER DEFAULT 0,
                sen_placed              INTEGER DEFAULT 0,
                observer_kills          INTEGER DEFAULT 0,
                sentry_kills            INTEGER DEFAULT 0,
                tower_kills             INTEGER DEFAULT 0,
                roshans_killed          INTEGER DEFAULT 0,
                firstblood_claimed      INTEGER DEFAULT 0,
                teamfight_participation REAL DEFAULT 0,
                stuns                   REAL DEFAULT 0,
                camps_stacked           INTEGER DEFAULT 0,
                rune_pickups            INTEGER DEFAULT 0,
                tormentor_kills           INTEGER DEFAULT 0,
                watcher_captures          INTEGER DEFAULT 0,
                team_first_tormentor_time INTEGER DEFAULT -1,
                player_slot               INTEGER DEFAULT -1,  -- 0-4 radiant, 128-132 dire
                defensive_item_uses       INTEGER DEFAULT 0    -- pipe/crimson/lotus/glimmer/force/pavise/solar/halberd/sphere activations
            );

            CREATE INDEX IF NOT EXISTS idx_players_match   ON players(match_id);
            CREATE INDEX IF NOT EXISTS idx_players_account ON players(account_id);
            CREATE INDEX IF NOT EXISTS idx_matches_start   ON matches(start_time);
            CREATE INDEX IF NOT EXISTS idx_matches_guild   ON matches(guild_id);

            -- Cached internal-rating results for /players. Populated by
            -- /refresh_ratings; one row per account_id (global cache).
            CREATE TABLE IF NOT EXISTS player_ratings_cache (
                account_id        INTEGER PRIMARY KEY,
                name              TEXT,
                internal_rating   INTEGER,
                raw_windrun       REAL,
                mmr_estimate      INTEGER,
                ad_last_year      INTEGER,
                ranked_last_year  INTEGER,
                explanation       TEXT,
                updated_at        TEXT NOT NULL
            );

            -- Manual skill-rating overrides for /lookup. Used to fix smurfs,
            -- non-NA leaderboard players, or any case where the API-inferred
            -- numbers are wrong. Scope is GLOBAL (account_id is the key).
            CREATE TABLE IF NOT EXISTS skill_overrides (
                account_id      INTEGER PRIMARY KEY,
                windrun_rating  REAL,
                ranked_mmr      INTEGER,
                note            TEXT,
                set_by_user_id  INTEGER,
                set_by_name     TEXT,
                set_at          TEXT NOT NULL
            );

            -- Player draft costs per season. (guild_id, season_start) scopes a season;
            -- account_id is the OpenDota account ID parsed from the Dotabuff URL.
            CREATE TABLE IF NOT EXISTS player_costs (
                guild_id      INTEGER NOT NULL,
                season_start  TEXT NOT NULL,
                account_id    INTEGER NOT NULL,
                cost          INTEGER NOT NULL,
                captain       TEXT,
                mmr           INTEGER,
                PRIMARY KEY (guild_id, season_start, account_id)
            );

            CREATE INDEX IF NOT EXISTS idx_player_costs_lookup
                ON player_costs(guild_id, season_start);

            -- Calibration sample for the ranked-MMR ↔ windrun-rating model.
            -- Populated by build_mmr_calibration.py; consumed by fit_mmr_model.py
            -- and (eventually) the conversion used in /lookup's internal rating.
            CREATE TABLE IF NOT EXISTS mmr_calibration (
                account_id        INTEGER PRIMARY KEY,
                windrun_rating    REAL NOT NULL,
                rank_tier         INTEGER,
                leaderboard_rank  INTEGER,
                mmr_estimate      INTEGER,
                ad_games          INTEGER,
                ranked_games      INTEGER,
                region            TEXT,
                fetched_at        TEXT NOT NULL
            );

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
                 radiant_win, radiant_score, dire_score, fetched_at, replay_salt)
            VALUES (:match_id, :guild_id, :league_id, :start_time, :duration, :game_mode,
                    :cluster, :radiant_win, :radiant_score, :dire_score, :fetched_at,
                    :replay_salt)
        """, match)


def upsert_players(players: list[dict]):
    """Insert player rows. Uses INSERT OR IGNORE keyed on (match_id, account_id)
    to avoid duplicates if we re-fetch a match."""
    with _conn() as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO players
                (match_id, account_id, name, hero_id, team_side, role_position,
                 kills, deaths, assists, gpm, xpm, last_hits, denies,
                 hero_damage, hero_healing, gold_spent, duration, won,
                 obs_placed, sen_placed, observer_kills, sentry_kills,
                 tower_kills, roshans_killed, firstblood_claimed,
                 teamfight_participation, stuns, camps_stacked, rune_pickups,
                 tormentor_kills, watcher_captures, team_first_tormentor_time,
                 player_slot, defensive_item_uses)
            VALUES
                (:match_id, :account_id, :name, :hero_id, :team_side, :role_position,
                 :kills, :deaths, :assists, :gpm, :xpm, :last_hits, :denies,
                 :hero_damage, :hero_healing, :gold_spent, :duration, :won,
                 :obs_placed, :sen_placed, :observer_kills, :sentry_kills,
                 :tower_kills, :roshans_killed, :firstblood_claimed,
                 :teamfight_participation, :stuns, :camps_stacked, :rune_pickups,
                 :tormentor_kills, :watcher_captures, :team_first_tormentor_time,
                 :player_slot, :defensive_item_uses)
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
                AVG(p.obs_placed)               AS obs_placed,
                AVG(p.sen_placed)               AS sen_placed,
                AVG(p.observer_kills)           AS observer_kills,
                AVG(p.sentry_kills)             AS sentry_kills,
                AVG(p.tower_kills)              AS tower_kills,
                AVG(p.roshans_killed)           AS roshans_killed,
                AVG(p.firstblood_claimed)       AS firstblood_claimed,
                AVG(p.teamfight_participation)  AS teamfight_participation,
                AVG(p.stuns)                    AS stuns,
                AVG(p.stuns * 60.0 / NULLIF(p.duration, 0))
                                                AS stuns_per_min,
                AVG(p.camps_stacked)            AS camps_stacked,
                AVG(p.rune_pickups)             AS rune_pickups,
                AVG(p.tormentor_kills)          AS tormentor_kills,
                AVG(p.watcher_captures)         AS watcher_captures,
                AVG(p.defensive_item_uses)      AS defensive_item_uses,
                AVG(CASE WHEN p.team_first_tormentor_time > 0
                         THEN p.team_first_tormentor_time ELSE NULL END)
                                                AS avg_first_tormentor_time,
                AVG(p.duration)                 AS avg_duration,
                AVG(CAST(p.hero_damage AS REAL) / NULLIF(match_dmg.total_damage, 0))
                                                AS avg_pct_damage,
                SUM(p.won)                      AS wins
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            JOIN (
                SELECT match_id, SUM(hero_damage) AS total_damage
                FROM players GROUP BY match_id
            ) AS match_dmg ON p.match_id = match_dmg.match_id
            WHERE m.guild_id = :guild_id
              AND m.start_time BETWEEN :start AND :end
            GROUP BY p.account_id
            ORDER BY gpm DESC
        """, {"guild_id": guild_id, "start": start, "end": end}).fetchall()

    diffs = _compute_player_diffs(guild_id, start, end)
    results = []
    for r in rows:
        d = dict(r)
        # KDA = (kills + assists) / max(deaths, 1)
        d["kda"] = round((d["total_kills"] + d["total_assists"]) / max(d["total_deaths"], 1), 2)
        # Fantasy points (see fantasy.py for the formula)
        from fantasy import calculate_fantasy_points
        d["fantasy_points"] = calculate_fantasy_points(d)
        d["diff"] = diffs.get(d["account_id"], 0.0)
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
                AVG(p.obs_placed)               AS obs_placed,
                AVG(p.sen_placed)               AS sen_placed,
                AVG(p.observer_kills)           AS observer_kills,
                AVG(p.sentry_kills)             AS sentry_kills,
                AVG(p.tower_kills)              AS tower_kills,
                AVG(p.roshans_killed)           AS roshans_killed,
                AVG(p.firstblood_claimed)       AS firstblood_claimed,
                AVG(p.teamfight_participation)  AS teamfight_participation,
                AVG(p.stuns)                    AS stuns,
                AVG(p.stuns * 60.0 / NULLIF(p.duration, 0))
                                                AS stuns_per_min,
                AVG(p.camps_stacked)            AS camps_stacked,
                AVG(p.rune_pickups)             AS rune_pickups,
                AVG(p.tormentor_kills)          AS tormentor_kills,
                AVG(p.watcher_captures)         AS watcher_captures,
                AVG(p.defensive_item_uses)      AS defensive_item_uses,
                AVG(CASE WHEN p.team_first_tormentor_time > 0
                         THEN p.team_first_tormentor_time ELSE NULL END)
                                                AS avg_first_tormentor_time,
                AVG(p.duration)                 AS avg_duration,
                AVG(CAST(p.hero_damage AS REAL) / NULLIF(match_dmg.total_damage, 0))
                                                AS avg_pct_damage,
                SUM(p.won)                      AS wins
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            JOIN (
                SELECT match_id, SUM(hero_damage) AS total_damage
                FROM players GROUP BY match_id
            ) AS match_dmg ON p.match_id = match_dmg.match_id
            WHERE m.guild_id = :guild_id
              AND m.start_time BETWEEN :start AND :end
            GROUP BY p.account_id
            ORDER BY gpm DESC
        """, {"guild_id": guild_id, "start": start, "end": end}).fetchall()

    diffs = _compute_player_diffs(guild_id, start, end)
    results = []
    for r in rows:
        d = dict(r)
        d["kda"] = round((d["total_kills"] + d["total_assists"]) / max(d["total_deaths"], 1), 2)
        from fantasy import calculate_fantasy_points
        d["fantasy_points"] = calculate_fantasy_points(d)
        d["diff"] = diffs.get(d["account_id"], 0.0)
        results.append(d)
    costs = get_player_costs(guild_id, season_start_date)
    results = _attach_costs(results, costs)
    _compute_value(results)
    attendance_map = _compute_attendance(guild_id, start, end, costs)
    for s in results:
        s["attendance"] = attendance_map.get(s["account_id"])
    return results


def get_all_time_stats(guild_id: int, season_start_date: str = None) -> list[dict]:
    """Return aggregated per-player stats across all matches for a division.

    If season_start_date is provided, only includes matches from that date onwards.
    """
    from fantasy import calculate_fantasy_points

    # Calculate season start timestamp if provided
    # Week 0 = one week before season_monday (since _season_week_start is 1-indexed)
    if season_start_date:
        season_start = datetime.strptime(season_start_date, "%Y-%m-%d")
        season_start = season_start.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        season_monday = season_start - timedelta(days=season_start.weekday())
        week_zero_start = season_monday - timedelta(weeks=1)
        start_ts = int(week_zero_start.timestamp())
    else:
        start_ts = 0

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
                AVG(p.obs_placed)               AS obs_placed,
                AVG(p.sen_placed)               AS sen_placed,
                AVG(p.observer_kills)           AS observer_kills,
                AVG(p.sentry_kills)             AS sentry_kills,
                AVG(p.tower_kills)              AS tower_kills,
                AVG(p.roshans_killed)           AS roshans_killed,
                AVG(p.firstblood_claimed)       AS firstblood_claimed,
                AVG(p.teamfight_participation)  AS teamfight_participation,
                AVG(p.stuns)                    AS stuns,
                AVG(p.stuns * 60.0 / NULLIF(p.duration, 0))
                                                AS stuns_per_min,
                AVG(p.camps_stacked)            AS camps_stacked,
                AVG(p.rune_pickups)             AS rune_pickups,
                AVG(p.tormentor_kills)          AS tormentor_kills,
                AVG(p.watcher_captures)         AS watcher_captures,
                AVG(p.defensive_item_uses)      AS defensive_item_uses,
                AVG(CASE WHEN p.team_first_tormentor_time > 0
                         THEN p.team_first_tormentor_time ELSE NULL END)
                                                AS avg_first_tormentor_time,
                AVG(p.duration)                 AS avg_duration,
                AVG(CAST(p.hero_damage AS REAL) / NULLIF(match_dmg.total_damage, 0))
                                                AS avg_pct_damage,
                SUM(p.won)                      AS wins
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            JOIN (
                SELECT match_id, SUM(hero_damage) AS total_damage
                FROM players GROUP BY match_id
            ) AS match_dmg ON p.match_id = match_dmg.match_id
            WHERE m.guild_id = ?
              AND m.start_time >= ?
            GROUP BY p.account_id
            ORDER BY gpm DESC
        """, (guild_id, start_ts)).fetchall()

    diffs = _compute_player_diffs(guild_id, start_ts)
    results = []
    for r in rows:
        d = dict(r)
        d["kda"] = round((d["total_kills"] + d["total_assists"]) / max(d["total_deaths"], 1), 2)
        d["fantasy_points"] = calculate_fantasy_points(d)
        d["diff"] = diffs.get(d["account_id"], 0.0)
        results.append(d)
    if season_start_date:
        costs = get_player_costs(guild_id, season_start_date)
        results = _attach_costs(results, costs)
        _compute_value(results)
        attendance_map = _compute_attendance(guild_id, start_ts, None, costs)
        for s in results:
            s["attendance"] = attendance_map.get(s["account_id"])
    else:
        for s in results:
            s["attendance"] = None
    return results


def _per_match_fp(row: dict) -> float:
    """Compute fantasy points for a single player-match row.

    `row` must include the player stat columns plus `match_duration` (seconds).
    """
    from fantasy import calculate_fantasy_points
    return calculate_fantasy_points({
        "games_played":            1,
        "total_kills":             row["kills"],
        "total_deaths":            row["deaths"],
        "total_assists":           row["assists"],
        "last_hits":               row["last_hits"],
        "denies":                  row["denies"],
        "gpm":                     row["gpm"],
        "xpm":                     row["xpm"],
        "hero_damage":             row["hero_damage"],
        "hero_healing":            row["hero_healing"],
        "obs_placed":              row["obs_placed"],
        "sen_placed":              row["sen_placed"],
        "observer_kills":          row["observer_kills"],
        "sentry_kills":            row["sentry_kills"],
        "tower_kills":             row["tower_kills"],
        "roshans_killed":          row["roshans_killed"],
        "firstblood_claimed":      row["firstblood_claimed"],
        "teamfight_participation": row["teamfight_participation"],
        "stuns":                   row["stuns"],
        "camps_stacked":           row["camps_stacked"],
        "rune_pickups":            row["rune_pickups"],
        "defensive_item_uses":     row["defensive_item_uses"],
        "avg_duration":            row["match_duration"],
    })


def _compute_player_diffs(guild_id: int, start: int, end: int | None = None) -> dict[int, float]:
    """For every player with matches in the time range, return the average
    per-match fantasy-points diff vs. the 4 same-side teammates.

    Returns {account_id: avg_diff}. Players with no qualifying matches are absent.
    """
    if end is None:
        sql = """
            SELECT p.*, m.duration AS match_duration
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.guild_id = ? AND m.start_time >= ?
        """
        params: tuple = (guild_id, start)
    else:
        sql = """
            SELECT p.*, m.duration AS match_duration
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.guild_id = ? AND m.start_time BETWEEN ? AND ?
        """
        params = (guild_id, start, end)

    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        return {}

    by_match: dict[int, list[dict]] = {}
    for r in rows:
        by_match.setdefault(r["match_id"], []).append(dict(r))

    diffs_by_account: dict[int, list[float]] = {}
    for match_rows in by_match.values():
        fps = {r["account_id"]: _per_match_fp(r) for r in match_rows}
        teams: dict[str, list[int]] = {}
        for r in match_rows:
            teams.setdefault(r["team_side"], []).append(r["account_id"])
        for r in match_rows:
            aid = r["account_id"]
            teammates = [a for a in teams[r["team_side"]] if a != aid]
            if not teammates:
                continue
            mate_avg = sum(fps[a] for a in teammates) / len(teammates)
            diffs_by_account.setdefault(aid, []).append(fps[aid] - mate_avg)

    return {aid: sum(ds) / len(ds) for aid, ds in diffs_by_account.items() if ds}


def get_guild_player_account_ids(guild_id: int) -> list[int]:
    """All distinct account_ids who've played in this guild's matches."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT p.account_id
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.guild_id = ? AND p.account_id > 0
        """, (guild_id,)).fetchall()
    return [r[0] for r in rows]


def upsert_rating_cache_row(row: dict) -> None:
    """Insert/update one row in player_ratings_cache. Expects keys account_id,
    name, internal_rating, raw_windrun, mmr_estimate, ad_last_year,
    ranked_last_year, explanation."""
    from datetime import datetime, timezone
    payload = {
        "account_id":       row["account_id"],
        "name":             row.get("name"),
        "internal_rating":  row.get("internal_rating"),
        "raw_windrun":      row.get("raw_windrun"),
        "mmr_estimate":     row.get("mmr_estimate"),
        "ad_last_year":     row.get("ad_last_year"),
        "ranked_last_year": row.get("ranked_last_year"),
        "explanation":      row.get("explanation"),
        "updated_at":       datetime.now(timezone.utc).isoformat(),
    }
    with _conn() as conn:
        conn.execute("""
            INSERT INTO player_ratings_cache
                (account_id, name, internal_rating, raw_windrun, mmr_estimate,
                 ad_last_year, ranked_last_year, explanation, updated_at)
            VALUES (:account_id, :name, :internal_rating, :raw_windrun,
                    :mmr_estimate, :ad_last_year, :ranked_last_year,
                    :explanation, :updated_at)
            ON CONFLICT(account_id) DO UPDATE SET
                name             = excluded.name,
                internal_rating  = excluded.internal_rating,
                raw_windrun      = excluded.raw_windrun,
                mmr_estimate     = excluded.mmr_estimate,
                ad_last_year     = excluded.ad_last_year,
                ranked_last_year = excluded.ranked_last_year,
                explanation      = excluded.explanation,
                updated_at       = excluded.updated_at
        """, payload)


def get_guild_cached_ratings(guild_id: int) -> list[dict]:
    """Cached rating rows for players who've appeared in this guild's matches."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT prc.*
            FROM player_ratings_cache prc
            WHERE prc.account_id IN (
                SELECT DISTINCT p.account_id
                FROM players p
                JOIN matches m ON p.match_id = m.match_id
                WHERE m.guild_id = ? AND p.account_id > 0
            )
        """, (guild_id,)).fetchall()
    return [dict(r) for r in rows]


def get_skill_override(account_id: int) -> dict | None:
    """Return the global skill override for this account, or None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM skill_overrides WHERE account_id = ?", (account_id,)
        ).fetchone()
    return dict(row) if row else None


def upsert_skill_override(
    account_id: int,
    windrun_rating: float | None,
    ranked_mmr: int | None,
    note: str | None,
    set_by_user_id: int | None,
    set_by_name: str | None,
) -> None:
    """Insert or update an override. Pass None for any field to KEEP the
    existing value (partial updates). To clear an override entirely, use
    delete_skill_override()."""
    existing = get_skill_override(account_id)
    if existing:
        wr   = windrun_rating if windrun_rating is not None else existing.get("windrun_rating")
        rm   = ranked_mmr     if ranked_mmr     is not None else existing.get("ranked_mmr")
        nt   = note           if note           is not None else existing.get("note")
    else:
        wr, rm, nt = windrun_rating, ranked_mmr, note
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO skill_overrides
                (account_id, windrun_rating, ranked_mmr, note,
                 set_by_user_id, set_by_name, set_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                windrun_rating = excluded.windrun_rating,
                ranked_mmr     = excluded.ranked_mmr,
                note           = excluded.note,
                set_by_user_id = excluded.set_by_user_id,
                set_by_name    = excluded.set_by_name,
                set_at         = excluded.set_at
        """, (account_id, wr, rm, nt, set_by_user_id, set_by_name, now))


def delete_skill_override(account_id: int) -> bool:
    """Remove any override row for this account. Returns True if a row was removed."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM skill_overrides WHERE account_id = ?", (account_id,)
        )
        return cur.rowcount > 0


def get_player_costs(guild_id: int, season_start: str) -> dict[int, dict]:
    """Return {account_id: {cost, captain, mmr}} for the season."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT account_id, cost, captain, mmr FROM player_costs "
            "WHERE guild_id = ? AND season_start = ?",
            (guild_id, season_start),
        ).fetchall()
    return {
        r["account_id"]: {"cost": r["cost"], "captain": r["captain"], "mmr": r["mmr"]}
        for r in rows
    }


def upsert_player_costs(guild_id: int, season_start: str, costs: list[dict]):
    """Insert/update player costs. Each dict needs account_id, cost; captain + mmr optional."""
    if not costs:
        return
    with _conn() as conn:
        conn.executemany("""
            INSERT INTO player_costs (guild_id, season_start, account_id, cost, captain, mmr)
            VALUES (:guild_id, :season_start, :account_id, :cost, :captain, :mmr)
            ON CONFLICT(guild_id, season_start, account_id) DO UPDATE SET
                cost    = excluded.cost,
                captain = excluded.captain,
                mmr     = excluded.mmr
        """, [{
            "guild_id":     guild_id,
            "season_start": season_start,
            "captain":      None,
            "mmr":          None,
            **c,
        } for c in costs])


def _compute_attendance(
    guild_id: int,
    start: int,
    end: int | None,
    costs: dict[int, dict],
) -> dict[int, float]:
    """Return {account_id: attendance_ratio} for drafted players only.

    Captain-agnostic: we never need to identify the captain's account_id.
    The captain field in `costs` is used only as a grouping label.

    Denominator (per captain): matches where ≥2 of that captain's drafted
        players were on the same team_side — i.e. matches the team actually
        played as a team. Excludes lone-standin appearances.
    Numerator (per drafted player): matches where ≥1 of their fellow
        drafted teammates (same captain) was on their team_side.
    """
    if not costs:
        return {}

    if end is None:
        sql_where = "WHERE m.guild_id = ? AND m.start_time >= ?"
        params: tuple = (guild_id, start)
    else:
        sql_where = "WHERE m.guild_id = ? AND m.start_time BETWEEN ? AND ?"
        params = (guild_id, start, end)

    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT p.match_id, p.account_id, p.team_side
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            {sql_where}
        """, params).fetchall()

    if not rows:
        return {}

    # captain_name → set of account_ids drafted by them
    drafted_by_captain: dict[str, set] = {}
    for aid, info in costs.items():
        cap = info.get("captain")
        if cap:
            drafted_by_captain.setdefault(cap, set()).add(aid)

    # Index account_ids present per (match, side)
    match_side_aids: dict[tuple, set] = {}
    for r in rows:
        match_side_aids.setdefault((r["match_id"], r["team_side"]), set()).add(r["account_id"])

    # Denominator per captain: matches where ≥2 of their drafted players share a side
    captain_team_games: dict[str, int] = {}
    for aids in match_side_aids.values():
        for captain, drafted in drafted_by_captain.items():
            if len(aids & drafted) >= 2:
                captain_team_games[captain] = captain_team_games.get(captain, 0) + 1

    # Numerator per player: matches where they had ≥1 fellow drafted teammate on their side
    player_team_games: dict[int, int] = {}
    for r in rows:
        info = costs.get(r["account_id"])
        if not info or not info.get("captain"):
            continue
        teammates = drafted_by_captain[info["captain"]] - {r["account_id"]}
        if match_side_aids[(r["match_id"], r["team_side"])] & teammates:
            player_team_games[r["account_id"]] = player_team_games.get(r["account_id"], 0) + 1

    # Ratio
    attendance: dict[int, float] = {}
    for aid, num in player_team_games.items():
        cap = (costs.get(aid) or {}).get("captain")
        denom = captain_team_games.get(cap, 0)
        if denom > 0:
            attendance[aid] = num / denom
    return attendance


def _attach_costs(stats: list[dict], costs: dict[int, dict]) -> list[dict]:
    """Mutate stats rows to add cost/captain/mmr. value/predicted_fp are filled
    later by _compute_value() since they require a cross-player regression."""
    for s in stats:
        info = costs.get(s["account_id"])
        if info and info.get("cost"):
            s["cost"]    = info["cost"]
            s["captain"] = info.get("captain")
            s["mmr"]     = info.get("mmr")
        else:
            s["cost"]    = None
            s["captain"] = None
            s["mmr"]     = None
        # Initialise so format_leaderboard's None-skip works cleanly.
        s.setdefault("value", None)
        s.setdefault("predicted_diff", None)
    return stats


def _det3(A: list[list[float]]) -> float:
    return (A[0][0] * (A[1][1] * A[2][2] - A[1][2] * A[2][1])
          - A[0][1] * (A[1][0] * A[2][2] - A[1][2] * A[2][0])
          + A[0][2] * (A[1][0] * A[2][1] - A[1][1] * A[2][0]))


def _solve3(M: list[list[float]], y: list[float]) -> tuple[float, float, float] | None:
    """Solve a 3x3 linear system via Cramer's rule. Returns (x0, x1, x2) or None."""
    D = _det3(M)
    if abs(D) < 1e-12:
        return None
    out = []
    for k in range(3):
        Mk = [row[:] for row in M]
        for r in range(3):
            Mk[r][k] = y[r]
        out.append(_det3(Mk) / D)
    return tuple(out)


def fit_cost_diff(stats: list[dict]) -> tuple[float, float] | None:
    """Univariate OLS: predicted_diff = a + b * cost across drafted players.

    Used by /leaderboard Value and /player's Draft section. The multivariate
    version (cost + mmr) is used by /suggested_cost only.

    Returns (a, b) or None if there isn't enough data to fit a slope.
    """
    data = [
        (s["cost"], s["diff"])
        for s in stats
        if s.get("cost") is not None and s.get("diff") is not None
    ]
    if len(data) < 2:
        return None
    n = len(data)
    sum_x  = sum(c for c, _ in data)
    sum_y  = sum(d for _, d in data)
    sum_xy = sum(c * d for c, d in data)
    sum_xx = sum(c * c for c, _ in data)
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return None
    b = (n * sum_xy - sum_x * sum_y) / denom
    a = (sum_y - b * sum_x) / n
    return (a, b)


def _solve_linear(A: list[list[float]], b: list[float]) -> list[float] | None:
    """Solve A·x = b via Gauss–Jordan with partial pivoting.

    Returns x as a list, or None if A is singular (or numerically close).
    Doesn't mutate the inputs.
    """
    n = len(A)
    # Augmented matrix
    M = [list(row) + [b[i]] for i, row in enumerate(A)]
    for i in range(n):
        # Partial pivot
        pivot = i
        for k in range(i + 1, n):
            if abs(M[k][i]) > abs(M[pivot][i]):
                pivot = k
        if pivot != i:
            M[i], M[pivot] = M[pivot], M[i]
        if abs(M[i][i]) < 1e-12:
            return None
        # Eliminate the i-th column in every other row
        for k in range(n):
            if k == i:
                continue
            factor = M[k][i] / M[i][i]
            for j in range(i, n + 1):
                M[k][j] -= factor * M[i][j]
    return [M[i][n] / M[i][i] for i in range(n)]


def fit_cost_from_perf(
    stats: list[dict],
) -> tuple[float, float, float, float] | None:
    """Multivariate OLS predicting cost from performance + skill:
        cost = a + b_d·diff + b_f·fp + b_m·mmr
    Returns (a, b_d, b_f, b_m) or None if rank-deficient or < 4 data points.

    This is the "forward" model used by /suggested_cost. It's bounded by the
    historical cost range (no inverse-extrapolation blow-ups) and accommodates
    both performance dimensions (raw fp + relative diff).
    """
    data = [
        (s["cost"], s["diff"], s["fantasy_points"], s["mmr"])
        for s in stats
        if s.get("cost") is not None
           and s.get("diff") is not None
           and s.get("fantasy_points") is not None
           and s.get("mmr") is not None
    ]
    if len(data) < 4:
        return None
    n = len(data)

    sd  = sum(d for _, d, _, _ in data)
    sf  = sum(f for _, _, f, _ in data)
    sm  = sum(m for _, _, _, m in data)
    sc  = sum(c for c, _, _, _ in data)

    sdd = sum(d * d for _, d, _, _ in data)
    sff = sum(f * f for _, _, f, _ in data)
    smm = sum(m * m for _, _, _, m in data)
    sdf = sum(d * f for _, d, f, _ in data)
    sdm = sum(d * m for _, d, _, m in data)
    sfm = sum(f * m for _, _, f, m in data)

    sdc = sum(d * c for c, d, _, _ in data)
    sfc = sum(f * c for c, _, f, _ in data)
    smc = sum(m * c for c, _, _, m in data)

    A = [
        [n,   sd,  sf,  sm ],
        [sd,  sdd, sdf, sdm],
        [sf,  sdf, sff, sfm],
        [sm,  sdm, sfm, smm],
    ]
    y = [sc, sdc, sfc, smc]
    sol = _solve_linear(A, y)
    if sol is None:
        return None
    return tuple(sol)


def fit_cost_mmr_diff(stats: list[dict]) -> tuple[float, float, float] | None:
    """Multivariate OLS: predicted_diff = a + b1 * cost + b2 * mmr.

    Returns (a, b1, b2) or None if there isn't enough data or the system is
    singular. Only considers rows with `cost`, `mmr`, and `diff` all set.
    """
    data = [
        (s["cost"], s["mmr"], s["diff"])
        for s in stats
        if s.get("cost") is not None and s.get("mmr") is not None and s.get("diff") is not None
    ]
    if len(data) < 3:
        return None
    n = len(data)
    sum_c  = sum(c for c, _, _ in data)
    sum_m  = sum(m for _, m, _ in data)
    sum_d  = sum(d for _, _, d in data)
    sum_cc = sum(c * c for c, _, _ in data)
    sum_mm = sum(m * m for _, m, _ in data)
    sum_cm = sum(c * m for c, m, _ in data)
    sum_cd = sum(c * d for c, _, d in data)
    sum_md = sum(m * d for _, m, d in data)
    M = [
        [n,      sum_c,  sum_m ],
        [sum_c,  sum_cc, sum_cm],
        [sum_m,  sum_cm, sum_mm],
    ]
    y = [sum_d, sum_cd, sum_md]
    return _solve3(M, y)


def _compute_value(stats: list[dict]) -> list[dict]:
    """Fit cost-vs-diff (univariate) and set s['predicted_diff'] and s['value']
    (= residual) for each drafted player. Untouched for players without a cost.

    /suggested_cost separately uses the multivariate cost+mmr fit; we keep value
    on the cost-only model so /leaderboard Value answers a simpler question:
    "given this player's draft cost alone, did they over- or under-perform?"
    """
    fit = fit_cost_diff(stats)
    if fit is None:
        # Not enough data — fall back to per-player residual = diff - mean(diff)
        drafted = [
            s for s in stats
            if s.get("cost") is not None and s.get("diff") is not None
        ]
        if not drafted:
            return stats
        mean_y = sum(s["diff"] for s in drafted) / len(drafted)
        for s in drafted:
            s["predicted_diff"] = round(mean_y, 2)
            s["value"] = round(s["diff"] - mean_y, 2)
        return stats

    a, b = fit
    for s in stats:
        if s.get("cost") is None or s.get("diff") is None:
            continue
        predicted = a + b * s["cost"]
        s["predicted_diff"] = round(predicted, 2)
        s["value"] = round(s["diff"] - predicted, 2)
    return stats


def get_player_team_diff(
    guild_id: int,
    account_id: int,
    season_start_date: str,
    week_number: int | None = None,
) -> dict | None:
    """
    Per-match comparison of one player's fantasy points to the average of their
    4 same-side teammates. Aggregated across all matches in the requested range.

    week_number=None or -1 → all-time (filtered by season_start_date).
    Otherwise scoped to that season week.

    Returns None if the player has no matches in range.
    """
    if week_number is None or week_number == -1:
        # All-time: mirror the season-start filter used by get_all_time_stats
        season_start = datetime.strptime(season_start_date, "%Y-%m-%d")
        season_start = season_start.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        season_monday = season_start - timedelta(days=season_start.weekday())
        week_zero_start = season_monday - timedelta(weeks=1)
        start = int(week_zero_start.timestamp())
        end = 2**31 - 1
    else:
        start, end = _season_week_start(week_number, season_start_date)

    with _conn() as conn:
        target_matches = conn.execute("""
            SELECT p.match_id
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.guild_id = ?
              AND p.account_id = ?
              AND m.start_time BETWEEN ? AND ?
        """, (guild_id, account_id, start, end)).fetchall()

        if not target_matches:
            return None

        match_ids = [r["match_id"] for r in target_matches]
        placeholders = ",".join("?" * len(match_ids))
        rows = conn.execute(f"""
            SELECT p.*, m.duration AS match_duration
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE p.match_id IN ({placeholders})
        """, match_ids).fetchall()

    # Group rows by match
    by_match: dict[int, list[dict]] = {}
    for r in rows:
        by_match.setdefault(r["match_id"], []).append(dict(r))

    player_fps: list[float] = []
    teammate_avg_fps: list[float] = []
    top_count = 0
    bottom_count = 0
    player_name: str | None = None

    for match_id, match_rows in by_match.items():
        target = next((r for r in match_rows if r["account_id"] == account_id), None)
        if not target:
            continue
        teammates = [
            r for r in match_rows
            if r["account_id"] != account_id and r["team_side"] == target["team_side"]
        ]
        if not teammates:
            continue  # data integrity issue — skip
        player_name = target["name"]

        target_fp = _per_match_fp(target)
        mate_fps = [_per_match_fp(m) for m in teammates]

        team_fps = mate_fps + [target_fp]
        if target_fp >= max(team_fps):
            top_count += 1
        if target_fp <= min(team_fps):
            bottom_count += 1

        player_fps.append(target_fp)
        teammate_avg_fps.append(sum(mate_fps) / len(mate_fps))

    if not player_fps:
        return None

    n = len(player_fps)
    player_avg = sum(player_fps) / n
    teammate_avg = sum(teammate_avg_fps) / n

    return {
        "name":                 player_name,
        "account_id":           account_id,
        "games_played":         n,
        "player_avg_fp":        player_avg,
        "teammate_avg_fp":      teammate_avg,
        "diff":                 player_avg - teammate_avg,
        "top_of_team_count":    top_count,
        "bottom_of_team_count": bottom_count,
    }


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


def get_match_ids_for_player(guild_id: int, player_name: str) -> set[int]:
    """Return the set of match IDs (for this guild) where a player matching the name played.

    Partial, case-insensitive match against the stored player name.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT p.match_id
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.guild_id = ?
              AND LOWER(p.name) LIKE LOWER(?)
        """, (guild_id, f"%{player_name}%")).fetchall()
    return {r["match_id"] for r in rows}


# ---------------------------------------------------------------------------
# Draft picks
# ---------------------------------------------------------------------------

def draft_exists(match_id: int) -> bool:
    """Return True if we already have draft pick data for this match."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM draft_picks WHERE match_id = ? LIMIT 1", (match_id,)
        ).fetchone()
    return row is not None


def upsert_draft_picks(match_id: int, picks: list[dict]):
    """
    Persist draft picks for a match.

    Each pick dict must have: pick_order, player_id, and either ability_name
    (preferred, e.g. "morphling_waveform") or ability_id (legacy integer).
    Uses INSERT OR IGNORE so re-running is safe.
    """
    with _conn() as conn:
        conn.executemany("""
            INSERT OR REPLACE INTO draft_picks
                (match_id, pick_order, ability_id, ability_name, pick_type, m_n_ability_id, player_id)
            VALUES (:match_id, :pick_order, :ability_id, :ability_name, :pick_type, :m_n_ability_id, :player_id)
        """, [{
            "match_id": match_id,
            "ability_id": 0,
            "ability_name": "",
            "pick_type": "ability",
            "m_n_ability_id": 0,
            **p,
        } for p in picks])


def get_ability_id_mapping(m_n_ids: list[int]) -> dict[int, dict]:
    """
    Look up multiple m_n_ability_id values in the static mapping table.

    Returns:
        {m_n_ability_id: {'pick_type': str, 'ability_name': str, 'hero_id': int}}
        for IDs that exist in the table. Missing IDs are absent from the dict.
    """
    if not m_n_ids:
        return {}
    placeholders = ",".join("?" * len(m_n_ids))
    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT m_n_ability_id, pick_type, ability_name, hero_id
            FROM ability_id_mapping
            WHERE m_n_ability_id IN ({placeholders})
        """, m_n_ids).fetchall()
    return {
        r["m_n_ability_id"]: {
            "pick_type": r["pick_type"],
            "ability_name": r["ability_name"],
            "hero_id": r["hero_id"],
        }
        for r in rows
    }


def upsert_ability_id_mappings(mappings: list[dict]):
    """
    Upsert ability_id_mapping rows. Each dict needs:
        m_n_ability_id, pick_type, ability_name, hero_id
    Uses INSERT OR REPLACE so collected ground truth always wins.
    """
    if not mappings:
        return
    with _conn() as conn:
        conn.executemany("""
            INSERT OR REPLACE INTO ability_id_mapping
                (m_n_ability_id, pick_type, ability_name, hero_id)
            VALUES (:m_n_ability_id, :pick_type, :ability_name, :hero_id)
        """, mappings)


def get_draft_picks(match_id: int) -> list[dict]:
    """Return all draft picks for a match, sorted by pick order."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT pick_order, ability_id, ability_name, pick_type, player_id
            FROM draft_picks
            WHERE match_id = ?
            ORDER BY pick_order
        """, (match_id,)).fetchall()
    return [dict(r) for r in rows]


def get_draft_picks_for_display(match_id: int) -> dict | None:
    """
    Return draft picks + player info needed to render a draft image.

    Draft seat mapping uses player_slot (stored since the player_slot migration):
      - player_slot 0-4   → draft seat 0-4   (radiant)
      - player_slot 128-132 → draft seat 5-9  (dire; seat = slot - 123)

    For older rows where player_slot was not yet stored (value -1), we fall back
    to insertion order within each team as a best-effort approximation.

    Returns:
        {
          'radiant_win': bool,
          'picks': [{'pick_order', 'ability_id', 'player_id'}, ...],
          'players_by_seat': {
              0: {'name': str, 'kills': int, 'deaths': int, 'assists': int},
              ...  (keys 0-9)
          }
        }
        or None if no draft picks exist for this match.
    """
    with _conn() as conn:
        match_row = conn.execute(
            "SELECT radiant_win FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()
        if not match_row:
            return None

        pick_rows = conn.execute("""
            SELECT pick_order, ability_id, ability_name, pick_type, player_id
            FROM draft_picks
            WHERE match_id = ?
            ORDER BY pick_order
        """, (match_id,)).fetchall()
        if not pick_rows:
            return None

        player_rows = conn.execute("""
            SELECT name, kills, deaths, assists, player_slot, team_side
            FROM players
            WHERE match_id = ?
        """, (match_id,)).fetchall()

    players_by_seat: dict[int, dict] = {}
    have_slots = any(r["player_slot"] >= 0 for r in player_rows)

    if have_slots:
        # Reliable path: use stored player_slot to derive draft seat
        for row in player_rows:
            slot = row["player_slot"]
            if 0 <= slot <= 4:
                seat = slot
            elif 128 <= slot <= 132:
                seat = slot - 123       # 128→5, 129→6, 130→7, 131→8, 132→9
            else:
                continue               # unknown slot, skip
            players_by_seat[seat] = {
                "name": row["name"],
                "kills": row["kills"],
                "deaths": row["deaths"],
                "assists": row["assists"],
            }
    else:
        # Legacy fallback: insertion order within each team (approximate)
        radiant = sorted(
            [r for r in player_rows if r["team_side"] == "radiant"],
            key=lambda r: r["name"],   # stable but arbitrary order
        )
        dire = sorted(
            [r for r in player_rows if r["team_side"] == "dire"],
            key=lambda r: r["name"],
        )
        for seat, row in enumerate(radiant):
            players_by_seat[seat] = dict(row)
        for i, row in enumerate(dire):
            players_by_seat[5 + i] = dict(row)

    return {
        "radiant_win": bool(match_row["radiant_win"]),
        "picks": [dict(r) for r in pick_rows],
        "players_by_seat": players_by_seat,
    }


def get_match_replay_info(match_id: int) -> tuple[int, int]:
    """Return (cluster, replay_salt) for a match. Both are 0 if not found."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT cluster, replay_salt FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()
    if row:
        return row["cluster"], row["replay_salt"]
    return 0, 0


def get_matches_without_drafts(guild_id: int) -> list[int]:
    """
    Return match IDs for this guild that have no draft_picks rows yet.

    Only returns AD matches (game_mode = 18).
    Ordered oldest-first so we backfill in chronological order.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT m.match_id
            FROM matches m
            WHERE m.guild_id = ?
              AND m.game_mode = 18
              AND NOT EXISTS (
                  SELECT 1 FROM draft_picks dp WHERE dp.match_id = m.match_id
              )
            ORDER BY m.start_time ASC
        """, (guild_id,)).fetchall()
    return [r["match_id"] for r in rows]


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
            conn.execute(f"DELETE FROM draft_picks WHERE match_id IN ({placeholders})", match_id_list)

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
