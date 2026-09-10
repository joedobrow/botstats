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
            ("building_damage",           "INTEGER DEFAULT 0"),   # damage dealt to enemy buildings (towers + barracks + throne)
        ]
        for col, col_type in new_player_cols:
            if col not in player_columns:
                logger.info("Migrating: adding %s column to players table", col)
                conn.execute(f"ALTER TABLE players ADD COLUMN {col} {col_type}")

        # --- Migration: add nickname column to skill_overrides if missing ---
        cursor = conn.execute("PRAGMA table_info(skill_overrides)")
        skill_cols = [row[1] for row in cursor.fetchall()]
        if skill_cols and "nickname" not in skill_cols:
            logger.info("Migrating: adding nickname column to skill_overrides")
            conn.execute("ALTER TABLE skill_overrides ADD COLUMN nickname TEXT")
        if skill_cols and "internal_rating_override" not in skill_cols:
            logger.info("Migrating: adding internal_rating_override column to skill_overrides")
            conn.execute("ALTER TABLE skill_overrides ADD COLUMN internal_rating_override INTEGER")
        if skill_cols and "hide_avatar" not in skill_cols:
            logger.info("Migrating: adding hide_avatar column to skill_overrides")
            conn.execute("ALTER TABLE skill_overrides ADD COLUMN hide_avatar INTEGER DEFAULT 0")

        # --- Migration: add avatar_url + ad_all_time to player_ratings_cache ---
        cursor = conn.execute("PRAGMA table_info(player_ratings_cache)")
        prc_cols = [row[1] for row in cursor.fetchall()]
        if prc_cols and "avatar_url" not in prc_cols:
            logger.info("Migrating: adding avatar_url column to player_ratings_cache")
            conn.execute("ALTER TABLE player_ratings_cache ADD COLUMN avatar_url TEXT")
        if prc_cols and "ad_all_time" not in prc_cols:
            logger.info("Migrating: adding ad_all_time column to player_ratings_cache")
            conn.execute("ALTER TABLE player_ratings_cache ADD COLUMN ad_all_time INTEGER")
        if prc_cols and "fh_unavailable" not in prc_cols:
            logger.info("Migrating: adding fh_unavailable column to player_ratings_cache")
            conn.execute("ALTER TABLE player_ratings_cache ADD COLUMN fh_unavailable INTEGER DEFAULT 0")
        if prc_cols and "rank_tier" not in prc_cols:
            logger.info("Migrating: adding rank_tier + leaderboard_rank to player_ratings_cache")
            conn.execute("ALTER TABLE player_ratings_cache ADD COLUMN rank_tier INTEGER")
            conn.execute("ALTER TABLE player_ratings_cache ADD COLUMN leaderboard_rank INTEGER")

        # --- Migration: add rank_tier + leaderboard_rank to skill_overrides ---
        # These replace ranked_mmr as the ranked-side override lever. ranked_mmr
        # is kept for now so existing rows still round-trip; the runtime migration
        # further down converts them into (rank_tier, leaderboard_rank) once.
        cursor = conn.execute("PRAGMA table_info(skill_overrides)")
        so_cols = [row[1] for row in cursor.fetchall()]
        if so_cols and "rank_tier" not in so_cols:
            logger.info("Migrating: adding rank_tier + leaderboard_rank to skill_overrides")
            conn.execute("ALTER TABLE skill_overrides ADD COLUMN rank_tier INTEGER")
            conn.execute("ALTER TABLE skill_overrides ADD COLUMN leaderboard_rank INTEGER")
        if so_cols and "alt_account_for" not in so_cols:
            logger.info("Migrating: adding alt_account_for column to skill_overrides")
            conn.execute("ALTER TABLE skill_overrides ADD COLUMN alt_account_for INTEGER")
        if so_cols and "discord_override" not in so_cols:
            logger.info("Migrating: adding discord_override column to skill_overrides")
            conn.execute("ALTER TABLE skill_overrides ADD COLUMN discord_override TEXT")

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
                defensive_item_uses       INTEGER DEFAULT 0,   -- pipe/crimson/lotus/glimmer/force/pavise/solar/halberd/sphere activations
                building_damage           INTEGER DEFAULT 0    -- damage dealt to enemy buildings (OpenDota tower_damage)
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
                rank_tier         INTEGER,
                leaderboard_rank  INTEGER,
                ad_last_year      INTEGER,
                ad_all_time       INTEGER,
                ranked_last_year  INTEGER,
                explanation       TEXT,
                avatar_url        TEXT,
                fh_unavailable    INTEGER DEFAULT 0,
                updated_at        TEXT NOT NULL
            );

            -- Manual skill-rating overrides for /lookup. Used to fix smurfs,
            -- non-NA leaderboard players, or any case where the API-inferred
            -- numbers are wrong. Scope is GLOBAL (account_id is the key).
            CREATE TABLE IF NOT EXISTS skill_overrides (
                account_id              INTEGER PRIMARY KEY,
                windrun_rating          REAL,
                ranked_mmr              INTEGER,       -- deprecated; migrated to rank_tier
                rank_tier               INTEGER,
                leaderboard_rank        INTEGER,
                nickname                TEXT,
                note                    TEXT,
                internal_rating_override INTEGER,
                hide_avatar             INTEGER DEFAULT 0,
                alt_account_for         INTEGER,        -- main account_id to defer rating to
                discord_override        TEXT,           -- overrides scraped rd2l handle for /sync_roles_channels
                set_by_user_id          INTEGER,
                set_by_name             TEXT,
                set_at                  TEXT NOT NULL
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

            -- Explicit week boundaries per season, replacing pure calendar
            -- math so bye weeks / reschedules can be handled directly
            -- (skip a week, or widen an adjacent one) instead of only ever
            -- being able to shift the whole season via season_start.
            -- week_number 0 is the pre-season/calibration window. A guild's
            -- season only uses this table once rows exist for it — see
            -- get_season_week_range/get_current_season_week, which fall
            -- back to the legacy calendar formula when none are defined.
            CREATE TABLE IF NOT EXISTS season_weeks (
                guild_id      INTEGER NOT NULL,
                season_start  TEXT NOT NULL,
                week_number   INTEGER NOT NULL,
                start_ts      INTEGER NOT NULL,
                end_ts        INTEGER NOT NULL,
                PRIMARY KEY (guild_id, season_start, week_number)
            );

            CREATE INDEX IF NOT EXISTS idx_season_weeks_lookup
                ON season_weeks(guild_id, season_start);

            -- Per-season team identity (real team name + logo), keyed by the
            -- captain string that appears in player_costs. Replaces the old
            -- hardcoded team_metadata.py dict so a new season is a data change,
            -- not a code change.
            -- One row per season this guild has run. Lets commands offer a
            -- season: filter and give it a human label; divisions only ever
            -- holds the *current* season, so without this the history is
            -- reachable but unnameable.
            CREATE TABLE IF NOT EXISTS seasons (
                guild_id      INTEGER NOT NULL,
                season_start  TEXT NOT NULL,
                league_id     INTEGER,
                label         TEXT,
                created_at    TEXT NOT NULL,
                PRIMARY KEY (guild_id, season_start)
            );

            CREATE TABLE IF NOT EXISTS season_teams (
                guild_id      INTEGER NOT NULL,
                season_start  TEXT NOT NULL,
                captain       TEXT NOT NULL,      -- lowercased captain key
                team_name     TEXT NOT NULL,
                logo_url      TEXT,
                PRIMARY KEY (guild_id, season_start, captain)
            );

            -- Per-guild roster of "tracked" players. Lets /set_player add a
            -- player to a guild even if they haven't appeared in a match yet,
            -- so /players will surface them.
            CREATE TABLE IF NOT EXISTS guild_roster (
                guild_id   INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                added_at   TEXT NOT NULL,
                PRIMARY KEY (guild_id, account_id)
            );

            -- Per-guild exclusion list. Used to hide alt/smurf accounts from
            -- /players even when match history exists.
            CREATE TABLE IF NOT EXISTS guild_exclusions (
                guild_id     INTEGER NOT NULL,
                account_id   INTEGER NOT NULL,
                excluded_at  TEXT NOT NULL,
                PRIMARY KEY (guild_id, account_id)
            );

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
    _migrate_ranked_mmr_overrides_to_rank()
    _seed_s38_season_teams()
    _backfill_season_registry()
    logger.info("Database initialised at %s", DB_PATH)


def _backfill_season_registry():
    """Register any season that already has data but predates the seasons table:
    each division's current season, plus every season_start seen in player_costs.
    League id comes from the division row where it lines up. Labels stay NULL —
    they render as the start date until /start_new_season sets a real one."""
    with _conn() as conn:
        divs = conn.execute(
            "SELECT guild_id, league_id, season_start FROM divisions").fetchall()
        for d in divs:
            conn.execute("""
                INSERT INTO seasons (guild_id, season_start, league_id, label, created_at)
                VALUES (?, ?, ?, NULL, datetime('now'))
                ON CONFLICT(guild_id, season_start) DO UPDATE SET
                    league_id = COALESCE(seasons.league_id, excluded.league_id)
            """, (d["guild_id"], d["season_start"], d["league_id"]))
        conn.execute("""
            INSERT INTO seasons (guild_id, season_start, league_id, label, created_at)
            SELECT DISTINCT pc.guild_id, pc.season_start, NULL, NULL, datetime('now')
            FROM player_costs pc
            WHERE NOT EXISTS (
                SELECT 1 FROM seasons s
                WHERE s.guild_id = pc.guild_id AND s.season_start = pc.season_start
            )
        """)


def _seed_s38_season_teams():
    """One-shot: move the old hardcoded team_metadata.py dict into season_teams
    so the S38 cards/H2H keep their real names + logos after the table exists.
    No-op once the rows are present (or if that season isn't configured here)."""
    S38_GUILD, S38_SEASON = 1481800158826991718, "2026-04-28"
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM season_teams WHERE guild_id = ? AND season_start = ? LIMIT 1",
            (S38_GUILD, S38_SEASON),
        ).fetchone()
        if row:
            return
    try:
        from team_metadata import _LEGACY_S38_TEAMS
    except Exception:
        return
    teams = [
        {"captain": cap, "team_name": info["team_name"], "logo_url": info.get("logo_url")}
        for cap, info in _LEGACY_S38_TEAMS.items()
    ]
    if teams:
        upsert_season_teams(S38_GUILD, S38_SEASON, teams)
        logger.info("Seeded %d S38 team identities into season_teams", len(teams))


def _migrate_ranked_mmr_overrides_to_rank():
    """One-shot migration: turn existing skill_overrides.ranked_mmr into an
    equivalent (rank_tier, leaderboard_rank) pair so the pre-squish overrides
    keep behaving the same under the new badge-based pipeline.

    Reversal recipe:
      - MMR ≥ Immortal floor (5420): rank_tier = 80. If MMR > 6000, back-solve
        leaderboard rank via the old log formula. If MMR ≤ 6000, generic Immortal.
      - MMR < 5420: find the medal bracket, pick the star sub-band whose midpoint
        is closest to the stored MMR value.
    """
    try:
        from rating_model import legacy_mmr_to_leaderboard_rank
    except ImportError:
        # Public clone with no rating model — there are no legacy overrides to
        # migrate in that case, so skipping is correct rather than fatal.
        logger.info("rating_model absent; skipping legacy ranked_mmr migration")
        return
    _BRACKETS = [
        (1, 0, 770), (2, 770, 1540), (3, 1540, 2310), (4, 2310, 3080),
        (5, 3080, 3850), (6, 3850, 4620), (7, 4620, 5420),
    ]
    with _conn() as conn:
        rows = conn.execute(
            "SELECT account_id, ranked_mmr FROM skill_overrides "
            "WHERE ranked_mmr IS NOT NULL AND rank_tier IS NULL"
        ).fetchall()
        for r in rows:
            mmr = r["ranked_mmr"]
            if mmr is None:
                continue
            if mmr >= 5420:
                rt = 80
                if mmr > 8000:
                    # High-MMR overrides map to real leaderboard positions via
                    # the legacy fit, which lives in the private rating model.
                    lb = legacy_mmr_to_leaderboard_rank(mmr)
                else:
                    # Low-Immortal (5420-8000) doesn't cleanly correspond to a
                    # leaderboard rank; treat as generic Immortal.
                    lb = None
            else:
                # Find the closest star midpoint in the applicable bracket.
                bracket = next((b for b in _BRACKETS if b[1] <= mmr < b[2]), _BRACKETS[0])
                tier, lo, hi = bracket
                width = hi - lo
                stars = min(range(1, 6), key=lambda s: abs((lo + width * (2 * s - 1) / 10) - mmr))
                rt = tier * 10 + stars
                lb = None
            conn.execute(
                "UPDATE skill_overrides SET rank_tier = ?, leaderboard_rank = ?, ranked_mmr = NULL "
                "WHERE account_id = ?",
                (rt, lb, r["account_id"]),
            )
            logger.info("Migrated override for %d: ranked_mmr=%d → rank_tier=%d lb=%s",
                        r["account_id"], mmr, rt, lb)


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
                 player_slot, defensive_item_uses, building_damage)
            VALUES
                (:match_id, :account_id, :name, :hero_id, :team_side, :role_position,
                 :kills, :deaths, :assists, :gpm, :xpm, :last_hits, :denies,
                 :hero_damage, :hero_healing, :gold_spent, :duration, :won,
                 :obs_placed, :sen_placed, :observer_kills, :sentry_kills,
                 :tower_kills, :roshans_killed, :firstblood_claimed,
                 :teamfight_participation, :stuns, :camps_stacked, :rune_pickups,
                 :tormentor_kills, :watcher_captures, :team_first_tormentor_time,
                 :player_slot, :defensive_item_uses, :building_damage)
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


def _season_weeks_defined(guild_id: int, season_start_date: str) -> bool:
    """True once any explicit season_weeks rows exist for this guild/season."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM season_weeks WHERE guild_id = ? AND season_start = ? LIMIT 1",
            (guild_id, season_start_date),
        ).fetchone()
    return row is not None


def get_season_week_range(guild_id: int, season_start_date: str, week_number: int) -> tuple[int, int]:
    """Canonical (start_unix, end_unix) for a season week. Prefers an
    explicit season_weeks row (so byes/reschedules can override a single
    week); falls back to the calendar formula when the guild/season has no
    explicit weeks defined yet (e.g. any season before this feature existed)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT start_ts, end_ts FROM season_weeks "
            "WHERE guild_id = ? AND season_start = ? AND week_number = ?",
            (guild_id, season_start_date, week_number),
        ).fetchone()
    if row:
        return row["start_ts"], row["end_ts"]
    return _season_week_start(week_number, season_start_date)


def get_current_season_week(guild_id: int, season_start_date: str) -> int:
    """Canonical 'current week' resolver, used wherever week=None needs to
    mean something concrete (leaderboard/player/matches default, /summary).

    With explicit weeks defined: the week whose range contains right now, or
    the closest one (last if we're past every defined week, first if we're
    before all of them). Falls back to the legacy get_latest_season_week
    calendar logic when no explicit weeks exist for this guild/season."""
    if not _season_weeks_defined(guild_id, season_start_date):
        return get_latest_season_week(guild_id, season_start_date) or 1

    now_ts = int(datetime.now(timezone.utc).timestamp())
    with _conn() as conn:
        rows = conn.execute(
            "SELECT week_number, start_ts, end_ts FROM season_weeks "
            "WHERE guild_id = ? AND season_start = ? ORDER BY start_ts",
            (guild_id, season_start_date),
        ).fetchall()

    current = rows[0]["week_number"]
    for r in rows:
        if r["start_ts"] <= now_ts:
            current = r["week_number"]
        else:
            break
    return current


# ---------------------------------------------------------------------------
# Season teams (team identity: real name + logo, per season)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Season registry (which seasons this guild has run)
# ---------------------------------------------------------------------------

def get_seasons(guild_id: int) -> list[dict]:
    """Every season this guild has run, newest first.

    Unions the registry with any season_start that actually has costs or
    weeks on file, so seasons predating the registry still show up.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT s.season_start,
                   s.league_id,
                   s.label
            FROM seasons s WHERE s.guild_id = :g
            UNION
            SELECT DISTINCT pc.season_start, NULL, NULL
            FROM player_costs pc WHERE pc.guild_id = :g
              AND pc.season_start NOT IN (SELECT season_start FROM seasons WHERE guild_id = :g)
            UNION
            SELECT DISTINCT sw.season_start, NULL, NULL
            FROM season_weeks sw WHERE sw.guild_id = :g
              AND sw.season_start NOT IN (SELECT season_start FROM seasons WHERE guild_id = :g)
            ORDER BY season_start DESC
        """, {"g": guild_id}).fetchall()
    return [dict(r) for r in rows]


def upsert_season(guild_id: int, season_start: str,
                  league_id: int | None = None, label: str | None = None) -> None:
    """Register a season. Passing None for league_id/label keeps any existing value."""
    from datetime import datetime as _dt, timezone as _tz
    with _conn() as conn:
        conn.execute("""
            INSERT INTO seasons (guild_id, season_start, league_id, label, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, season_start) DO UPDATE SET
                league_id = COALESCE(excluded.league_id, seasons.league_id),
                label     = COALESCE(excluded.label,     seasons.label)
        """, (guild_id, season_start, league_id, label, _dt.now(_tz.utc).isoformat()))


def season_label(season: dict) -> str:
    """Display name for a season row: its label if we have one, else the date."""
    lbl = season.get("label")
    return lbl if lbl else f"Season starting {season['season_start']}"


def get_season_teams(guild_id: int, season_start_date: str) -> dict[str, dict]:
    """All teams for a guild/season as {lowercased_captain: {team_name, logo_url}}."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT captain, team_name, logo_url FROM season_teams "
            "WHERE guild_id = ? AND season_start = ?",
            (guild_id, season_start_date),
        ).fetchall()
    return {r["captain"]: {"team_name": r["team_name"], "logo_url": r["logo_url"]} for r in rows}


def upsert_season_teams(guild_id: int, season_start_date: str, teams: list[dict]) -> int:
    """Insert/update team identities. Each dict needs captain + team_name;
    logo_url optional. Captain is stored lowercased so lookups are
    case-insensitive against whatever player_costs holds."""
    if not teams:
        return 0
    with _conn() as conn:
        conn.executemany("""
            INSERT INTO season_teams (guild_id, season_start, captain, team_name, logo_url)
            VALUES (:guild_id, :season_start, :captain, :team_name, :logo_url)
            ON CONFLICT(guild_id, season_start, captain) DO UPDATE SET
                team_name = excluded.team_name,
                logo_url  = COALESCE(excluded.logo_url, season_teams.logo_url)
        """, [
            {
                "guild_id": guild_id,
                "season_start": season_start_date,
                "captain": (t["captain"] or "").strip().lower(),
                "team_name": t["team_name"],
                "logo_url": t.get("logo_url"),
            }
            for t in teams if t.get("captain") and t.get("team_name")
        ])
    return len(teams)


def get_season_weeks(guild_id: int, season_start_date: str) -> list[dict]:
    """All explicitly-defined weeks for this guild/season, ordered by week_number."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT week_number, start_ts, end_ts FROM season_weeks "
            "WHERE guild_id = ? AND season_start = ? ORDER BY week_number",
            (guild_id, season_start_date),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_season_week(guild_id: int, season_start_date: str, week_number: int, start_ts: int, end_ts: int) -> None:
    """Insert/update one explicit week boundary."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO season_weeks (guild_id, season_start, week_number, start_ts, end_ts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, season_start, week_number) DO UPDATE SET
                start_ts = excluded.start_ts,
                end_ts   = excluded.end_ts
        """, (guild_id, season_start_date, week_number, start_ts, end_ts))


def delete_season_week(guild_id: int, season_start_date: str, week_number: int) -> bool:
    """Remove one week's explicit definition. Returns True if a row was removed."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM season_weeks WHERE guild_id = ? AND season_start = ? AND week_number = ?",
            (guild_id, season_start_date, week_number),
        )
    return cur.rowcount > 0


def generate_season_weeks(guild_id: int, season_start_date: str, num_weeks: int,
                          first_week: int = 1) -> None:
    """Bulk-create consecutive 7-day week blocks starting at `first_week`.

    first_week=1 (default) numbers them 1..num_weeks, with week 1 being the
    calendar week containing season_start. first_week=0 numbers them
    0..num_weeks-1 for seasons that open with a scrim/"week 0" that doesn't
    count toward seeding — week 0 is then the week containing season_start
    and week 1 is the week after.

    Individual weeks can be adjusted afterward via upsert_season_week /
    delete_season_week (e.g. a bye) without affecting any other week."""
    with _conn() as conn:
        conn.executemany("""
            INSERT INTO season_weeks (guild_id, season_start, week_number, start_ts, end_ts)
            VALUES (:guild_id, :season_start, :week_number, :start_ts, :end_ts)
            ON CONFLICT(guild_id, season_start, week_number) DO UPDATE SET
                start_ts = excluded.start_ts,
                end_ts   = excluded.end_ts
        """, [
            {
                "guild_id": guild_id,
                "season_start": season_start_date,
                "week_number": n,
                "start_ts": _season_week_start(n - first_week + 1, season_start_date)[0],
                "end_ts": _season_week_start(n - first_week + 1, season_start_date)[1],
            }
            # Offset so `first_week` maps onto the calendar week containing
            # season_start, whatever we've chosen to number that week.
            for n in range(first_week, first_week + num_weeks)
        ])


def _all_time_start_ts(guild_id: int, season_start_date: str | None) -> int:
    """Lower bound for 'all-time' queries: the earliest explicitly-defined
    week's start (if any weeks are defined for this guild/season), else the
    legacy week-zero calendar calculation, else 0 (no season_start at all)."""
    if not season_start_date:
        return 0
    if _season_weeks_defined(guild_id, season_start_date):
        with _conn() as conn:
            row = conn.execute(
                "SELECT MIN(start_ts) AS s FROM season_weeks WHERE guild_id = ? AND season_start = ?",
                (guild_id, season_start_date),
            ).fetchone()
        if row and row["s"] is not None:
            return row["s"]
    season_start = datetime.strptime(season_start_date, "%Y-%m-%d")
    season_start = season_start.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    season_monday = season_start - timedelta(days=season_start.weekday())
    week_zero_start = season_monday - timedelta(weeks=1)
    return int(week_zero_start.timestamp())


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
                AVG(p.observer_kills * 60.0 / NULLIF(p.duration, 0))
                                                AS observer_kills_per_min,
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
                AVG(p.building_damage)          AS building_damage,
                AVG(CASE WHEN p.team_first_tormentor_time > 0
                         THEN p.team_first_tormentor_time ELSE NULL END)
                                                AS avg_first_tormentor_time,
                AVG(p.duration)                 AS avg_duration,
                AVG(CASE WHEN p.player_slot >= 0   AND p.player_slot < 128
                         THEN p.duration ELSE NULL END) AS avg_duration_radiant,
                AVG(CASE WHEN p.player_slot >= 128
                         THEN p.duration ELSE NULL END) AS avg_duration_dire,
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
    diffs_vs_lobby = _compute_player_diffs_vs_lobby(guild_id, start, end)
    results = []
    for r in rows:
        d = dict(r)
        # KDA = (kills + assists) / max(deaths, 1)
        d["kda"] = round((d["total_kills"] + d["total_assists"]) / max(d["total_deaths"], 1), 2)
        # Fantasy points (see fantasy.py for the formula)
        from fantasy import calculate_fantasy_points
        d["fantasy_points"] = calculate_fantasy_points(d)
        d["diff"] = diffs.get(d["account_id"], 0.0)
        d["diff_vs_lobby"] = diffs_vs_lobby.get(d["account_id"], 0.0)
        results.append(d)
    return results


def get_stats_for_season_week(guild_id: int, week_number: int, season_start_date: str) -> list[dict]:
    """Return aggregated per-player stats for a specific season week.

    Resolves through get_season_week_range so an explicitly-defined week
    (including a week 0, or a bye/reschedule override) wins over the plain
    calendar formula — the formula assumes week 1 is the first week, so it
    puts week 0 a week BEFORE the season and finds nothing.
    """
    start, end = get_season_week_range(guild_id, season_start_date, week_number)
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
                AVG(p.observer_kills * 60.0 / NULLIF(p.duration, 0))
                                                AS observer_kills_per_min,
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
                AVG(p.building_damage)          AS building_damage,
                AVG(CASE WHEN p.team_first_tormentor_time > 0
                         THEN p.team_first_tormentor_time ELSE NULL END)
                                                AS avg_first_tormentor_time,
                AVG(p.duration)                 AS avg_duration,
                AVG(CASE WHEN p.player_slot >= 0   AND p.player_slot < 128
                         THEN p.duration ELSE NULL END) AS avg_duration_radiant,
                AVG(CASE WHEN p.player_slot >= 128
                         THEN p.duration ELSE NULL END) AS avg_duration_dire,
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
    diffs_vs_lobby = _compute_player_diffs_vs_lobby(guild_id, start, end)
    results = []
    for r in rows:
        d = dict(r)
        d["kda"] = round((d["total_kills"] + d["total_assists"]) / max(d["total_deaths"], 1), 2)
        from fantasy import calculate_fantasy_points
        d["fantasy_points"] = calculate_fantasy_points(d)
        d["diff"] = diffs.get(d["account_id"], 0.0)
        d["diff_vs_lobby"] = diffs_vs_lobby.get(d["account_id"], 0.0)
        results.append(d)
    costs = get_player_costs(guild_id, season_start_date)
    results = _attach_costs(results, costs)
    _compute_value(results)
    attendance_map = _compute_attendance(guild_id, start, end, costs)
    for s in results:
        s["attendance"] = attendance_map.get(s["account_id"])
    return results


def get_all_time_stats(guild_id: int, season_start_date: str = None,
                       cross_season: bool = False) -> list[dict]:
    """Return aggregated per-player stats across all matches for a division.

    If season_start_date is provided, only includes matches from that date
    onwards. With cross_season=True the match window opens up to every match
    the guild has ever recorded (all seasons), while cost-derived fields
    (cost/captain/value/attendance) still come from season_start_date — those
    describe a specific draft, so they stay season-scoped by definition.
    """
    from fantasy import calculate_fantasy_points

    # cross_season widens the match window to the guild's entire history;
    # cost/attendance below still key off season_start_date.
    start_ts = 0 if cross_season else _all_time_start_ts(guild_id, season_start_date)

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
                AVG(p.observer_kills * 60.0 / NULLIF(p.duration, 0))
                                                AS observer_kills_per_min,
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
                AVG(p.building_damage)          AS building_damage,
                AVG(CASE WHEN p.team_first_tormentor_time > 0
                         THEN p.team_first_tormentor_time ELSE NULL END)
                                                AS avg_first_tormentor_time,
                AVG(p.duration)                 AS avg_duration,
                AVG(CASE WHEN p.player_slot >= 0   AND p.player_slot < 128
                         THEN p.duration ELSE NULL END) AS avg_duration_radiant,
                AVG(CASE WHEN p.player_slot >= 128
                         THEN p.duration ELSE NULL END) AS avg_duration_dire,
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
    diffs_vs_lobby = _compute_player_diffs_vs_lobby(guild_id, start_ts)
    results = []
    for r in rows:
        d = dict(r)
        d["kda"] = round((d["total_kills"] + d["total_assists"]) / max(d["total_deaths"], 1), 2)
        d["fantasy_points"] = calculate_fantasy_points(d)
        d["diff"] = diffs.get(d["account_id"], 0.0)
        d["diff_vs_lobby"] = diffs_vs_lobby.get(d["account_id"], 0.0)
        results.append(d)
    if season_start_date:
        costs = get_player_costs(guild_id, season_start_date)
        results = _attach_costs(results, costs)
        _compute_value(results)
        # Attendance is "what share of your team's season did you show up for",
        # so it always uses the season window even when the stat rows above
        # were widened to every season by cross_season.
        season_ts = _all_time_start_ts(guild_id, season_start_date)
        attendance_map = _compute_attendance(guild_id, season_ts, None, costs)
        for s in results:
            s["attendance"] = attendance_map.get(s["account_id"])
    else:
        for s in results:
            s["attendance"] = None

    # Mirror alt accounts: an alt typically has no match rows of its own
    # (their games were played under the main's Steam account), so add a
    # copy of the main's full stat row under the alt's account_id too — see
    # _mirror_alts_into for the general version of this used elsewhere.
    by_aid = {s["account_id"]: s for s in results}
    with _conn() as conn:
        alt_rows = conn.execute(
            "SELECT account_id, alt_account_for FROM skill_overrides WHERE alt_account_for IS NOT NULL"
        ).fetchall()
    for ar in alt_rows:
        alt_id, main_id = ar["account_id"], ar["alt_account_for"]
        if alt_id not in by_aid and main_id in by_aid:
            mirrored = dict(by_aid[main_id])
            mirrored["account_id"] = alt_id
            results.append(mirrored)

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
        "building_damage":         (row["building_damage"] if "building_damage" in row.keys() else 0) or 0,
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


def _compute_player_diffs_vs_lobby(guild_id: int, start: int, end: int | None = None) -> dict[int, float]:
    """For every player with matches in the time range, return the average
    per-match fantasy-points diff vs. the OTHER 9 players in the match —
    both teammates AND opponents. Opponent-strength-aware in a way the
    teammates-only diff isn't: a great performance against a weak enemy
    team gets discounted; standing out against a stacked enemy gets bonus.

    Used only by /leaderboard Value (via _compute_value). The teammates-only
    diff powers the fantasy adjustment path — this doesn't replace it.
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
        aids = list(fps.keys())
        if len(aids) < 2:
            continue
        for aid in aids:
            others = [fps[a] for a in aids if a != aid]
            other_avg = sum(others) / len(others)
            diffs_by_account.setdefault(aid, []).append(fps[aid] - other_avg)

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
    name, internal_rating, raw_windrun, mmr_estimate, ad_last_year, ad_all_time,
    ranked_last_year, explanation, avatar_url."""
    from datetime import datetime, timezone
    payload = {
        "account_id":       row["account_id"],
        "name":             row.get("name"),
        "internal_rating":  row.get("internal_rating"),
        "raw_windrun":      row.get("raw_windrun"),
        "mmr_estimate":     row.get("mmr_estimate"),
        "rank_tier":        row.get("rank_tier"),
        "leaderboard_rank": row.get("leaderboard_rank"),
        "ad_last_year":     row.get("ad_last_year"),
        "ad_all_time":      row.get("ad_all_time"),
        "ranked_last_year": row.get("ranked_last_year"),
        "explanation":      row.get("explanation"),
        "avatar_url":       row.get("avatar_url"),
        "fh_unavailable":   1 if row.get("fh_unavailable") else 0 if "fh_unavailable" in row else None,
        "updated_at":       datetime.now(timezone.utc).isoformat(),
    }
    with _conn() as conn:
        conn.execute("""
            INSERT INTO player_ratings_cache
                (account_id, name, internal_rating, raw_windrun, mmr_estimate,
                 rank_tier, leaderboard_rank,
                 ad_last_year, ad_all_time, ranked_last_year, explanation, avatar_url,
                 fh_unavailable, updated_at)
            VALUES (:account_id, :name, :internal_rating, :raw_windrun, :mmr_estimate,
                    :rank_tier, :leaderboard_rank,
                    :ad_last_year, :ad_all_time, :ranked_last_year,
                    :explanation, :avatar_url, :fh_unavailable, :updated_at)
            ON CONFLICT(account_id) DO UPDATE SET
                name             = COALESCE(excluded.name, player_ratings_cache.name),
                internal_rating  = COALESCE(excluded.internal_rating, player_ratings_cache.internal_rating),
                raw_windrun      = COALESCE(excluded.raw_windrun, player_ratings_cache.raw_windrun),
                mmr_estimate     = COALESCE(excluded.mmr_estimate, player_ratings_cache.mmr_estimate),
                rank_tier        = COALESCE(excluded.rank_tier, player_ratings_cache.rank_tier),
                leaderboard_rank = COALESCE(excluded.leaderboard_rank, player_ratings_cache.leaderboard_rank),
                ad_last_year     = COALESCE(excluded.ad_last_year, player_ratings_cache.ad_last_year),
                ad_all_time      = COALESCE(excluded.ad_all_time, player_ratings_cache.ad_all_time),
                ranked_last_year = COALESCE(excluded.ranked_last_year, player_ratings_cache.ranked_last_year),
                explanation      = COALESCE(excluded.explanation, player_ratings_cache.explanation),
                avatar_url       = COALESCE(excluded.avatar_url, player_ratings_cache.avatar_url),
                fh_unavailable   = COALESCE(excluded.fh_unavailable, player_ratings_cache.fh_unavailable),
                updated_at       = excluded.updated_at
        """, payload)


def get_rating_cache_row(account_id: int) -> dict | None:
    """Return a single cached rating row (with override nickname + hide_avatar
    joined in), or None if not present.

    If the override has alt_account_for set, the returned row substitutes the
    main account's internal_rating (and explanation) into this row so callers
    transparently treat the alt as having the main's skill. Alt's own name,
    avatar, and stats are preserved.
    """
    with _conn() as conn:
        row = conn.execute("""
            SELECT prc.*, so.nickname AS override_nickname,
                   so.hide_avatar AS hide_avatar,
                   so.alt_account_for AS alt_account_for
            FROM player_ratings_cache prc
            LEFT JOIN skill_overrides so ON so.account_id = prc.account_id
            WHERE prc.account_id = ?
        """, (account_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        main_id = result.get("alt_account_for")
        if main_id and main_id != account_id:
            main_row = conn.execute(
                "SELECT internal_rating, explanation, name FROM player_ratings_cache "
                "WHERE account_id = ?",
                (main_id,),
            ).fetchone()
            if main_row and main_row["internal_rating"] is not None:
                main_name = main_row["name"] or f"#{main_id}"
                result["internal_rating"] = main_row["internal_rating"]
                result["explanation"] = f"linked to {main_name} ({main_row['explanation'] or 'main account'})"
        return result


def get_all_rating_cache_rows() -> list[dict]:
    """Return account_id + internal_rating (+ override nickname) for every
    cached player. Used by the /api/ratings endpoint.

    Mirrors get_rating_cache_row's alt_account_for substitution: an alt's
    internal_rating is swapped for its main account's, when the main has one.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT prc.account_id,
                   COALESCE(so.nickname, prc.name) AS name,
                   CASE
                       WHEN so.alt_account_for IS NOT NULL AND main.internal_rating IS NOT NULL
                       THEN main.internal_rating
                       ELSE prc.internal_rating
                   END AS internal_rating
            FROM player_ratings_cache prc
            LEFT JOIN skill_overrides so ON so.account_id = prc.account_id
            LEFT JOIN player_ratings_cache main ON main.account_id = so.alt_account_for
        """).fetchall()
    return [dict(r) for r in rows]


def is_avatar_hidden(account_id: int) -> bool:
    """True when the account has hide_avatar set in skill_overrides."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT hide_avatar FROM skill_overrides WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return bool(row and row["hide_avatar"])


def compute_fantasy_adjusted_ratings(guild_id: int, season_start: str) -> dict[int, dict]:
    """For each cached player in this guild, fit a regression line of
    fantasy diff (boosted by winrate) vs. base internal_rating, then return a
    per-account_id dict of fantasy-adjusted ratings.

    The adjustment is the residual (actual diff − expected diff from the fit),
    normalized so the max-magnitude residual maps to ±6%. Players without
    match history (just added via /set_player) get no entry in the result
    and should be shown at their base rating.

    Returns: {account_id: {
        "base_rating": int,
        "adjusted_rating": int,
        "diff": float, "expected": float, "residual": float, "pct": float,
    }}
    """
    cached = get_guild_cached_ratings(guild_id)
    rows = [dict(r) for r in cached]
    if not rows:
        return {}

    stats = get_all_time_stats(guild_id, season_start)
    diff_by_aid: dict[int, float] = {}
    games_by_aid: dict[int, int] = {}
    for s in stats:
        aid = s["account_id"]
        base_diff = s.get("diff") or 0.0
        games = s.get("games_played") or 0
        wins  = s.get("wins") or 0
        wr_adj = ((wins / games) - 0.5) * 6.0 if games > 0 else 0.0
        diff_by_aid[aid] = base_diff + wr_adj
        games_by_aid[aid] = games

    # Exclude manually-rated players from the fit: their internal_rating is
    # artificial, so it would skew the regression line.
    fit_data = [
        (r["internal_rating"], diff_by_aid[r["account_id"]])
        for r in rows
        if r.get("internal_rating") is not None
        and r["account_id"] in diff_by_aid
        and r.get("rating_override") is None
    ]
    if len(fit_data) < 2:
        return {}

    n = len(fit_data)
    sx  = sum(x for x, _ in fit_data)
    sy  = sum(y for _, y in fit_data)
    sxy = sum(x * y for x, y in fit_data)
    sxx = sum(x * x for x, _ in fit_data)
    denom = n * sxx - sx * sx
    if denom != 0:
        slope     = (n * sxy - sx * sy) / denom
        intercept = (sy - slope * sx) / n
    else:
        slope, intercept = 0.0, sy / n

    residuals: list[tuple[int, float, float, float, int]] = []
    for r in rows:
        aid = r["account_id"]
        if r.get("internal_rating") is None or aid not in diff_by_aid:
            continue
        # Manually-rated players are pinned — don't apply the adjustment.
        if r.get("rating_override") is not None:
            continue
        actual   = diff_by_aid[aid]
        expected = intercept + slope * r["internal_rating"]
        residual = actual - expected
        residuals.append((aid, actual, expected, residual, r["internal_rating"]))
    if not residuals:
        return {}

    max_abs = max(abs(rsd) for _, _, _, rsd, _ in residuals) or 1.0
    # Confidence scaling: a player with N=10 league games gets full credit;
    # below that the adjustment scales linearly down to 0 at 0 games.
    CONFIDENCE_GAMES = 10
    result: dict[int, dict] = {}
    for aid, actual, expected, residual, base in residuals:
        confidence = min(games_by_aid.get(aid, 0) / CONFIDENCE_GAMES, 1.0)
        pct = (residual / max_abs) * 0.06 * confidence
        result[aid] = {
            "base_rating":     base,
            "adjusted_rating": round(base * (1 + pct)),
            "diff":            actual,
            "expected":        expected,
            "residual":        residual,
            "pct":             pct,
            "confidence":      confidence,
        }
    return result


def get_guild_cached_ratings(guild_id: int, include_all: bool = False) -> list[dict]:
    """Cached rating rows. By default, only players who've appeared in this
    guild's matches. Joins skill_overrides so callers see any override-nickname
    and rating-override inline. Excludes any account marked as excluded for
    this guild (alt/smurf accounts).

    If include_all=True, returns every cached player globally (minus this
    guild's exclusions) — used by /players show_all:true. This is also how
    manually-added players (via /set_player on someone without match history)
    become visible.
    """
    with _conn() as conn:
        if include_all:
            rows = conn.execute("""
                SELECT prc.*, so.nickname AS override_nickname,
                       so.internal_rating_override AS rating_override,
                       so.alt_account_for AS alt_account_for
                FROM player_ratings_cache prc
                LEFT JOIN skill_overrides so ON so.account_id = prc.account_id
                WHERE prc.account_id NOT IN (
                    SELECT account_id FROM guild_exclusions WHERE guild_id = ?
                )
            """, (guild_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT prc.*, so.nickname AS override_nickname,
                       so.internal_rating_override AS rating_override,
                       so.alt_account_for AS alt_account_for
                FROM player_ratings_cache prc
                LEFT JOIN skill_overrides so ON so.account_id = prc.account_id
                WHERE prc.account_id IN (
                    SELECT DISTINCT p.account_id
                    FROM players p
                    JOIN matches m ON p.match_id = m.match_id
                    WHERE m.guild_id = ? AND p.account_id > 0
                )
                AND prc.account_id NOT IN (
                    SELECT account_id FROM guild_exclusions WHERE guild_id = ?
                )
            """, (guild_id, guild_id)).fetchall()

        # Substitute the main account's internal_rating for any alt-linked row,
        # matching get_rating_cache_row's behavior so /players shows alts at
        # their main's skill. Main may be outside this guild's roster, so pull
        # any missing mains explicitly.
        needed_mains = {r["alt_account_for"] for r in rows if r["alt_account_for"]}
        have = {r["account_id"] for r in rows}
        missing = needed_mains - have
        main_ratings = {r["account_id"]: r["internal_rating"] for r in rows
                        if r["internal_rating"] is not None}
        if missing:
            placeholders = ",".join("?" * len(missing))
            extra = conn.execute(
                f"SELECT account_id, internal_rating FROM player_ratings_cache "
                f"WHERE account_id IN ({placeholders}) AND internal_rating IS NOT NULL",
                tuple(missing),
            ).fetchall()
            for e in extra:
                main_ratings[e["account_id"]] = e["internal_rating"]
        result = []
        for r in rows:
            d = dict(r)
            main_id = d.get("alt_account_for")
            if main_id and main_id in main_ratings:
                d["internal_rating"] = main_ratings[main_id]
            result.append(d)
    return result


def exclude_from_guild(guild_id: int, account_id: int) -> None:
    """Add this account to the guild's exclusion list (for alts/smurfs)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO guild_exclusions (guild_id, account_id, excluded_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, account_id) DO NOTHING
        """, (guild_id, account_id, now))


def unexclude_from_guild(guild_id: int, account_id: int) -> bool:
    """Remove an exclusion. Returns True if a row was deleted."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM guild_exclusions WHERE guild_id = ? AND account_id = ?",
            (guild_id, account_id),
        )
    return cur.rowcount > 0


def add_to_guild_roster(guild_id: int, account_id: int) -> None:
    """Mark this account_id as part of this guild's tracked roster."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO guild_roster (guild_id, account_id, added_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, account_id) DO NOTHING
        """, (guild_id, account_id, now))


def remove_from_guild_roster(guild_id: int, account_id: int) -> bool:
    """Remove a manually-added roster entry. Returns True if a row was removed."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM guild_roster WHERE guild_id = ? AND account_id = ?",
            (guild_id, account_id),
        )
    return cur.rowcount > 0


def guild_player_has_matches(guild_id: int, account_id: int) -> bool:
    """True if the account has at least one match in this guild's history."""
    with _conn() as conn:
        row = conn.execute("""
            SELECT 1 FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.guild_id = ? AND p.account_id = ?
            LIMIT 1
        """, (guild_id, account_id)).fetchone()
    return row is not None


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
    rank_tier: int | None,
    leaderboard_rank: int | None,
    note: str | None,
    set_by_user_id: int | None,
    set_by_name: str | None,
    nickname: str | None = None,
    internal_rating_override: int | None = None,
    hide_avatar: bool | None = None,
    alt_account_for: int | None = None,
    discord_override: str | None = None,
) -> None:
    """Insert or update an override. Pass None for any field to KEEP the
    existing value (partial updates). To clear an override entirely, use
    delete_skill_override().

    rank_tier + leaderboard_rank replace the legacy ranked_mmr override. The
    legacy column stays in the row schema but new writes always leave it None.

    alt_account_for links this account to a main account — get_rating_cache_row
    transparently substitutes the main's internal_rating for the alt.
    """
    existing = get_skill_override(account_id)
    if existing:
        wr   = windrun_rating if windrun_rating is not None else existing.get("windrun_rating")
        rt   = rank_tier      if rank_tier      is not None else existing.get("rank_tier")
        lb   = leaderboard_rank if leaderboard_rank is not None else existing.get("leaderboard_rank")
        nt   = note           if note           is not None else existing.get("note")
        nk   = nickname       if nickname       is not None else existing.get("nickname")
        ir   = internal_rating_override if internal_rating_override is not None else existing.get("internal_rating_override")
        ha   = (1 if hide_avatar else 0) if hide_avatar is not None else existing.get("hide_avatar") or 0
        alt  = alt_account_for if alt_account_for is not None else existing.get("alt_account_for")
        dov  = discord_override if discord_override is not None else existing.get("discord_override")
    else:
        wr, rt, lb, nt, nk, ir = windrun_rating, rank_tier, leaderboard_rank, note, nickname, internal_rating_override
        ha = 1 if hide_avatar else 0
        alt = alt_account_for
        dov = discord_override
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO skill_overrides
                (account_id, windrun_rating, ranked_mmr, rank_tier, leaderboard_rank,
                 nickname, note, internal_rating_override, hide_avatar, alt_account_for,
                 discord_override, set_by_user_id, set_by_name, set_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                windrun_rating          = excluded.windrun_rating,
                ranked_mmr              = NULL,
                rank_tier               = excluded.rank_tier,
                leaderboard_rank        = excluded.leaderboard_rank,
                nickname                = excluded.nickname,
                note                    = excluded.note,
                internal_rating_override = excluded.internal_rating_override,
                hide_avatar             = excluded.hide_avatar,
                alt_account_for         = excluded.alt_account_for,
                discord_override        = excluded.discord_override,
                set_by_user_id          = excluded.set_by_user_id,
                set_by_name             = excluded.set_by_name,
                set_at                  = excluded.set_at
        """, (account_id, wr, rt, lb, nk, nt, ir, ha, alt, dov, set_by_user_id, set_by_name, now))


def delete_skill_override(account_id: int) -> bool:
    """Remove any override row for this account. Returns True if a row was removed."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM skill_overrides WHERE account_id = ?", (account_id,)
        )
        return cur.rowcount > 0


def get_captain_account_ids(guild_id: int, season_start: str) -> dict[str, int | None]:
    """Map each captain name from player_costs to an account_id by exact
    case-insensitive name match against (in order):
      1. skill_overrides.nickname
      2. player_ratings_cache.name
      3. players.name within this guild
      4. players.name globally
    Returns {captain_name: account_id_or_None}.
    """
    with _conn() as conn:
        captains = [
            r["captain"]
            for r in conn.execute(
                "SELECT DISTINCT captain FROM player_costs "
                "WHERE guild_id = ? AND season_start = ? AND captain IS NOT NULL",
                (guild_id, season_start),
            ).fetchall()
        ]
        result: dict[str, int | None] = {cap: None for cap in captains}
        for cap in captains:
            row = conn.execute(
                "SELECT account_id FROM skill_overrides "
                "WHERE nickname IS NOT NULL AND LOWER(nickname) = LOWER(?)",
                (cap,),
            ).fetchone()
            if row:
                result[cap] = row["account_id"]; continue
            row = conn.execute(
                "SELECT account_id FROM player_ratings_cache "
                "WHERE name IS NOT NULL AND LOWER(name) = LOWER(?)",
                (cap,),
            ).fetchone()
            if row:
                result[cap] = row["account_id"]; continue
            row = conn.execute("""
                SELECT DISTINCT p.account_id
                FROM players p JOIN matches m ON p.match_id = m.match_id
                WHERE m.guild_id = ? AND LOWER(p.name) = LOWER(?)
                LIMIT 1
            """, (guild_id, cap)).fetchone()
            if row:
                result[cap] = row["account_id"]; continue
            row = conn.execute(
                "SELECT DISTINCT account_id FROM players "
                "WHERE LOWER(name) = LOWER(?) LIMIT 1",
                (cap,),
            ).fetchone()
            if row:
                result[cap] = row["account_id"]
    return result


def get_head_to_head_matches(
    guild_id: int, season_start: str, captain_a: str, captain_b: str,
) -> list[dict]:
    """Find matches where both teams had ≥3 of their roster on opposite sides.

    Each returned dict has:
        match_id, start_time, duration, a_won (bool)
    Sorted by start_time ascending.
    """
    start_ts = _all_time_start_ts(guild_id, season_start)

    costs = get_player_costs(guild_id, season_start)
    cap_aids = get_captain_account_ids(guild_id, season_start)

    def _roster(cap_name: str) -> set[int]:
        s = {aid for aid, c in costs.items() if c.get("captain") == cap_name}
        if cap_aids.get(cap_name):
            s.add(cap_aids[cap_name])
        return s

    roster_a = _roster(captain_a)
    roster_b = _roster(captain_b)
    if not roster_a or not roster_b:
        return []

    with _conn() as conn:
        rows = conn.execute("""
            SELECT p.match_id, p.account_id, p.player_slot,
                   m.radiant_win, m.duration, m.start_time
            FROM players p JOIN matches m ON p.match_id = m.match_id
            WHERE m.guild_id = ? AND m.start_time >= ?
        """, (guild_id, start_ts)).fetchall()

    by_match: dict[int, dict] = {}
    for r in rows:
        d = dict(r)
        m = by_match.setdefault(d["match_id"], {
            "radiant": set(), "dire": set(),
            "radiant_win": d["radiant_win"],
            "duration":    d["duration"],
            "start_time":  d["start_time"],
        })
        slot = d.get("player_slot")
        if slot is None:
            continue
        if 0 <= slot < 128:
            m["radiant"].add(d["account_id"])
        elif slot >= 128:
            m["dire"].add(d["account_id"])

    results: list[dict] = []
    for match_id, m in by_match.items():
        r_a = len(roster_a & m["radiant"])
        d_a = len(roster_a & m["dire"])
        r_b = len(roster_b & m["radiant"])
        d_b = len(roster_b & m["dire"])
        a_radiant = r_a >= 3 and d_b >= 3
        a_dire    = d_a >= 3 and r_b >= 3
        if not (a_radiant or a_dire):
            continue
        if a_radiant:
            a_won = bool(m["radiant_win"])
        else:
            a_won = not bool(m["radiant_win"])
        results.append({
            "match_id":   match_id,
            "start_time": m["start_time"],
            "duration":   m["duration"],
            "a_won":      a_won,
        })
    results.sort(key=lambda x: x["start_time"])
    return results


def get_match_team_captains(
    guild_id: int, season_start: str, match_ids: list[int],
) -> dict[int, dict[str, str | None]]:
    """Attribute each side of each match to a team, keyed by captain.

    Same rule as get_head_to_head_matches / get_team_match_aggregates: a side
    belongs to a team when ≥3 of that team's roster (4 drafted + captain when
    resolvable) played on it. Sides no team qualifies for (stand-in-heavy
    lineups, matches from another season) map to None.

    Returns {match_id: {"radiant": captain_or_None, "dire": captain_or_None}}.
    """
    if not match_ids or not season_start:
        return {}

    costs = get_player_costs(guild_id, season_start)
    cap_aids = get_captain_account_ids(guild_id, season_start)
    rosters: dict[str, set[int]] = {}
    for aid, c in costs.items():
        if c.get("captain"):
            rosters.setdefault(c["captain"], set()).add(aid)
    for cap, cap_aid in cap_aids.items():
        if cap_aid and cap in rosters:
            rosters[cap].add(cap_aid)

    sides = {mid: {"radiant": set(), "dire": set()} for mid in match_ids}
    if rosters:
        ids = list(sides)
        with _conn() as conn:
            for i in range(0, len(ids), 500):  # stay under SQLite's bound-parameter limit
                chunk = ids[i:i + 500]
                rows = conn.execute(
                    f"SELECT match_id, account_id, team_side FROM players "
                    f"WHERE match_id IN ({','.join('?' * len(chunk))})",
                    chunk,
                ).fetchall()
                for r in rows:
                    side = sides[r["match_id"]].get(r["team_side"])
                    if side is not None:
                        side.add(r["account_id"])

    result: dict[int, dict[str, str | None]] = {}
    for mid, by_side in sides.items():
        result[mid] = {}
        for side, aids in by_side.items():
            cap = max(rosters, key=lambda c: len(rosters[c] & aids), default=None)
            result[mid][side] = cap if cap and len(rosters[cap] & aids) >= 3 else None
    return result


def get_team_match_aggregates(guild_id: int, season_start: str) -> dict[str, dict]:
    """Per-team stats computed match-by-match instead of per-player-row.

    Algorithm:
      1. Build each team's roster (4 drafted + captain when resolvable).
      2. For every match in the season, split into Radiant and Dire sides.
      3. For each team, count how many of their roster appear on each side.
         If ≥3 → that match is a 'team-match' for them. The team's 5 players
         for that match are *whoever was on that side* (including stand-ins).
      4. For each team-match, compute per-match team aggregates (5-player
         averages for per-player stats, summed totals for KDA).
      5. Average across all of a team's team-matches.

    Returns: {captain_name: {games_played, wins, gpm, xpm, kda, ..., members}}
    games_played here counts *team matches* (not player-games), and wins is
    a real team W-L.
    """
    start_ts = _all_time_start_ts(guild_id, season_start)

    # Rosters: captain → {account_ids}
    costs = get_player_costs(guild_id, season_start)
    captain_aids = get_captain_account_ids(guild_id, season_start)
    rosters: dict[str, set[int]] = {}
    for aid, c in costs.items():
        cap = c.get("captain")
        if not cap:
            continue
        rosters.setdefault(cap, set()).add(aid)
    for cap, cap_aid in captain_aids.items():
        if cap_aid and cap in rosters:
            rosters[cap].add(cap_aid)
    if not rosters:
        return {}

    # Display-name list per team (for the roster line in the embed).
    all_stats = {s["account_id"]: s for s in get_all_time_stats(guild_id, season_start)}
    member_names: dict[str, list[str]] = {}
    for cap, aids in rosters.items():
        names = []
        for aid in aids:
            s = all_stats.get(aid)
            names.append((s.get("name") if s else None) or f"Player_{aid}")
        member_names[cap] = names

    # Pull all the player-rows for this season + their match metadata.
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                p.match_id, p.account_id, p.name, p.player_slot,
                p.kills, p.deaths, p.assists,
                p.gpm, p.xpm, p.last_hits, p.denies,
                p.hero_damage, p.hero_healing,
                p.obs_placed, p.sen_placed,
                p.observer_kills, p.sentry_kills,
                p.tower_kills, p.roshans_killed,
                p.firstblood_claimed, p.teamfight_participation,
                p.stuns, p.camps_stacked, p.rune_pickups,
                p.tormentor_kills, p.watcher_captures,
                p.defensive_item_uses, p.building_damage,
                p.team_first_tormentor_time,
                m.radiant_win, m.duration AS match_duration
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.guild_id = ? AND m.start_time >= ?
        """, (guild_id, start_ts)).fetchall()

    # Group player-rows by match → radiant/dire.
    matches: dict[int, dict] = {}
    for r in rows:
        d = dict(r)
        m = matches.setdefault(d["match_id"], {
            "radiant": [], "dire": [],
            "radiant_win": d["radiant_win"],
            "duration":    d["match_duration"],
        })
        slot = d.get("player_slot")
        if slot is None:
            slot = -1
        if 0 <= slot < 128:
            m["radiant"].append(d)
        elif slot >= 128:
            m["dire"].append(d)

    # Stats that sum across the 5 players (team totals per match).
    PER_PLAYER_SUM_KEYS = (
        "gpm", "xpm",
        "last_hits", "denies", "hero_damage", "hero_healing",
        "tower_kills", "observer_kills", "roshans_killed", "camps_stacked",
        "rune_pickups", "defensive_item_uses", "tormentor_kills",
        "watcher_captures", "building_damage",
    )
    # Stats kept as the 5-player average (percentages, etc.).
    PER_PLAYER_AVG_KEYS = ("teamfight_participation",)

    def _per_match_team_stats(side: list[dict], duration: int) -> dict:
        n = len(side)
        out = {}
        for k in PER_PLAYER_SUM_KEYS:
            out[k] = sum((p.get(k) or 0) for p in side)
        for k in PER_PLAYER_AVG_KEYS:
            out[k] = sum((p.get(k) or 0) for p in side) / max(1, n)
        tk = sum((p.get("kills") or 0)   for p in side)
        td = sum((p.get("deaths") or 0)  for p in side)
        ta = sum((p.get("assists") or 0) for p in side)
        out["kda"] = (tk + ta) / max(1, td)
        total_stuns = sum((p.get("stuns") or 0) for p in side)
        out["stuns_per_min"] = (total_stuns * 60.0 / duration) if duration else 0
        out["avg_duration"] = duration or 0
        # First tormentor time: every player on the side carries the same
        # team_first_tormentor_time, so take the first non-(-1).
        ftt = next(
            (p.get("team_first_tormentor_time") for p in side
             if (p.get("team_first_tormentor_time") or -1) > 0),
            None,
        )
        out["avg_first_tormentor_time"] = ftt
        # Fantasy points per match: SUM of the 5 players' fp (team total).
        fps = []
        for p in side:
            try:
                fps.append(_per_match_fp(p))
            except Exception:
                pass
        out["fantasy_points"] = sum(fps) if fps else 0
        return out

    teams: dict[str, dict] = {}
    for cap, roster in rosters.items():
        team_match_stats: list[dict] = []
        wins = 0
        for match_id, m in matches.items():
            r_overlap = sum(1 for p in m["radiant"] if p["account_id"] in roster)
            d_overlap = sum(1 for p in m["dire"]    if p["account_id"] in roster)
            side, won = None, False
            # When both sides have ≥3 of the team (rare — e.g., mixed lobby),
            # pick the side with the higher overlap.
            if r_overlap >= 3 and r_overlap >= d_overlap:
                side, won = m["radiant"], bool(m["radiant_win"])
            elif d_overlap >= 3:
                side, won = m["dire"], not bool(m["radiant_win"])
            if not side or len(side) != 5:
                continue
            team_match_stats.append(_per_match_team_stats(side, m["duration"]))
            if won:
                wins += 1

        n = len(team_match_stats)
        if n == 0:
            continue
        agg = {
            "captain":      cap,
            "team_size":    len(roster),
            "games_played": n,
            "wins":         wins,
            "members":      member_names.get(cap, []),
        }
        # Average each per-match stat across team-matches.
        all_keys: set = set()
        for tms in team_match_stats:
            all_keys.update(tms.keys())
        for k in all_keys:
            vals = [tms.get(k) for tms in team_match_stats if tms.get(k) is not None]
            if vals:
                agg[k] = sum(vals) / len(vals)
        teams[cap] = agg
    return teams


def get_team_aggregates(guild_id: int, season_start: str) -> dict[str, dict]:
    """Aggregate season stats by team (keyed by captain name).

    Per-game stats (gpm, kda, hero_damage, etc.) are weighted-averaged by
    games_played so high-volume players matter more. Wins and games_played
    are summed.

    Returns: {captain_name: {games_played, wins, gpm, xpm, ...}}
    Stats that don't aggregate meaningfully (diff, value, attendance) are
    omitted.
    """
    costs = get_player_costs(guild_id, season_start)
    if not costs:
        return {}
    all_stats = {s["account_id"]: s for s in get_all_time_stats(guild_id, season_start)}

    by_captain: dict[str, list[dict]] = {}
    for aid, c in costs.items():
        cap = c.get("captain")
        if not cap:
            continue
        s = all_stats.get(aid)
        if s and (s.get("games_played") or 0) > 0:
            by_captain.setdefault(cap, []).append(s)

    # Fold the captain's own stats into their team (when we can resolve the
    # captain name to an account_id).
    captain_aids = get_captain_account_ids(guild_id, season_start)
    for cap, cap_aid in captain_aids.items():
        if not cap_aid or cap not in by_captain:
            continue
        if any(m.get("account_id") == cap_aid for m in by_captain[cap]):
            continue  # captain already counted (shouldn't happen, but safe)
        s = all_stats.get(cap_aid)
        if s and (s.get("games_played") or 0) > 0:
            by_captain[cap].append(s)

    # Per-game stats — weight by games_played.
    AVG_KEYS = (
        "gpm", "xpm", "last_hits", "denies", "hero_damage", "hero_healing",
        "avg_pct_damage", "teamfight_participation", "stuns_per_min",
        "tower_kills", "observer_kills", "roshans_killed", "camps_stacked",
        "rune_pickups", "defensive_item_uses", "tormentor_kills",
        "watcher_captures", "building_damage", "firstblood_claimed",
        "fantasy_points", "avg_duration",
    )

    teams: dict[str, dict] = {}
    for cap, members in by_captain.items():
        total_games = sum((m.get("games_played") or 0) for m in members)
        if total_games == 0:
            continue
        agg: dict = {
            "captain":      cap,
            "team_size":    len(members),
            "games_played": total_games,
            "wins":         sum((m.get("wins") or 0) for m in members),
            "members":      [m.get("name") or f"Player_{m.get('account_id')}" for m in members],
        }
        for k in AVG_KEYS:
            num = sum((m.get(k) or 0) * (m.get("games_played") or 0) for m in members)
            agg[k] = num / total_games
        # KDA aggregated from raw totals (not weighted of per-player KDA, which
        # would mangle the ratio).
        tk = sum((m.get("total_kills") or 0) for m in members)
        td = sum((m.get("total_deaths") or 0) for m in members)
        ta = sum((m.get("total_assists") or 0) for m in members)
        agg["kda"] = (tk + ta) / max(1, td)
        agg["total_kills"]   = tk
        agg["total_deaths"]  = td
        agg["total_assists"] = ta
        teams[cap] = agg
    return teams


def _mirror_alts_into(result: dict[int, dict]) -> None:
    """For every skill_overrides row with alt_account_for set, if the alt's
    account_id has no entry but its main's does, mirror the main's entry
    under the alt's account_id too. Lets a dict keyed by account_id resolve
    correctly regardless of which of the two accounts a caller looks up —
    e.g. someone who signed up for a season under their alt but whose real
    match history lives under their main."""
    with _conn() as conn:
        alt_rows = conn.execute(
            "SELECT account_id, alt_account_for FROM skill_overrides WHERE alt_account_for IS NOT NULL"
        ).fetchall()
    for ar in alt_rows:
        alt_id, main_id = ar["account_id"], ar["alt_account_for"]
        if alt_id not in result and main_id in result:
            result[alt_id] = result[main_id]


def get_player_costs(guild_id: int, season_start: str) -> dict[int, dict]:
    """Return {account_id: {cost, captain, mmr}} for the season. Alts with
    no cost row of their own get their main's mirrored under their own
    account_id too — see _mirror_alts_into."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT account_id, cost, captain, mmr FROM player_costs "
            "WHERE guild_id = ? AND season_start = ?",
            (guild_id, season_start),
        ).fetchall()
    result = {
        r["account_id"]: {"cost": r["cost"], "captain": r["captain"], "mmr": r["mmr"]}
        for r in rows
    }
    _mirror_alts_into(result)
    return result


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


MIN_GAMES_FOR_VALUE = 5


def _compute_value(stats: list[dict]) -> list[dict]:
    """Assign each drafted player a "deserved cost" and a value delta.

    Approach: rank drafted players by opponent-adjusted fantasy diff
    (best-to-worst), rank their actual costs (highest-to-lowest), and match
    them position-for-position. The best-performing player deserves the top
    actual cost in the pool, 2nd-best deserves the 2nd-highest, etc.

    value = deserved_cost − actual_cost
      positive → underpaid (steal),  negative → overpaid.

    Filters:
      - must have a cost (drafted)
      - must have MIN_GAMES_FOR_VALUE games this season (noise floor)
      - must have a diff_vs_lobby populated (opponent-adjusted diff — see
        _compute_player_diffs_vs_lobby in db.py)

    Everyone else gets s['value']=None, s['deserved_cost']=None.
    """
    # Initialise all rows so callers don't KeyError.
    for s in stats:
        s.setdefault("value", None)
        s.setdefault("deserved_cost", None)

    eligible = [
        s for s in stats
        if s.get("cost") is not None
        and s.get("diff_vs_lobby") is not None
        and (s.get("games_played") or 0) >= MIN_GAMES_FOR_VALUE
    ]
    if len(eligible) < 2:
        return stats

    # Rank-match: sort players by performance, sort costs, pair them up.
    # deserved_cost floored at 0 (no-op today since RD2L actual costs are all
    # positive, but keeps the intent explicit if we ever switch mapping methods).
    # Value itself is un-floored: negative Value = overpay, shown as such.
    perf_sorted = sorted(eligible, key=lambda s: -s["diff_vs_lobby"])
    costs_sorted = sorted((s["cost"] for s in eligible), reverse=True)
    for player, deserved in zip(perf_sorted, costs_sorted):
        deserved = max(0, deserved)
        player["deserved_cost"] = deserved
        player["value"] = round(deserved - player["cost"], 2)
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
        start = _all_time_start_ts(guild_id, season_start_date)
        end = 2**31 - 1
    else:
        start, end = get_season_week_range(guild_id, season_start_date, week_number)

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
    start, end = get_season_week_range(guild_id, season_start_date, week_number)
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


def get_latest_matches(guild_id: int, season_start_date: str | None = None) -> list[dict]:
    """Return all matches from the current season week (via
    get_current_season_week/get_season_week_range, so this respects explicit
    week boundaries — including byes — instead of a raw calendar week).

    Falls back to the plain "most recent calendar week with a match" when no
    season_start_date is available at all (e.g. division not configured)."""
    if season_start_date:
        week_number = get_current_season_week(guild_id, season_start_date)
        start, end = get_season_week_range(guild_id, season_start_date, week_number)
        with _conn() as conn:
            rows = conn.execute("""
                SELECT match_id, start_time, duration, radiant_win, radiant_score, dire_score
                FROM matches
                WHERE guild_id = :guild_id AND start_time BETWEEN :start AND :end
                ORDER BY start_time DESC
            """, {"guild_id": guild_id, "start": start, "end": end}).fetchall()
        return [dict(r) for r in rows]

    with _conn() as conn:
        latest = conn.execute(
            "SELECT MAX(start_time) as max_time FROM matches WHERE guild_id = ?",
            (guild_id,)
        ).fetchone()
        if not latest or not latest["max_time"]:
            return []

        dt = datetime.fromtimestamp(latest["max_time"], tz=timezone.utc)
        monday = dt - timedelta(days=dt.weekday())
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        sunday = monday + timedelta(days=7) - timedelta(seconds=1)

        rows = conn.execute("""
            SELECT match_id, start_time, duration, radiant_win, radiant_score, dire_score
            FROM matches
            WHERE guild_id = :guild_id AND start_time BETWEEN :start AND :end
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
