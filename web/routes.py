"""Page handlers for the league website."""

from __future__ import annotations

import csv
import io
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

import jinja2
from aiohttp import web

import db
from config import LEAGUE_TZ, STEAM_API_KEY
from player_api import account_id_from
from web import steam, store

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "session"

# Steam accounts (Dota account ids or Steam64 ids) allowed into /admin.
ADMIN_IDS: set[int] = {
    aid for aid in (account_id_from(x) for x in os.environ.get("SITE_ADMIN_STEAM_IDS", "").split(","))
    if aid
}
# Public URL of the site, e.g. https://dota-bot.fly.dev. Without it the
# Steam return URL is built from the request's own host.
BASE_URL = os.environ.get("SITE_BASE_URL", "").rstrip("/")

POSITION_NAMES = {1: "Safe lane", 2: "Mid", 3: "Off lane", 4: "Soft support", 5: "Hard support"}

routes = web.RouteTableDef()


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _fmt_date(ts: int | None, fmt: str = "%b %-d, %Y") -> str:
    return datetime.fromtimestamp(ts, LEAGUE_TZ).strftime(fmt) if ts else ""


def _fmt_duration(seconds: int | float | None) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in (text or "").split("\n\n") if p.strip()]


def season_url(season: dict) -> str:
    return f"/season/{season['guild_id']}/{season['season_start']}"


def team_url(season: dict, captain: str) -> str:
    return f"{season_url(season)}/team/{quote(captain, safe='')}"


env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
env.filters.update(date=_fmt_date, duration=_fmt_duration, paragraphs=_paragraphs)
env.globals.update(season_url=season_url, team_url=team_url, hero=store.hero,
                   POSITION_NAMES=POSITION_NAMES)


def render(request: web.Request, template: str, status: int = 200, **ctx) -> web.Response:
    ctx.update(user=request.get("user"), is_admin=request.get("is_admin", False),
               csrf=request.get("csrf", ""), path=request.path,
               # Changes whenever the stylesheet does, so browsers never keep a stale copy.
               css_version=int((STATIC_DIR / "style.css").stat().st_mtime))
    html = env.get_template(template).render(**ctx)
    return web.Response(text=html, content_type="text/html", status=status)


def not_found(request: web.Request, what: str = "Page") -> web.Response:
    return render(request, "message.html", status=404, title="Not found",
                  message=f"{what} not found.")


# ---------------------------------------------------------------------------
# Session middleware
# ---------------------------------------------------------------------------

@web.middleware
async def session_middleware(request: web.Request, handler):
    token = request.cookies.get(COOKIE)
    sess = store.get_session(token) if token else None
    if sess:
        request["user"] = store.get_user(sess["account_id"])
        request["csrf"] = sess["csrf"]
        request["is_admin"] = sess["account_id"] in ADMIN_IDS
    if request.method == "POST" and not request.path.startswith("/api/"):
        form = await request.post()
        if not sess or form.get("csrf") != sess["csrf"]:
            return render(request, "message.html", status=403, title="Expired form",
                          message="That form expired. Go back, reload the page and try again.")
    return await handler(request)


def _require_user(request: web.Request) -> dict:
    user = request.get("user")
    if not user:
        raise web.HTTPFound("/login?" + urlencode({"next": request.path_qs}))
    return user


def _require_admin(request: web.Request) -> None:
    _require_user(request)
    if not request.get("is_admin"):
        raise web.HTTPForbidden(text="Admins only.")


def _base_url(request: web.Request) -> str:
    if BASE_URL:
        return BASE_URL
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    return f"{proto}://{request.host}"


def _safe_next(target: str | None) -> str:
    return target if target and target.startswith("/") and not target.startswith("//") else "/"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@routes.get("/login")
async def login(request: web.Request):
    base = _base_url(request)
    nxt = _safe_next(request.query.get("next"))
    return_to = f"{base}/auth/steam/callback?" + urlencode({"next": nxt})
    raise web.HTTPFound(steam.login_url(return_to, base))


@routes.get("/auth/steam/callback")
async def steam_callback(request: web.Request):
    base = _base_url(request)
    account_id = await steam.verify(dict(request.query), f"{base}/auth/steam/callback")
    if account_id is None:
        return render(request, "message.html", status=400, title="Login failed",
                      message="Steam didn't confirm the login. Please try again.")
    name, avatar = await steam.player_summary(account_id, STEAM_API_KEY)
    store.upsert_user(account_id, name, avatar)
    token = store.create_session(account_id)
    resp = web.HTTPFound(_safe_next(request.query.get("next")))
    resp.set_cookie(COOKIE, token, max_age=store.SESSION_DAYS * 86400, httponly=True,
                    samesite="Lax", secure=base.startswith("https://"), path="/")
    raise resp


@routes.post("/logout")
async def logout(request: web.Request):
    token = request.cookies.get(COOKIE)
    if token:
        store.delete_session(token)
    resp = web.HTTPFound("/")
    resp.del_cookie(COOKIE, path="/")
    raise resp


@routes.get("/settings")
async def settings(request: web.Request):
    user = request.get("user")
    if not user:
        return render(request, "settings.html", title="Settings")
    seasons = {(s["guild_id"], s["season_start"]): s for s in store.all_seasons()}
    signups = [seasons[(r["guild_id"], r["season_start"])]
               for r in store.signups_for_player(user["account_id"])
               if (r["guild_id"], r["season_start"]) in seasons]
    return render(request, "settings.html", title="Settings", signups=signups)


@routes.post("/settings")
async def settings_save(request: web.Request):
    user = _require_user(request)
    form = await request.post()
    store.set_user_discord(user["account_id"], (form.get("discord") or "").strip()[:64] or None)
    raise web.HTTPFound("/settings")


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

def _open_seasons() -> list[dict]:
    return [s for s in store.all_seasons() if s["signups_open"]]


@routes.get("/")
async def home(request: web.Request):
    return render(request, "home.html", signups_open=bool(_open_seasons()))


@routes.get("/signup")
async def signup_hub(request: web.Request):
    """The home page's Sign-Up tile: straight to the form when one season is
    open, a choice when several are."""
    open_seasons = _open_seasons()
    if len(open_seasons) == 1:
        raise web.HTTPFound(season_url(open_seasons[0]) + "/signup")
    return render(request, "signup_hub.html", title="Sign up", open_seasons=open_seasons)


@routes.get("/info")
async def info(request: web.Request):
    return render(request, "info.html", title="Info")


@routes.get("/about")
async def about(request: web.Request):
    return render(request, "text_page.html", title="About", text=store.get_text("about"))


@routes.get("/rules")
async def rules(request: web.Request):
    return render(request, "text_page.html", title="Rules", text=store.get_text("rules"))


@routes.get("/seasons")
async def seasons_page(request: web.Request):
    by_div: dict[str, list[dict]] = {}
    for s in store.all_seasons():
        by_div.setdefault(s["division"], []).append(s)
    return render(request, "seasons.html", title="Seasons", by_div=by_div)


def _season_or_404(request: web.Request) -> dict:
    try:
        guild_id = int(request.match_info["guild_id"])
    except ValueError:
        raise web.HTTPNotFound()
    season = store.get_season(guild_id, request.match_info["season_start"])
    if not season:
        raise web.HTTPNotFound()
    return season


@routes.get("/season/{guild_id}/{season_start}")
async def season_page(request: web.Request):
    season = _season_or_404(request)
    tab = request.query.get("tab", "teams")
    ctx: dict = {"season": season, "tab": tab, "title": season["label"]}
    if tab == "signups":
        ctx["signups"] = store.list_signups(season["guild_id"], season["season_start"])
    elif tab == "leaders":
        ctx["leaders"] = store.season_leaders(season)
    elif tab == "matches":
        matches = store.season_matches(season)
        ctx["matches"] = [m for m in matches if m["radiant_team"] or m["dire_team"]]
        ctx["other_matches"] = [m for m in matches if not (m["radiant_team"] or m["dire_team"])]
        ctx["team_names"] = {t["captain"]: t["name"] for t in store.season_teams(season, matches)}
    else:
        ctx["tab"] = "teams"
        teams = store.season_teams(season)
        idents = store.player_identities(
            [p["account_id"] for t in teams for p in t["roster"]]
            + [t["captain_account_id"] for t in teams if t["captain_account_id"]])
        ctx["teams"], ctx["idents"] = teams, idents
    if request.get("user"):
        ctx["my_signup"] = store.get_signup(season["guild_id"], season["season_start"],
                                            request["user"]["account_id"])
    return render(request, "season.html", **ctx)


@routes.get("/season/{guild_id}/{season_start}/team/{captain}")
async def team_page(request: web.Request):
    season = _season_or_404(request)
    captain = request.match_info["captain"]
    matches = store.season_matches(season)
    teams = {t["captain"]: t for t in store.season_teams(season, matches)}
    team = teams.get(captain)
    if not team:
        return not_found(request, "Team")
    team_matches = [m for m in matches if captain in (m["radiant_team"], m["dire_team"])]
    member_ids = [p["account_id"] for p in team["roster"]]
    if team["captain_account_id"]:
        member_ids.append(team["captain_account_id"])
    leaders = {p["account_id"]: p for p in store.season_leaders(season)}
    return render(request, "team.html", title=team["name"], season=season, team=team,
                  matches=team_matches, idents=store.player_identities(member_ids),
                  stats=leaders, team_names={c: t["name"] for c, t in teams.items()})


@routes.get("/players")
async def players_page(request: web.Request):
    q = request.query.get("q", "")
    return render(request, "players.html", title="Players", q=q, players=store.search_players(q))


@routes.get("/player/{account_id}")
async def player_page(request: web.Request):
    try:
        account_id = int(request.match_info["account_id"])
    except ValueError:
        return not_found(request, "Player")
    matches = store.player_matches(account_id)
    ident = store.player_identities([account_id])[account_id]
    site_user = store.get_user(account_id)
    if not matches and not site_user and not ident["name"]:
        return not_found(request, "Player")
    seasons = store.all_seasons()
    by_season: dict[tuple, list[dict]] = {}
    for m in matches:
        m["hero"] = store.hero(m["hero_id"])
        s = store._season_for(seasons, m["guild_id"], m["start_time"])
        m["season"] = s
        if s:
            by_season.setdefault((s["guild_id"], s["season_start"]), []).append(m)
    season_rows = []
    for s in seasons:
        rows = by_season.get((s["guild_id"], s["season_start"]))
        if rows:
            costs = db.get_player_costs(s["guild_id"], s["season_start"]).get(account_id) or {}
            cap = costs.get("captain")
            team = db.get_season_teams(s["guild_id"], s["season_start"]).get((cap or "").lower(), {})
            season_rows.append({"season": s, "stats": store.aggregate(rows), "captain": cap,
                                "team_name": team.get("team_name") or cap, "cost": costs.get("cost")})
    hero_counts: dict[int, dict] = {}
    for m in matches:
        h = hero_counts.setdefault(m["hero_id"], {"hero": m["hero"], "games": 0, "wins": 0})
        h["games"] += 1
        h["wins"] += m["won"]
    return render(request, "player.html", title=ident["name"] or str(account_id),
                  account_id=account_id, ident=ident, overall=store.aggregate(matches),
                  season_rows=season_rows, matches=matches[:50],
                  heroes=sorted(hero_counts.values(), key=lambda h: -h["games"])[:10])


@routes.get("/match/{match_id}")
async def match_page(request: web.Request):
    try:
        m = store.match_detail(int(request.match_info["match_id"]))
    except ValueError:
        m = None
    if not m:
        return not_found(request, "Match")
    return render(request, "match.html", title=f"Match {m['match_id']}", m=m)


# ---------------------------------------------------------------------------
# Signups
# ---------------------------------------------------------------------------

@routes.get("/season/{guild_id}/{season_start}/signup")
async def signup_form(request: web.Request):
    season = _season_or_404(request)
    user = _require_user(request)
    existing = store.get_signup(season["guild_id"], season["season_start"], user["account_id"])
    if not season["signups_open"] and not existing:
        return render(request, "message.html", title="Signups closed",
                      message="Signups for this season aren't open.")
    form = existing or {"discord": user.get("discord"), "positions": "", "captain": "no"}
    return render(request, "signup.html", title="Sign up", season=season, form=form,
                  existing=existing, errors=[])


@routes.post("/season/{guild_id}/{season_start}/signup")
async def signup_submit(request: web.Request):
    season = _season_or_404(request)
    user = _require_user(request)
    g, s, aid = season["guild_id"], season["season_start"], user["account_id"]
    existing = store.get_signup(g, s, aid)
    data = await request.post()
    if not season["signups_open"]:
        return render(request, "message.html", title="Signups closed",
                      message="Signups for this season are closed, so it can't be changed.")
    if data.get("action") == "withdraw":
        store.delete_signup(g, s, aid)
        raise web.HTTPFound(season_url(season) + "?tab=signups")

    errors = []
    positions = sorted({int(p) for p in data.getall("positions", []) if p.isdigit() and int(p) in store.POSITIONS})
    if not positions:
        errors.append("Pick at least one position.")
    captain = data.get("captain", "")
    if captain not in store.CAPTAIN_CHOICES:
        errors.append("Say whether you'd captain.")
    mmr_raw = (data.get("mmr") or "").strip()
    mmr = None
    if mmr_raw:
        if not mmr_raw.isdigit() or not 0 <= int(mmr_raw) <= 20000:
            errors.append("MMR should be a number, or left blank.")
        else:
            mmr = int(mmr_raw)
    discord = (data.get("discord") or "").strip()[:64]
    if not discord:
        errors.append("Enter your Discord name so admins and captains can reach you.")
    statement = (data.get("statement") or "").strip()[:1000]
    if errors:
        form = dict(data, positions=",".join(map(str, positions)))
        return render(request, "signup.html", status=400, title="Sign up", season=season,
                      form=form, existing=existing, errors=errors)
    store.save_signup(g, s, aid, mmr=mmr, positions=positions, captain=captain,
                      discord=discord, statement=statement or None)
    if not user.get("discord"):
        store.set_user_discord(aid, discord)
    raise web.HTTPFound(season_url(season) + "?tab=signups")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@routes.get("/admin")
async def admin_page(request: web.Request):
    # The top bar links here for everyone, so explain instead of refusing.
    if not request.get("is_admin"):
        return render(request, "admin_denied.html", title="Admin")
    divisions = store.division_names()
    with db._conn() as conn:
        div_rows = [dict(r) for r in conn.execute("SELECT * FROM divisions")]
    return render(request, "admin.html", title="Admin", seasons=store.all_seasons(),
                  divisions=divisions, div_rows=div_rows,
                  about=store.get_text("about"), rules=store.get_text("rules"))


@routes.post("/admin/season")
async def admin_new_season(request: web.Request):
    _require_admin(request)
    data = await request.post()
    try:
        guild_id = int(data["guild_id"])
        start = datetime.strptime(data["season_start"], "%Y-%m-%d").strftime("%Y-%m-%d")
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="Pick a division and a start date.")
    if guild_id not in store.division_names():
        raise web.HTTPBadRequest(text="Unknown division.")
    league_id = data.get("league_id", "").strip()
    store.create_season(guild_id, start, (data.get("label") or "").strip() or None,
                        int(league_id) if league_id.isdigit() else None)
    raise web.HTTPFound("/admin")


@routes.post("/admin/signups")
async def admin_toggle_signups(request: web.Request):
    _require_admin(request)
    data = await request.post()
    store.set_signups_open(int(data["guild_id"]), data["season_start"], data.get("open") == "1")
    raise web.HTTPFound(_safe_next(data.get("next")) if data.get("next") else "/admin")


@routes.post("/admin/division")
async def admin_division(request: web.Request):
    _require_admin(request)
    data = await request.post()
    name = (data.get("name") or "").strip()[:80]
    if name:
        store.set_division_name(int(data["guild_id"]), name)
    raise web.HTTPFound("/admin")


@routes.post("/admin/text")
async def admin_text(request: web.Request):
    _require_admin(request)
    data = await request.post()
    if data.get("key") in store.DEFAULT_TEXT:
        store.set_text(data["key"], (data.get("value") or "").strip())
    raise web.HTTPFound("/admin")


@routes.post("/admin/signup/delete")
async def admin_delete_signup(request: web.Request):
    _require_admin(request)
    data = await request.post()
    season = store.get_season(int(data["guild_id"]), data["season_start"])
    if season:
        store.delete_signup(season["guild_id"], season["season_start"], int(data["account_id"]))
        raise web.HTTPFound(season_url(season) + "?tab=signups")
    raise web.HTTPFound("/admin")


@routes.get("/season/{guild_id}/{season_start}/signups.csv")
async def admin_signups_csv(request: web.Request):
    _require_admin(request)
    season = _season_or_404(request)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Account ID", "Name", "Badge", "MMR", "Positions", "Captain", "Discord",
                "Statement", "Dotabuff", "Signed up"])
    for r in store.list_signups(season["guild_id"], season["season_start"]):
        w.writerow([r["account_id"], r["name"], r["badge"] or "", r["mmr"] or "",
                    " ".join(map(str, r["positions"])), r["captain"], r["discord"] or "",
                    r["statement"] or "", f"https://www.dotabuff.com/players/{r['account_id']}",
                    r["created_at"]])
    return web.Response(
        text=buf.getvalue(), content_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="signups-{season["season_start"]}.csv"'},
    )


# ---------------------------------------------------------------------------

@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPNotFound:
        return not_found(request)


def setup(app: web.Application) -> None:
    """Add the website's routes to the bot's aiohttp app."""
    store.init_site_tables()
    app.middlewares.append(error_middleware)
    app.middlewares.append(session_middleware)
    app.router.add_routes(routes)
    app.router.add_static("/static/", path=str(STATIC_DIR), show_index=False)
