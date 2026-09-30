"""Database access for the website.

The site's own tables (site_*) hold what only the website needs: logins,
signups, and display settings. Everything about matches, teams and seasons
is read from the bot's tables, so the site and the bot never disagree.

Nothing here selects a rating column (internal_rating, raw_windrun,
mmr_estimate, explanation, group_rating) — public pages show stats and the
ranked badge only.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
from db import _conn

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
                captain       TEXT NOT NULL,        -- yes / maybe / no
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
        """)



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
    for s in out:
        s["status_label"] = SEASON_STATUSES[s["status"]]
        s["signups_open"] = s["status"] == "signups"
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


def season_of_match(seasons: list[dict], match: dict) -> dict | None:
    """The season (in its division) a match was played in."""
    return next((s for s in seasons
                 if s["id"] == match["league_id"] and s["division_id"] == match["division_id"]), None)


# ---------------------------------------------------------------------------
# Signups
# ---------------------------------------------------------------------------

def get_signup(season: dict, account_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM site_signups WHERE season_id = ? AND division_id = ? AND account_id = ?",
            (season["id"], season["division_id"], account_id)).fetchone()
    return dict(row) if row else None


def save_signup(season: dict, account_id: int, *, mmr: int | None, positions: list[int],
                captain: str, discord: str | None, statement: str | None) -> None:
    now = _now()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO site_signups (season_id, division_id, account_id, mmr, positions,
                                      captain, discord, statement, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season_id, division_id, account_id) DO UPDATE SET
                mmr = excluded.mmr, positions = excluded.positions, captain = excluded.captain,
                discord = excluded.discord, statement = excluded.statement,
                updated_at = excluded.updated_at
        """, (season["id"], season["division_id"], account_id, mmr, ",".join(map(str, positions)),
              captain, discord, statement, now, now))


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
    """Everyone who has played a league match or logged in, filtered by name."""
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
    out = []
    ql = q.strip().lower()
    for aid, s in stats.items():
        ident = idents.get(aid, {})
        s.update(ident)
        s["name"] = s["name"] or str(aid)
        if ql and ql not in s["name"].lower() and ql != str(aid):
            continue
        out.append(s)
    ratings = player_ratings()
    for s in out:
        s["rating"] = ratings.get(s["account_id"])
    out.sort(key=lambda s: (-s["games"], s["name"].lower()))
    return out[:limit] if limit else out


def player_matches(account_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT p.*, m.start_time, m.division_id, m.league_id, m.duration AS match_duration,
                   m.radiant_score, m.dire_score
            FROM players p JOIN matches m ON m.match_id = p.match_id
            WHERE p.account_id = ?
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
    return sorted(teams.values(), key=lambda t: (-t["wins"], t["losses"], t["name"].lower()))


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
