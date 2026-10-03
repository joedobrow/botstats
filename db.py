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
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/dota_stats.db" if os.path.exists("/data") else "dota_stats.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


# ---------------------------------------------------------------------------
# Derived-stat cache
#
# A season's player stats are a pure function of the matches on record, and
# those change only when the fetcher runs or an admin edits something — yet
# one page could rebuild them three times over, because asking for one
# player's row computes every player's. Caching them turns that into one
# computation shared by every page until the data actually changes.
#
# The invalidation signal is the database's own files. Writes in WAL mode land
# in the -wal file, so watching the size and modification time of both catches
# every write from any process, with no bookkeeping to forget at a call site.
# ---------------------------------------------------------------------------

_derived_cache: dict = {}


def _db_version() -> tuple:
    out = []
    for path in (DB_PATH, f"{DB_PATH}-wal"):
        try:
            st = os.stat(path)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _derived(key: tuple, build):
    """Return build()'s rows, recomputing only when the database has changed.

    Callers add their own keys to these rows (a breakdown, a display name), so
    each gets its own copy of each row — otherwise one page's annotations
    would leak into the next page's data.
    """
    version = _db_version()
    hit = _derived_cache.get(key)
    if hit is None or hit[0] != version:
        if hit is not None or len(_derived_cache) > 48:
            _derived_cache.clear()          # the data moved: everything is stale
        rows = build()
        _derived_cache[key] = (version, rows)
    return [dict(r) for r in _derived_cache[key][1]]


# Canonical definitions of the two big tables, shared by the fresh-database
# schema in init_db and the rebuild in _migrate_to_divisions.
MATCHES_TABLE = """
CREATE TABLE IF NOT EXISTS matches (
    match_id        INTEGER PRIMARY KEY,
    division_id     INTEGER REFERENCES divisions(id),  -- NULL: not a game of any division
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
);"""

PLAYERS_TABLE = """
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
    building_damage           INTEGER DEFAULT 0,   -- damage dealt to enemy buildings (OpenDota tower_damage)
    UNIQUE (match_id, player_slot)                 -- one row per seat; re-fetches update it
);"""


# Ability Draft picks, one row per pick, from windrun.io (fetch_draft in
# fetcher.py). ability_id < 0 is a hero "model" pick (-hero_id).
DRAFTS_TABLE = """
CREATE TABLE IF NOT EXISTS match_drafts (
    match_id    INTEGER NOT NULL REFERENCES matches(match_id),
    pick_order  INTEGER NOT NULL,
    account_id  INTEGER NOT NULL,
    ability_id  INTEGER NOT NULL,
    PRIMARY KEY (match_id, pick_order)
)"""


# Bumped by each structural migration in _MIGRATIONS; stored in the database
# as PRAGMA user_version so every migration runs exactly once, in order.
SCHEMA_VERSION = 3


def init_db():
    """Create tables if they don't exist, and migrate an older database to the
    current schema. Call once at startup."""
    fresh = _run_migrations()
    with _conn() as conn:
        conn.executescript(f"""
            -- A division: one competition within the league (e.g. NA, EU).
            -- Divisions share seasons (a season is one Dota league); games,
            -- drafts and signups belong to a division.
            CREATE TABLE IF NOT EXISTS divisions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT NOT NULL,         -- shown everywhere, e.g. "NA"
                slug              TEXT NOT NULL UNIQUE,  -- used in website URLs, e.g. "na"
                region            TEXT NOT NULL,         -- config.REGION_CLUSTERS key: which game servers count
                game_mode         TEXT NOT NULL,         -- 'cm' or 'ad'
                guild_id          INTEGER,               -- Discord server its commands run in (several divisions may share one)
                current_season_id INTEGER REFERENCES seasons(id),  -- what commands default to
                created_at        TEXT NOT NULL
            );

            -- Per-Discord-server settings.
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id            INTEGER PRIMARY KEY,
                default_division_id INTEGER REFERENCES divisions(id),  -- when a server has several
                scold_channel_id    INTEGER
            );

            {MATCHES_TABLE};
        """)

        # --- Migration: add columns to existing matches table if missing ---
        cursor = conn.execute("PRAGMA table_info(matches)")
        columns = [row[1] for row in cursor.fetchall()]
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
            if player_columns and col not in player_columns:
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
        if prc_cols and "windrun_games" not in prc_cols:
            # How many games the windrun rating rests on — the 100-game test
            # (formatters.MIN_WINDRUN_GAMES) has to be applied per account when
            # a player's accounts are compared, not just at fetch time.
            logger.info("Migrating: adding windrun_games to player_ratings_cache")
            conn.execute("ALTER TABLE player_ratings_cache ADD COLUMN windrun_games INTEGER")
        cursor = conn.execute("PRAGMA table_info(matches)")
        match_cols = [row[1] for row in cursor.fetchall()]
        if match_cols and "excluded" not in match_cols:
            # Taking a game out can't mean deleting the row: upsert_match is
            # INSERT OR IGNORE, so the next fetch of that league would put it
            # straight back. A flag the fetch never writes is what makes the
            # removal stick — scrim-week games, remakes, a lobby that counted
            # for nothing.
            logger.info("Migrating: adding excluded to matches")
            conn.execute("ALTER TABLE matches ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE matches ADD COLUMN excluded_reason TEXT")
            conn.execute("ALTER TABLE matches ADD COLUMN excluded_by INTEGER")
            conn.execute("ALTER TABLE matches ADD COLUMN excluded_at TEXT")
        if prc_cols and "ranked_all_time" not in prc_cols:
            # The last of the rating formula's inputs to go unstored, which
            # left a rating rebuilt from the cache (rating_compute
            # .inputs_from_cache) unable to reproduce the fetched one.
            logger.info("Migrating: adding ranked_all_time to player_ratings_cache")
            conn.execute("ALTER TABLE player_ratings_cache ADD COLUMN ranked_all_time INTEGER")
        if prc_cols and "group_rating" not in prc_cols:
            # One rating for everyone who plays several accounts, built from
            # the best evidence each account offers (see rating_group.py).
            logger.info("Migrating: adding group_rating + group_explanation to player_ratings_cache")
            conn.execute("ALTER TABLE player_ratings_cache ADD COLUMN group_rating INTEGER")
            conn.execute("ALTER TABLE player_ratings_cache ADD COLUMN group_explanation TEXT")

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

        # --- Migration: add ability_name column to draft_picks if missing ---
        cursor = conn.execute("PRAGMA table_info(draft_picks)")
        draft_columns = [row[1] for row in cursor.fetchall()]
        if draft_columns and "ability_name" not in draft_columns:
            logger.info("Migrating: adding ability_name column to draft_picks table")
            conn.execute("ALTER TABLE draft_picks ADD COLUMN ability_name TEXT DEFAULT ''")

        # --- Migration: add pick_type column ('ability' or 'model') ---
        if draft_columns and "pick_type" not in draft_columns:
            logger.info("Migrating: adding pick_type column to draft_picks table")
            conn.execute("ALTER TABLE draft_picks ADD COLUMN pick_type TEXT DEFAULT 'ability'")

        # --- Migration: add m_n_ability_id (opaque Source 2 pick ID) so we
        #     can retroactively resolve ability_name from windrun bootstrap. ---
        if draft_columns and "m_n_ability_id" not in draft_columns:
            logger.info("Migrating: adding m_n_ability_id column to draft_picks table")
            conn.execute("ALTER TABLE draft_picks ADD COLUMN m_n_ability_id INTEGER DEFAULT 0")

        conn.executescript(f"""

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

            {PLAYERS_TABLE};

            {DRAFTS_TABLE};

            CREATE INDEX IF NOT EXISTS idx_players_match   ON players(match_id);
            CREATE INDEX IF NOT EXISTS idx_players_account ON players(account_id);
            CREATE INDEX IF NOT EXISTS idx_matches_start   ON matches(start_time);
            CREATE INDEX IF NOT EXISTS idx_matches_division ON matches(division_id);
            CREATE INDEX IF NOT EXISTS idx_matches_league   ON matches(league_id);

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

            -- Player draft costs per season and division. account_id is the
            -- OpenDota account ID parsed from the Dotabuff URL.
            CREATE TABLE IF NOT EXISTS player_costs (
                season_id     INTEGER NOT NULL REFERENCES seasons(id),
                division_id   INTEGER NOT NULL REFERENCES divisions(id),
                account_id    INTEGER NOT NULL,
                cost          INTEGER NOT NULL,
                captain       TEXT,
                mmr           INTEGER,
                PRIMARY KEY (season_id, division_id, account_id)
            );

            -- Explicit week boundaries per season, replacing pure calendar
            -- math so bye weeks / reschedules can be handled directly
            -- (skip a week, or widen an adjacent one) instead of only ever
            -- being able to shift the whole season via season_start.
            -- week_number 0 is the pre-season/calibration window. A guild's
            -- season only uses this table once rows exist for it — see
            -- get_season_week_range/get_current_season_week, which fall
            -- back to the legacy calendar formula when none are defined.
            CREATE TABLE IF NOT EXISTS season_weeks (
                season_id     INTEGER NOT NULL REFERENCES seasons(id),
                division_id   INTEGER NOT NULL REFERENCES divisions(id),
                week_number   INTEGER NOT NULL,
                start_ts      INTEGER NOT NULL,
                end_ts        INTEGER NOT NULL,
                PRIMARY KEY (season_id, division_id, week_number)
            );

            -- One row per season, identified by the season's Dota league id
            -- (RD2L Season 39 = 20056, as on OpenDota/Dotabuff). A season can
            -- span several divisions playing in the same league; its games
            -- are the games played in that league. The start date is just a
            -- field.
            CREATE TABLE IF NOT EXISTS seasons (
                id            INTEGER PRIMARY KEY,  -- Dota league id
                season_start  TEXT NOT NULL,        -- 'YYYY-MM-DD'
                label         TEXT,
                created_at    TEXT NOT NULL
            );

            -- Which divisions play in which seasons.
            CREATE TABLE IF NOT EXISTS season_divisions (
                season_id     INTEGER NOT NULL REFERENCES seasons(id),
                division_id   INTEGER NOT NULL REFERENCES divisions(id),
                PRIMARY KEY (season_id, division_id)
            );

            -- Per-season, per-division team identity (real team name +
            -- logo), keyed by the captain string that appears in player_costs.
            CREATE TABLE IF NOT EXISTS season_teams (
                season_id     INTEGER NOT NULL REFERENCES seasons(id),
                division_id   INTEGER NOT NULL REFERENCES divisions(id),
                captain       TEXT NOT NULL,      -- lowercased captain key
                team_name     TEXT NOT NULL,
                logo_url      TEXT,
                PRIMARY KEY (season_id, division_id, captain)
            );

            -- The playoff bracket for a season: how many teams are in it and
            -- how long a series is. Who plays whom in the first round is a
            -- judgement — the top seed picks their opponent — so it's placed
            -- by hand (playoff_seeds). Everything after that is derived from
            -- the games, the way a rating is: a winner isn't stored, it's
            -- whoever won the series, and a bracket redraws itself when a
            -- result is corrected.
            CREATE TABLE IF NOT EXISTS playoff_brackets (
                season_id   INTEGER NOT NULL,
                division_id INTEGER NOT NULL,
                size        INTEGER NOT NULL,          -- teams in the bracket
                format      TEXT    NOT NULL DEFAULT 'single',
                best_of     INTEGER NOT NULL DEFAULT 3,
                started_at  TEXT    NOT NULL,          -- games before this are regular season
                created_by  INTEGER,
                PRIMARY KEY (season_id, division_id)
            );

            -- The first round's seats, in order: 0 plays 1, 2 plays 3, and so
            -- on. A seat with no team yet is a bracket still being filled in.
            CREATE TABLE IF NOT EXISTS playoff_seeds (
                season_id   INTEGER NOT NULL,
                division_id INTEGER NOT NULL,
                position    INTEGER NOT NULL,
                captain     TEXT,
                PRIMARY KEY (season_id, division_id, position)
            );

            -- Which team each side of a match belongs to, when the roster
            -- count can't say. A side is attributed automatically once three
            -- of a team's five played on it (get_match_team_captains), which
            -- a night of stand-ins defeats — three of Actor's five missing
            -- left their Sep 23 win credited to nobody and uncounted. An
            -- admin sets it here instead, and every reader honours it: the
            -- site, the Discord cards, the standings.
            -- captain NULL means "really was stand-ins"; no row means "work
            -- it out from the rosters".
            CREATE TABLE IF NOT EXISTS match_team_overrides (
                match_id   INTEGER NOT NULL,
                side       TEXT NOT NULL CHECK (side IN ('radiant', 'dire')),
                captain    TEXT,
                set_by     INTEGER,
                set_at     TEXT NOT NULL,
                PRIMARY KEY (match_id, side)
            );

            -- Per-division roster of "tracked" players. Lets /set_player add
            -- a player to a division even if they haven't appeared in a match
            -- yet, so /players will surface them.
            CREATE TABLE IF NOT EXISTS division_roster (
                division_id INTEGER NOT NULL REFERENCES divisions(id),
                account_id  INTEGER NOT NULL,
                added_at    TEXT NOT NULL,
                PRIMARY KEY (division_id, account_id)
            );

            -- Per-division exclusion list. Used to hide alt/smurf accounts
            -- from /players even when match history exists.
            CREATE TABLE IF NOT EXISTS division_exclusions (
                division_id  INTEGER NOT NULL REFERENCES divisions(id),
                account_id   INTEGER NOT NULL,
                excluded_at  TEXT NOT NULL,
                PRIMARY KEY (division_id, account_id)
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
    if fresh:
        with _conn() as conn:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    _migrate_ranked_mmr_overrides_to_rank()
    logger.info("Database initialised at %s (schema v%d)", DB_PATH, SCHEMA_VERSION)


def _run_migrations() -> bool:
    """Bring an existing database up to SCHEMA_VERSION, one numbered
    migration at a time (PRAGMA user_version records how far it got). Each
    migration runs in its own transaction and checks its row counts, so a
    failure leaves the database at the last version that fully landed.
    Returns True for a brand-new database (nothing to migrate)."""
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # transactions managed explicitly
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "matches" not in tables:
            return True
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            # Databases from before versioning: seasons keyed by date are
            # version 0; seasons keyed by league id are version 1.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(player_costs)")}
            version = 0 if "season_start" in cols else 1
        for number, migrate in enumerate(_MIGRATIONS, start=1):
            if version < number:
                migrate(conn)
                version = number
        return False
    finally:
        conn.close()


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    """Run ;-separated statements one by one via execute(), which (unlike
    executescript) never commits the surrounding transaction."""
    for stmt in script.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def _migrate_seasons_to_ids(conn: sqlite3.Connection) -> None:
    """Migration 1: seasons keyed by (guild_id, season_start) become seasons
    keyed by their Dota league id. Every season any table mentions becomes a
    seasons row whose id is its league id — from the seasons registry, or
    for a division's current season, the division's league. Costs, weeks
    and teams are rebuilt with season_id, and divisions'
    (league_id, season_start) pair becomes current_season_id.

    Refuses (and changes nothing) if a season has no league id, or two
    seasons claim the same one."""
    try:
        run = lambda script: _run_script(conn, script)
        conn.execute("BEGIN IMMEDIATE")
        if "fetch_day" in _cols(conn, "divisions"):
            logger.warning("Dropping divisions.fetch_day, which no code reads. Values were: %s",
                           [tuple(r) for r in conn.execute("SELECT guild_id, fetch_day FROM divisions")])
        before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("player_costs", "season_weeks", "season_teams", "divisions")}

        run("""
            CREATE TEMP TABLE all_seasons AS
                SELECT guild_id, season_start FROM seasons
                UNION SELECT guild_id, season_start FROM divisions
                UNION SELECT guild_id, season_start FROM player_costs
                UNION SELECT guild_id, season_start FROM season_weeks
                UNION SELECT guild_id, season_start FROM season_teams;

            -- A division's league is the one it actually fetches, so it wins
            -- for that division's current season.
            CREATE TEMP TABLE season_keys AS
                SELECT a.guild_id, a.season_start,
                       COALESCE(d.league_id, s.league_id) AS id,
                       s.label, COALESCE(s.created_at, datetime('now')) AS created_at
                FROM all_seasons a
                LEFT JOIN seasons s   ON s.guild_id = a.guild_id AND s.season_start = a.season_start
                LEFT JOIN divisions d ON d.guild_id = a.guild_id AND d.season_start = a.season_start;
        """)
        missing = conn.execute(
            "SELECT guild_id, season_start FROM temp.season_keys WHERE id IS NULL OR id = 0").fetchall()
        dupes = conn.execute(
            "SELECT id, COUNT(*) FROM temp.season_keys GROUP BY id HAVING COUNT(*) > 1").fetchall()
        if missing or dupes:
            raise RuntimeError(
                "Can't key seasons by league id. "
                + (f"No league id for: {[tuple(r) for r in missing]}. " if missing else "")
                + (f"League ids used by more than one season: {[tuple(r) for r in dupes]}. " if dupes else "")
                + "Set seasons.league_id for these, then restart.")

        run("""
            CREATE TABLE seasons_new (
                id            INTEGER PRIMARY KEY,
                guild_id      INTEGER NOT NULL,
                season_start  TEXT NOT NULL,
                label         TEXT,
                created_at    TEXT NOT NULL,
                UNIQUE (guild_id, season_start)
            );
            INSERT INTO seasons_new (id, guild_id, season_start, label, created_at)
                SELECT id, guild_id, season_start, label, created_at FROM temp.season_keys;

            CREATE TABLE player_costs_new (
                season_id     INTEGER NOT NULL REFERENCES seasons(id),
                account_id    INTEGER NOT NULL,
                cost          INTEGER NOT NULL,
                captain       TEXT,
                mmr           INTEGER,
                PRIMARY KEY (season_id, account_id)
            );
            INSERT INTO player_costs_new
                SELECT n.id, pc.account_id, pc.cost, pc.captain, pc.mmr
                FROM player_costs pc JOIN seasons_new n USING (guild_id, season_start);

            CREATE TABLE season_weeks_new (
                season_id     INTEGER NOT NULL REFERENCES seasons(id),
                week_number   INTEGER NOT NULL,
                start_ts      INTEGER NOT NULL,
                end_ts        INTEGER NOT NULL,
                PRIMARY KEY (season_id, week_number)
            );
            INSERT INTO season_weeks_new
                SELECT n.id, w.week_number, w.start_ts, w.end_ts
                FROM season_weeks w JOIN seasons_new n USING (guild_id, season_start);

            CREATE TABLE season_teams_new (
                season_id     INTEGER NOT NULL REFERENCES seasons(id),
                captain       TEXT NOT NULL,
                team_name     TEXT NOT NULL,
                logo_url      TEXT,
                PRIMARY KEY (season_id, captain)
            );
            INSERT INTO season_teams_new
                SELECT n.id, t.captain, t.team_name, t.logo_url
                FROM season_teams t JOIN seasons_new n USING (guild_id, season_start);

            CREATE TABLE divisions_new (
                guild_id          INTEGER PRIMARY KEY,
                region            TEXT NOT NULL,
                game_mode         TEXT NOT NULL,
                current_season_id INTEGER REFERENCES seasons(id),
                created_at        TEXT NOT NULL,
                scold_channel_id  INTEGER DEFAULT NULL
            );
            INSERT INTO divisions_new
                SELECT d.guild_id, d.region, d.game_mode, n.id, d.created_at, d.scold_channel_id
                FROM divisions d LEFT JOIN seasons_new n USING (guild_id, season_start);

            DROP TABLE player_costs;  ALTER TABLE player_costs_new RENAME TO player_costs;
            DROP TABLE season_weeks;  ALTER TABLE season_weeks_new RENAME TO season_weeks;
            DROP TABLE season_teams;  ALTER TABLE season_teams_new RENAME TO season_teams;
            DROP TABLE divisions;     ALTER TABLE divisions_new    RENAME TO divisions;
            DROP TABLE seasons;       ALTER TABLE seasons_new      RENAME TO seasons;
            DROP TABLE temp.all_seasons;
            DROP TABLE temp.season_keys;
        """)

        # The website's tables, if the site has run here, were keyed the
        # same way; the migration after this adds their division.
        site_lost = 0
        if "season_start" in _cols(conn, "site_season_settings"):
            has_status = "status" in _cols(conn, "site_season_settings")
            n = conn.execute("SELECT COUNT(*) FROM site_season_settings").fetchone()[0]
            run(f"""
                ALTER TABLE site_season_settings RENAME TO site_season_settings_old;
                CREATE TABLE site_season_settings (
                    season_id INTEGER PRIMARY KEY REFERENCES seasons(id), status TEXT);
                INSERT INTO site_season_settings
                    SELECT s.id, {"o.status" if has_status else "NULL"}
                    FROM site_season_settings_old o JOIN seasons s USING (guild_id, season_start);
                DROP TABLE site_season_settings_old
            """)
            site_lost += n - conn.execute("SELECT COUNT(*) FROM site_season_settings").fetchone()[0]
        if "season_start" in _cols(conn, "site_signups"):
            n = conn.execute("SELECT COUNT(*) FROM site_signups").fetchone()[0]
            run("""
                ALTER TABLE site_signups RENAME TO site_signups_old;
                CREATE TABLE site_signups (
                    season_id INTEGER NOT NULL REFERENCES seasons(id), account_id INTEGER NOT NULL,
                    mmr INTEGER, positions TEXT NOT NULL, captain TEXT NOT NULL, discord TEXT,
                    statement TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (season_id, account_id));
                INSERT INTO site_signups
                    SELECT s.id, o.account_id, o.mmr, o.positions, o.captain, o.discord, o.statement,
                           o.created_at, o.updated_at
                    FROM site_signups_old o JOIN seasons s USING (guild_id, season_start);
                DROP TABLE site_signups_old
            """)
            site_lost += n - conn.execute("SELECT COUNT(*) FROM site_signups").fetchone()[0]

        after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
        orphans = conn.execute("SELECT COUNT(*) FROM divisions WHERE current_season_id IS NULL").fetchone()[0]
        if after != before or orphans or site_lost:
            raise RuntimeError(f"season id migration lost rows: before={before} after={after} "
                               f"divisions without a season={orphans} website rows={site_lost}")
        conn.execute("PRAGMA user_version = 1")
        conn.execute("COMMIT")
        seasons = conn.execute("SELECT id, season_start, label FROM seasons ORDER BY season_start").fetchall()
        logger.info("Migration 1: seasons keyed by league id: %s; rows %s", [tuple(r) for r in seasons], after)
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _slugify(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "division"


def _migrate_to_divisions(conn: sqlite3.Connection) -> None:
    """Migration 2: divisions become their own records instead of one per
    Discord server. Each existing (per-server) division becomes a division
    with an id, name and slug; its server becomes guild_settings.
    Seasons stop belonging to a server (season_divisions says which
    divisions play in them); costs, weeks and teams gain division_id;
    matches.guild_id becomes matches.division_id; the per-server player
    roster and exclusion lists become per-division; the website's season
    status and signups gain division_id, and its division names move onto
    divisions.

    Games stored under a server with no division (server 0: games imported
    one at a time) join the division of the season whose league they were
    played in, when that season has exactly one division; any others keep
    no division."""
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        counted = [t for t in ("player_costs", "season_weeks", "season_teams", "matches", "guild_roster",
                               "guild_exclusions", "site_season_settings", "site_signups") if t in tables]
        conn.execute("BEGIN IMMEDIATE")
        before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counted}
        before["players"] = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        before_distinct = {
            "matches": conn.execute("SELECT COUNT(DISTINCT match_id) FROM matches").fetchone()[0],
            "players": conn.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT match_id, player_slot FROM players)").fetchone()[0],
        }

        # Divisions: names from the website's naming table if it has one,
        # else from region + mode.
        site_names = ({r["guild_id"]: r["name"] for r in conn.execute("SELECT * FROM site_divisions")}
                      if "site_divisions" in tables else {})
        old = [dict(r) for r in conn.execute("SELECT * FROM divisions ORDER BY created_at")]
        conn.execute("""
            CREATE TABLE divisions_new (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT NOT NULL,
                slug              TEXT NOT NULL UNIQUE,
                region            TEXT NOT NULL,
                game_mode         TEXT NOT NULL,
                guild_id          INTEGER,
                current_season_id INTEGER REFERENCES seasons(id),
                created_at        TEXT NOT NULL
            )""")
        conn.execute("CREATE TEMP TABLE guild_division (guild_id INTEGER PRIMARY KEY, division_id INTEGER)")
        used_slugs: set[str] = set()
        for d in old:
            mode = "Ability Draft" if d["game_mode"] == "ad" else "Captains Mode"
            region = {"us_west": "US West", "us_east": "US East"}.get(d["region"], "")
            name = site_names.get(d["guild_id"]) or " ".join(filter(None, [region, mode]))
            slug, n = _slugify(name), 2
            while slug in used_slugs:
                slug, n = f"{_slugify(name)}-{n}", n + 1
            used_slugs.add(slug)
            cur = conn.execute("""
                INSERT INTO divisions_new (name, slug, region, game_mode, guild_id, current_season_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (name, slug, d["region"], d["game_mode"], d["guild_id"], d["current_season_id"], d["created_at"]))
            conn.execute("INSERT INTO temp.guild_division VALUES (?, ?)", (d["guild_id"], cur.lastrowid))

        _run_script(conn, """
            CREATE TABLE guild_settings (
                guild_id            INTEGER PRIMARY KEY,
                default_division_id INTEGER REFERENCES divisions(id),
                scold_channel_id    INTEGER
            );
            INSERT INTO guild_settings (guild_id, default_division_id, scold_channel_id)
                SELECT d.guild_id, g.division_id, d.scold_channel_id
                FROM divisions d JOIN temp.guild_division g USING (guild_id);

            CREATE TEMP TABLE season_division AS
                SELECT s.id AS season_id, g.division_id
                FROM seasons s JOIN temp.guild_division g USING (guild_id);

            CREATE TABLE seasons_new (
                id            INTEGER PRIMARY KEY,
                season_start  TEXT NOT NULL,
                label         TEXT,
                created_at    TEXT NOT NULL
            );
            INSERT INTO seasons_new SELECT id, season_start, label, created_at FROM seasons;

            CREATE TABLE season_divisions (
                season_id     INTEGER NOT NULL REFERENCES seasons(id),
                division_id   INTEGER NOT NULL REFERENCES divisions(id),
                PRIMARY KEY (season_id, division_id)
            );
            INSERT INTO season_divisions SELECT season_id, division_id FROM temp.season_division;

            CREATE TABLE player_costs_new (
                season_id     INTEGER NOT NULL REFERENCES seasons(id),
                division_id   INTEGER NOT NULL REFERENCES divisions(id),
                account_id    INTEGER NOT NULL,
                cost          INTEGER NOT NULL,
                captain       TEXT,
                mmr           INTEGER,
                PRIMARY KEY (season_id, division_id, account_id)
            );
            INSERT INTO player_costs_new
                SELECT c.season_id, sd.division_id, c.account_id, c.cost, c.captain, c.mmr
                FROM player_costs c JOIN temp.season_division sd USING (season_id);

            CREATE TABLE season_weeks_new (
                season_id     INTEGER NOT NULL REFERENCES seasons(id),
                division_id   INTEGER NOT NULL REFERENCES divisions(id),
                week_number   INTEGER NOT NULL,
                start_ts      INTEGER NOT NULL,
                end_ts        INTEGER NOT NULL,
                PRIMARY KEY (season_id, division_id, week_number)
            );
            INSERT INTO season_weeks_new
                SELECT w.season_id, sd.division_id, w.week_number, w.start_ts, w.end_ts
                FROM season_weeks w JOIN temp.season_division sd USING (season_id);

            CREATE TABLE season_teams_new (
                season_id     INTEGER NOT NULL REFERENCES seasons(id),
                division_id   INTEGER NOT NULL REFERENCES divisions(id),
                captain       TEXT NOT NULL,
                team_name     TEXT NOT NULL,
                logo_url      TEXT,
                PRIMARY KEY (season_id, division_id, captain)
            );
            INSERT INTO season_teams_new
                SELECT t.season_id, sd.division_id, t.captain, t.team_name, t.logo_url
                FROM season_teams t JOIN temp.season_division sd USING (season_id);

            CREATE TABLE division_roster (
                division_id INTEGER NOT NULL REFERENCES divisions(id),
                account_id  INTEGER NOT NULL,
                added_at    TEXT NOT NULL,
                PRIMARY KEY (division_id, account_id)
            );
            INSERT INTO division_roster
                SELECT g.division_id, r.account_id, r.added_at
                FROM guild_roster r JOIN temp.guild_division g USING (guild_id);

            CREATE TABLE division_exclusions (
                division_id  INTEGER NOT NULL REFERENCES divisions(id),
                account_id   INTEGER NOT NULL,
                excluded_at  TEXT NOT NULL,
                PRIMARY KEY (division_id, account_id)
            );
            INSERT INTO division_exclusions
                SELECT g.division_id, e.account_id, e.excluded_at
                FROM guild_exclusions e JOIN temp.guild_division g USING (guild_id);

            DROP TABLE player_costs;      ALTER TABLE player_costs_new RENAME TO player_costs;
            DROP TABLE season_weeks;      ALTER TABLE season_weeks_new RENAME TO season_weeks;
            DROP TABLE season_teams;      ALTER TABLE season_teams_new RENAME TO season_teams;
            DROP TABLE guild_roster;
            DROP TABLE guild_exclusions;
            DROP TABLE divisions;         ALTER TABLE divisions_new    RENAME TO divisions;
            DROP TABLE seasons;           ALTER TABLE seasons_new      RENAME TO seasons
        """)

        _rebuild_matches_and_players(conn)

        # The website's per-season tables, if the site has run here.
        if "site_season_settings" in tables:
            _run_script(conn, """
                CREATE TABLE site_season_settings_new (
                    season_id   INTEGER NOT NULL REFERENCES seasons(id),
                    division_id INTEGER NOT NULL REFERENCES divisions(id),
                    status      TEXT,
                    PRIMARY KEY (season_id, division_id)
                );
                INSERT INTO site_season_settings_new
                    SELECT o.season_id, sd.division_id, o.status
                    FROM site_season_settings o JOIN temp.season_division sd USING (season_id);
                DROP TABLE site_season_settings;
                ALTER TABLE site_season_settings_new RENAME TO site_season_settings
            """)
        if "site_signups" in tables:
            _run_script(conn, """
                CREATE TABLE site_signups_new (
                    season_id     INTEGER NOT NULL REFERENCES seasons(id),
                    division_id   INTEGER NOT NULL REFERENCES divisions(id),
                    account_id    INTEGER NOT NULL,
                    mmr           INTEGER,
                    positions     TEXT NOT NULL,
                    captain       TEXT NOT NULL,
                    discord       TEXT,
                    statement     TEXT,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL,
                    PRIMARY KEY (season_id, division_id, account_id)
                );
                INSERT INTO site_signups_new
                    SELECT o.season_id, sd.division_id, o.account_id, o.mmr, o.positions, o.captain,
                           o.discord, o.statement, o.created_at, o.updated_at
                    FROM site_signups o JOIN temp.season_division sd USING (season_id);
                DROP TABLE site_signups;
                ALTER TABLE site_signups_new RENAME TO site_signups
            """)
        if "site_divisions" in tables:
            conn.execute("DROP TABLE site_divisions")  # names now live on divisions

        renamed = {"guild_roster": "division_roster", "guild_exclusions": "division_exclusions"}
        after = {t: conn.execute(f"SELECT COUNT(*) FROM {renamed.get(t, t)}").fetchone()[0] for t in counted}
        # matches and players shrink by exactly the duplicates the rebuild
        # merged; every other table must come through whole.
        expected = dict(before, matches=before_distinct["matches"], players=before_distinct["players"])
        after["players"] = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        if after != expected:
            raise RuntimeError(f"division migration lost rows: expected={expected} after={after}")
        unassigned = conn.execute(
            "SELECT match_id, league_id FROM matches WHERE division_id IS NULL").fetchall()
        conn.execute("DROP TABLE temp.guild_division")
        conn.execute("DROP TABLE temp.season_division")
        conn.execute("PRAGMA user_version = 2")
        conn.execute("COMMIT")
        divisions = conn.execute("SELECT id, name, slug, guild_id FROM divisions").fetchall()
        logger.info("Migration 2: divisions %s; rows %s; %d games with no division (league ids %s)",
                    [tuple(r) for r in divisions], after, len(unassigned),
                    sorted({r["league_id"] for r in unassigned}))
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _rebuild_matches_and_players(conn: sqlite3.Connection) -> None:
    """Part of migration 2. Rebuilds matches and players to their canonical
    definitions (MATCHES_TABLE / PLAYERS_TABLE). Older databases drifted:
    matches was keyed (match_id, guild_id), so one game could be stored
    under two servers; players had no column types, no ids and no unique
    seat, so a re-fetched game could gain a second row for the same seat.

    matches: one row per game. guild_id becomes division_id — the guild's
    division; for a game under a server with no division, the division of
    its league's season when that season has exactly one. Where a game was
    stored twice, the row with a division wins.
    players: one row per (match, seat), preferring the row with a real
    account over an anonymous (account 0) one. Columns the code doesn't
    define are carried over as they are."""
    _run_script(conn, """
        CREATE TEMP TABLE match_division AS
            SELECT m.match_id,
                   COALESCE(
                       MIN(g.division_id),
                       (SELECT MIN(sd.division_id) FROM temp.season_division sd
                        WHERE sd.season_id = MIN(m.league_id)
                        HAVING COUNT(*) = 1)
                   ) AS division_id
            FROM matches m LEFT JOIN temp.guild_division g ON g.guild_id = m.guild_id
            GROUP BY m.match_id
    """)
    match_cols = [c for c in _cols(conn, "matches") if c not in ("guild_id",)]
    conn.execute("ALTER TABLE matches RENAME TO matches_old")
    conn.execute("DROP INDEX IF EXISTS idx_matches_guild")
    conn.execute("DROP INDEX IF EXISTS idx_matches_start")
    conn.execute(MATCHES_TABLE)
    keep = ", ".join(c for c in match_cols if c in _cols(conn, "matches"))
    conn.execute(f"""
        INSERT INTO matches (division_id, {keep})
        SELECT md.division_id, {", ".join("o." + c for c in keep.split(", "))}
        FROM matches_old o JOIN temp.match_division md USING (match_id)
        WHERE o.rowid = (
            SELECT o2.rowid FROM matches_old o2
            LEFT JOIN temp.guild_division g2 ON g2.guild_id = o2.guild_id
            WHERE o2.match_id = o.match_id
            ORDER BY (g2.division_id IS NULL), o2.rowid
            LIMIT 1)
    """)
    conn.execute("DROP TABLE matches_old")
    conn.execute("DROP TABLE temp.match_division")

    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(players)")]
    types = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(players)")}
    conn.execute("ALTER TABLE players RENAME TO players_old")
    for idx in ("idx_players_match", "idx_players_account"):
        conn.execute(f"DROP INDEX IF EXISTS {idx}")
    conn.execute(PLAYERS_TABLE)
    new_cols = _cols(conn, "players")
    extras = [c for c in old_cols if c not in new_cols]
    for c in extras:
        conn.execute(f'ALTER TABLE players ADD COLUMN "{c}" {types[c] or ""}')
        logger.info("players: keeping column %s, which the code doesn't define", c)
    copy = [c for c in old_cols if c != "id"]
    col_list = ", ".join(f'"{c}"' for c in copy)
    conn.execute(f"""
        INSERT INTO players ({col_list})
        SELECT {col_list} FROM players_old o
        WHERE o.rowid = (
            SELECT o2.rowid FROM players_old o2
            WHERE o2.match_id = o.match_id AND o2.player_slot = o.player_slot
            ORDER BY (o2.account_id = 0), o2.rowid
            LIMIT 1)
    """)
    conn.execute("DROP TABLE players_old")
    _run_script(conn, """
        CREATE INDEX IF NOT EXISTS idx_players_match   ON players(match_id);
        CREATE INDEX IF NOT EXISTS idx_players_account ON players(account_id);
        CREATE INDEX IF NOT EXISTS idx_matches_start   ON matches(start_time);
        CREATE INDEX IF NOT EXISTS idx_matches_division ON matches(division_id);
        CREATE INDEX IF NOT EXISTS idx_matches_league  ON matches(league_id)
    """)


def _add_match_drafts(conn: sqlite3.Connection) -> None:
    """Migration 3: a table for Ability Draft picks (match_drafts)."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(DRAFTS_TABLE)
        conn.execute("PRAGMA user_version = 3")
        conn.execute("COMMIT")
        logger.info("Migration 3: added match_drafts")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# Structural migrations, in order; the database's PRAGMA user_version is the
# number of the last one applied. Append new ones; never reorder or edit.
_MIGRATIONS = [_migrate_seasons_to_ids, _migrate_to_divisions, _add_match_drafts]


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

def get_division(division_id: int) -> dict | None:
    """One division, or None."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM divisions WHERE id = ?", (division_id,)).fetchone()
    return dict(row) if row else None


def get_guild_divisions(guild_id: int) -> list[dict]:
    """The divisions whose commands run in this Discord server, oldest first."""
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM divisions WHERE guild_id = ? ORDER BY id", (guild_id,)).fetchall()
    return [dict(r) for r in rows]


def get_guild_division(guild_id: int, name: str | None = None) -> dict | None:
    """The division a command in this server is about: the one named (by
    name or slug, any case), else the server's default division, else its
    only division. None if that doesn't pick exactly one."""
    divisions = get_guild_divisions(guild_id)
    if name:
        wanted = name.strip().lower()
        return next((d for d in divisions if wanted in (d["name"].lower(), d["slug"])), None)
    if len(divisions) == 1:
        return divisions[0]
    default = get_guild_settings(guild_id).get("default_division_id")
    return next((d for d in divisions if d["id"] == default), None)


def get_division_by_slug(slug: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM divisions WHERE slug = ?", (slug,)).fetchone()
    return dict(row) if row else None


def create_division(name: str, slug: str, region: str, game_mode: str,
                    guild_id: int | None, current_season_id: int | None = None) -> int:
    """Create a division; returns its id."""
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO divisions (name, slug, region, game_mode, guild_id, current_season_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (name, slug, region, game_mode, guild_id, current_season_id,
              datetime.now(timezone.utc).isoformat()))
        return cur.lastrowid


def update_division(division_id: int, **fields) -> None:
    """Change any of: name, slug, region, game_mode, guild_id, current_season_id."""
    allowed = {"name", "slug", "region", "game_mode", "guild_id", "current_season_id"}
    assert set(fields) <= allowed, set(fields) - allowed
    if fields:
        with _conn() as conn:
            conn.execute(f"UPDATE divisions SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                         [*fields.values(), division_id])


def get_all_divisions() -> list[dict]:
    """Get all configured divisions."""
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM divisions").fetchall()
    return [dict(r) for r in rows]


def get_guild_settings(guild_id: int) -> dict:
    """{default_division_id, scold_channel_id} for a Discord server ({} if none)."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
    return dict(row) if row else {}


def set_guild_settings(guild_id: int, **fields) -> None:
    """Set any of: default_division_id, scold_channel_id."""
    assert set(fields) <= {"default_division_id", "scold_channel_id"}, fields
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))
        if fields:
            conn.execute(f"UPDATE guild_settings SET {', '.join(f'{k} = ?' for k in fields)} WHERE guild_id = ?",
                         [*fields.values(), guild_id])


def get_scold_channel(guild_id: int) -> int | None:
    """Get the scold channel ID for a Discord server, or None if not set."""
    return get_guild_settings(guild_id).get("scold_channel_id") or None


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def upsert_match(match: dict):
    """Insert a match row, skip if match_id already exists."""
    with _conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO matches
                (match_id, division_id, league_id, start_time, duration, game_mode, cluster,
                 radiant_win, radiant_score, dire_score, fetched_at, replay_salt)
            VALUES (:match_id, :division_id, :league_id, :start_time, :duration, :game_mode,
                    :cluster, :radiant_win, :radiant_score, :dire_score, :fetched_at,
                    :replay_salt)
        """, match)


def upsert_players(players: list[dict]):
    """Insert player rows, one per (match, seat). Re-fetching a match updates
    its rows in place — e.g. an account that was private (account_id 0)
    the first time gets its real id."""
    with _conn() as conn:
        conn.executemany("""
            INSERT INTO players
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
            ON CONFLICT(match_id, player_slot) DO UPDATE SET
                account_id = excluded.account_id,
                name = excluded.name,
                hero_id = excluded.hero_id,
                team_side = excluded.team_side,
                role_position = excluded.role_position,
                kills = excluded.kills,
                deaths = excluded.deaths,
                assists = excluded.assists,
                gpm = excluded.gpm,
                xpm = excluded.xpm,
                last_hits = excluded.last_hits,
                denies = excluded.denies,
                hero_damage = excluded.hero_damage,
                hero_healing = excluded.hero_healing,
                gold_spent = excluded.gold_spent,
                duration = excluded.duration,
                won = excluded.won,
                obs_placed = excluded.obs_placed,
                sen_placed = excluded.sen_placed,
                observer_kills = excluded.observer_kills,
                sentry_kills = excluded.sentry_kills,
                tower_kills = excluded.tower_kills,
                roshans_killed = excluded.roshans_killed,
                firstblood_claimed = excluded.firstblood_claimed,
                teamfight_participation = excluded.teamfight_participation,
                stuns = excluded.stuns,
                camps_stacked = excluded.camps_stacked,
                rune_pickups = excluded.rune_pickups,
                tormentor_kills = excluded.tormentor_kills,
                watcher_captures = excluded.watcher_captures,
                team_first_tormentor_time = excluded.team_first_tormentor_time,
                defensive_item_uses = excluded.defensive_item_uses,
                building_damage = excluded.building_damage
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


def get_random_quote(division_id: int) -> dict | None:
    """Return a random chat message from the database, filtering out boring ones."""
    with _conn() as conn:
        # Fetch a batch of random candidates and filter in Python
        # (SQLite doesn't have good regex support for normalization)
        rows = conn.execute("""
            SELECT cm.message, cm.player_name, cm.time, cm.match_id
            FROM chat_messages cm
            JOIN matches m ON cm.match_id = m.match_id
            WHERE m.division_id = ?
              AND LENGTH(TRIM(cm.message)) > 2
            ORDER BY RANDOM()
            LIMIT 100
        """, (division_id,)).fetchall()

    for row in rows:
        normalized = _normalize_message(row["message"])
        if normalized and normalized not in BORING_MESSAGES and len(normalized) > 2:
            return dict(row)

    return None


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MatchScope:
    """Which matches a query covers: one division's games in one season (by
    league id — every match records the league it was played in, and a
    season's id is its league id), or with no league given, in any season
    the division plays in; optionally only a time window, e.g. a week.
    Games outside every season (lobbies stored in the past) never count.
    Every season- or week-scoped query builds its WHERE clause from one of
    these, so they all agree on what "this season" means."""
    division_id: int
    league_id: int | None = None
    start: int | None = None   # unix seconds, inclusive
    end: int | None = None     # unix seconds, inclusive

    def where(self, alias: str = "m") -> tuple[str, list]:
        """(SQL condition, params) over the matches table aliased `alias`."""
        # A game somebody took out stays in the table — deleting it would only
        # have it fetched again — and is left out of every scoped query from
        # the one place they all build their WHERE from.
        clauses, params = [f"{alias}.division_id = ?", f"{alias}.excluded = 0"], [self.division_id]
        if self.league_id is not None:
            clauses.append(f"{alias}.league_id = ?"); params.append(self.league_id)
        else:
            clauses.append(f"{alias}.league_id IN "
                           f"(SELECT season_id FROM season_divisions WHERE division_id = ?)")
            params.append(self.division_id)
        if self.start is not None:
            clauses.append(f"{alias}.start_time >= ?"); params.append(self.start)
        if self.end is not None:
            clauses.append(f"{alias}.start_time <= ?"); params.append(self.end)
        return " AND ".join(clauses), params


def season_scope(season_id: int, division_id: int) -> MatchScope:
    """Every match a division played in a season (its league)."""
    return MatchScope(division_id, league_id=season_id)


def season_week_scope(season_id: int, division_id: int, week_number: int) -> MatchScope:
    """A division's matches of a season within one of its weeks."""
    start, end = get_season_week_range(season_id, division_id, week_number)
    return MatchScope(division_id, league_id=season_id, start=start, end=end)


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
    """Return (start_unix, end_unix) for a specific season week, by calendar.

    week_number=1 means the calendar week (Mon–Sun, UTC) containing the
    season's start date. season_start_date is a 'YYYY-MM-DD' string.
    """
    season_start = datetime.strptime(season_start_date, "%Y-%m-%d")
    season_start = season_start.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    season_monday = season_start - timedelta(days=season_start.weekday())
    target_monday = season_monday + timedelta(weeks=week_number - 1)
    target_sunday = target_monday + timedelta(days=7) - timedelta(seconds=1)
    return int(target_monday.timestamp()), int(target_sunday.timestamp())


# ---------------------------------------------------------------------------
# Seasons
#
# A season is a row in `seasons`, identified by seasons.id — the season's
# Dota league id (RD2L Season 39 = 20056). Everything filed under a season —
# costs, weeks, teams, a division's current season, the website's signups —
# refers to that id. The start date is an ordinary field.
# ---------------------------------------------------------------------------

def get_season(season_id: int) -> dict | None:
    """One season: {id (its Dota league id), season_start, label, created_at}."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM seasons WHERE id = ?", (season_id,)).fetchone()
    return dict(row) if row else None


def get_season_by_start(division_id: int, season_start: str) -> dict | None:
    """The division's season starting on this date, if there is one."""
    with _conn() as conn:
        row = conn.execute("""
            SELECT s.* FROM seasons s JOIN season_divisions sd ON sd.season_id = s.id
            WHERE sd.division_id = ? AND s.season_start = ?
        """, (division_id, season_start)).fetchone()
    return dict(row) if row else None


def get_seasons(division_id: int) -> list[dict]:
    """Every season this division has played in, newest first."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT s.* FROM seasons s JOIN season_divisions sd ON sd.season_id = s.id
            WHERE sd.division_id = ? ORDER BY s.season_start DESC
        """, (division_id,)).fetchall()
    return [dict(r) for r in rows]


def get_season_divisions(season_id: int) -> list[dict]:
    """The divisions playing in a season."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT d.* FROM divisions d JOIN season_divisions sd ON sd.division_id = d.id
            WHERE sd.season_id = ? ORDER BY d.id
        """, (season_id,)).fetchall()
    return [dict(r) for r in rows]


def add_season_division(season_id: int, division_id: int) -> None:
    """Record that a division plays in a season."""
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO season_divisions (season_id, division_id) VALUES (?, ?)",
                     (season_id, division_id))


def get_all_seasons() -> list[dict]:
    """Every season of every guild, newest first."""
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM seasons ORDER BY season_start DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


def upsert_season(season_id: int, season_start: str, label: str | None = None) -> None:
    """Create a season — season_id is its Dota league id — or update its
    start date if it exists. label=None keeps any existing label. Which
    divisions play in it is add_season_division."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO seasons (id, season_start, label, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                season_start = excluded.season_start,
                label        = COALESCE(excluded.label, seasons.label)
        """, (season_id, season_start, label, datetime.now(timezone.utc).isoformat()))


def update_season(season_id: int, *, label: str | None = None, season_start: str | None = None) -> None:
    """Rename a season and/or move its start date. Nothing else changes:
    costs, weeks, teams and signups point at the season's id. Explicit week
    boundaries keep their own dates. Raises ValueError for a malformed date."""
    with _conn() as conn:
        if season_start is not None:
            datetime.strptime(season_start, "%Y-%m-%d")  # ValueError on a bad date
            conn.execute("UPDATE seasons SET season_start = ? WHERE id = ?", (season_start, season_id))
        if label is not None:
            conn.execute("UPDATE seasons SET label = ? WHERE id = ?", (label, season_id))


def season_label(season: dict) -> str:
    """Display name for a season row: its label if we have one, else the date."""
    lbl = season.get("label")
    return lbl if lbl else f"Season starting {season['season_start']}"


def get_current_season(division_id: int) -> dict | None:
    """The division's current season — what commands use when no season is given."""
    with _conn() as conn:
        row = conn.execute("""
            SELECT s.* FROM divisions d JOIN seasons s ON s.id = d.current_season_id
            WHERE d.id = ?
        """, (division_id,)).fetchone()
    return dict(row) if row else None


def get_active_draft_season() -> dict | None:
    """The newest current season, of any division, that has draft data, as
    the season's row plus the division's id under "division_id" — what the
    stream-card pages show, since they take no season parameter."""
    with _conn() as conn:
        row = conn.execute("""
            SELECT s.*, d.id AS division_id
            FROM divisions d JOIN seasons s ON s.id = d.current_season_id
            WHERE EXISTS (SELECT 1 FROM player_costs pc
                          WHERE pc.season_id = s.id AND pc.division_id = d.id)
            ORDER BY s.season_start DESC
            LIMIT 1
        """).fetchone()
    return dict(row) if row else None


def set_current_season(division_id: int, season_id: int) -> None:
    """Make a season the division's current one (and record that it plays in it)."""
    add_season_division(season_id, division_id)
    with _conn() as conn:
        conn.execute("UPDATE divisions SET current_season_id = ? WHERE id = ?", (season_id, division_id))


# ---------------------------------------------------------------------------
# Season weeks
# ---------------------------------------------------------------------------

def _season_weeks_defined(season_id: int, division_id: int) -> bool:
    """True once any explicit season_weeks rows exist for this season."""
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM season_weeks WHERE season_id = ? AND division_id = ? LIMIT 1", (season_id, division_id)).fetchone()
    return row is not None


def get_season_week_range(season_id: int, division_id: int, week_number: int) -> tuple[int, int]:
    """Canonical (start_unix, end_unix) for a season week. Prefers an
    explicit season_weeks row (so byes/reschedules can override a single
    week); falls back to the calendar formula when the season has no
    explicit weeks defined (e.g. any season before this feature existed)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT start_ts, end_ts FROM season_weeks WHERE season_id = ? AND division_id = ? AND week_number = ?",
            (season_id, division_id, week_number),
        ).fetchone()
    if row:
        return row["start_ts"], row["end_ts"]
    return _season_week_start(week_number, get_season(season_id)["season_start"])


def get_current_season_week(season_id: int, division_id: int) -> int:
    """Canonical 'current week' resolver, used wherever week=None needs to
    mean something concrete (leaderboard/player/matches default, /summary).

    With explicit weeks defined: the week whose range contains right now, or
    the closest one (last if we're past every defined week, first if we're
    before all of them). Falls back to the legacy get_latest_season_week
    calendar logic when the season has no explicit weeks."""
    if not _season_weeks_defined(season_id, division_id):
        return get_latest_season_week(season_id, division_id) or 1

    now_ts = int(datetime.now(timezone.utc).timestamp())
    with _conn() as conn:
        rows = conn.execute(
            "SELECT week_number, start_ts, end_ts FROM season_weeks WHERE season_id = ? AND division_id = ? ORDER BY start_ts",
            (season_id, division_id),
        ).fetchall()

    current = rows[0]["week_number"]
    for r in rows:
        if r["start_ts"] <= now_ts:
            current = r["week_number"]
        else:
            break
    return current


def get_season_weeks(season_id: int, division_id: int) -> list[dict]:
    """All explicitly-defined weeks for this season, ordered by week_number."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT week_number, start_ts, end_ts FROM season_weeks WHERE season_id = ? AND division_id = ? ORDER BY week_number",
            (season_id, division_id),
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_season_week(season_id: int, division_id: int, week_number: int, start_ts: int, end_ts: int) -> None:
    """Insert/update one explicit week boundary."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO season_weeks (season_id, division_id, week_number, start_ts, end_ts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(season_id, division_id, week_number) DO UPDATE SET
                start_ts = excluded.start_ts,
                end_ts   = excluded.end_ts
        """, (season_id, division_id, week_number, start_ts, end_ts))


def delete_season_week(season_id: int, division_id: int, week_number: int) -> bool:
    """Remove one week's explicit definition. Returns True if a row was removed."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM season_weeks WHERE season_id = ? AND division_id = ? AND week_number = ?",
                           (season_id, division_id, week_number))
    return cur.rowcount > 0


def generate_season_weeks(season_id: int, division_id: int, num_weeks: int, first_week: int = 1) -> None:
    """Bulk-create consecutive 7-day week blocks starting at `first_week`.

    first_week=1 (default) numbers them 1..num_weeks, with week 1 being the
    calendar week containing the season's start date. first_week=0 numbers
    them 0..num_weeks-1 for seasons that open with a scrim/"week 0" that
    doesn't count toward seeding — week 0 is then the week containing the
    start date and week 1 is the week after.

    Individual weeks can be adjusted afterward via upsert_season_week /
    delete_season_week (e.g. a bye) without affecting any other week."""
    start_date = get_season(season_id)["season_start"]
    with _conn() as conn:
        conn.executemany("""
            INSERT INTO season_weeks (season_id, division_id, week_number, start_ts, end_ts)
            VALUES (:season_id, :division_id, :week_number, :start_ts, :end_ts)
            ON CONFLICT(season_id, division_id, week_number) DO UPDATE SET
                start_ts = excluded.start_ts,
                end_ts   = excluded.end_ts
        """, [
            {
                "season_id": season_id,
                "division_id": division_id,
                "week_number": n,
                # Offset so `first_week` maps onto the calendar week containing
                # the start date, whatever we've chosen to number that week.
                "start_ts": _season_week_start(n - first_week + 1, start_date)[0],
                "end_ts": _season_week_start(n - first_week + 1, start_date)[1],
            }
            for n in range(first_week, first_week + num_weeks)
        ])


# ---------------------------------------------------------------------------
# Season teams (team identity: real name + logo, per season)
# ---------------------------------------------------------------------------

def get_season_teams(season_id: int, division_id: int) -> dict[str, dict]:
    """A division's teams for a season as {lowercased_captain: {team_name, logo_url}}."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT captain, team_name, logo_url FROM season_teams WHERE season_id = ? AND division_id = ?", (season_id, division_id),
        ).fetchall()
    return {r["captain"]: {"team_name": r["team_name"], "logo_url": r["logo_url"]} for r in rows}


def upsert_season_teams(season_id: int, division_id: int, teams: list[dict]) -> int:
    """Insert/update team identities. Each dict needs captain + team_name;
    logo_url optional. Captain is stored lowercased so lookups are
    case-insensitive against whatever player_costs holds."""
    if not teams:
        return 0
    with _conn() as conn:
        conn.executemany("""
            INSERT INTO season_teams (season_id, division_id, captain, team_name, logo_url)
            VALUES (:season_id, :division_id, :captain, :team_name, :logo_url)
            ON CONFLICT(season_id, division_id, captain) DO UPDATE SET
                team_name = excluded.team_name,
                logo_url  = COALESCE(excluded.logo_url, season_teams.logo_url)
        """, [
            {
                "season_id": season_id,
                "division_id": division_id,
                "captain": (t["captain"] or "").strip().lower(),
                "team_name": t["team_name"],
                "logo_url": t.get("logo_url"),
            }
            for t in teams if t.get("captain") and t.get("team_name")
        ])
    return len(teams)



def _apply_override_nicknames(results: list[dict]) -> list[dict]:
    """Give each stat row the player's display name — the one /player shows
    (see get_display_names) — instead of whichever in-game name the
    aggregate query happened to pick from their match rows."""
    names = get_display_names([r["account_id"] for r in results if r.get("account_id") is not None])
    for r in results:
        if r.get("account_id") in names:
            r["name"] = names[r["account_id"]]
    return results


def get_display_names(account_ids: list[int]) -> dict[int, str]:
    """The name to show for each player, the way /player, /lookup and
    /api/player show it: the /set_player nickname, else the rating-cache
    name, else the name on their most recent league match. Accounts with
    none of those are left out. The website uses this so it always agrees
    with the bot."""
    ids = list(set(account_ids))
    out: dict[int, str] = {}
    with _conn() as conn:
        for i in range(0, len(ids), 500):  # stay under SQLite's bound-parameter limit
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            # Lowest priority first; each later source overwrites.
            for r in conn.execute(f"""
                SELECT p.account_id, p.name FROM players p
                JOIN (SELECT account_id, MAX(match_id) AS m FROM players
                      WHERE account_id IN ({marks}) GROUP BY account_id) last
                  ON last.account_id = p.account_id AND last.m = p.match_id""", chunk):
                if (r["name"] or "").strip():
                    out[r["account_id"]] = r["name"].strip()
            for r in conn.execute(
                f"SELECT account_id, name FROM player_ratings_cache WHERE account_id IN ({marks})", chunk):
                if (r["name"] or "").strip():
                    out[r["account_id"]] = r["name"].strip()
            for r in conn.execute(
                f"SELECT account_id, nickname FROM skill_overrides WHERE account_id IN ({marks})", chunk):
                if (r["nickname"] or "").strip():
                    out[r["account_id"]] = r["nickname"].strip()
    return out


def _player_stats(scope: MatchScope) -> list[dict]:
    """Aggregated per-player stats over the matches in scope: raw totals and
    per-game averages, plus kda, fantasy_points, and fantasy diff vs
    teammates (diff) and vs the lobby (diff_vs_lobby). Draft fields (cost,
    value, attendance) and display names are added by the callers."""
    from fantasy import calculate_fantasy_points
    where, params = scope.where("m")
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
            WHERE {where}
              AND p.account_id > 0   -- 0 is every private-profile player at once
            GROUP BY p.account_id
            ORDER BY gpm DESC
        """.format(where=where), params).fetchall()

    diffs = _compute_player_diffs(scope)
    diffs_vs_lobby = _compute_player_diffs_vs_lobby(scope)
    roles = _usual_roles(scope)
    results = []
    for r in rows:
        d = dict(r)
        d["role_position"] = roles.get(d["account_id"])
        d["kda"] = round((d["total_kills"] + d["total_assists"]) / max(d["total_deaths"], 1), 2)
        d["fantasy_points"] = calculate_fantasy_points(d)
        d["diff"] = diffs.get(d["account_id"], 0.0)
        d["diff_vs_lobby"] = diffs_vs_lobby.get(d["account_id"], 0.0)
        results.append(d)
    return results


def _usual_roles(scope: MatchScope) -> dict[int, int]:
    """Each player's most-played position in scope (ties: the lower number).
    The aggregate query can't pick this itself: a non-aggregated column in a
    GROUP BY comes from an arbitrary row."""
    where, params = scope.where("m")
    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT p.account_id, p.role_position, COUNT(*) AS n
            FROM players p JOIN matches m ON p.match_id = m.match_id
            WHERE {where} AND p.role_position IS NOT NULL
            GROUP BY p.account_id, p.role_position
            ORDER BY p.account_id, n DESC, p.role_position
        """, params).fetchall()
    roles: dict[int, int] = {}
    for r in rows:
        roles.setdefault(r["account_id"], r["role_position"])
    return roles


def get_latest_week_stats(division_id: int, week_offset: int = 0) -> list[dict]:
    """Aggregated per-player stats for a calendar week (0 = this week,
    1 = last week, ...), across every league the division plays in."""
    start, end = _week_start(week_offset)
    return _apply_override_nicknames(_player_stats(MatchScope(division_id, start=start, end=end)))


def get_stats_for_season_week(season_id: int, division_id: int, week_number: int) -> list[dict]:
    """Return aggregated per-player stats for a specific season week.

    Resolves through get_season_week_range so an explicitly-defined week
    (including a week 0, or a bye/reschedule override) wins over the plain
    calendar formula — the formula assumes week 1 is the first week, so it
    puts week 0 a week BEFORE the season and finds nothing.
    """
    scope = season_week_scope(season_id, division_id, week_number)
    results = _player_stats(scope)
    costs = get_player_costs(season_id, division_id)
    results = _attach_costs(results, costs)
    _compute_value(results)
    attendance_map = _compute_attendance(scope, costs,
                                         get_captain_account_ids(season_id, division_id))
    for s in results:
        s["attendance"] = attendance_map.get(s["account_id"])
    return _apply_override_nicknames(results)


def get_all_time_stats(division_id: int, season_id: int | None = None,
                       cross_season: bool = False) -> list[dict]:
    """Cached; see _derived. The work is in _all_time_stats below."""
    return _derived(("all_time_stats", division_id, season_id, cross_season),
                    lambda: _all_time_stats(division_id, season_id, cross_season))


def _all_time_stats(division_id: int, season_id: int | None = None,
                    cross_season: bool = False) -> list[dict]:
    """Return aggregated per-player stats for a division.

    With season_id: that season's matches (its league). Without, or with
    cross_season=True: every match the division has ever recorded, all
    seasons — while cost-derived fields (cost/captain/value/attendance)
    still come from season_id, since those describe a specific draft.
    """
    results = _player_stats(season_scope(season_id, division_id) if season_id and not cross_season
                            else MatchScope(division_id))
    if season_id:
        costs = get_player_costs(season_id, division_id)
        results = _attach_costs(results, costs)
        _compute_value(results)
        # Attendance is "what share of your team's season did you show up
        # for", so it always uses the season's matches even when the stat
        # rows above were widened to every season by cross_season.
        attendance_map = _compute_attendance(
            season_scope(season_id, division_id), costs,
            get_captain_account_ids(season_id, division_id))
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
            mirrored["alt_of"] = main_id   # a copy for lookups; ranked() leaves it out
            results.append(mirrored)

    return _apply_override_nicknames(results)


def qualified(stats: list[dict], min_games: int | None = None) -> tuple[list[dict], int, int]:
    """Players with enough games to rank, so stand-ins don't top a
    leaderboard. The default bar is half as many games as the most active
    player (rounded down); min_games sets it outright instead, and 0 ranks
    everyone. Returns (rows, threshold, max_games)."""
    max_games = max((s.get("games_played") or 0 for s in stats), default=0)
    threshold = max_games // 2 if min_games is None else max(0, min_games)
    return [s for s in stats if (s.get("games_played") or 0) >= threshold], threshold, max_games


def ranked(stats: list[dict]) -> list[dict]:
    """Stat rows with one row per person, for anything that ranks or pools
    players (leaderboards, percentile pools). Drops the copies
    get_all_time_stats adds under alt accounts, which exist so that looking
    a player up by their alt finds their stats."""
    return [s for s in stats if not s.get("alt_of")]


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


def _compute_player_diffs(scope: MatchScope) -> dict[int, float]:
    """For every player with matches in scope, return the average
    per-match fantasy-points diff vs. the 4 same-side teammates.

    Returns {account_id: avg_diff}. Players with no qualifying matches are absent.
    """
    where, params = scope.where("m")
    sql = f"""
        SELECT p.*, m.duration AS match_duration
        FROM players p
        JOIN matches m ON p.match_id = m.match_id
        WHERE {where}
    """
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


def _compute_player_diffs_vs_lobby(scope: MatchScope) -> dict[int, float]:
    """For every player with matches in scope, return the average
    per-match fantasy-points diff vs. the OTHER 9 players in the match —
    both teammates AND opponents. Opponent-strength-aware in a way the
    teammates-only diff isn't: a great performance against a weak enemy
    team gets discounted; standing out against a stacked enemy gets bonus.

    Used only by /leaderboard Value (via _compute_value). The teammates-only
    diff powers the fantasy adjustment path — this doesn't replace it.
    """
    where, params = scope.where("m")
    sql = f"""
        SELECT p.*, m.duration AS match_duration
        FROM players p
        JOIN matches m ON p.match_id = m.match_id
        WHERE {where}
    """
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


def get_division_player_account_ids(division_id: int) -> list[int]:
    """All distinct account_ids who've played in this division's matches."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT p.account_id
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.division_id = ? AND p.account_id > 0
        """, (division_id,)).fetchall()
    return [r[0] for r in rows]


_CACHE_COLUMNS = [
    "name", "internal_rating", "raw_windrun", "mmr_estimate", "rank_tier",
    "leaderboard_rank", "ad_last_year", "ad_all_time", "ranked_last_year",
    "ranked_all_time", "windrun_games", "explanation", "avatar_url", "fh_unavailable",
]


def upsert_rating_cache_row(row: dict, authoritative: tuple[str, ...] = ()) -> None:
    """Insert/update one row in player_ratings_cache.

    A field left out, or set to None, normally keeps whatever was there: a
    windrun-only refresh shouldn't wipe the OpenDota half of someone's row when
    OpenDota is down. But that also made "we now know this is unknown"
    unsayable — a private-history player kept a stale game count that the
    rating had stopped using, so rebuilding the rating from the cache gave a
    different answer than the fetch had. `authoritative` names the fields the
    caller actually reached the source for, which are written as given, None
    included."""
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
        "ranked_all_time":  row.get("ranked_all_time"),
        "windrun_games":    row.get("windrun_games"),
        "explanation":      row.get("explanation"),
        "avatar_url":       row.get("avatar_url"),
        "fh_unavailable":   1 if row.get("fh_unavailable") else 0 if "fh_unavailable" in row else None,
        "updated_at":       datetime.now(timezone.utc).isoformat(),
    }
    cols = ", ".join(_CACHE_COLUMNS)
    values = ", ".join(f":{c}" for c in _CACHE_COLUMNS)
    updates = ",\n                ".join(
        f"{c} = excluded.{c}" if c in authoritative
        else f"{c} = COALESCE(excluded.{c}, player_ratings_cache.{c})"
        for c in _CACHE_COLUMNS)
    with _conn() as conn:
        conn.execute(f"""
            INSERT INTO player_ratings_cache (account_id, {cols}, updated_at)
            VALUES (:account_id, {values}, :updated_at)
            ON CONFLICT(account_id) DO UPDATE SET
                {updates},
                updated_at = excluded.updated_at
        """, payload)


def set_group_rating(account_ids: list[int], rating: int | None, explanation: str | None) -> None:
    """Store one group's shared rating on each of its accounts (see
    rating_group.py). Pass rating=None to clear it, for an account that no
    longer shares a group.

    Rows that already hold this rating are left alone: the recompute runs on
    every boot and every refresh, and touching updated_at when nothing moved
    would break the conditional requests /api/player serves off it.
    """
    if not account_ids:
        return
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        placeholders = ",".join("?" * len(account_ids))
        stale = [r["account_id"] for r in conn.execute(
            f"SELECT account_id, group_rating FROM player_ratings_cache "
            f"WHERE account_id IN ({placeholders})", tuple(account_ids))
            if r["group_rating"] != rating]
        if not stale:
            return
        conn.executemany(
            "UPDATE player_ratings_cache SET group_rating = ?, group_explanation = ?, "
            "updated_at = ? WHERE account_id = ?",
            [(rating, explanation, now, aid) for aid in stale])


def alt_links() -> dict[int, int]:
    """{second account: main account} for every linked pair."""
    with _conn() as conn:
        return {r["account_id"]: r["alt_account_for"] for r in conn.execute(
            "SELECT account_id, alt_account_for FROM skill_overrides "
            "WHERE alt_account_for IS NOT NULL")}


def account_groups(links: dict[int, int] | None = None) -> dict[int, list[int]]:
    """{account: every account the same person plays on, itself included}.

    Only accounts in a group appear; everyone else is a group of one and is
    left out, so callers fall back to the account's own rating.
    """
    links = alt_links() if links is None else links
    groups: dict[int, set[int]] = {}
    for alt, main in links.items():
        groups.setdefault(main, {main}).add(alt)
    return {aid: sorted(group) for group in groups.values() for aid in group}


def raw_cache_ratings(account_ids: list[int]) -> dict[int, dict]:
    """{account_id: {name, internal_rating}} straight from the cache, without
    the group rating — for showing what each account earned on its own."""
    if not account_ids:
        return {}
    with _conn() as conn:
        placeholders = ",".join("?" * len(account_ids))
        return {r["account_id"]: {"name": r["name"], "internal_rating": r["internal_rating"]}
                for r in conn.execute(
                    f"SELECT account_id, name, internal_rating FROM player_ratings_cache "
                    f"WHERE account_id IN ({placeholders})", tuple(account_ids))}


def get_rating_cache_row(account_id: int) -> dict | None:
    """Return a single cached rating row (with override nickname + hide_avatar
    joined in), or None if not present.

    When the player has more than one account (skill_overrides.alt_account_for),
    internal_rating becomes the rating their accounts earn pooled together —
    see rating_group.py — and `explanation` says how. `own_rating` keeps what
    this account alone is worth, and `linked_accounts` lists the group. Each
    row keeps its own name, avatar and stats.
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
        result["linked_accounts"] = account_groups().get(account_id, [account_id])
        result["own_rating"] = result.get("internal_rating")
        if result.get("group_rating") is not None:
            result["internal_rating"] = result["group_rating"]
            result["explanation"] = result.get("group_explanation") or result.get("explanation")
        return result


def get_all_rating_cache_rows() -> list[dict]:
    """Return account_id + internal_rating (+ override nickname) for every
    cached player. Used by the /api/ratings endpoint.

    Mirrors get_rating_cache_row: someone with several accounts is reported
    at their pooled rating, on every one of their accounts.
    """
    with _conn() as conn:
        return [dict(r) for r in conn.execute("""
            SELECT prc.account_id,
                   COALESCE(so.nickname, prc.name) AS name,
                   COALESCE(prc.group_rating, prc.internal_rating) AS internal_rating
            FROM player_ratings_cache prc
            LEFT JOIN skill_overrides so ON so.account_id = prc.account_id
        """).fetchall()]


def is_avatar_hidden(account_id: int) -> bool:
    """True when the account has hide_avatar set in skill_overrides."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT hide_avatar FROM skill_overrides WHERE account_id = ?",
            (account_id,),
        ).fetchone()
    return bool(row and row["hide_avatar"])


def compute_fantasy_adjusted_ratings(season_id: int, division_id: int) -> dict[int, dict]:
    """For each cached player in this division, fit a regression line of
    fantasy diff (boosted by winrate) vs. base internal_rating, then return a
    per-account_id dict of fantasy-adjusted ratings.

    The adjustment is the residual (actual diff − expected diff from the fit),
    normalized so the max-magnitude residual maps to ±6%. Players without
    match history (just added via /set_player) get no entry in the result
    and should be shown at their base rating.

    Uses every match the division has ever recorded, across all seasons: a
    rating is a judgement of the player, not of their current season.
    (season_id only scopes the draft-cost fields get_all_time_stats also
    returns, which this doesn't use.) Before this was explicit, the S39
    rollover quietly narrowed it to the current season's games.

    Returns: {account_id: {
        "base_rating": int,
        "adjusted_rating": int,
        "diff": float, "expected": float, "residual": float, "pct": float,
    }}
    """
    cached = get_division_cached_ratings(division_id)
    rows = [dict(r) for r in cached]
    if not rows:
        return {}

    stats = get_all_time_stats(division_id, season_id, cross_season=True)
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


def get_division_cached_ratings(division_id: int, include_all: bool = False) -> list[dict]:
    """Cached rating rows. By default, only players who've appeared in this
    division's matches. Joins skill_overrides so callers see any override-nickname
    and rating-override inline. Excludes any account marked as excluded for
    this division (alt/smurf accounts).

    If include_all=True, returns every cached player globally (minus this
    division's exclusions) — used by /players show_all:true. This is also how
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
                    SELECT account_id FROM division_exclusions WHERE division_id = ?
                )
            """, (division_id,)).fetchall()
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
                    WHERE m.division_id = ? AND p.account_id > 0
                )
                AND prc.account_id NOT IN (
                    SELECT account_id FROM division_exclusions WHERE division_id = ?
                )
            """, (division_id, division_id)).fetchall()

        # Anyone with several accounts is shown at the rating those accounts
        # earn pooled together (rating_group.py), which is stored on each of
        # them — matching get_rating_cache_row.
        result = []
        for r in rows:
            d = dict(r)
            d["own_rating"] = d.get("internal_rating")
            if d.get("group_rating") is not None:
                d["internal_rating"] = d["group_rating"]
            result.append(d)
    return result


def displayed_ratings(division_id: int) -> dict[int, int]:
    """{account_id: rating} — the internal rating to show each rated player,
    as of the division's current season: a /set_player rating override as
    set; otherwise the cached rating (pooled across a player's accounts),
    nudged by the season's fantasy adjustment (routed through the main
    account for an alt). The one place this is decided, so every view of a
    rating agrees. Shows results only: nothing about how they're computed."""
    division = get_division(division_id)
    if not division:
        return {}
    season_id = division.get("current_season_id")
    adjustments = compute_fantasy_adjusted_ratings(season_id, division_id) if season_id else {}
    with _conn() as conn:
        overrides = {r[0]: r[1] for r in conn.execute(
            "SELECT account_id, internal_rating_override FROM skill_overrides "
            "WHERE internal_rating_override IS NOT NULL")}
    out: dict[int, int] = {}
    for r in get_division_cached_ratings(division_id, include_all=True):
        aid = r["account_id"]
        if aid in overrides:
            out[aid] = int(overrides[aid])
        elif r.get("internal_rating") is not None:
            adj = adjustments.get(r.get("alt_account_for") or aid)
            out[aid] = int(adj["adjusted_rating"] if adj else r["internal_rating"])
    for aid, rating in overrides.items():
        out.setdefault(aid, int(rating))
    return out


def exclude_from_division(division_id: int, account_id: int) -> None:
    """Add this account to the division's exclusion list (for alts/smurfs)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO division_exclusions (division_id, account_id, excluded_at)
            VALUES (?, ?, ?)
            ON CONFLICT(division_id, account_id) DO NOTHING
        """, (division_id, account_id, now))


def unexclude_from_division(division_id: int, account_id: int) -> bool:
    """Remove an exclusion. Returns True if a row was deleted."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM division_exclusions WHERE division_id = ? AND account_id = ?",
            (division_id, account_id),
        )
    return cur.rowcount > 0


def add_to_division_roster(division_id: int, account_id: int) -> None:
    """Mark this account_id as part of this division's tracked roster."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO division_roster (division_id, account_id, added_at)
            VALUES (?, ?, ?)
            ON CONFLICT(division_id, account_id) DO NOTHING
        """, (division_id, account_id, now))


def remove_from_division_roster(division_id: int, account_id: int) -> bool:
    """Remove a manually-added roster entry. Returns True if a row was removed."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM division_roster WHERE division_id = ? AND account_id = ?",
            (division_id, account_id),
        )
    return cur.rowcount > 0


def division_player_has_matches(division_id: int, account_id: int) -> bool:
    """True if the account has at least one match in this division's history."""
    with _conn() as conn:
        row = conn.execute("""
            SELECT 1 FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.division_id = ? AND p.account_id = ?
            LIMIT 1
        """, (division_id, account_id)).fetchone()
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


def get_captain_account_ids(season_id: int, division_id: int) -> dict[str, int | None]:
    """Map each captain name from player_costs to an account_id by exact
    case-insensitive name match against (in order):
      1. skill_overrides.nickname
      2. player_ratings_cache.name
      3. players.name within this division
      4. players.name globally
    Returns {captain_name: account_id_or_None}.
    """
    with _conn() as conn:
        captains = [
            r["captain"]
            for r in conn.execute(
                "SELECT DISTINCT captain FROM player_costs "
                "WHERE season_id = ? AND division_id = ? AND captain IS NOT NULL",
                (season_id, division_id),
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
                WHERE m.division_id = ? AND LOWER(p.name) = LOWER(?)
                LIMIT 1
            """, (division_id, cap)).fetchone()
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
    season_id: int, division_id: int, captain_a: str, captain_b: str,
) -> list[dict]:
    """Find matches where both teams had ≥3 of their roster on opposite sides.

    Each returned dict has:
        match_id, start_time, duration, a_won (bool)
    Sorted by start_time ascending.
    """
    costs = get_player_costs(season_id, division_id)
    cap_aids = get_captain_account_ids(season_id, division_id)

    def _roster(cap_name: str) -> set[int]:
        s = {aid for aid, c in costs.items() if c.get("captain") == cap_name}
        if cap_aids.get(cap_name):
            s.add(cap_aids[cap_name])
        return s

    roster_a = _roster(captain_a)
    roster_b = _roster(captain_b)
    if not roster_a or not roster_b:
        return []

    where, params = season_scope(season_id, division_id).where("m")
    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT p.match_id, p.account_id, p.player_slot,
                   m.radiant_win, m.duration, m.start_time
            FROM players p JOIN matches m ON p.match_id = m.match_id
            WHERE {where}
        """, params).fetchall()

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


def add_manual_match(*, division_id: int, league_id: int, start_time: int,
                     winner: str, loser: str, set_by: int | None = None) -> int:
    """Record one game that was played but has no Dota match of its own —
    the usual reason being that nobody ticketed it, so OpenDota has nothing to
    fetch and the result would otherwise be lost.

    The row carries no players, no duration and no scores, because none of
    that is known: only who played and who won. Its id is negative, which no
    real match id ever is, so "entered by hand" is legible everywhere from the
    id alone. Both sides get a team override, which is what lets the record
    count it without any player rows to attribute.

    Returns the new (negative) match id.
    """
    now = datetime.now(timezone.utc)
    with _conn() as conn:
        lowest = conn.execute("SELECT MIN(match_id) FROM matches").fetchone()[0] or 0
        match_id = min(lowest, 0) - 1
        conn.execute("""
            INSERT INTO matches (match_id, division_id, league_id, start_time, duration,
                                 game_mode, cluster, radiant_win, radiant_score, dire_score,
                                 fetched_at)
            VALUES (?, ?, ?, ?, 0, 0, 0, 1, 0, 0, ?)
        """, (match_id, division_id, league_id, start_time, now.isoformat()))
    # Radiant is the winner: with no real sides to speak of, one of them has
    # to be, and this keeps radiant_win meaning what it means everywhere else.
    set_match_team_override(match_id, "radiant", winner, set_by=set_by)
    set_match_team_override(match_id, "dire", loser, set_by=set_by)
    logger.info("Manual result %d: %s beat %s in division %s", match_id, winner, loser, division_id)
    return match_id


def get_playoff_bracket(season_id: int, division_id: int) -> dict | None:
    """The bracket's shape and its seats, or None if playoffs haven't started."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM playoff_brackets WHERE season_id = ? AND division_id = ?",
            (season_id, division_id)).fetchone()
        if not row:
            return None
        seats = {r["position"]: r["captain"] for r in conn.execute(
            "SELECT position, captain FROM playoff_seeds WHERE season_id = ? AND division_id = ? "
            "ORDER BY position", (season_id, division_id))}
    bracket = dict(row)
    bracket["seats"] = [seats.get(i) for i in range(bracket["size"])]
    return bracket


# Every playoff series is a best of three. Kept as a column rather than a
# constant so a season that wants a longer final doesn't need a migration.
PLAYOFF_BEST_OF = 3


def start_playoffs(season_id: int, division_id: int, *, size: int,
                   best_of: int = PLAYOFF_BEST_OF,
                   started_at: str, created_by: int | None = None) -> None:
    """Open a bracket of `size` teams. Replaces any bracket already there,
    seats and all, since changing the size changes what a seat means."""
    if size < 2 or size & (size - 1):
        raise ValueError("a single-elimination bracket needs a power of two")
    with _conn() as conn:
        conn.execute("DELETE FROM playoff_seeds WHERE season_id = ? AND division_id = ?",
                     (season_id, division_id))
        conn.execute("""
            INSERT INTO playoff_brackets (season_id, division_id, size, format, best_of,
                                          started_at, created_by)
            VALUES (?, ?, ?, 'single', ?, ?, ?)
            ON CONFLICT(season_id, division_id) DO UPDATE SET
                size = excluded.size, best_of = excluded.best_of,
                started_at = excluded.started_at, created_by = excluded.created_by
        """, (season_id, division_id, size, best_of, started_at, created_by))


def set_playoff_seats(season_id: int, division_id: int, seats: list[str | None]) -> None:
    """Place the first round. A team may sit in one seat only; an empty seat
    is a bracket still being filled in, which is allowed — seeding happens
    over a few minutes with captains picking in turn."""
    placed = [c for c in seats if c]
    if len(placed) != len(set(placed)):
        raise ValueError("a team can only be in one seat")
    with _conn() as conn:
        conn.execute("DELETE FROM playoff_seeds WHERE season_id = ? AND division_id = ?",
                     (season_id, division_id))
        conn.executemany(
            "INSERT INTO playoff_seeds (season_id, division_id, position, captain) VALUES (?, ?, ?, ?)",
            [(season_id, division_id, i, c) for i, c in enumerate(seats)])


def delete_playoff_bracket(season_id: int, division_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM playoff_seeds WHERE season_id = ? AND division_id = ?",
                     (season_id, division_id))
        conn.execute("DELETE FROM playoff_brackets WHERE season_id = ? AND division_id = ?",
                     (season_id, division_id))


def set_match_excluded(match_id: int, excluded: bool, *, reason: str | None = None,
                       by: int | None = None) -> bool:
    """Take a game out of the league's reckoning, or put it back.

    The row stays: it's a real game that really happened, and deleting it
    would only mean fetching it again next time the league is pulled. Every
    scoped query skips it (MatchScope), so records, stats, standings and
    matchups all stop counting it at once.
    """
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE matches SET excluded = ?, excluded_reason = ?, excluded_by = ?, "
            "excluded_at = ? WHERE match_id = ?",
            (1 if excluded else 0, reason if excluded else None, by if excluded else None,
             datetime.now(timezone.utc).isoformat() if excluded else None, match_id))
    return cur.rowcount > 0


def delete_manual_match(match_id: int) -> bool:
    """Remove a hand-entered result. Refuses anything with a real match id:
    those came from OpenDota and are re-fetchable facts, not data entry."""
    if match_id >= 0:
        raise ValueError("only hand-entered results (negative ids) can be deleted")
    with _conn() as conn:
        cur = conn.execute("DELETE FROM matches WHERE match_id = ?", (match_id,))
        conn.execute("DELETE FROM match_team_overrides WHERE match_id = ?", (match_id,))
    return cur.rowcount > 0


def set_match_team_override(match_id: int, side: str, captain: str | None,
                           *, automatic: bool = False, set_by: int | None = None) -> None:
    """Say which team played one side of a match. `automatic` removes the
    override and goes back to counting rosters; captain=None records that the
    side really was stand-ins."""
    if side not in ("radiant", "dire"):
        raise ValueError(f"side must be radiant or dire, not {side!r}")
    with _conn() as conn:
        if automatic:
            conn.execute("DELETE FROM match_team_overrides WHERE match_id = ? AND side = ?",
                         (match_id, side))
            return
        conn.execute("""
            INSERT INTO match_team_overrides (match_id, side, captain, set_by, set_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(match_id, side) DO UPDATE SET
                captain = excluded.captain, set_by = excluded.set_by, set_at = excluded.set_at
        """, (match_id, side, captain, set_by, datetime.now(timezone.utc).isoformat()))


def get_match_team_overrides(match_ids: list[int]) -> dict[int, dict[str, str | None]]:
    """{match_id: {side: captain_or_None}} for the sides an admin has set."""
    if not match_ids:
        return {}
    out: dict[int, dict[str, str | None]] = {}
    with _conn() as conn:
        for i in range(0, len(match_ids), 500):
            chunk = match_ids[i:i + 500]
            for r in conn.execute(
                f"SELECT match_id, side, captain FROM match_team_overrides "
                f"WHERE match_id IN ({','.join('?' * len(chunk))})", chunk):
                out.setdefault(r["match_id"], {})[r["side"]] = r["captain"]
    return out


def get_match_team_captains_for_matches(matches: list[dict]) -> dict[int, dict[str, str | None]]:
    """Attribute both sides of each match, where the matches may come from
    different seasons and divisions — get_match_team_captains works a season
    at a time, because a roster only means anything within one."""
    out: dict[int, dict[str, str | None]] = {}
    by_season: dict[tuple[int, int], list[int]] = {}
    for m in matches:
        if m.get("league_id") and m.get("division_id"):
            by_season.setdefault((m["league_id"], m["division_id"]), []).append(m["match_id"])
    for (season_id, division_id), ids in by_season.items():
        out.update(get_match_team_captains(season_id, division_id, ids))
    return out


def get_match_team_captains(
    season_id: int, division_id: int, match_ids: list[int],
) -> dict[int, dict[str, str | None]]:
    """Attribute each side of each match to a team, keyed by captain.

    Same rule as get_head_to_head_matches / get_team_match_aggregates: a side
    belongs to a team when ≥3 of that team's roster (4 drafted + captain when
    resolvable) played on it. Sides no team qualifies for (stand-in-heavy
    lineups, matches from another season) map to None.

    Returns {match_id: {"radiant": captain_or_None, "dire": captain_or_None}}.
    """
    if not match_ids or not season_id:
        return {}

    costs = get_player_costs(season_id, division_id)
    cap_aids = get_captain_account_ids(season_id, division_id)
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

    overrides = get_match_team_overrides(list(sides))
    result: dict[int, dict[str, str | None]] = {}
    for mid, by_side in sides.items():
        result[mid] = {}
        for side, aids in by_side.items():
            if side in overrides.get(mid, {}):
                result[mid][side] = overrides[mid][side]   # an admin said so
                continue
            cap = max(rosters, key=lambda c: len(rosters[c] & aids), default=None)
            result[mid][side] = cap if cap and len(rosters[cap] & aids) >= 3 else None
    return result


def get_team_match_aggregates(season_id: int, division_id: int) -> dict[str, dict]:
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

    # Rosters: captain → {account_ids}
    costs = get_player_costs(season_id, division_id)
    captain_aids = get_captain_account_ids(season_id, division_id)
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
    all_stats = {s["account_id"]: s for s in get_all_time_stats(division_id, season_id)}
    member_names: dict[str, list[str]] = {}
    for cap, aids in rosters.items():
        names = []
        for aid in aids:
            s = all_stats.get(aid)
            names.append((s.get("name") if s else None) or f"Player_{aid}")
        member_names[cap] = names

    # Pull all the player-rows for this season + their match metadata.
    where, params = season_scope(season_id, division_id).where("m")
    with _conn() as conn:
        rows = conn.execute(f"""
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
            WHERE {where}
        """, params).fetchall()

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


def get_team_aggregates(season_id: int, division_id: int) -> dict[str, dict]:
    """Aggregate season stats by team (keyed by captain name).

    Per-game stats (gpm, kda, hero_damage, etc.) are weighted-averaged by
    games_played so high-volume players matter more. Wins and games_played
    are summed.

    Returns: {captain_name: {games_played, wins, gpm, xpm, ...}}
    Stats that don't aggregate meaningfully (diff, value, attendance) are
    omitted.
    """
    costs = get_player_costs(season_id, division_id)
    if not costs:
        return {}
    all_stats = {s["account_id"]: s for s in get_all_time_stats(division_id, season_id)}

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
    captain_aids = get_captain_account_ids(season_id, division_id)
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


def get_player_costs(season_id: int, division_id: int) -> dict[int, dict]:
    """Return {account_id: {cost, captain, mmr}} for the season. Alts with
    no cost row of their own get their main's mirrored under their own
    account_id too — see _mirror_alts_into."""
    with _conn() as conn:
        rows = conn.execute(
            # Explicit order (priciest first) — rosters and member lists
            # follow it, and table order isn't guaranteed.
            "SELECT account_id, cost, captain, mmr FROM player_costs WHERE season_id = ? AND division_id = ? "
            "ORDER BY cost DESC, account_id",
            (season_id, division_id),
        ).fetchall()
    result = {
        r["account_id"]: {"cost": r["cost"], "captain": r["captain"], "mmr": r["mmr"]}
        for r in rows
    }
    _mirror_alts_into(result)
    return result


def upsert_player_costs(season_id: int, division_id: int, costs: list[dict]):
    """Insert/update player costs. Each dict needs account_id, cost; captain + mmr optional."""
    if not costs:
        return
    with _conn() as conn:
        conn.executemany("""
            INSERT INTO player_costs (season_id, division_id, account_id, cost, captain, mmr)
            VALUES (:season_id, :division_id, :account_id, :cost, :captain, :mmr)
            ON CONFLICT(season_id, division_id, account_id) DO UPDATE SET
                cost    = excluded.cost,
                captain = excluded.captain,
                mmr     = excluded.mmr
        """, [{
            "season_id":    season_id,
            "division_id":  division_id,
            "captain":      None,
            "mmr":          None,
            **c,
        } for c in costs])


def _compute_attendance(scope: MatchScope, costs: dict[int, dict],
                        captain_aids: dict[str, int] | None = None) -> dict[int, float]:
    """Return {account_id: attendance_ratio} for a team's players.

    Denominator (per captain): matches where >= 2 of that team shared a
        team_side — matches the team actually played as a team, rather than a
        lone stand-in appearance.
    Numerator (per player): matches where >= 1 of their team-mates was on
        their side.

    A captain is on the team they drafted but has no row in player_costs,
    since they did the drafting rather than being drafted. captain_aids puts
    them back. Without it every captain's attendance comes out missing, and a
    night a captain played alongside one of their own picks doesn't count as a
    game the team played at all.
    """
    if not costs:
        return {}

    where, params = scope.where("m")
    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT p.match_id, p.account_id, p.team_side
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE {where}
        """, params).fetchall()

    if not rows:
        return {}

    # account_id → the captain naming their team, and the reverse.
    team_of: dict[int, str] = {aid: info["captain"] for aid, info in costs.items()
                               if info.get("captain")}
    members: dict[str, set] = {}
    for aid, cap in team_of.items():
        members.setdefault(cap, set()).add(aid)
    for cap, aid in (captain_aids or {}).items():
        if aid and cap in members:
            members[cap].add(aid)
            team_of[aid] = cap

    # Who was on each side of each match.
    match_side_aids: dict[tuple, set] = {}
    for r in rows:
        match_side_aids.setdefault((r["match_id"], r["team_side"]), set()).add(r["account_id"])

    captain_team_games: dict[str, int] = {}
    for aids in match_side_aids.values():
        for captain, team in members.items():
            if len(aids & team) >= 2:
                captain_team_games[captain] = captain_team_games.get(captain, 0) + 1

    player_team_games: dict[int, int] = {}
    for r in rows:
        cap = team_of.get(r["account_id"])
        if not cap:
            continue
        if match_side_aids[(r["match_id"], r["team_side"])] & (members[cap] - {r["account_id"]}):
            player_team_games[r["account_id"]] = player_team_games.get(r["account_id"], 0) + 1

    attendance: dict[int, float] = {}
    for aid, num in player_team_games.items():
        denom = captain_team_games.get(team_of[aid], 0)
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
    account_id: int,
    season_id: int,
    division_id: int,
    week_number: int | None = None,
) -> dict | None:
    """
    Per-match comparison of one player's fantasy points to the average of their
    4 same-side teammates. Aggregated across all matches in the requested range.

    week_number=None or -1 → the whole season.
    Otherwise scoped to that season week.

    Returns None if the player has no matches in range.
    """
    scope = (season_scope(season_id, division_id) if week_number is None or week_number == -1
             else season_week_scope(season_id, division_id, week_number))
    where, params = scope.where("m")
    with _conn() as conn:
        target_matches = conn.execute(f"""
            SELECT p.match_id
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE {where} AND p.account_id = ?
        """, params + [account_id]).fetchall()

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


def get_all_weeks(division_id: int) -> list[dict]:
    """Return a list of distinct weeks that have data, with match counts."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                strftime('%Y-%m-%d', start_time, 'unixepoch') AS match_date,
                COUNT(*) AS match_count
            FROM matches
            WHERE division_id = ?
            GROUP BY date(start_time, 'unixepoch', 'weekday 1')
            ORDER BY match_date DESC
        """, (division_id,)).fetchall()
    return [dict(r) for r in rows]


def get_latest_season_week(season_id: int, division_id: int) -> int | None:
    """Return the latest season week number that has data, or None if no data."""
    season = get_season(season_id)
    season_start_date = season["season_start"]
    where, params = season_scope(season_id, division_id).where("m")
    with _conn() as conn:
        row = conn.execute(f"SELECT MAX(m.start_time) AS latest_time FROM matches m WHERE {where}",
                           params).fetchone()

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


def _list_matches(scope: MatchScope) -> list[dict]:
    """Matches in scope, newest first, with basic info."""
    where, params = scope.where("m")
    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT m.match_id, m.start_time, m.duration, m.radiant_win, m.radiant_score, m.dire_score
            FROM matches m
            WHERE {where}
            ORDER BY m.start_time DESC
        """, params).fetchall()
    return [dict(r) for r in rows]


def get_matches_for_week(division_id: int, week_offset: int = 0) -> list[dict]:
    """Return all matches for a given calendar week with basic info."""
    start, end = _week_start(week_offset)
    return _list_matches(MatchScope(division_id, start=start, end=end))


def get_matches_for_season_week(season_id: int, division_id: int, week_number: int) -> list[dict]:
    """Return all of a season's matches in one of its weeks."""
    return _list_matches(season_week_scope(season_id, division_id, week_number))


def latest_match_fetch(division_id: int) -> str | None:
    """When this division last gained a match (matches.fetched_at). Ratings
    move when anyone plays — the season fantasy adjustment is fitted across
    the whole division — so this is half of "when did a player's numbers last
    change", alongside their own cache row's updated_at."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT MAX(fetched_at) FROM matches WHERE division_id = ?", (division_id,)
        ).fetchone()
    return row[0] if row else None


def get_latest_matches(division_id: int, season_id: int | None = None) -> list[dict]:
    """Return all matches from the current season week (via
    get_current_season_week/get_season_week_range, so this respects explicit
    week boundaries — including byes — instead of a raw calendar week).

    Falls back to the plain "most recent calendar week with a match" when no
    season is available at all (e.g. division not configured)."""
    if season_id:
        return _list_matches(season_week_scope(season_id, division_id, get_current_season_week(season_id, division_id)))

    with _conn() as conn:
        latest = conn.execute(
            "SELECT MAX(start_time) as max_time FROM matches WHERE division_id = ?",
            (division_id,)
        ).fetchone()
        if not latest or not latest["max_time"]:
            return []

        dt = datetime.fromtimestamp(latest["max_time"], tz=timezone.utc)
        monday = dt - timedelta(days=dt.weekday())
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        sunday = monday + timedelta(days=7) - timedelta(seconds=1)

    return _list_matches(MatchScope(division_id, start=int(monday.timestamp()), end=int(sunday.timestamp())))


def get_all_matches(division_id: int) -> list[dict]:
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
            WHERE division_id = ?
            ORDER BY start_time DESC
        """, (division_id,)).fetchall()
    return [dict(r) for r in rows]


def get_match_ids_for_player(division_id: int, player_name: str) -> set[int]:
    """Return the set of match IDs (for this division) where a player matching the name played.

    Partial, case-insensitive match against the in-game name, the
    /set_player nickname, or the rating-cache name — so the name /player
    displays always finds the player.
    """
    with _conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT p.match_id
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.division_id = ?
              AND (LOWER(p.name) LIKE LOWER(?)
                   OR p.account_id IN (
                       SELECT account_id FROM skill_overrides
                       WHERE LOWER(nickname) LIKE LOWER(?)
                   )
                   OR p.account_id IN (
                       SELECT account_id FROM player_ratings_cache
                       WHERE LOWER(name) LIKE LOWER(?)
                   ))
        """, (division_id, f"%{player_name}%", f"%{player_name}%", f"%{player_name}%")).fetchall()
    return {r["match_id"] for r in rows}


def nuke_data(division_id: int):
    """Delete all match/player/chat data for a specific division."""
    with _conn() as conn:
        # Get match IDs for this division to delete related data
        match_ids = conn.execute(
            "SELECT match_id FROM matches WHERE division_id = ?", (division_id,)
        ).fetchall()
        match_id_list = [r["match_id"] for r in match_ids]

        if match_id_list:
            placeholders = ",".join("?" for _ in match_id_list)
            conn.execute(f"DELETE FROM players WHERE match_id IN ({placeholders})", match_id_list)
            conn.execute(f"DELETE FROM chat_messages WHERE match_id IN ({placeholders})", match_id_list)
            conn.execute(f"DELETE FROM draft_picks WHERE match_id IN ({placeholders})", match_id_list)

        conn.execute("DELETE FROM matches WHERE division_id = ?", (division_id,))
    logger.info("Data nuked for division %d", division_id)


def match_exists(match_id: int) -> bool:
    """Whether a game is stored. A game is stored once, in one division."""
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    return row is not None


def assign_builds_to_heroes(match_id: int, builds: dict[int, list[tuple[int, int]]]) -> dict[int, list[tuple[int, int]]]:
    """Put each drafted build on the player who actually played it.

    builds: {account_id as the draft source said: [(pick_order, ability_id)]}.
    The source can put a build on the wrong player (seat swaps), but every
    build includes its hero model pick (ability_id = -hero_id), and the
    players table knows who played which hero — so a build goes to the
    player whose hero is its model. Builds with no model, or whose hero no
    one played, keep the source's player."""
    with _conn() as conn:
        played = {r["hero_id"]: r["account_id"] for r in conn.execute(
            "SELECT account_id, hero_id FROM players WHERE match_id = ?", (match_id,))}
    out: dict[int, list[tuple[int, int]]] = {}
    for source_account, picks in builds.items():
        model = next((-a for _, a in picks if a < 0), None)
        out.setdefault(played.get(model, source_account), []).extend(picks)
    return out


def realign_stored_drafts() -> int:
    """Re-file every stored draft's builds by hero (assign_builds_to_heroes).
    No network. Returns how many games changed."""
    with _conn() as conn:
        match_ids = [r[0] for r in conn.execute("SELECT DISTINCT match_id FROM match_drafts")]
    changed = 0
    for mid in match_ids:
        with _conn() as conn:
            rows = conn.execute("SELECT pick_order, account_id, ability_id FROM match_drafts WHERE match_id = ?",
                                (mid,)).fetchall()
        builds: dict[int, list[tuple[int, int]]] = {}
        for r in rows:
            builds.setdefault(r["account_id"], []).append((r["pick_order"], r["ability_id"]))
        fixed = assign_builds_to_heroes(mid, builds)
        if fixed != builds:
            save_match_draft(mid, [(po, aid, ab) for aid, picks in fixed.items() for po, ab in picks])
            changed += 1
    return changed


def save_match_draft(match_id: int, picks: list[tuple[int, int, int]]) -> None:
    """Store a game's draft: (pick_order, account_id, ability_id) per pick,
    replacing any stored before."""
    with _conn() as conn:
        conn.execute("DELETE FROM match_drafts WHERE match_id = ?", (match_id,))
        conn.executemany("INSERT INTO match_drafts (match_id, pick_order, account_id, ability_id) VALUES (?, ?, ?, ?)",
                         [(match_id, *p) for p in picks])


def get_match_draft(match_id: int) -> dict[int, list[int]]:
    """{account_id: [ability ids]} for a game: the hero model (negative id)
    first, then abilities in the order they were picked. {} if none stored."""
    with _conn() as conn:
        rows = conn.execute("SELECT account_id, ability_id FROM match_drafts WHERE match_id = ? ORDER BY pick_order",
                            (match_id,)).fetchall()
    out: dict[int, list[int]] = {}
    for r in rows:
        out.setdefault(r["account_id"], []).append(r["ability_id"])
    return {aid: sorted(ids, key=lambda i: i >= 0) for aid, ids in out.items()}  # model first, stable


def games_missing_drafts(season_id: int) -> list[int]:
    """Ability Draft games of a season with no stored draft."""
    with _conn() as conn:
        return [r[0] for r in conn.execute("""
            SELECT m.match_id FROM matches m
            WHERE m.league_id = ? AND m.game_mode = 18
              AND NOT EXISTS (SELECT 1 FROM match_drafts d WHERE d.match_id = m.match_id)
            ORDER BY m.start_time
        """, (season_id,))]


def match_is_complete(match_id: int) -> bool:
    """Whether a game is stored with its player rows. A game row without
    them (an interrupted fetch, say) should be fetched again."""
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM players WHERE match_id = ? LIMIT 1", (match_id,)).fetchone()
    return row is not None


def set_match_division(match_id: int, division_id: int | None) -> None:
    with _conn() as conn:
        conn.execute("UPDATE matches SET division_id = ? WHERE match_id = ?", (division_id, match_id))


