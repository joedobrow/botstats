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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db
from db import _conn

SESSION_DAYS = 30
POSITIONS = (1, 2, 3, 4, 5)
CAPTAIN_CHOICES = ("yes", "maybe", "no")

_HEROES: dict[int, dict] = {
    int(k): v for k, v in
    json.loads((Path(__file__).parent / "static" / "heroes.json").read_text()).items()
}


def hero(hero_id: int) -> dict:
    return _HEROES.get(hero_id) or {"name": f"Hero {hero_id}", "slug": ""}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_site_tables() -> None:
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

            -- Display name for each bot division (a Discord guild).
            CREATE TABLE IF NOT EXISTS site_divisions (
                guild_id  INTEGER PRIMARY KEY,
                name      TEXT NOT NULL
            );

            -- Site-only settings for a season in the bot's seasons table.
            CREATE TABLE IF NOT EXISTS site_season_settings (
                guild_id      INTEGER NOT NULL,
                season_start  TEXT NOT NULL,
                signups_open  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, season_start)
            );

            CREATE TABLE IF NOT EXISTS site_signups (
                guild_id      INTEGER NOT NULL,
                season_start  TEXT NOT NULL,
                account_id    INTEGER NOT NULL,
                mmr           INTEGER,
                positions     TEXT NOT NULL,        -- e.g. "1,3"
                captain       TEXT NOT NULL,        -- yes / maybe / no
                discord       TEXT,
                statement     TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                PRIMARY KEY (guild_id, season_start, account_id)
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


# ---------------------------------------------------------------------------
# Divisions and seasons
# ---------------------------------------------------------------------------

def division_names() -> dict[int, str]:
    """{guild_id: display name} for every division the bot knows."""
    with _conn() as conn:
        named = {r["guild_id"]: r["name"] for r in conn.execute("SELECT * FROM site_divisions")}
        divs = conn.execute("SELECT guild_id, region, game_mode FROM divisions").fetchall()
    out = {}
    for d in divs:
        mode = "Ability Draft" if d["game_mode"] == "ad" else "Captains Mode"
        region = {"us_west": "US West", "us_east": "US East"}.get(d["region"], "")
        out[d["guild_id"]] = named.get(d["guild_id"]) or " ".join(filter(None, [region, mode]))
    return out


def set_division_name(guild_id: int, name: str) -> None:
    with _conn() as conn:
        conn.execute("""
            INSERT INTO site_divisions (guild_id, name) VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET name = excluded.name
        """, (guild_id, name))


def all_seasons() -> list[dict]:
    """Every season of every division, newest first, with site settings and
    its match window [start_ts, end_ts). A season ends where the division's
    next season begins."""
    names = division_names()
    with _conn() as conn:
        settings = {
            (r["guild_id"], r["season_start"]): r["signups_open"]
            for r in conn.execute("SELECT * FROM site_season_settings")
        }
        signup_counts = {
            (r["guild_id"], r["season_start"]): r["n"]
            for r in conn.execute(
                "SELECT guild_id, season_start, COUNT(*) AS n FROM site_signups GROUP BY 1, 2")
        }
    out = []
    for guild_id in names:
        seasons = sorted(db.get_seasons(guild_id), key=lambda s: s["season_start"])
        for i, s in enumerate(seasons):
            start = s["season_start"]
            nxt = seasons[i + 1]["season_start"] if i + 1 < len(seasons) else None
            out.append({
                "guild_id": guild_id,
                "season_start": start,
                "label": db.season_label(s),
                "division": names[guild_id],
                "league_id": s.get("league_id"),
                "start_ts": db._all_time_start_ts(guild_id, start),
                "end_ts": db._all_time_start_ts(guild_id, nxt) if nxt else None,
                "signups_open": bool(settings.get((guild_id, start))),
                "signup_count": signup_counts.get((guild_id, start), 0),
            })
    out.sort(key=lambda s: s["season_start"], reverse=True)
    return out


def get_season(guild_id: int, season_start: str) -> dict | None:
    return next((s for s in all_seasons()
                 if s["guild_id"] == guild_id and s["season_start"] == season_start), None)


def create_season(guild_id: int, season_start: str, label: str, league_id: int | None) -> None:
    db.upsert_season(guild_id, season_start, league_id=league_id, label=label)


def set_signups_open(guild_id: int, season_start: str, is_open: bool) -> None:
    with _conn() as conn:
        conn.execute("""
            INSERT INTO site_season_settings (guild_id, season_start, signups_open) VALUES (?, ?, ?)
            ON CONFLICT(guild_id, season_start) DO UPDATE SET signups_open = excluded.signups_open
        """, (guild_id, season_start, int(is_open)))


def _season_for(seasons: list[dict], guild_id: int, ts: int) -> dict | None:
    for s in seasons:
        if s["guild_id"] == guild_id and s["start_ts"] <= ts and (s["end_ts"] is None or ts < s["end_ts"]):
            return s
    return None


# ---------------------------------------------------------------------------
# Signups
# ---------------------------------------------------------------------------

def get_signup(guild_id: int, season_start: str, account_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM site_signups WHERE guild_id = ? AND season_start = ? AND account_id = ?",
            (guild_id, season_start, account_id),
        ).fetchone()
    return dict(row) if row else None


def save_signup(guild_id: int, season_start: str, account_id: int, *, mmr: int | None,
                positions: list[int], captain: str, discord: str | None, statement: str | None) -> None:
    now = _now()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO site_signups (guild_id, season_start, account_id, mmr, positions,
                                      captain, discord, statement, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, season_start, account_id) DO UPDATE SET
                mmr = excluded.mmr, positions = excluded.positions, captain = excluded.captain,
                discord = excluded.discord, statement = excluded.statement,
                updated_at = excluded.updated_at
        """, (guild_id, season_start, account_id, mmr, ",".join(map(str, positions)),
              captain, discord, statement, now, now))


def delete_signup(guild_id: int, season_start: str, account_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM site_signups WHERE guild_id = ? AND season_start = ? AND account_id = ?",
            (guild_id, season_start, account_id),
        )


def list_signups(guild_id: int, season_start: str) -> list[dict]:
    """Signups in signup order, with each player's display name and badge."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT s.*, u.steam_name, u.avatar_url
            FROM site_signups s LEFT JOIN site_users u ON u.account_id = s.account_id
            WHERE s.guild_id = ? AND s.season_start = ?
            ORDER BY s.created_at
        """, (guild_id, season_start)).fetchall()
    out = [dict(r) for r in rows]
    idents = player_identities([r["account_id"] for r in out])
    for r in out:
        ident = idents.get(r["account_id"], {})
        r["name"] = ident.get("name") or r["steam_name"] or str(r["account_id"])
        r["badge"] = ident.get("badge")
        r["positions"] = [int(p) for p in r["positions"].split(",") if p]
    return out


def signups_for_player(account_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT guild_id, season_start FROM site_signups WHERE account_id = ?", (account_id,)
        ).fetchall()
    return [dict(r) for r in rows]


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


def search_players(q: str, limit: int = 200) -> list[dict]:
    """Everyone who has played a league match or logged in, filtered by name."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT account_id, COUNT(*) AS games, SUM(won) AS wins, MAX(match_id) AS last_match
            FROM players GROUP BY account_id
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
    out.sort(key=lambda s: (-s["games"], s["name"].lower()))
    return out[:limit]


def player_matches(account_id: int) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT p.*, m.start_time, m.guild_id, m.duration AS match_duration,
                   m.radiant_score, m.dire_score
            FROM players p JOIN matches m ON m.match_id = p.match_id
            WHERE p.account_id = ?
            ORDER BY m.start_time DESC
        """, (account_id,)).fetchall()
    return [dict(r) for r in rows]


def aggregate(rows: list[dict]) -> dict:
    """Season-style aggregate of per-match player rows, with fantasy points
    computed the same way the bot's leaderboard computes them."""
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

def season_match_rows(season: dict) -> list[dict]:
    """Every player-match row in the season's window."""
    q = """
        SELECT p.*, m.start_time, m.duration AS match_duration, m.radiant_win
        FROM players p JOIN matches m ON m.match_id = p.match_id
        WHERE m.guild_id = ? AND m.start_time >= ?
    """
    args: list = [season["guild_id"], season["start_ts"]]
    if season["end_ts"]:
        q += " AND m.start_time < ?"
        args.append(season["end_ts"])
    with _conn() as conn:
        return [dict(r) for r in conn.execute(q, args)]


def season_leaders(season: dict) -> list[dict]:
    by_player: dict[int, list[dict]] = {}
    for r in season_match_rows(season):
        by_player.setdefault(r["account_id"], []).append(r)
    idents = player_identities(list(by_player))
    out = []
    for aid, rows in by_player.items():
        agg = aggregate(rows)
        agg["account_id"] = aid
        agg["name"] = idents.get(aid, {}).get("name") or rows[0]["name"]
        out.append(agg)
    out.sort(key=lambda a: -a["fantasy_points"])
    return out


def season_matches(season: dict) -> list[dict]:
    """Matches in the season, newest first, with each side's team (by captain)."""
    q = "SELECT * FROM matches WHERE guild_id = ? AND start_time >= ?"
    args: list = [season["guild_id"], season["start_ts"]]
    if season["end_ts"]:
        q += " AND start_time < ?"
        args.append(season["end_ts"])
    with _conn() as conn:
        matches = [dict(r) for r in conn.execute(q + " ORDER BY start_time DESC", args)]
    caps = db.get_match_team_captains(season["guild_id"], season["season_start"],
                                      [m["match_id"] for m in matches])
    for m in matches:
        m["radiant_team"] = caps.get(m["match_id"], {}).get("radiant")
        m["dire_team"] = caps.get(m["match_id"], {}).get("dire")
    return matches


def season_teams(season: dict, matches: list[dict] | None = None) -> list[dict]:
    """Teams from the season's draft (player_costs), with identity and a
    win/loss record counted from matches both sides can be attributed in."""
    g, s = season["guild_id"], season["season_start"]
    costs = db.get_player_costs(g, s)
    identity = db.get_season_teams(g, s)
    cap_aids = db.get_captain_account_ids(g, s)
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
    idents = player_identities([p["account_id"] for p in players])
    for p in players:
        p["display_name"] = idents.get(p["account_id"], {}).get("name") or p["name"]
        p["hero"] = hero(p["hero_id"])
        p["match_duration"] = m["duration"]
        p["fantasy_points"] = db._per_match_fp(p)
    m["radiant"] = [p for p in players if p["team_side"] == "radiant"]
    m["dire"] = [p for p in players if p["team_side"] == "dire"]
    season = _season_for(all_seasons(), m["guild_id"], m["start_time"])
    m["season"] = season
    if season:
        caps = db.get_match_team_captains(season["guild_id"], season["season_start"], [match_id])
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
