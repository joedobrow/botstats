"""
aiohttp web server for the AD helper page + stream cards.

Designed to run inside the Discord bot's asyncio event loop.
"""

from __future__ import annotations

import html as _html
import logging
from pathlib import Path

from aiohttp import web

from botstats.data_builder import build_ad_data

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
ASSETS_DIR   = Path(__file__).resolve().parent.parent / "assets"


# Shortened stat labels — duplicated from bot.py so the web server can run
# independently. Keep in sync with bot.py's _CARD_SHORT_LABELS.
_CARD_SHORT_LABELS: dict[str, str] = {
    "Fantasy Points":            "Fantasy Pts",
    "Fantasy Diff vs Teammates": "FP Diff",
    "GPM":                       "GPM",
    "XPM":                       "XPM",
    "KDA":                       "KDA",
    "Last Hits/game":            "Avg Last Hits",
    "Denies/game":               "Avg Denies",
    "Hero Damage/game":          "Avg Hero Dmg",
    "Damage Share %":            "Dmg Share %",
    "Hero Healing/game":         "Avg Hero Heal",
    "Teamfight %":               "TF Part %",
    "Stuns/Min":                 "Stuns/Min",
    "Tower Kills/game":          "Avg Towers",
    "Obs Kills/Min":             "Obs Kills/Min",
    "Roshans Killed/game":       "Avg Roshes",
    "Camp Stacks/game":          "Avg Stacks",
    "Rune Pickups/game":         "Avg Runes",
    "Defensive Item Uses/game":  "Avg Def Items",
    "Tormentor Kills/game":      "Avg Tormentors",
    "Watcher Captures/game":     "Avg Watchers",
    "Building Damage/game":      "Avg Bld Dmg",
    "First Blood Rate":          "First Blood %",
    "Avg Game Length":           "Avg Game Len",
    "First Tormentor Time":      "1st Torm Time",
}

ROLE_SHORT = {
    1: "Safe Lane",
    2: "Mid Lane",
    3: "Off Lane",
    4: "Roamer",
    5: "Hard Support",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_card_line(line: str) -> str:
    """Strip markdown, drop the word 'rank', shorten label. Mirrors
    bot._clean_card_line."""
    s = line.replace("**", "").replace("(rank ", "(")
    if ":" in s:
        label, rest = s.split(":", 1)
        short = _CARD_SHORT_LABELS.get(label.strip(), label.strip())
        s = f"{short}:{rest}"
    return s


def _render_stat_lines_html(lines: list[str]) -> str:
    """Each line is 'Label: value (N/M)'. Wrap value/rank in spans for styling."""
    out = []
    for raw in lines[:3]:
        cleaned = _clean_card_line(raw)
        # Split label : rest (rest contains the numeric value and possibly (N/M))
        if ":" in cleaned:
            label, rest = cleaned.split(":", 1)
            label = label.strip()
            rest = rest.strip()
            # Pull out the trailing (N/M) into a separate span if present
            rank_span = ""
            if rest.endswith(")") and "(" in rest:
                idx = rest.rfind("(")
                rank_span = f' <span class="rank">{_html.escape(rest[idx:])}</span>'
                rest = rest[:idx].strip()
            out.append(
                f'<div class="stat-line">'
                f'  <span class="label">{_html.escape(label)}:</span> '
                f'{_html.escape(rest)}{rank_span}'
                f'</div>'
            )
        else:
            out.append(f'<div class="stat-line">{_html.escape(cleaned)}</div>')
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Card handlers
# ---------------------------------------------------------------------------

async def player_card_handler(request: web.Request) -> web.Response:
    """Serve a player card as HTML. URL: /card/player/<account_id>"""
    try:
        account_id = int(request.match_info["account_id"])
    except (KeyError, ValueError):
        return web.Response(text="Invalid account_id", status=400)

    # Defer heavy imports so the web server boots cleanly even if some are slow.
    from db import (
        get_all_time_stats, get_rating_cache_row, compute_fantasy_adjusted_ratings,
        _conn,
    )
    from formatters import _compute_strengths_weaknesses
    from config import ROLE_LABELS

    # Find which guild + season this account belongs to. For our single-guild
    # deployment this is simple — pick the first division with a season.
    with _conn() as conn:
        # Pick the active season — most-recent division that actually has draft data.
        div = conn.execute("""
            SELECT d.guild_id, d.season_start
            FROM divisions d
            WHERE d.season_start IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM player_costs pc
                  WHERE pc.guild_id = d.guild_id AND pc.season_start = d.season_start
              )
            ORDER BY d.season_start DESC
            LIMIT 1
        """).fetchone()
    if not div:
        return web.Response(text="No division configured", status=503)
    guild_id, season_start = div["guild_id"], div["season_start"]

    stats = get_all_time_stats(guild_id, season_start)
    target = next((s for s in stats if s.get("account_id") == account_id), None)
    if target is None:
        return web.Response(text=f"Account {account_id} has no league matches", status=404)

    pool = [s for s in stats if (s.get("games_played") or 0) >= 5]
    strengths, weaknesses = _compute_strengths_weaknesses(target, pool)

    # Rating + avatar from cache
    rating: int | None = None
    cache_row = get_rating_cache_row(account_id)
    if cache_row and cache_row.get("internal_rating") is not None:
        rating = cache_row["internal_rating"]
        adjustments = compute_fantasy_adjusted_ratings(guild_id, season_start)
        adj = adjustments.get(account_id)
        if adj:
            rating = adj["adjusted_rating"]

    cr = cache_row or {}
    avatar_url = "" if cr.get("hide_avatar") else (cr.get("avatar_url") or "")
    name = (
        (cache_row or {}).get("override_nickname")
        or target.get("name") or f"Player_{account_id}"
    )
    games = target.get("games_played", 0) or 0
    wins = target.get("wins", 0) or 0
    losses = games - wins
    record = f"{wins}W–{losses}L"
    full_role = ROLE_LABELS.get(target.get("role_position"), "—")
    role = full_role.split(" (")[0]

    template = (TEMPLATE_DIR / "player_card.html").read_text(encoding="utf-8")
    html = (
        template
        .replace("<<NAME>>",            _html.escape(name))
        .replace("<<RATING>>",          _html.escape(str(rating) if rating is not None else "—"))
        .replace("<<RECORD>>",          _html.escape(record))
        .replace("<<ROLE>>",            _html.escape(role))
        .replace("<<AVATAR_URL>>",      _html.escape(avatar_url))
        .replace("<<STRENGTHS_HTML>>",  _render_stat_lines_html(strengths))
        .replace("<<WEAKNESSES_HTML>>", _render_stat_lines_html(weaknesses))
    )
    return web.Response(text=html, content_type="text/html")


async def team_card_handler(request: web.Request) -> web.Response:
    """Serve a team card as HTML. URL: /card/team/<captain_name>"""
    captain = request.match_info.get("captain", "")
    if not captain:
        return web.Response(text="Missing captain name", status=400)

    from urllib.parse import unquote
    captain = unquote(captain)

    from db import (
        _conn, get_player_costs, get_team_match_aggregates,
        get_captain_account_ids, get_rating_cache_row, get_all_time_stats,
    )
    from formatters import _compute_strengths_weaknesses, _TEAM_NOTABLE_STATS

    with _conn() as conn:
        # Pick the active season — most-recent division that actually has draft data.
        div = conn.execute("""
            SELECT d.guild_id, d.season_start
            FROM divisions d
            WHERE d.season_start IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM player_costs pc
                  WHERE pc.guild_id = d.guild_id AND pc.season_start = d.season_start
              )
            ORDER BY d.season_start DESC
            LIMIT 1
        """).fetchone()
    if not div:
        return web.Response(text="No division configured", status=503)
    guild_id, season_start = div["guild_id"], div["season_start"]

    costs = get_player_costs(guild_id, season_start)
    if not any(c.get("captain") == captain for c in costs.values()):
        return web.Response(text=f"Unknown team captain: {captain}", status=404)

    team_aggs = get_team_match_aggregates(guild_id, season_start)
    target_agg = team_aggs.get(captain)
    if not target_agg:
        return web.Response(text=f"Team {captain} has no data yet", status=404)

    cap_aids = get_captain_account_ids(guild_id, season_start)
    captain_aid = cap_aids.get(captain)
    drafted_aids = [aid for aid, c in costs.items() if c.get("captain") == captain]
    roster_aids = drafted_aids + ([captain_aid] if captain_aid else [])

    stats_by_aid = {
        s["account_id"]: s for s in get_all_time_stats(guild_id, season_start)
    }

    members = []
    for aid in roster_aids:
        cr = get_rating_cache_row(aid) or {}
        s = stats_by_aid.get(aid) or {}
        av_url = "" if cr.get("hide_avatar") else (cr.get("avatar_url") or "")
        members.append({
            "account_id": aid,
            "name": cr.get("override_nickname") or cr.get("name") or f"Player_{aid}",
            "is_captain": aid == captain_aid,
            "role_position": s.get("role_position"),
            "avatar_url": av_url,
        })

    # Sort left→right by role.
    members.sort(key=lambda m: (m.get("role_position") or 99))

    roster_html_parts = []
    for m in members[:5]:
        cls = "slot captain" if m["is_captain"] else "slot"
        cap_label = '<div class="cap-label">CAPTAIN</div>' if m["is_captain"] else ""
        role_text = ROLE_SHORT.get(m.get("role_position"), "")
        roster_html_parts.append(f"""
          <div class="{cls}">
            <div class="avatar-wrap">
              {cap_label}
              <div class="avatar" style="background-image: url('{_html.escape(m["avatar_url"])}')"></div>
            </div>
            <div class="slot-name">{_html.escape(m["name"])}</div>
            <div class="slot-role">{_html.escape(role_text)}</div>
          </div>
        """)
    roster_html = "\n".join(roster_html_parts)

    strengths, weaknesses = _compute_strengths_weaknesses(
        target_agg, list(team_aggs.values()), stat_list=_TEAM_NOTABLE_STATS,
    )

    games = target_agg.get("games_played", 0) or 0
    wins = target_agg.get("wins", 0) or 0
    losses = games - wins
    avg_dur = target_agg.get("avg_duration")
    if avg_dur:
        mm = int(avg_dur) // 60
        ss = int(avg_dur) % 60
        record = f"{wins}W–{losses}L · {mm}:{ss:02d} avg game length"
    else:
        record = f"{wins}W–{losses}L"

    template = (TEMPLATE_DIR / "team_card.html").read_text(encoding="utf-8")
    html = (
        template
        .replace("<<TEAM_LABEL>>",      _html.escape(captain))
        .replace("<<RECORD>>",          _html.escape(record))
        .replace("<<ROSTER_HTML>>",     roster_html)
        .replace("<<STRENGTHS_HTML>>",  _render_stat_lines_html(strengths))
        .replace("<<WEAKNESSES_HTML>>", _render_stat_lines_html(weaknesses))
    )
    return web.Response(text=html, content_type="text/html")


# ---------------------------------------------------------------------------
# Ratings API (for the Google Sheets scout-sheet sync)
# ---------------------------------------------------------------------------

# Cap how many never-before-seen players get computed per request, so one
# hourly sync call can't run long enough to hit a client-side timeout.
# Windrun's ~5s per-account rate limit dominates: 8 accounts ~= 40s worst
# case. Any stragglers beyond the cap just get picked up on the next sync.
RATINGS_BACKFILL_LIMIT = 8


async def _account_ids_from_draft_sheet(draft_sheet_url: str) -> list[int]:
    """Fetch the public rd2l.gg draft-sheet CSV and pull out every player's
    Steam account_id from their dotabuff/opendota profile link."""
    import csv
    import io
    import re
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(draft_sheet_url) as resp:
            resp.raise_for_status()
            text = await resp.text()

    reader = csv.DictReader(io.StringIO(text))
    ids = []
    for row in reader:
        m = re.search(r"/players/(\d+)", row.get("dotabuff") or row.get("opendota") or "")
        if m:
            ids.append(int(m.group(1)))
    return ids


async def ratings_api_handler(request: web.Request) -> web.Response:
    """GET /api/ratings[?draft_sheet_url=...][&guild_id=...&season_start=...] ->
        {"account_id": {"internal_rating": int, "name": str}}

    Requires the X-Api-Key header to match RATINGS_API_KEY. Returns every
    cached player_ratings_cache row, keyed by account_id as a string (JSON
    object keys must be strings).

    When draft_sheet_url is given, first computes + caches ratings (up to
    RATINGS_BACKFILL_LIMIT per call) for any account in that CSV that isn't
    cached yet, so newly-signed-up players get a rating without anyone
    needing to run /player or /refresh_ratings by hand.

    When guild_id + season_start are given, internal_rating is the same
    fantasy-adjusted number /player displays (base rating nudged up to
    ±6% by how much someone over/underperformed expectations that season),
    not the raw player_ratings_cache value — matching /player exactly for
    anyone with match history that season. Falls back to the raw rating for
    anyone without an adjustment entry (no match history / manually rated),
    same as /player does. Omit these params to get the raw rating, as before.
    """
    from config import RATINGS_API_KEY

    if not RATINGS_API_KEY:
        return web.Response(text="Ratings API not configured", status=503)
    if request.headers.get("X-Api-Key") != RATINGS_API_KEY:
        return web.Response(text="Unauthorized", status=401)

    from db import get_all_rating_cache_rows, get_rating_cache_row

    draft_sheet_url = request.query.get("draft_sheet_url")
    if draft_sheet_url:
        from rating_compute import compute_and_cache_rating
        try:
            account_ids = await _account_ids_from_draft_sheet(draft_sheet_url)
        except Exception:
            logger.exception("Failed to fetch draft sheet for ratings backfill")
            account_ids = []

        missing = [aid for aid in account_ids if get_rating_cache_row(aid) is None]
        for aid in missing[:RATINGS_BACKFILL_LIMIT]:
            await compute_and_cache_rating(aid)

    rows = get_all_rating_cache_rows()

    adjustments = {}
    guild_id = request.query.get("guild_id")
    season_start = request.query.get("season_start")
    if guild_id and season_start:
        from db import compute_fantasy_adjusted_ratings
        adjustments = compute_fantasy_adjusted_ratings(int(guild_id), season_start)

    data = {}
    for r in rows:
        aid = r["account_id"]
        rating = r["internal_rating"]
        adj = adjustments.get(aid)
        if adj:
            rating = adj["adjusted_rating"]
        data[str(aid)] = {"internal_rating": rating, "name": r["name"]}
    return web.json_response(data)


async def costs_api_handler(request: web.Request) -> web.Response:
    """GET /api/costs?guild_id=...&season_start=YYYY-MM-DD ->
        {"account_id": {"cost": int, "captain": str|None}}

    Requires the same X-Api-Key as /api/ratings. Read-only wrapper around
    db.get_player_costs, for Google Sheets analytics that want a past
    season's draft costs (e.g. "what did this player go for last season").
    """
    from config import RATINGS_API_KEY

    if not RATINGS_API_KEY:
        return web.Response(text="Ratings API not configured", status=503)
    if request.headers.get("X-Api-Key") != RATINGS_API_KEY:
        return web.Response(text="Unauthorized", status=401)

    guild_id = request.query.get("guild_id")
    season_start = request.query.get("season_start")
    if not guild_id or not season_start:
        return web.Response(text="Missing guild_id or season_start query param", status=400)

    from db import get_player_costs

    costs = get_player_costs(int(guild_id), season_start)
    data = {
        str(aid): {"cost": c["cost"], "captain": c["captain"]}
        for aid, c in costs.items()
    }
    return web.json_response(data)


async def season_stats_api_handler(request: web.Request) -> web.Response:
    """GET /api/season_stats?guild_id=...&season_start=YYYY-MM-DD ->
        {"account_id": {games_played, wins, attendance, fantasy_points,
                         cost, deserved_cost, value}}

    Requires the same X-Api-Key as /api/ratings. Thin wrapper around
    db.get_all_time_stats for a given season — the exact same numbers
    /leaderboard's "Value ($ deserved − $ paid)" is built from, exposed for
    Google Sheets draft-prep analytics (e.g. computing a suggested bid from
    a past season's performance-vs-cost outcome).
    """
    from config import RATINGS_API_KEY

    if not RATINGS_API_KEY:
        return web.Response(text="Ratings API not configured", status=503)
    if request.headers.get("X-Api-Key") != RATINGS_API_KEY:
        return web.Response(text="Unauthorized", status=401)

    guild_id = request.query.get("guild_id")
    season_start = request.query.get("season_start")
    if not guild_id or not season_start:
        return web.Response(text="Missing guild_id or season_start query param", status=400)

    from db import get_all_time_stats

    rows = get_all_time_stats(int(guild_id), season_start)
    data = {
        str(r["account_id"]): {
            "games_played":   r.get("games_played"),
            "wins":           r.get("wins"),
            "attendance":     r.get("attendance"),
            "fantasy_points": r.get("fantasy_points"),
            "cost":           r.get("cost"),
            "deserved_cost":  r.get("deserved_cost"),
            "value":          r.get("value"),
        }
        for r in rows
    }
    return web.json_response(data)


# ---------------------------------------------------------------------------
# AD helper (existing)
# ---------------------------------------------------------------------------

async def ad_helper_handler(request: web.Request) -> web.Response:
    data = await build_ad_data()
    if not data:
        return web.Response(text="Failed to load ability data. Try again later.", status=503)

    template_path = TEMPLATE_DIR / "ad_helper.html"
    html = template_path.read_text(encoding="utf-8")

    html = (
        html
        .replace("<<DATA_JSON>>", data["data_json"])
        .replace("<<HS_JSON>>",   data["hs_json"])
        .replace("<<ROLES_JSON>>",data["roles_json"])
    )
    return web.Response(text=html, content_type="text/html")


async def vibecheck_handler(request: web.Request) -> web.Response:
    """Static content piece: the S39 Discord-vibes scouting report.

    Served from here rather than as a Claude artifact so the link is public —
    anyone in the Discord can open it without an account.
    """
    path = TEMPLATE_DIR / "vibecheck_s39.html"
    if not path.exists():
        return web.Response(text="Not found.", status=404)
    return web.Response(text=path.read_text(encoding="utf-8"), content_type="text/html")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/vibecheck", vibecheck_handler)
    app.router.add_get("/ad-helper", ad_helper_handler)
    app.router.add_get("/card/player/{account_id}", player_card_handler)
    app.router.add_get("/card/team/{captain}",       team_card_handler)
    app.router.add_get("/api/ratings",                ratings_api_handler)
    app.router.add_get("/api/costs",                  costs_api_handler)
    app.router.add_get("/api/season_stats",            season_stats_api_handler)
    # Serve the rd2l logo + any other static assets from the assets/ dir.
    if ASSETS_DIR.exists():
        app.router.add_static("/assets/", path=str(ASSETS_DIR), show_index=False)
    return app


async def start_web_server(host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Web server running on http://%s:%d/ (cards: /card/player/<id>, /card/team/<captain>)", host, port)
    return runner
