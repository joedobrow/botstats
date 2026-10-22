"""Database access for the website.

The site's own tables (site_*) hold what only the website needs: logins,
signups, and display settings. Everything about matches, teams and seasons
is read from the bot's tables, so the site and the bot never disagree.

Nothing here selects a rating column (internal_rating, raw_windrun,
mmr_estimate, explanation, group_rating) — public pages show stats and the
ranked badge only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
from config import LEAGUE_TZ
from db import _conn

logger = logging.getLogger(__name__)

SESSION_DAYS = 30
# Season status: key -> label. "signups" is the only one that takes signups.
SEASON_STATUSES = {
    "upcoming": "Upcoming",
    "signups": "Signups open",
    "active": "Active",
    "completed": "Completed",
}
POSITIONS = (1, 2, 3, 4, 5)
CAPTAIN_CHOICES = ("yes", "maybe", "no")

_HEROES: dict[int, dict] = {
    int(k): v for k, v in
    json.loads((Path(__file__).parent / "static" / "heroes.json").read_text()).items()
}


def hero(hero_id: int) -> dict:
    return _HEROES.get(hero_id) or {"name": f"Hero {hero_id}", "slug": ""}


_STATIC = Path(__file__).parent / "static"
_ABILITIES: dict[int, dict] = {int(k): v for k, v in json.loads((_STATIC / "abilities.json").read_text()).items()}
_SERVERS: dict[int, str] = {int(k): v for k, v in json.loads((_STATIC / "servers.json").read_text()).items()}


def draft_pick(ability_id: int) -> dict:
    """A drafted pick for display: {"kind": "hero"|"ability", "name", "slug"}.
    Negative ids are hero models."""
    if ability_id < 0:
        h = hero(-ability_id)
        return {"kind": "hero", "name": h["name"], "slug": h["slug"]}
    a = _ABILITIES.get(ability_id)
    return {"kind": "ability", "name": a["dname"] if a else f"Ability {ability_id}",
            "img": a["img"] if a and a.get("img") else ""}


def server_name(cluster: int) -> str:
    """The region a game server is in, from OpenDota's list."""
    return _SERVERS.get(cluster) or f"Server {cluster}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_site_tables() -> None:
    """Create the website's tables. Changes to existing ones are numbered
    migrations in db.py, which run first (db.init_db)."""
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS site_users (
                account_id     INTEGER PRIMARY KEY,   -- 32-bit Dota account id
                steam_name     TEXT,
                avatar_url     TEXT,
                discord        TEXT,
                created_at     TEXT NOT NULL,
                last_login_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS site_sessions (
                token       TEXT PRIMARY KEY,
                account_id  INTEGER NOT NULL,
                csrf        TEXT NOT NULL,
                expires_at  TEXT NOT NULL
            );

            -- Site-only settings for a season in one division.
            CREATE TABLE IF NOT EXISTS site_season_settings (
                season_id     INTEGER NOT NULL REFERENCES seasons(id),
                division_id   INTEGER NOT NULL REFERENCES divisions(id),
                status        TEXT,                -- a SEASON_STATUSES key; NULL = default
                PRIMARY KEY (season_id, division_id)
            );

            CREATE TABLE IF NOT EXISTS site_signups (
                season_id     INTEGER NOT NULL REFERENCES seasons(id),
                division_id   INTEGER NOT NULL REFERENCES divisions(id),
                account_id    INTEGER NOT NULL,
                mmr           INTEGER,
                positions     TEXT NOT NULL,        -- e.g. "1,3"
                captain       TEXT NOT NULL,        -- yes / maybe / no (would they captain)
                level         TEXT NOT NULL DEFAULT 'player',  -- what they signed up as
                level_override TEXT,                  -- what an admin made it instead
                discord       TEXT,
                statement     TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                PRIMARY KEY (season_id, division_id, account_id)
            );

            -- Site admins. Uberadmins come from SITE_UBERADMIN_STEAM_IDS
            -- and manage this list from /admin.
            CREATE TABLE IF NOT EXISTS site_admins (
                account_id  INTEGER PRIMARY KEY,
                added_by    INTEGER,
                added_at    TEXT NOT NULL
            );

            -- Editable text blocks (home intro, rules).
            CREATE TABLE IF NOT EXISTS site_text (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL
            );

            -- Who won a season, in one division: one row per player, in the
            -- order they should be listed. Recorded rather than derived — a
            -- season can be won before the site kept brackets (37, 38), or in
            -- a division whose draft was never loaded here.
            CREATE TABLE IF NOT EXISTS site_season_champions (
                season_id   INTEGER NOT NULL REFERENCES seasons(id),
                division_id INTEGER NOT NULL REFERENCES divisions(id),
                position    INTEGER NOT NULL,
                account_id  INTEGER,            -- NULL when only a name is known
                name        TEXT NOT NULL,
                PRIMARY KEY (season_id, division_id, position)
            );
        """)
    init_view_tables()
    prune_views()
    with _conn() as conn:
        # The team they won as, and anything that needs saying about it, live
        # beside the season's other site-only settings.
        have = {r[1] for r in conn.execute("PRAGMA table_info(site_season_settings)")}
        for col in ("champion_team", "champion_note"):   # note is legacy, never read
            if col not in have:
                conn.execute(f"ALTER TABLE site_season_settings ADD COLUMN {col} TEXT")

        # What someone signed up as, and what an admin decided instead. Both,
        # because "they asked to captain and we said no" is worth being able
        # to see — see SIGNUP_LEVELS.
        have = {r[1] for r in conn.execute("PRAGMA table_info(site_signups)")}
        if "level" not in have:
            conn.execute("ALTER TABLE site_signups ADD COLUMN level TEXT NOT NULL DEFAULT 'player'")
            conn.execute("UPDATE site_signups SET level = 'captain' WHERE LOWER(captain) = 'yes'")
        if "level_override" not in have:
            conn.execute("ALTER TABLE site_signups ADD COLUMN level_override TEXT")



# ---------------------------------------------------------------------------
# Users and sessions
# ---------------------------------------------------------------------------

def upsert_user(account_id: int, steam_name: str | None, avatar_url: str | None) -> None:
    now = _now()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO site_users (account_id, steam_name, avatar_url, created_at, last_login_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                steam_name    = COALESCE(excluded.steam_name, site_users.steam_name),
                avatar_url    = COALESCE(excluded.avatar_url, site_users.avatar_url),
                last_login_at = excluded.last_login_at
        """, (account_id, steam_name, avatar_url, now, now))


def get_user(account_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM site_users WHERE account_id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


def set_user_discord(account_id: int, discord: str | None) -> None:
    with _conn() as conn:
        conn.execute("UPDATE site_users SET discord = ? WHERE account_id = ?", (discord, account_id))


def create_session(account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    with _conn() as conn:
        conn.execute("DELETE FROM site_sessions WHERE expires_at < ?", (_now(),))
        conn.execute(
            "INSERT INTO site_sessions (token, account_id, csrf, expires_at) VALUES (?, ?, ?, ?)",
            (token, account_id, secrets.token_urlsafe(24), expires.isoformat(timespec="seconds")),
        )
    return token


def get_session(token: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM site_sessions WHERE token = ? AND expires_at > ?", (token, _now())
        ).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM site_sessions WHERE token = ?", (token,))


def is_admin(account_id: int) -> bool:
    with _conn() as conn:
        return conn.execute("SELECT 1 FROM site_admins WHERE account_id = ?", (account_id,)).fetchone() is not None


def list_admins() -> list[dict]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM site_admins ORDER BY added_at")]


def add_admin(account_id: int, added_by: int) -> None:
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO site_admins (account_id, added_by, added_at) VALUES (?, ?, ?)",
                     (account_id, added_by, _now()))


def remove_admin(account_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM site_admins WHERE account_id = ?", (account_id,))


# ---------------------------------------------------------------------------
# Divisions and seasons
# ---------------------------------------------------------------------------

def divisions() -> list[dict]:
    """Every division, oldest first."""
    return sorted(db.get_all_divisions(), key=lambda d: d["id"])


def rename_division(division_id: int, name: str, slug: str) -> None:
    """Rename a division; slug is its URL name. Raises ValueError if the
    slug is taken."""
    taken = db.get_division_by_slug(slug)
    if taken and taken["id"] != division_id:
        raise ValueError(f"Another division already uses the URL name {slug!r}.")
    db.update_division(division_id, name=name, slug=slug)


def all_seasons() -> list[dict]:
    """Every season of every division, newest first: one entry per season
    per division playing in it, with the season's id (its Dota league id),
    the division's id, name and slug, status and signup count. A season's
    games in a division are that division's games in its league
    (db.season_scope)."""
    with _conn() as conn:
        statuses = {(r["season_id"], r["division_id"]): r["status"]
                    for r in conn.execute("SELECT * FROM site_season_settings")}
        signup_counts = {
            (r["season_id"], r["division_id"]): r["n"]
            for r in conn.execute("SELECT season_id, division_id, COUNT(*) AS n FROM site_signups GROUP BY 1, 2")
        }
        pairs = [tuple(r) for r in conn.execute("SELECT season_id, division_id FROM season_divisions")]
    seasons = {s["id"]: s for s in db.get_all_seasons()}
    divs = {d["id"]: d for d in divisions()}
    many_divisions = len(divs) > 1
    out = []
    for season_id, division_id in pairs:
        s, d = seasons.get(season_id), divs.get(division_id)
        if not s or not d:
            continue
        key = (season_id, division_id)
        out.append({
            "id": season_id,
            "season_start": s["season_start"],
            "label": db.season_label(s),
            "division_id": division_id,
            "division": d["name"],
            "division_slug": d["slug"],
            "show_division": many_divisions,
            "status": statuses.get(key),
            "signup_count": signup_counts.get(key, 0),
        })
    out.sort(key=lambda s: (s["season_start"], -s["division_id"]), reverse=True)
    _fill_default_statuses(out)
    today = datetime.now(timezone.utc).date().isoformat()
    for s in out:
        s["status_label"] = SEASON_STATUSES[s["status"]]
        s["signups_open"] = s["status"] == "signups"
        s["started"] = s["season_start"] <= today
    return out


def _fill_default_statuses(seasons: list[dict]) -> None:
    """Seasons no admin has set a status for, per division: the newest
    season that has started is active, earlier ones completed, future ones
    upcoming."""
    today = datetime.now(timezone.utc).date().isoformat()
    newest: dict[int, dict] = {}
    for s in seasons:  # newest first
        if s["season_start"] <= today:
            newest.setdefault(s["division_id"], s)
    for s in seasons:
        if s["status"] in SEASON_STATUSES:
            continue
        if s["season_start"] > today:
            s["status"] = "upcoming"
        else:
            s["status"] = "active" if newest.get(s["division_id"]) is s else "completed"


def get_season(season_id: int, division_slug: str | None = None) -> dict | None:
    """A season in one division. The division may be left out when the
    season has only one."""
    found = [s for s in all_seasons() if s["id"] == season_id
             and (division_slug is None or s["division_slug"] == division_slug)]
    return found[0] if len(found) == 1 else None


def season_divisions(season_id: int) -> list[dict]:
    """The site's entries for each division playing in a season."""
    return [s for s in all_seasons() if s["id"] == season_id]


def create_season(season_id: int, division_id: int, season_start: str, label: str | None) -> None:
    """Add a division to a season (creating the season — season_id is its
    Dota league id — if it's new). Raises ValueError if the division is
    already in it."""
    if any(d["id"] == division_id for d in db.get_season_divisions(season_id)):
        raise ValueError(f"That division already plays in league {season_id}.")
    if not db.get_season(season_id):
        db.upsert_season(season_id, season_start, label=label)
    db.add_season_division(season_id, division_id)


def rename_season(season_id: int, label: str) -> None:
    """Rename a season. It's the bot's seasons.label, so Discord's season
    filters show the new name too."""
    db.update_season(season_id, label=label)


def set_season_start(season_id: int, season_start: str) -> None:
    """Move a season's start date (ValueError on a bad date)."""
    db.update_season(season_id, season_start=season_start)


def set_status(season_id: int, division_id: int, status: str) -> None:
    with _conn() as conn:
        conn.execute("""
            INSERT INTO site_season_settings (season_id, division_id, status) VALUES (?, ?, ?)
            ON CONFLICT(season_id, division_id) DO UPDATE SET status = excluded.status
        """, (season_id, division_id, status))


# Paths that aren't a page someone read: assets, the API, the stream cards.
_NOT_A_PAGE = ("/static/", "/api/", "/card/", "/uploads/", "/favicon", "/auth/")
# Substrings that mark a crawler. Not exhaustive, and not meant to be — it
# keeps the obvious robots out of a human count.
_BOTS = ("bot", "crawl", "spider", "slurp", "curl/", "wget", "python-requests",
         "headless", "lighthouse", "monitor", "preview", "scanner", "facebookexternalhit")
VIEW_RETENTION_DAYS = 400


# Page views live in their own database file. They are written on every
# request, and the derived-stat cache invalidates whenever the league's
# database changes — so keeping them together meant every page view threw away
# the cache that every page depends on, and nothing was ever cached at all.
VIEWS_DB = str(Path(db.DB_PATH).with_name("site_views.db"))


def _views_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(VIEWS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_view_tables() -> None:
    with _views_conn() as conn:
        conn.executescript("""
            -- One row per page view. No IP address and no user agent are
            -- kept: `visitor` is a hash of them with a salt that changes
            -- daily, which counts a person once a day without recording who
            -- they are, and cannot be worked backwards once the day's salt is
            -- dropped.
            CREATE TABLE IF NOT EXISTS site_views (
                ts          TEXT NOT NULL,       -- ISO, UTC
                day         TEXT NOT NULL,       -- YYYY-MM-DD, for grouping
                path        TEXT NOT NULL,
                referrer    TEXT,                -- host only, never the full URL
                visitor     TEXT NOT NULL,
                account_id  INTEGER              -- NULL unless logged in
            );
            CREATE INDEX IF NOT EXISTS site_views_day ON site_views (day);

            -- The daily salt. Kept in the database rather than in memory so a
            -- restart doesn't split one person into two visitors, and deleted
            -- with the day it belongs to.
            CREATE TABLE IF NOT EXISTS site_view_salts (
                day   TEXT PRIMARY KEY,
                salt  TEXT NOT NULL
            );

        """)
    _migrate_views_out_of_main_db()


def _migrate_views_out_of_main_db() -> None:
    """Carry across what was recorded while views lived in the league's own
    database, then drop them from it. One-time; a no-op afterwards."""
    with _conn() as main:
        tables = {r[0] for r in main.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'site_view%'")}
        if not tables:
            return
        views = main.execute("SELECT ts, day, path, referrer, visitor, account_id "
                             "FROM site_views").fetchall() if "site_views" in tables else []
        salts = main.execute("SELECT day, salt FROM site_view_salts").fetchall() \
            if "site_view_salts" in tables else []
    with _views_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM site_views").fetchone()[0] == 0 and views:
            conn.executemany("INSERT INTO site_views (ts, day, path, referrer, visitor, account_id) "
                             "VALUES (?, ?, ?, ?, ?, ?)", [tuple(r) for r in views])
        conn.executemany("INSERT OR IGNORE INTO site_view_salts (day, salt) VALUES (?, ?)",
                         [tuple(r) for r in salts])
    with _conn() as main:
        for t in tables:
            main.execute(f"DROP TABLE IF EXISTS {t}")
    logger.info("moved %d page views into %s", len(views), VIEWS_DB)


def _visitor_salt(day: str) -> str:
    """The day's salt, made once and shared by every process."""
    with _views_conn() as conn:
        row = conn.execute("SELECT salt FROM site_view_salts WHERE day = ?", (day,)).fetchone()
        if row:
            return row["salt"]
        salt = secrets.token_hex(16)
        conn.execute("INSERT OR IGNORE INTO site_view_salts (day, salt) VALUES (?, ?)", (day, salt))
        row = conn.execute("SELECT salt FROM site_view_salts WHERE day = ?", (day,)).fetchone()
    return row["salt"]


def record_view(path: str, *, ip: str, user_agent: str, referrer: str,
                account_id: int | None) -> None:
    """Record one page view, if it looks like a person reading a page."""
    if any(path.startswith(p) for p in _NOT_A_PAGE):
        return
    ua = (user_agent or "").lower()
    if not ua or any(b in ua for b in _BOTS):
        return
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    visitor = hashlib.sha256(f"{_visitor_salt(day)}|{ip}|{user_agent}".encode()).hexdigest()[:16]
    host = ""
    if referrer:
        try:
            netloc = referrer.split("//", 1)[-1].split("/", 1)[0].lower()
            host = "" if netloc.endswith("adcl.gg") or "dota-bot.fly.dev" in netloc else netloc[:120]
        except Exception:
            host = ""
    with _views_conn() as conn:
        conn.execute("INSERT INTO site_views (ts, day, path, referrer, visitor, account_id) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (now.isoformat(timespec="seconds"), day, path[:200], host or None,
                      visitor, account_id))


def prune_views() -> None:
    """Drop views and salts past the retention window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=VIEW_RETENTION_DAYS)).date().isoformat()
    with _views_conn() as conn:
        conn.execute("DELETE FROM site_views WHERE day < ?", (cutoff,))
        conn.execute("DELETE FROM site_view_salts WHERE day < ?", (cutoff,))


def traffic(days: int = 30) -> dict:
    """Views and visitors by day, plus the busiest pages and where people came
    from, over the last `days` days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date().isoformat()
    with _views_conn() as conn:
        by_day = [dict(r) for r in conn.execute(
            "SELECT day, COUNT(*) AS views, COUNT(DISTINCT visitor) AS visitors "
            "FROM site_views WHERE day >= ? GROUP BY day ORDER BY day", (since,))]
        pages = [dict(r) for r in conn.execute(
            "SELECT path, COUNT(*) AS views, COUNT(DISTINCT visitor) AS visitors "
            "FROM site_views WHERE day >= ? GROUP BY path "
            "ORDER BY views DESC LIMIT 20", (since,))]
        referrers = [dict(r) for r in conn.execute(
            "SELECT referrer, COUNT(*) AS views, COUNT(DISTINCT visitor) AS visitors "
            "FROM site_views WHERE day >= ? AND referrer IS NOT NULL GROUP BY referrer "
            "ORDER BY views DESC LIMIT 15", (since,))]
        totals = conn.execute(
            "SELECT COUNT(*) AS views, COUNT(DISTINCT visitor) AS visitors, "
            "       COUNT(DISTINCT account_id) AS signed_in "
            "FROM site_views WHERE day >= ?", (since,)).fetchone()
        # Only counts what someone did while signed in, which is the only part
        # of the traffic that has a name attached to it.
        members = [dict(r) for r in conn.execute(
            "SELECT account_id, COUNT(*) AS views, COUNT(DISTINCT day) AS days, "
            "       MAX(ts) AS last_seen "
            "FROM site_views WHERE day >= ? AND account_id IS NOT NULL "
            "GROUP BY account_id ORDER BY last_seen DESC", (since,))]
        first = conn.execute("SELECT MIN(day) AS d FROM site_views").fetchone()["d"]
    # Days with no visits are still days; a gap in a chart should be a zero.
    seen = {d["day"]: d for d in by_day}
    start = datetime.fromisoformat(since).date()
    series = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        series.append(seen.get(d) or {"day": d, "views": 0, "visitors": 0})
    idents = player_identities([m["account_id"] for m in members])
    for m in members:
        ident = idents.get(m["account_id"]) or {}
        m["name"] = ident.get("name") or str(m["account_id"])
        m["avatar_url"] = ident.get("avatar_url")
    return {"days": days, "series": series, "pages": pages, "referrers": referrers,
            "members": members, "totals": dict(totals), "recording_since": first,
            "peak": max((d["views"] for d in series), default=0)}


def season_weeks(season: dict) -> list[dict]:
    """A season's explicitly-defined weeks, each with its dates as YYYY-MM-DD
    for a date input. Stored as unix seconds, UTC, the way the week boundary
    helper builds them."""
    out = []
    for w in db.get_season_weeks(season["id"], season["division_id"]):
        out.append({
            "week_number": w["week_number"],
            "start": datetime.fromtimestamp(w["start_ts"], timezone.utc).date().isoformat(),
            "end": datetime.fromtimestamp(w["end_ts"], timezone.utc).date().isoformat(),
        })
    return out


def set_season_week(season_id: int, division_id: int, week_number: int,
                    start: str, end: str) -> None:
    """Define one week by its dates, inclusive at both ends (ValueError on a
    bad or backwards range)."""
    try:
        s_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        e_dt = datetime.strptime(end, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("Dates must be YYYY-MM-DD.")
    if e_dt <= s_dt:
        raise ValueError("The end of a week has to come after its start.")
    db.upsert_season_week(season_id, division_id, week_number,
                          int(s_dt.timestamp()), int(e_dt.timestamp()))


def current_seasons() -> list[dict]:
    """The season each division is playing now: its active one, or failing
    that the newest that has started. One per division, newest first."""
    out: dict[int, dict] = {}
    seasons = all_seasons()
    for s in seasons:
        if s["status"] == "active":
            out.setdefault(s["division_id"], s)
    for s in seasons:
        if s["started"]:
            out.setdefault(s["division_id"], s)
    return sorted(out.values(), key=lambda s: (s["season_start"], s["id"]), reverse=True)


def team_stats(season: dict) -> dict[str, dict]:
    """{captain: per-game team stats} for a season: the bot's /team numbers.

    A game counts for a team when three of its five played one side, and the
    side's numbers are whoever was on it — a stand-in's kills are the team's
    kills that night. Per-player rates (GPM, XPM) are the five's average."""
    aggs = db.get_team_match_aggregates(season["id"], season["division_id"])
    for a in aggs.values():
        for k in ("gpm", "xpm"):
            if a.get(k) is not None:
                a[k] = a[k] / 5
    return aggs


def free_agents(season: dict) -> list[dict]:
    """Players not on any roster this season, best rating first — who a team
    can bring in. Someone drafted under any of their accounts is on a team,
    so the whole person is out, not just the account that was drafted.
    """
    sid, did = season["id"], season["division_id"]
    drafted = set(db.get_player_costs(sid, did))
    drafted |= {a for a in db.get_captain_account_ids(sid, did).values() if a}
    everyone = search_players()
    person = {p["account_id"]: (p.get("alt_account_for") or p["account_id"]) for p in everyone}
    drafted_people = {person.get(a, a) for a in drafted}
    out = [p for p in everyone if person[p["account_id"]] not in drafted_people]
    out.sort(key=lambda p: (-(p.get("rating") or -1), p["name"].lower()))
    return out


def alt_links() -> dict[int, int]:
    """{alt account: the account it belongs to}, cached against the database
    version. The query is trivial but the connection it needs isn't, and
    pages ask this once per division."""
    rows = db._derived(("alt_links",),
                       lambda: [{"alt": a, "main": m} for a, m in db.alt_links().items()])
    return {r["alt"]: r["main"] for r in rows}


def season_people(season: dict) -> set[int]:
    """Everyone in a division's season — drafted into it or captaining a
    team — as the account their alts resolve to.

    Cached against the database's version: resolving captains is a dozen
    name lookups apiece, and the front page asks this on every signed-in
    view.
    """
    sid, did = season["id"], season["division_id"]

    def build() -> list[dict]:
        aids = set(db.get_player_costs(sid, did))
        aids |= {a for a in db.get_captain_account_ids(sid, did).values() if a}
        links = alt_links()
        return [{"account_id": links.get(a, a)} for a in aids]

    return {r["account_id"] for r in db._derived(("season_people", sid, did), build)}


def plays_in(season: dict, account_id: int) -> bool:
    """Is this account's owner in the season? Someone drafted under any of
    their accounts is in it, so the whole person counts, not just the
    account they happened to log in on."""
    return alt_links().get(account_id, account_id) in season_people(season)


def season_champions(season: dict) -> dict | None:
    """Who won this season, or None if nobody has yet.

    A recorded result wins. Failing that, a finished playoff bracket names the
    team, and its roster comes from the draft — so a season that plays out its
    bracket here needs nobody to type anything in.
    """
    sid, did = season["id"], season["division_id"]
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT account_id, name FROM site_season_champions "
            "WHERE season_id = ? AND division_id = ? ORDER BY position", (sid, did))]
        meta = conn.execute(
            "SELECT champion_team FROM site_season_settings "
            "WHERE season_id = ? AND division_id = ?", (sid, did)).fetchone()
    if rows:
        out = {"team": (meta["champion_team"] if meta else None),
               "members": rows, "source": "recorded"}
    else:
        bracket = playoff_bracket(season)
        if not (bracket and bracket.get("champion")):
            return None
        team = next((t for t in season_teams(season) if t["captain"] == bracket["champion"]), None)
        if not team:
            return None
        out = {"team": team["name"], "source": "bracket",
               "members": [{"account_id": p["account_id"], "name": None} for p in
                           sorted(team["roster"], key=lambda p: -(p["cost"] or 0))]}
    # A name is only a fallback: whoever we can identify is shown as they are
    # known now, not as they were called the season they won.
    idents = player_identities([m["account_id"] for m in out["members"] if m["account_id"]])
    for m in out["members"]:
        ident = idents.get(m["account_id"]) or {}
        m["name"] = ident.get("name") or m["name"] or str(m["account_id"] or "")
        m["avatar_url"] = ident.get("avatar_url")
    return out


# Rings are drawn next to a name on nearly every page, so the counts are read
# once and kept until they change.
_champion_counts: dict[int, int] | None = None
_champion_counts_at = 0.0
_CHAMPION_TTL = 60.0


def champion_counts() -> dict[int, int]:
    """{account_id: seasons won}, for the ring after a player's name.

    Cached because a page asks for it once per name. The cache is this
    process's own, so it expires on a timer as well as on a write here —
    otherwise a change made on another machine would never show.
    """
    global _champion_counts, _champion_counts_at
    if _champion_counts is None or time.monotonic() - _champion_counts_at > _CHAMPION_TTL:
        _champion_counts_at = time.monotonic()
        with _conn() as conn:
            _champion_counts = {r["account_id"]: r["n"] for r in conn.execute(
                "SELECT account_id, COUNT(*) AS n FROM site_season_champions "
                "WHERE account_id IS NOT NULL GROUP BY account_id")}
    return _champion_counts


# The five voted awards, in the order they're announced, plus the All-AD
# Team, which isn't voted — it's the top average fantasy points per position.
AWARDS = [
    ("breakout", "Breakout Player of the Season"),
    ("support", "Support of the Season"),
    ("core", "Core of the Season"),
    ("leader", "Leader of the Season"),
    ("mvp", "Most Valuable Player"),
]
ALL_AD_TEAM = "all_ad_team"
AWARD_LABELS = dict(AWARDS) | {ALL_AD_TEAM: "All-AD Team"}


def season_awards(season: dict) -> list[dict]:
    """A season's awards in announcement order, each with its winners and the
    write-up that announced it. Winners carry an avatar where we know one."""
    rows = db.get_season_awards(season["id"], season["division_id"])
    if not rows:
        return []
    idents = player_identities([r["account_id"] for r in rows if r["account_id"]])
    by_award: dict[str, dict] = {}
    for r in rows:
        a = by_award.setdefault(r["award"], {
            "key": r["award"], "label": AWARD_LABELS.get(r["award"], r["award"]),
            "citation": r["citation"], "winners": [],
        })
        ident = idents.get(r["account_id"]) or {}
        a["winners"].append({"account_id": r["account_id"], "position": r["position"],
                             "name": ident.get("name") or r["name"],
                             "avatar_url": ident.get("avatar_url")})
    order = [k for k, _ in AWARDS] + [ALL_AD_TEAM]
    return [by_award[k] for k in order if k in by_award]


def set_season_award(season: dict, award: str, text: str, citation: str) -> int:
    """Record one award from pasted text, the way champions are recorded.
    Returns how many winners were stored; an empty paste clears the award."""
    winners = resolve_champion_names(text)
    db.set_season_award(season["id"], season["division_id"], award, winners,
                        (citation or "").strip() or None)
    return len(winners)


def hall_of_fame() -> list[dict]:
    """Every season that has a trophy or an award, newest first."""
    out = []
    for season in all_seasons():
        won, awards = season_champions(season), season_awards(season)
        if won or awards:
            out.append({"season": season, "champions": won, "awards": awards})
    return out


def all_champions() -> list[dict]:
    """Every season that has been won, newest first, with its winners."""
    out = []
    for season in all_seasons():
        won = season_champions(season)
        if won:
            out.append({"season": season, **won})
    return out


def set_season_champions(season_id: int, division_id: int, members: list[dict],
                         team: str = "") -> None:
    """Record (or, with an empty members list, clear) a season's winners."""
    global _champion_counts
    _champion_counts = None
    with _conn() as conn:
        conn.execute("DELETE FROM site_season_champions WHERE season_id = ? AND division_id = ?",
                     (season_id, division_id))
        conn.executemany(
            "INSERT INTO site_season_champions (season_id, division_id, position, account_id, name) "
            "VALUES (?, ?, ?, ?, ?)",
            [(season_id, division_id, i, m.get("account_id"), m["name"])
             for i, m in enumerate(members)])
        conn.execute("""
            INSERT INTO site_season_settings (season_id, division_id, champion_team, champion_note)
            VALUES (?, ?, ?, NULL)
            ON CONFLICT(season_id, division_id) DO UPDATE SET
                champion_team = excluded.champion_team, champion_note = NULL
        """, (season_id, division_id, team.strip() or None))


def _squash(name: str) -> str:
    """A name with its spacing and punctuation taken out, for matching only."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def resolve_champion_names(text: str) -> list[dict]:
    """Turn a pasted list of winners into {account_id, name} rows.

    Written for what a congratulations message looks like when it's pasted:
    names separated by @, which is the only separator that survives a name
    with a space in it ("grey poupon"). One per line, or comma-separated,
    works too. A bare number is an account id. A name is matched against
    everyone the site knows, exactly and ignoring case; anything left
    unmatched is kept as text, so a winner who never played here still shows.
    """
    tokens: list[str] = []
    for line in (text or "").replace("\r", "").split("\n"):
        # Anything before the first @ is the sentence the names were in
        # ("Congratulations to Season 38 winners!!"), not a winner.
        parts = line.split("@")[1:] if "@" in line else line.split(",")
        tokens += [p.strip(" \t,.!") for p in parts]
    tokens = [t for t in tokens if t]
    if not tokens:
        return []
    known: dict[str, set] = {}
    squashed: dict[str, set] = {}
    for p in search_players():
        name = p["name"].strip()
        known.setdefault(name.lower(), set()).add(p["account_id"])
        squashed.setdefault(_squash(name), set()).add(p["account_id"])

    def match(token: str) -> int | None:
        # Two people can be called the same thing — there are two Space
        # Cowboys — and a name can move between them, so a name that fits
        # more than one account resolves to none of them. Better an
        # unresolved winner the notice names than a confident wrong one.
        for index in (known, squashed):
            # A Discord handle drops the spaces an in-game name keeps, so
            # "LovesMuffins" finds "loves muffins" on the second pass.
            hit = index.get(token.lower() if index is known else _squash(token)) or set()
            if len(hit) == 1:
                return next(iter(hit))
            if hit:
                return None          # ambiguous: don't fall through and guess
        return None

    out = []
    for t in tokens:
        out.append({"account_id": int(t) if t.isdigit() else match(t), "name": t})
    return out


def season_of_match(seasons: list[dict], match: dict) -> dict | None:
    """The season (in its division) a match was played in."""
    return next((s for s in seasons
                 if s["id"] == match["league_id"] and s["division_id"] == match["division_id"]), None)


# ---------------------------------------------------------------------------
# Signups
# ---------------------------------------------------------------------------

# What someone is in a season. A captain drafts a team; a player is in the
# draft to be picked; a stand-in isn't drafted at all and is only on record
# as someone who can fill in. People pick their own, and an admin's choice
# wins — a season needs a particular number of captains, and wanting to be
# one isn't the same as being given one.
SIGNUP_LEVELS = [
    ("captain", "Captain"),
    ("player", "Player"),
    ("standin", "Stand-in"),
]
SIGNUP_LEVEL_LABELS = dict(SIGNUP_LEVELS)
DEFAULT_SIGNUP_LEVEL = "player"


def signup_level(row: dict) -> str:
    """What a signup counts as: the admin's decision, or theirs."""
    return (row.get("level_override") or row.get("level") or DEFAULT_SIGNUP_LEVEL)


def set_signup_level(season: dict, account_id: int, level: str | None) -> None:
    """An admin's decision about one signup. None clears it, leaving whatever
    the player asked for."""
    if level is not None and level not in SIGNUP_LEVEL_LABELS:
        raise ValueError(f"unknown signup level {level!r}")
    with _conn() as conn:
        conn.execute("UPDATE site_signups SET level_override = ?, updated_at = ? "
                     "WHERE season_id = ? AND division_id = ? AND account_id = ?",
                     (level, _now(), season["id"], season["division_id"], account_id))


def get_signup(season: dict, account_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM site_signups WHERE season_id = ? AND division_id = ? AND account_id = ?",
            (season["id"], season["division_id"], account_id)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["level_is"] = signup_level(out)
    return out


def save_signup(season: dict, account_id: int, *, mmr: int | None, positions: list[int],
                captain: str, discord: str | None, statement: str | None,
                level: str = DEFAULT_SIGNUP_LEVEL) -> None:
    """Record a signup. `level` is what they signed up as; an admin's override
    is left alone, so editing your own signup can't undo their decision."""
    if level not in SIGNUP_LEVEL_LABELS:
        level = DEFAULT_SIGNUP_LEVEL
    now = _now()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO site_signups (season_id, division_id, account_id, mmr, positions,
                                      captain, level, discord, statement, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season_id, division_id, account_id) DO UPDATE SET
                mmr = excluded.mmr, positions = excluded.positions, captain = excluded.captain,
                level = excluded.level, discord = excluded.discord,
                statement = excluded.statement, updated_at = excluded.updated_at
        """, (season["id"], season["division_id"], account_id, mmr, ",".join(map(str, positions)),
              captain, level, discord, statement, now, now))


def delete_signup(season: dict, account_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM site_signups WHERE season_id = ? AND division_id = ? AND account_id = ?",
                     (season["id"], season["division_id"], account_id))


def list_signups(season: dict) -> list[dict]:
    """Signups in signup order, with each player's display name and badge."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT s.*, u.steam_name, u.avatar_url
            FROM site_signups s LEFT JOIN site_users u ON u.account_id = s.account_id
            WHERE s.season_id = ? AND s.division_id = ?
            ORDER BY s.created_at
        """, (season["id"], season["division_id"])).fetchall()
    out = [dict(r) for r in rows]
    idents = player_identities([r["account_id"] for r in out])
    for r in out:
        ident = idents.get(r["account_id"], {})
        r["name"] = ident.get("name") or r["steam_name"] or str(r["account_id"])
        r["badge"] = ident.get("badge")
        r["positions"] = [int(p) for p in r["positions"].split(",") if p]
        r["level_is"] = signup_level(r)
        r["level_label"] = SIGNUP_LEVEL_LABELS[r["level_is"]]
    return out


def signed_up_seasons(account_id: int) -> list[tuple[int, int]]:
    """(season_id, division_id) for every season this player signed up for."""
    with _conn() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT season_id, division_id FROM site_signups WHERE account_id = ?", (account_id,))]


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------

def player_identities(account_ids: list[int]) -> dict[int, dict]:
    """{account_id: {name, avatar_url, badge}}, shown the way the bot shows
    them: name from db.get_display_names, avatar and badge from the rating
    cache with /set_player overrides applied. Someone the bot has never seen
    falls back to their Steam name from logging in to the site."""
    from formatters import format_badge
    if not account_ids:
        return {}
    ids = list(set(account_ids))
    names = db.get_display_names(ids)
    out: dict[int, dict] = {aid: {"name": names.get(aid), "avatar_url": None, "badge": None} for aid in ids}
    with _conn() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT account_id, steam_name, avatar_url FROM site_users WHERE account_id IN ({marks})", chunk):
                o = out[r["account_id"]]
                o["name"] = o["name"] or r["steam_name"]
                o["avatar_url"] = r["avatar_url"]
            for r in conn.execute(f"""
                SELECT account_id, avatar_url, rank_tier, leaderboard_rank
                FROM player_ratings_cache WHERE account_id IN ({marks})""", chunk):
                o = out[r["account_id"]]
                o["avatar_url"] = r["avatar_url"] or o["avatar_url"]
                if r["rank_tier"]:
                    o["badge"] = format_badge(r["rank_tier"], r["leaderboard_rank"])
            for r in conn.execute(f"""
                SELECT account_id, hide_avatar, rank_tier, leaderboard_rank
                FROM skill_overrides WHERE account_id IN ({marks})""", chunk):
                o = out[r["account_id"]]
                if r["hide_avatar"]:
                    o["avatar_url"] = None
                if r["rank_tier"] is not None:
                    o["badge"] = format_badge(r["rank_tier"], r["leaderboard_rank"])
    return out


def search_players(q: str = "", limit: int | None = None) -> list[dict]:
    """Everyone who has played a league match or logged in, filtered by name.

    The gathering is cached on the database's own version (see db._derived);
    the filtering is per call, since it is a string compare over a list that
    is already in memory.
    """
    rows = db._derived(("site_player_index",), _player_index)
    ql = q.strip().lower()
    out = [r for r in rows if not ql or ql in r["name"].lower() or ql == str(r["account_id"])]
    return out[:limit] if limit else out


def _player_index() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT account_id, COUNT(*) AS games, SUM(won) AS wins, MAX(match_id) AS last_match
            FROM players WHERE account_id > 0   -- 0 is every private-profile player at once
            GROUP BY account_id
        """).fetchall()
        users = [r["account_id"] for r in conn.execute("SELECT account_id FROM site_users")]
    stats = {r["account_id"]: dict(r) for r in rows}
    for aid in users:
        stats.setdefault(aid, {"account_id": aid, "games": 0, "wins": 0, "last_match": 0})
    idents = player_identities(list(stats))
    ratings = player_ratings()
    out = []
    for aid, s in stats.items():
        s.update(idents.get(aid, {}))
        s["name"] = s["name"] or str(aid)
        s["rating"] = ratings.get(aid)
        out.append(s)
    out.sort(key=lambda s: (-s["games"], s["name"].lower()))
    return out


def player_matches(account_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT p.*, m.start_time, m.division_id, m.league_id, m.duration AS match_duration,
                   m.radiant_score, m.dire_score
            FROM players p JOIN matches m ON m.match_id = p.match_id
            WHERE p.account_id = ? AND m.excluded = 0
            ORDER BY m.start_time DESC
        """, (account_id,)).fetchall()
    return [dict(r) for r in rows]


def aggregate(rows: list[dict]) -> dict:
    """Aggregate of per-match player rows, with fantasy points computed the
    way the bot computes them. For totals the bot has no view of (a
    player's career across divisions); per-season numbers come from
    db.get_all_time_stats so they match the bot exactly."""
    from fantasy import calculate_fantasy_points
    n = len(rows)
    if not n:
        return {"games_played": 0}
    avg = lambda k: sum((r.get(k) or 0) for r in rows) / n
    agg = {
        "games_played": n,
        "wins": sum(r["won"] for r in rows),
        "total_kills": sum(r["kills"] for r in rows),
        "total_deaths": sum(r["deaths"] for r in rows),
        "total_assists": sum(r["assists"] for r in rows),
        "avg_duration": sum(r.get("match_duration") or r.get("duration") or 0 for r in rows) / n,
    }
    for k in ("gpm", "xpm", "last_hits", "denies", "hero_damage", "hero_healing",
              "obs_placed", "sen_placed", "observer_kills", "sentry_kills", "tower_kills",
              "roshans_killed", "firstblood_claimed", "teamfight_participation", "stuns",
              "camps_stacked", "rune_pickups", "defensive_item_uses", "building_damage"):
        agg[k] = avg(k)
    agg["kda"] = (agg["total_kills"] + agg["total_assists"]) / max(agg["total_deaths"], 1)
    agg["fantasy_points"] = calculate_fantasy_points(agg)
    return agg


# ---------------------------------------------------------------------------
# Season pages
# ---------------------------------------------------------------------------

def season_leaders(season: dict) -> tuple[list[dict], int, int]:
    """The season's leaderboard, best fantasy points first: the same rows
    the bot's season-scoped stats come from, qualified the way /leaderboard
    qualifies them (db.qualified). Returns (rows, threshold, max_games)."""
    rows, threshold, max_games = db.qualified(db.ranked(db.get_all_time_stats(season["division_id"], season["id"])))
    return sorted(rows, key=lambda r: -r["fantasy_points"]), threshold, max_games


# Stats that only mean anything against one draft, so they stay in a season
# and skip the games cut: a deserved cost or an attendance rate is measured
# from the draft, not from the games.
DRAFT_RELATIVE = {"value", "attendance"}

# How many games it takes to appear on the leaderboard. A flat number rather
# than a share of the most active player's: that moved every time anyone
# played, and late in a season it climbed high enough to hide half the league.
DEFAULT_MIN_GAMES = 10


def fantasy_table(season: dict | None, stat: str = "fantasy_points", week: int | None = None,
                  position: int | None = None, min_games: int | None = None,
                  only_divisions: list[dict] | None = None) -> dict:
    """The leaderboard the bot's /leaderboard shows, with every player's
    fantasy score broken into the components that made it and each component
    set against the field's average — the part that answers "why is she top?".

    season=None pools every season; week picks one of that season's weeks.
    min_games keeps a stand-in's one good night off the top of the table, and
    defaults to DEFAULT_MIN_GAMES; 0 shows everyone. A draft-relative stat
    ignores it, since being drafted is the qualification there.
    """
    import fantasy

    if season and week is not None:
        rows = db.get_stats_for_season_week(season["id"], season["division_id"], week)
    elif season:
        # That season's matches, not every season's. cross_season exists for
        # the bot's "week -1 means all-time" case; here, choosing a season is
        # the whole point of choosing it.
        rows = db.ranked(db.get_all_time_stats(season["division_id"], season["id"]))
    else:
        rows = []
        for d in (only_divisions if only_divisions is not None else divisions()):
            rows += db.ranked(db.get_all_time_stats(d["id"], None))
    if position:
        rows = [r for r in rows if r.get("role_position") == position]
    if stat in DRAFT_RELATIVE:
        rows = [r for r in rows if r.get("cost") is not None]

    max_games = max((r.get("games_played") or 0 for r in rows), default=0)
    if stat in DRAFT_RELATIVE:
        threshold = 0
    else:
        threshold = DEFAULT_MIN_GAMES if min_games is None else min_games
    qualified = [r for r in rows if (r.get("games_played") or 0) >= threshold]
    # Early in a season nobody has ten games yet, and a leaderboard showing
    # nothing is worse than one showing everyone with a line saying why the
    # filter was dropped. The number asked for is named in that line, so this
    # is visible rather than silent.
    asked = threshold
    relaxed = bool(rows) and not qualified
    if relaxed:
        threshold = 0
        qualified = list(rows)

    for r in rows:
        r["breakdown"] = fantasy.breakdown(r)
        r["qualifies"] = (r.get("games_played") or 0) >= threshold
    # The field's average for each component, so a player's row can be read as
    # "ahead here, behind there" rather than as twenty-one bare numbers.
    field: dict[str, dict] = {}
    for r in qualified:
        for c in r["breakdown"]:
            f = field.setdefault(c["key"], {"label": c["label"], "unit": c["unit"],
                                            "weight": c["weight"], "value": 0.0, "points": 0.0})
            f["value"] += c["value"]; f["points"] += c["points"]
    for f in field.values():
        f["value"] /= max(len(qualified), 1)
        f["points"] /= max(len(qualified), 1)

    # Everyone gets a name and a comparison, so that looking up a player who
    # hasn't played enough to rank still answers the question — the ranking is
    # what the games cut is for, not the explanation.
    idents = player_identities([r["account_id"] for r in rows])
    for r in rows:
        r["name"] = idents.get(r["account_id"], {}).get("name") or str(r["account_id"])
        r["avatar_url"] = idents.get(r["account_id"], {}).get("avatar_url")
        for c in r["breakdown"]:
            c["vs_field"] = c["points"] - field.get(c["key"], {}).get("points", 0.0)
    # Sorted and written the way the bot sorts and writes them, so the two
    # leaderboards agree about what "top" means.
    from formatters import _sort_key, format_stat_value
    qualified.sort(key=lambda r: _sort_key(r, stat), reverse=True)
    for r in qualified:
        r["stat_text"] = format_stat_value(r, stat)
    qualified = [r for r in qualified if r["stat_text"] is not None]
    return {"rows": qualified, "everyone": rows, "field": field, "threshold": threshold,
            "max_games": max_games, "weights": fantasy.WEIGHTS, "stat": stat,
            "relaxed": relaxed, "asked_min_games": asked,
            "default_min_games": DEFAULT_MIN_GAMES}


def player_ratings() -> dict[int, int]:
    """{account_id: displayed internal rating} for everyone, as the bot
    shows it (db.displayed_ratings) in the division they last played in."""
    by_division = {d["id"]: db.displayed_ratings(d["id"]) for d in divisions()}
    with _conn() as conn:
        last_division = {r[0]: r[1] for r in conn.execute("""
            SELECT p.account_id, m.division_id FROM players p JOIN matches m USING (match_id)
            JOIN (SELECT account_id, MAX(match_id) AS last FROM players GROUP BY account_id) l
              ON l.account_id = p.account_id AND l.last = p.match_id""")}
    out: dict[int, int] = {}
    for ratings in by_division.values():
        for aid, r in ratings.items():
            out.setdefault(aid, r)
    for aid, did in last_division.items():
        if aid in by_division.get(did, {}):
            out[aid] = by_division[did][aid]
    return out


def season_matches(season: dict) -> list[dict]:
    """Matches in the season, newest first, with each side's team (by captain)."""
    where, params = db.season_scope(season["id"], season["division_id"]).where("m")
    with _conn() as conn:
        matches = [dict(r) for r in conn.execute(
            f"SELECT m.* FROM matches m WHERE {where} ORDER BY m.start_time DESC", params)]
    caps = db.get_match_team_captains(season["id"], season["division_id"],
                                      [m["match_id"] for m in matches])
    for m in matches:
        m["radiant_team"] = caps.get(m["match_id"], {}).get("radiant")
        m["dire_team"] = caps.get(m["match_id"], {}).get("dire")
    return matches


def season_teams(season: dict, matches: list[dict] | None = None) -> list[dict]:
    """Teams from the season's draft (player_costs), with identity and a
    win/loss record counted from matches both sides can be attributed in."""
    sid, did = season["id"], season["division_id"]
    costs = db.get_player_costs(sid, did)
    identity = db.get_season_teams(sid, did)
    cap_aids = db.get_captain_account_ids(sid, did)
    teams: dict[str, dict] = {}
    for aid, c in costs.items():
        cap = c.get("captain")
        if not cap:
            continue
        t = teams.setdefault(cap, {"captain": cap, "roster": [], "wins": 0, "losses": 0})
        t["roster"].append({"account_id": aid, "cost": c.get("cost")})
    for cap, t in teams.items():
        # Priciest first; a player who joined without a draft price goes last.
        t["roster"].sort(key=lambda p: (p["cost"] is None, -(p["cost"] or 0)))
        ident = identity.get(cap.lower(), {})
        t["name"] = ident.get("team_name") or cap
        t["logo_url"] = ident.get("logo_url")
        t["captain_account_id"] = cap_aids.get(cap)
    if matches is None:
        matches = season_matches(season)
    for m in matches:
        rad, dire = m["radiant_team"], m["dire_team"]
        if not rad or not dire or rad == dire:
            continue
        winner, loser = (rad, dire) if m["radiant_win"] else (dire, rad)
        if winner in teams:
            teams[winner]["wins"] += 1
        if loser in teams:
            teams[loser]["losses"] += 1
        for side, other in ((rad, dire), (dire, rad)):
            if side in teams:
                teams[side].setdefault("opponents", set()).add(other)

    # Buchholz: the combined wins of everyone you've played. Two teams on the
    # same record aren't level — the one who got there against the teams who
    # went on to win is ahead, and that's what the league breaks ties on.
    for t in teams.values():
        t["opponents"] = sorted(t.get("opponents", set()))
        t["buchholz"] = sum(teams[o]["wins"] for o in t["opponents"] if o in teams)
    return sorted(teams.values(),
                  key=lambda t: (-t["wins"], t["losses"], -t["buchholz"], t["name"].lower()))


MATCHES_PER_PAGE = 50


def search_matches(*, player: str = "", season: int | None = None, division: int | None = None,
                   kind: str = "", date_from: str = "", date_to: str = "",
                   min_minutes: int | None = None, max_minutes: int | None = None,
                   page: int = 1, per_page: int = MATCHES_PER_PAGE) -> dict:
    """Matches matching the filters, newest first, one page at a time.

    `player` is a name or an account id and narrows to matches that player
    was in, which also means each row can carry what they did in it. `kind`
    is one of "league" (part of a season), "other" (played outside one) or
    "manual" (entered by hand, which is what a negative match id means).

    Returns {rows, total, page, pages, per_page}.
    """
    where, params = ["1=1"], []
    account_id = None
    if player.strip():
        from player_api import account_id_from
        account_id = account_id_from(player)
        if account_id is None:
            names = [p["account_id"] for p in search_players(player)]
            if not names:
                return {"rows": [], "total": 0, "page": 1, "pages": 1, "per_page": per_page,
                        "unknown_player": player}
            account_id = names[0] if len(names) == 1 else None
            if account_id is None:
                where.append(f"m.match_id IN (SELECT match_id FROM players WHERE account_id IN "
                             f"({','.join('?' * len(names))}))")
                params += names
        if account_id is not None:
            where.append("m.match_id IN (SELECT match_id FROM players WHERE account_id = ?)")
            params.append(account_id)
    if season:
        where.append("m.league_id = ?"); params.append(season)
    if division:
        where.append("m.division_id = ?"); params.append(division)
    if kind == "league":
        where.append("m.league_id > 0 AND m.match_id > 0 AND m.excluded = 0")
    elif kind == "other":
        where.append("m.league_id = 0 AND m.match_id > 0 AND m.excluded = 0")
    elif kind == "manual":
        where.append("m.match_id < 0 AND m.excluded = 0")
    elif kind == "excluded":
        where.append("m.excluded = 1")
    else:
        where.append("m.excluded = 0")
    for value, op in ((date_from, ">="), (date_to, "<=")):
        if value:
            try:
                when = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=LEAGUE_TZ)
            except ValueError:
                continue
            if op == "<=":
                when = when.replace(hour=23, minute=59, second=59)
            where.append(f"m.start_time {op} ?"); params.append(int(when.timestamp()))
    if min_minutes:
        where.append("m.duration >= ?"); params.append(min_minutes * 60)
    if max_minutes:
        where.append("m.duration <= ?"); params.append(max_minutes * 60)

    clause = " AND ".join(where)
    page = max(1, page)
    with _conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM matches m WHERE {clause}", params).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            f"SELECT m.* FROM matches m WHERE {clause} ORDER BY m.start_time DESC, m.match_id DESC "
            f"LIMIT ? OFFSET ?", params + [per_page, (page - 1) * per_page])]
        if account_id is not None and rows:
            marks = ",".join("?" * len(rows))
            mine = {r["match_id"]: dict(r) for r in conn.execute(
                f"SELECT match_id, hero_id, team_side, kills, deaths, assists, gpm, won "
                f"FROM players WHERE account_id = ? AND match_id IN ({marks})",
                [account_id] + [r["match_id"] for r in rows])}
            for r in rows:
                r["me"] = mine.get(r["match_id"])

    # Teams, season and division labels, so a row reads without a lookup.
    seasons = all_seasons()
    div_names = {d["id"]: d["name"] for d in divisions()}
    caps = db.get_match_team_captains_for_matches(rows) if rows else {}
    for r in rows:
        r["season"] = season_of_match(seasons, r)
        r["division_name"] = div_names.get(r["division_id"])
        sides = caps.get(r["match_id"], {})
        r["radiant_team"], r["dire_team"] = sides.get("radiant"), sides.get("dire")
        r["by_hand"] = r["match_id"] < 0
        r["excluded"] = bool(r.get("excluded"))
        if r.get("me"):
            r["me"]["hero"] = hero(r["me"]["hero_id"])
    pages = max(1, -(-total // per_page))
    return {"rows": rows, "total": total, "page": min(page, pages), "pages": pages,
            "per_page": per_page, "account_id": account_id}


def rating_breakdown(account_id: int) -> dict | None:
    """How this player's rating was arrived at — the same inputs and the same
    explanation string the bot's /lookup prints, for showing an admin.

    Built from the cache, so the page costs no API calls: every input a rating
    is computed from is stored alongside it, and re-resolving them reproduces
    the number (rating_compute.inputs_from_cache).
    """
    import rating_compute
    from formatters import format_badge

    row = db.get_rating_cache_row(account_id)
    if not row:
        return None
    override = db.get_skill_override(account_id) or {}
    inputs = rating_compute.inputs_from_cache(account_id)
    resolved = rating_compute.resolve(inputs, override)
    windrun_used = rating_compute.trusted_windrun(row.get("raw_windrun"), row.get("windrun_games"))
    age = rating_compute.age_in_days(account_id)
    return {
        "rating": row.get("internal_rating"),
        "own_rating": row.get("own_rating"),
        "group_rating": row.get("group_rating"),
        "group_explanation": row.get("group_explanation"),
        "explanation": (resolved[1] if resolved else None) or row.get("explanation"),
        "reproduced": bool(resolved) and resolved[0] == row.get("own_rating"),
        "windrun": row.get("raw_windrun"),
        "windrun_games": row.get("windrun_games"),
        "windrun_counts": windrun_used is not None,
        "min_windrun_games": __import__("formatters").MIN_WINDRUN_GAMES,
        "badge": format_badge(row.get("rank_tier"), row.get("leaderboard_rank"))
                 if row.get("rank_tier") else None,
        "ad_last_year": row.get("ad_last_year"),
        "ad_all_time": row.get("ad_all_time"),
        "ranked_last_year": row.get("ranked_last_year"),
        "ranked_all_time": row.get("ranked_all_time"),
        "history_hidden": bool(row.get("fh_unavailable")),
        "updated_at": row.get("updated_at"),
        "age_days": round(age, 1) if age is not None else None,
        "stale": age is not None and age >= rating_compute.MAX_AGE_DAYS,
        "linked_accounts": [a for a in (row.get("linked_accounts") or []) if a != account_id],
        "overrides": {k: override[k] for k in
                      ("windrun_rating", "rank_tier", "leaderboard_rank",
                       "internal_rating_override", "nickname")
                      if override.get(k) is not None},
        "override_by": override.get("set_by_name"),
    }


def playoff_bracket(season: dict) -> dict | None:
    """The bracket, filled in as far as the games have decided it.

    Only the first round's seats are stored; every round after is worked out
    from the results, so a bracket can't disagree with the games it came from.
    A series is whichever pair are seated against each other, counting the
    games they've played since playoffs started, first to best_of // 2 + 1.

    Returns {size, best_of, started_at, rounds: [[match, ...], ...], champion,
    unplaced, teams} where a match is {a, b, a_name, b_name, a_wins, b_wins,
    winner, decided, match_ids, round, idx}.
    """
    bracket = db.get_playoff_bracket(season["id"], season["division_id"])
    if not bracket:
        return None
    matches = season_matches(season)
    teams = {t["captain"]: t for t in season_teams(season, matches)}
    started_ts = _t_iso(bracket["started_at"])

    # Games between a pair since playoffs began, newest last.
    played: dict[frozenset, list[dict]] = {}
    for m in matches:
        rad, dire = m["radiant_team"], m["dire_team"]
        if not rad or not dire or rad == dire:
            continue
        if m["start_time"] < started_ts:      # a regular-season game between the pair
            continue
        played.setdefault(frozenset((rad, dire)), []).append(m)

    needed = bracket["best_of"] // 2 + 1

    def series(a: str | None, b: str | None) -> dict:
        games = played.get(frozenset((a, b)), []) if a and b else []
        a_wins = sum(1 for m in games
                     if (m["radiant_team"] == a) == bool(m["radiant_win"]))
        b_wins = len(games) - a_wins
        winner = a if a_wins >= needed else (b if b_wins >= needed else None)
        return {"a": a, "b": b,
                "a_name": teams[a]["name"] if a in teams else a,
                "b_name": teams[b]["name"] if b in teams else b,
                "a_wins": a_wins, "b_wins": b_wins, "winner": winner,
                "decided": winner is not None,
                "match_ids": [m["match_id"] for m in games]}

    rounds, seats = [], list(bracket["seats"])
    while len(seats) >= 2:
        row = []
        for i in range(0, len(seats), 2):
            m = series(seats[i], seats[i + 1])
            m["round"], m["idx"] = len(rounds) + 1, i // 2
            row.append(m)
        rounds.append(row)
        seats = [m["winner"] for m in row]        # the next round is the winners
    champion = seats[0] if seats else None

    placed = {c for c in bracket["seats"] if c}
    bracket.update({
        "rounds": rounds,
        "champion": champion,
        "champion_name": teams[champion]["name"] if champion in teams else champion,
        "needed": needed,
        "teams": [{"captain": t["captain"], "name": t["name"], "wins": t["wins"],
                   "losses": t["losses"], "buchholz": t["buchholz"]} for t in teams.values()],
        "unplaced": [{"captain": t["captain"], "name": t["name"]}
                     for t in teams.values() if t["captain"] not in placed],
        "complete": all(m["decided"] for r in rounds for m in r),
    })
    return bracket


def _t_iso(iso: str) -> int:
    """An ISO timestamp as unix seconds, for comparing with a match's."""
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp())


def round_name(round_number: int, total_rounds: int) -> str:
    """Final, Semi-finals, Quarter-finals, then Round of N."""
    from_end = total_rounds - round_number
    if from_end == 0:
        return "Final"
    if from_end == 1:
        return "Semi-finals"
    if from_end == 2:
        return "Quarter-finals"
    return f"Round of {2 ** (from_end + 1)}"


def weeks_with_games(season: dict) -> list[dict]:
    """The season's weeks that actually have games in them.

    A season is created with its full run of weeks, so offering all of them
    as filters means most of the list returns nothing — and a week emptied by
    taking its games out shouldn't be offered either, which is why this counts
    matches rather than reading the calendar.
    """
    weeks = db.get_season_weeks(season["id"], season["division_id"])
    if not weeks:
        return []
    starts = [m["start_time"] for m in season_matches(season)]
    return [w for w in weeks
            if any(w["start_ts"] <= t <= w["end_ts"] for t in starts)]


def season_matchups(season: dict) -> list[dict]:
    """Who plays whom, week by week.

    A week that has been played reports its real pairings, read from the
    matches. The next one is a Swiss pairing: teams are taken in standings
    order and paired with the nearest team they haven't met yet, so equal
    records meet and nobody plays the same side twice while there's anyone
    left.

    The week being played comes first, then the completed ones newest first.
    Weeks after the next turn on results nobody has yet, so they aren't
    returned at all — an empty row for each of them was only ever noise.

    Returns [{week, label, dates, played, pairs: [{a, b, a_name, b_name,
    a_record, b_record, winner, match_ids, rematch, bye}]}].
    """
    matches = season_matches(season)
    teams = season_teams(season, matches)
    by_captain = {t["captain"]: t for t in teams}
    weeks = db.get_season_weeks(season["id"], season["division_id"])

    def team_name(cap: str | None) -> str:
        return by_captain[cap]["name"] if cap in by_captain else (cap or "Stand-ins")

    def record(cap: str | None) -> str:
        t = by_captain.get(cap)
        return f"{t['wins']}–{t['losses']}" if t else ""

    def tiebreak(cap: str | None) -> int | None:
        t = by_captain.get(cap)
        return t["buchholz"] if t else None

    # Which pairs have met, and what each week's games were.
    met: set[frozenset] = set()
    per_week: dict[int, dict[frozenset, dict]] = {}
    for m in matches:
        rad, dire = m["radiant_team"], m["dire_team"]
        if not rad or not dire or rad == dire:
            continue
        met.add(frozenset((rad, dire)))
        week = next((w["week_number"] for w in weeks
                     if w["start_ts"] <= m["start_time"] <= w["end_ts"]), None)
        if week is None:
            continue
        pair = per_week.setdefault(week, {}).setdefault(
            frozenset((rad, dire)), {"a": rad, "b": dire, "wins": {rad: 0, dire: 0}, "match_ids": []})
        pair["match_ids"].append(m["match_id"])
        pair["wins"][rad if m["radiant_win"] else dire] += 1

    def _pair(order: tuple[str, ...], budget: int) -> list[tuple[str, str, bool]] | None:
        """Pair everyone, using at most `budget` rematches. Taking the first
        free-looking opponent for each team in turn can strand the last two
        having already played, when a different assignment higher up would
        have left nobody stranded — so this backs out and tries again rather
        than declaring a rematch unavoidable."""
        if not order:
            return []
        a, rest = order[0], order[1:]
        for i, b in enumerate(rest):
            rematch = frozenset((a, b)) in met
            if rematch and budget == 0:
                continue
            tail = _pair(rest[:i] + rest[i + 1:], budget - (1 if rematch else 0))
            if tail is not None:
                return [(a, b, rematch)] + tail
        return None

    def swiss(order: list[str]) -> list[dict]:
        """The next round: strongest against strongest, nobody replaying
        anyone they've already had unless the field leaves no choice."""
        bye = None
        if len(order) % 2:
            # The bye goes as low as it can while still leaving a pairing the
            # rest of the field can take without rematches.
            for candidate in reversed(order):
                rest = tuple(t for t in order if t != candidate)
                if _pair(rest, 0) is not None:
                    bye, order = candidate, list(rest)
                    break
            else:
                bye, order = order[-1], order[:-1]
        for budget in range(0, len(order) // 2 + 1):
            found = _pair(tuple(order), budget)
            if found is not None:
                break
        pairs = [{"a": a, "b": b, "a_name": team_name(a), "b_name": team_name(b),
                  "a_record": record(a), "b_record": record(b),
                  "a_tiebreak": tiebreak(a), "b_tiebreak": tiebreak(b), "winner": None,
                  "match_ids": [], "rematch": rematch, "bye": False}
                 for a, b, rematch in (found or [])]
        if bye:
            pairs.append({"a": bye, "b": None, "a_name": team_name(bye), "b_name": None,
                          "a_record": record(bye), "b_record": "", "winner": None,
                          "match_ids": [], "rematch": False, "bye": True})
        return pairs

    standings = [t["captain"] for t in teams]     # season_teams sorts by record already
    last_played = max((w["week_number"] for w in weeks if per_week.get(w["week_number"])),
                      default=None)
    completed, upcoming = [], None
    for w in weeks:
        played = per_week.get(w["week_number"], {})
        if played:
            pairs = []
            for p in played.values():
                a, b = p["a"], p["b"]
                winner = a if p["wins"][a] > p["wins"][b] else (b if p["wins"][b] > p["wins"][a] else None)
                pairs.append({"a": a, "b": b, "a_name": team_name(a), "b_name": team_name(b),
                              "a_score": p["wins"][a], "b_score": p["wins"][b],
                              "a_record": record(a), "b_record": record(b), "winner": winner,
                              "match_ids": p["match_ids"], "rematch": False, "bye": False})
            pairs.sort(key=lambda p: p["a_name"].lower())
            completed.append({"week": w["week_number"], "label": f"Week {w['week_number']}",
                              "dates": w, "played": True, "pairs": pairs})
        elif (upcoming is None and len(standings) > 1
              and (last_played is None or w["week_number"] > last_played)):
            upcoming = {"week": w["week_number"], "label": f"Week {w['week_number']}",
                        "dates": w, "played": False, "pairs": swiss(standings)}
    return ([upcoming] if upcoming else []) + completed[::-1]


def team_of(season: dict, account_id: int) -> dict | None:
    """The team a player was on in a season: drafted onto it, or its captain."""
    for t in season_teams(season, matches=[]):
        if t.get("captain_account_id") == account_id or any(p["account_id"] == account_id for p in t["roster"]):
            return t
    return None


def set_team_identity(season: dict, team: dict, *, name: str | None = None,
                      logo_url: str | None = None) -> None:
    """Rename a team and/or give it a new logo. It's the bot's season_teams
    row, so Discord's cards show the change too."""
    db.upsert_season_teams(season["id"], season["division_id"], [{
        "captain": team["captain"],
        "team_name": name or team["name"],
        "logo_url": logo_url,          # None keeps the current logo
    }])


def match_detail(match_id: int) -> dict | None:
    with _conn() as conn:
        m = conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
        if not m:
            return None
        players = [dict(r) for r in conn.execute(
            "SELECT * FROM players WHERE match_id = ? ORDER BY player_slot, id", (match_id,))]
    m = dict(m)
    m["server"] = server_name(m["cluster"])
    draft = db.get_match_draft(match_id)
    idents = player_identities([p["account_id"] for p in players])
    for p in players:
        p["display_name"] = idents.get(p["account_id"], {}).get("name") or p["name"]
        p["hero"] = hero(p["hero_id"])
        p["draft"] = [draft_pick(i) for i in draft.get(p["account_id"], [])]
        p["match_duration"] = m["duration"]
        p["fantasy_points"] = db._per_match_fp(p)
    m["radiant"] = [p for p in players if p["team_side"] == "radiant"]
    m["dire"] = [p for p in players if p["team_side"] == "dire"]
    season = season_of_match(all_seasons(), m)
    m["season"] = season
    if season:
        caps = db.get_match_team_captains(season["id"], season["division_id"], [match_id])
        teams = {t["captain"]: t for t in season_teams(season, matches=[])}
        for side in ("radiant", "dire"):
            cap = caps.get(match_id, {}).get(side)
            m[f"{side}_team"] = teams.get(cap) if cap else None
    return m


# ---------------------------------------------------------------------------
# Editable text
# ---------------------------------------------------------------------------

DEFAULT_TEXT = {
    "about": (
        "A casual Dota 2 league. Sign up as an individual, captains draft teams, "
        "and teams play a weekly regular season followed by playoffs.\n\n"
        "Log in with Steam from Settings to sign up for a season when signups are open."
    ),
    "rules": "The league admins haven't posted rules yet.",
}


def get_text(key: str) -> str:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM site_text WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_TEXT.get(key, "")


def set_text(key: str, value: str) -> None:
    with _conn() as conn:
        conn.execute("""
            INSERT INTO site_text (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))
