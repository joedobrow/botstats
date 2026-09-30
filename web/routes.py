"""Page handlers for the league website."""

from __future__ import annotations

import csv
import io
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

import jinja2
from aiohttp import web

import db
from config import LEAGUE_TZ, STEAM_API_KEY
from player_api import account_id_from
from web import steam, store, uploads

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "session"

# Uberadmins (Dota account ids or Steam64 ids): everything admins can do,
# plus adding and removing admins. Set only here, so the site can't lock
# them out. Admins themselves are kept in the site_admins table.
UBERADMIN_IDS: set[int] = {
    aid for aid in (account_id_from(x) for x in os.environ.get("SITE_UBERADMIN_STEAM_IDS", "").split(","))
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
    """A season's page in one division: /season/<league id>/<division slug>."""
    return f"/season/{season['id']}/{season['division_slug']}"


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
                   POSITION_NAMES=POSITION_NAMES, SEASON_STATUSES=store.SEASON_STATUSES)


def render(request: web.Request, template: str, status: int = 200, **ctx) -> web.Response:
    ctx.update(user=request.get("user"), is_admin=request.get("is_admin", False),
               is_uberadmin=request.get("is_uberadmin", False),
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
        request["is_uberadmin"] = sess["account_id"] in UBERADMIN_IDS
        request["is_admin"] = request["is_uberadmin"] or store.is_admin(sess["account_id"])
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


def _require_uberadmin(request: web.Request) -> None:
    _require_user(request)
    if not request.get("is_uberadmin"):
        raise web.HTTPForbidden(text="Uberadmins only.")


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
    seasons = {(s["id"], s["division_id"]): s for s in store.all_seasons()}
    signups = [seasons[k] for k in store.signed_up_seasons(user["account_id"]) if k in seasons]
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
    seasons = sorted(store.all_seasons(), key=lambda s: s["season_start"], reverse=True)
    return render(request, "seasons.html", title="Seasons", seasons=seasons)


def _season_or_404(request: web.Request) -> dict:
    """The season (in one division) a /season/<league id>/<division slug>/... URL names."""
    season_id = request.match_info["season_id"]
    season = (store.get_season(int(season_id), request.match_info["division"])
              if season_id.isdigit() else None)
    if not season:
        raise web.HTTPNotFound()
    return season


def _form_season(data) -> dict:
    """The season (in one division) an admin form names by its season_id and division_id fields."""
    season_id, division_id = str(data.get("season_id") or ""), str(data.get("division_id") or "")
    season = next((s for s in store.all_seasons() if season_id.isdigit() and division_id.isdigit()
                   and s["id"] == int(season_id) and s["division_id"] == int(division_id)), None)
    if not season:
        raise web.HTTPBadRequest(text="Unknown season.")
    return season


@routes.get("/season/{season_id}")
async def season_any_division(request: web.Request):
    """/season/<league id>: straight to the division when the season has one,
    otherwise a choice of divisions."""
    season_id = request.match_info["season_id"]
    entries = store.season_divisions(int(season_id)) if season_id.isdigit() else []
    if not entries:
        raise web.HTTPNotFound()
    if len(entries) == 1:
        raise web.HTTPFound(season_url(entries[0]) + (f"?{request.query_string}" if request.query_string else ""))
    return render(request, "season_divisions.html", title=entries[0]["label"], entries=entries)


@routes.get("/season/{season_id}/{division}")
async def season_page(request: web.Request):
    season = _season_or_404(request)
    tab = request.query.get("tab", "teams")
    ctx: dict = {"season": season, "tab": tab, "title": season["label"]}
    if tab == "signups":
        ctx["signups"] = store.list_signups(season)
    elif tab == "leaders":
        ctx["leaders"], ctx["min_games"], ctx["max_games"] = store.season_leaders(season)
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
        ctx["my_signup"] = store.get_signup(season, request["user"]["account_id"])
    return render(request, "season.html", **ctx)


@routes.get("/season/{season_id}/{division}/team/{captain}")
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
    # Every row, alt copies included: a roster can list a player's alt.
    stats = {p["account_id"]: p for p in db.get_all_time_stats(season["division_id"], season["id"])}
    return render(request, "team.html", title=team["name"], season=season, team=team,
                  matches=team_matches, idents=store.player_identities(member_ids),
                  stats=stats, team_names={c: t["name"] for c, t in teams.items()},
                  can_edit=_can_edit_team(request, team), notice=request.query.get("notice"))


def _can_edit_team(request: web.Request, team: dict) -> bool:
    """Admins, and the team's own captain, may rename it and change its logo."""
    user = request.get("user")
    return bool(request.get("is_admin")
                or (user and team.get("captain_account_id") == user["account_id"]))


def _editable_team(request: web.Request) -> tuple[dict, dict]:
    season = _season_or_404(request)
    team = next((t for t in store.season_teams(season, matches=[])
                 if t["captain"] == request.match_info["captain"]), None)
    if not team:
        raise web.HTTPNotFound()
    _require_user(request)
    if not _can_edit_team(request, team):
        raise web.HTTPForbidden(text="Only an admin or this team's captain can change it.")
    return season, team


@routes.post("/season/{season_id}/{division}/team/{captain}/name")
async def team_rename(request: web.Request):
    season, team = _editable_team(request)
    name = ((await request.post()).get("label") or "").strip()[:60]
    if name:
        store.set_team_identity(season, team, name=name)
    raise web.HTTPFound(team_url(season, team["captain"]))


@routes.post("/season/{season_id}/{division}/team/{captain}/logo")
async def team_logo(request: web.Request):
    season, team = _editable_team(request)
    upload = (await request.post()).get("logo")
    if not isinstance(upload, web.FileField):
        raise web.HTTPFound(team_url(season, team["captain"]) + "?notice=Choose+an+image+file.")
    try:
        url = uploads.save_team_logo(upload.file.read(), season["id"], season["division_id"], team["captain"])
    except uploads.BadImage as e:
        raise web.HTTPFound(team_url(season, team["captain"]) + "?" + urlencode({"notice": str(e)}))
    old = team.get("logo_url")
    store.set_team_identity(season, team, logo_url=url)
    if old != url:
        uploads.delete_upload(old)
    raise web.HTTPFound(team_url(season, team["captain"]))


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
        s = store.season_of_match(seasons, m)
        m["season"] = s
        if s:
            by_season.setdefault((s["id"], s["division_id"]), []).append(m)
    season_rows = []
    for s in seasons:
        rows = by_season.get((s["id"], s["division_id"]))
        if rows:
            costs = db.get_player_costs(s["id"], s["division_id"]).get(account_id) or {}
            cap = costs.get("captain")
            team = db.get_season_teams(s["id"], s["division_id"]).get((cap or "").lower(), {})
            # The bot's season stats for this player, so the two agree.
            stats = next((r for r in db.get_all_time_stats(s["division_id"], s["id"])
                          if r["account_id"] == account_id), None) or store.aggregate(rows)
            season_rows.append({"season": s, "stats": stats, "captain": cap,
                                "team_name": team.get("team_name") or cap, "cost": costs.get("cost")})
    return render(request, "player.html", title=ident["name"] or str(account_id),
                  account_id=account_id, ident=ident, overall=store.aggregate(matches),
                  season_rows=season_rows, matches=matches[:50],
                  rating=store.player_ratings().get(account_id))


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

@routes.get("/season/{season_id}/{division}/signup")
async def signup_form(request: web.Request):
    season = _season_or_404(request)
    user = _require_user(request)
    existing = store.get_signup(season, user["account_id"])
    if not season["signups_open"] and not existing:
        return render(request, "message.html", title="Signups closed",
                      message="Signups for this season aren't open.")
    form = existing or {"discord": user.get("discord"), "positions": "", "captain": "no"}
    return render(request, "signup.html", title="Sign up", season=season, form=form,
                  existing=existing, errors=[])


@routes.post("/season/{season_id}/{division}/signup")
async def signup_submit(request: web.Request):
    season = _season_or_404(request)
    user = _require_user(request)
    aid = user["account_id"]
    existing = store.get_signup(season, aid)
    data = await request.post()
    if not season["signups_open"]:
        return render(request, "message.html", title="Signups closed",
                      message="Signups for this season are closed, so it can't be changed.")
    if data.get("action") == "withdraw":
        store.delete_signup(season, aid)
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
    store.save_signup(season, aid, mmr=mmr, positions=positions, captain=captain,
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
    admins = []
    if request.get("is_uberadmin"):
        rows = store.list_admins()
        ids = sorted(UBERADMIN_IDS) + [r["account_id"] for r in rows if r["account_id"] not in UBERADMIN_IDS]
        idents = store.player_identities(ids)
        added_by = {r["account_id"]: r for r in rows}
        admins = [{"account_id": aid, "name": idents[aid]["name"], "uberadmin": aid in UBERADMIN_IDS,
                   "added_at": (added_by.get(aid) or {}).get("added_at")} for aid in ids]
    return render(request, "admin.html", title="Admin", seasons=store.all_seasons(),
                  divisions=store.divisions(), admins=admins,
                  about=store.get_text("about"), rules=store.get_text("rules"))


@routes.post("/admin/season/rename")
async def admin_rename_season(request: web.Request):
    _require_admin(request)
    data = await request.post()
    season = _form_season(data)
    label = (data.get("label") or "").strip()[:80]
    if label:
        store.rename_season(season["id"], label)
    raise web.HTTPFound(_safe_next(data.get("next")) if data.get("next") else "/admin")


@routes.post("/admin/season/start")
async def admin_change_season_start(request: web.Request):
    _require_uberadmin(request)
    data = await request.post()
    season = _form_season(data)
    new_start = (data.get("new_start") or "").strip()
    if new_start != season["season_start"]:
        try:
            store.set_season_start(season["id"], new_start)
        except ValueError as e:
            return render(request, "message.html", status=400, title="Start date not changed",
                          message=str(e) if "already" in str(e) else "That isn't a valid date.")
    raise web.HTTPFound(_safe_next(data.get("next")) if data.get("next") else "/admin")


@routes.post("/admin/admins")
async def admin_manage_admins(request: web.Request):
    _require_uberadmin(request)
    data = await request.post()
    aid = account_id_from(data.get("account_id", ""))
    if aid and aid not in UBERADMIN_IDS:
        if data.get("action") == "remove":
            store.remove_admin(aid)
        else:
            store.add_admin(aid, request["user"]["account_id"])
    raise web.HTTPFound("/admin#admins")


@routes.post("/admin/season")
async def admin_new_season(request: web.Request):
    _require_admin(request)
    data = await request.post()
    try:
        division_id = int(data["division_id"])
        league_id = int(data["league_id"])
        start = datetime.strptime(data["season_start"], "%Y-%m-%d").strftime("%Y-%m-%d")
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="Pick a division, and enter the league ID and a start date.")
    if not db.get_division(division_id):
        raise web.HTTPBadRequest(text="Unknown division.")
    try:
        store.create_season(league_id, division_id, start, (data.get("label") or "").strip() or None)
    except ValueError as e:
        return render(request, "message.html", status=400, title="Season not created", message=str(e))
    raise web.HTTPFound("/admin")


@routes.post("/admin/status")
async def admin_set_status(request: web.Request):
    _require_admin(request)
    data = await request.post()
    if data.get("status") not in store.SEASON_STATUSES:
        raise web.HTTPBadRequest(text="Unknown status.")
    season = _form_season(data)
    store.set_status(season["id"], season["division_id"], data["status"])
    raise web.HTTPFound(_safe_next(data.get("next")) if data.get("next") else "/admin")


@routes.post("/admin/division")
async def admin_division(request: web.Request):
    _require_admin(request)
    data = await request.post()
    name = (data.get("name") or "").strip()[:80]
    slug = re.sub(r"[^a-z0-9]+", "-", (data.get("slug") or name).lower()).strip("-")[:40]
    division = db.get_division(int(data.get("division_id") or 0))
    if not division:
        raise web.HTTPBadRequest(text="Unknown division.")
    if name and slug:
        try:
            store.rename_division(division["id"], name, slug)
        except ValueError as e:
            return render(request, "message.html", status=400, title="Division not renamed", message=str(e))
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
    season = _form_season(data)
    store.delete_signup(season, int(data["account_id"]))
    raise web.HTTPFound(season_url(season) + "?tab=signups")


@routes.get("/season/{season_id}/{division}/signups.csv")
async def admin_signups_csv(request: web.Request):
    _require_admin(request)
    season = _season_or_404(request)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Account ID", "Name", "Badge", "MMR", "Positions", "Captain", "Discord",
                "Statement", "Dotabuff", "Signed up"])
    for r in store.list_signups(season):
        w.writerow([r["account_id"], r["name"], r["badge"] or "", r["mmr"] or "",
                    " ".join(map(str, r["positions"])), r["captain"], r["discord"] or "",
                    r["statement"] or "", f"https://www.dotabuff.com/players/{r['account_id']}",
                    r["created_at"]])
    return web.Response(
        text=buf.getvalue(), content_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="signups-{season["id"]}-{season["division_slug"]}.csv"'},
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
    uploads.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    app.router.add_static(uploads.URL_PREFIX, path=str(uploads.UPLOAD_DIR), show_index=False)
