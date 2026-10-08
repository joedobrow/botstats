"""Page handlers for the league website."""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import jinja2
from aiohttp import web
from markupsafe import Markup

import db
from config import GAME_MODE_FILTERS, LEAGUE_TZ, REGION_CLUSTERS, STEAM_API_KEY
from player_api import account_id_from
from web import markup, steam, store, uploads

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

logger = logging.getLogger(__name__)
routes = web.RouteTableDef()


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _fmt_date(ts: int | None, fmt: str = "%b %-d, %Y") -> str:
    return datetime.fromtimestamp(ts, LEAGUE_TZ).strftime(fmt) if ts else ""


def _fmt_duration(seconds: int | float | None) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


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
env.filters.update(date=_fmt_date, duration=_fmt_duration,
                   markdown=lambda text: Markup(markup.render(text)))
def rings(account_id: int | None) -> Markup:
    """One ring per season won, for after a player's name."""
    n = store.champion_counts().get(account_id) if account_id else 0
    if not n:
        return Markup("")
    return Markup('<span class="rings" title="Won {} season{}">{}</span>'.format(
        n, "" if n == 1 else "s", "\N{RING}" * n))


env.globals.update(season_url=season_url, team_url=team_url, hero=store.hero, rings=rings,
                   POSITION_NAMES=POSITION_NAMES, SEASON_STATUSES=store.SEASON_STATUSES)


def _asset_version() -> int:
    """The newest mtime in static/, as the cache-busting query for every asset
    the pages link to. A dozen stats per render, against a deploy that would
    otherwise be invisible to a browser holding the old file."""
    return int(max((p.stat().st_mtime for p in STATIC_DIR.iterdir() if p.is_file()), default=0))


def render(request: web.Request, template: str, status: int = 200, **ctx) -> web.Response:
    ctx.update(user=request.get("user"), is_admin=request.get("is_admin", False),
               is_uberadmin=request.get("is_uberadmin", False),
               csrf=request.get("csrf", ""), path=request.path,
               # Changes whenever any static file does. Keying it to the
               # stylesheet alone meant a new favicon or logo kept the old
               # query, so browsers went on serving the copy they had.
               asset_version=_asset_version())
    html = env.get_template(template).render(**ctx)
    return web.Response(text=html, content_type="text/html", status=status)


def not_found(request: web.Request, what: str = "Page") -> web.Response:
    # The only message page that gets the mascot: being lost is what he's for.
    # A failed form or a closed signup is an answer, not a confusion.
    return render(request, "message.html", status=404, title="Not found",
                  message=f"{what} not found.", mascot=True)


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
    tab = request.query.get("tab", "matchups")
    ctx: dict = {"season": season, "tab": tab, "title": season["label"]}
    if tab == "playoffs":
        ctx["bracket"] = store.playoff_bracket(season)
        ctx["standings"] = store.season_teams(season)
        ctx["round_name"] = store.round_name
        ctx["notice"] = request.query.get("notice")
    elif tab == "matchups":
        ctx["rounds"] = store.season_matchups(season)
        ctx["weeks"] = store.season_weeks(season)
        ctx["notice"] = request.query.get("notice")
    elif tab == "signups":
        ctx["signups"] = store.list_signups(season)
    elif tab == "champions":
        ctx["champions"] = store.season_champions(season)
        ctx["season_team_names"] = sorted(t["name"] for t in store.season_teams(season))
        ctx["notice"] = request.query.get("notice")
    elif tab == "leaders":
        ctx["leaders"], ctx["min_games"], ctx["max_games"] = store.season_leaders(season)
    elif tab == "matches":
        matches = store.season_matches(season)
        attributed = [m for m in matches if m["radiant_team"] or m["dire_team"]]
        unattributed = [m for m in matches if not (m["radiant_team"] or m["dire_team"])]
        # The first twenty of each; the rest are a filtered Matches page away.
        ctx["matches"], ctx["other_matches"] = attributed[:20], unattributed[:20]
        ctx["match_total"] = len(matches)
        ctx["more_matches"] = len(attributed) > 20 or len(unattributed) > 20
        ctx["team_names"] = {t["captain"]: t["name"] for t in store.season_teams(season, matches)}
    elif tab == "teams":
        teams = store.season_teams(season)
        idents = store.player_identities(
            [p["account_id"] for t in teams for p in t["roster"]]
            + [t["captain_account_id"] for t in teams if t["captain_account_id"]])
        ctx["teams"], ctx["idents"] = teams, idents
    else:
        ctx["tab"] = "matchups"
        ctx["rounds"] = store.season_matchups(season)
    if request.get("user"):
        ctx["my_signup"] = store.get_signup(season, request["user"]["account_id"])
    return render(request, "season.html", **ctx)


@routes.get("/favicon.ico")
async def favicon(request: web.Request):
    """Browsers ask for this path whether or not the page links to it — and
    without the version query the <link> carries, so a long max-age here undoes
    the cache-busting everywhere else. no-cache means revalidate, not don't
    store: the ETag makes that a 304 almost every time, and an icon that
    changed arrives the next time the page is opened rather than tomorrow."""
    return web.FileResponse(STATIC_DIR / "favicon.ico",
                            headers={"Cache-Control": "no-cache"})


@routes.get("/robots.txt")
async def robots(request: web.Request):
    """Served from the root, which is the only place a crawler looks."""
    return web.FileResponse(STATIC_DIR / "robots.txt",
                            headers={"Content-Type": "text/plain; charset=utf-8",
                                     "Cache-Control": "no-cache"})


@routes.get("/teams")
async def teams_page(request: web.Request):
    """Every team of one season, with what they average a game. The season
    defaults to the one being played, so the teams are one click away."""
    seasons = store.all_seasons()
    q = request.query
    season = next((s for s in seasons if str(s["id"]) == q.get("season", "")
                   and (not q.get("division") or s["division_slug"] == q["division"])), None)
    if season is None:
        current = store.current_seasons()
        season = current[0] if current else (seasons[0] if seasons else None)
    teams, stats, idents = [], {}, {}
    if season:
        teams = store.season_teams(season)
        stats = store.team_stats(season)
        idents = store.player_identities(
            [p["account_id"] for t in teams for p in t["roster"]]
            + [t["captain_account_id"] for t in teams if t["captain_account_id"]])
    return render(request, "teams.html", title="Teams", season=season, seasons=seasons,
                  teams=teams, stats=stats, idents=idents)


@routes.get("/hall-of-fame")
async def hall_of_fame(request: web.Request):
    return render(request, "hall_of_fame.html", title="Hall of Fame",
                  champions=store.all_champions())


def _weeks_redirect(season: dict, notice: str):
    return web.HTTPFound(f"{season_url(season)}?tab=matchups&" + urlencode({"notice": notice}))


@routes.post("/season/{season_id}/{division}/weeks/generate")
async def season_weeks_generate(request: web.Request):
    """Lay out N weeks as plain seven-day blocks. Which number the first one
    takes matters: a season that opens with a scrim week calls it week 0, so
    that week 1 is the first that counts."""
    season = _season_or_404(request)
    _require_admin(request)
    data = await request.post()
    try:
        weeks = int(data["weeks"])
        first = int(data.get("first_week", 1))
    except (KeyError, ValueError):
        raise _weeks_redirect(season, "How many weeks?")
    if not 1 <= weeks <= 30 or first not in (0, 1):
        raise _weeks_redirect(season, "Between 1 and 30 weeks, numbered from 0 or 1.")
    db.generate_season_weeks(season["id"], season["division_id"], weeks, first_week=first)
    raise _weeks_redirect(season, f"Weeks {first}–{first + weeks - 1} laid out from the season's start.")


@routes.post("/season/{season_id}/{division}/weeks/set")
async def season_weeks_set(request: web.Request):
    """Define or move one week — a bye, a reschedule, a week that ran long."""
    season = _season_or_404(request)
    _require_admin(request)
    data = await request.post()
    try:
        week = int(data["week"])
    except (KeyError, ValueError):
        raise _weeks_redirect(season, "Which week?")
    try:
        store.set_season_week(season["id"], season["division_id"], week,
                              data.get("start", ""), data.get("end", ""))
    except ValueError as e:
        raise _weeks_redirect(season, str(e))
    raise _weeks_redirect(season, f"Week {week} set.")


@routes.post("/season/{season_id}/{division}/weeks/delete")
async def season_weeks_delete(request: web.Request):
    """Drop one week's definition. The calendar formula takes over again."""
    season = _season_or_404(request)
    _require_admin(request)
    data = await request.post()
    try:
        week = int(data["week"])
    except (KeyError, ValueError):
        raise _weeks_redirect(season, "Which week?")
    removed = db.delete_season_week(season["id"], season["division_id"], week)
    raise _weeks_redirect(season, f"Week {week} removed." if removed
                          else f"Week {week} wasn't defined.")


@routes.post("/season/{season_id}/{division}/champions")
async def season_champions_set(request: web.Request):
    """Record who won a season. Names are matched to accounts where they can
    be; one that can't is kept as typed rather than dropped."""
    season = _season_or_404(request)
    _require_admin(request)
    data = await request.post()
    members = store.resolve_champion_names(data.get("winners", ""))
    store.set_season_champions(season["id"], season["division_id"], members,
                               team=data.get("team", ""))
    if not members:
        notice = "Champions cleared."
    else:
        unknown = [m["name"] for m in members if not m["account_id"]]
        notice = f"Recorded {len(members)} winners."
        if unknown:
            notice += (" No account here matches " + ", ".join(unknown)
                       + " — shown as typed. Paste an account id instead to link them.")
    raise web.HTTPFound(f"{season_url(season)}?tab=champions&" + urlencode({"notice": notice}))


@routes.post("/season/{season_id}/{division}/playoffs/start")
async def playoffs_start(request: web.Request):
    """Open a bracket. How many teams is a decision; who plays whom is the
    next one, and that's made by dragging teams into the first round."""
    season = _season_or_404(request)
    _require_admin(request)
    data = await request.post()
    try:
        size = int(data["size"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="Pick how many teams are in the bracket.")
    try:
        db.start_playoffs(season["id"], season["division_id"], size=size,
                          started_at=datetime.now(LEAGUE_TZ).isoformat(),
                          created_by=request["user"]["account_id"])
    except ValueError as e:
        raise web.HTTPFound(f"{season_url(season)}?tab=playoffs&"
                            + urlencode({"notice": str(e)}))
    raise web.HTTPFound(f"{season_url(season)}?tab=playoffs&"
                        + urlencode({"notice": "Bracket opened — place the first round."}))


@routes.post("/season/{season_id}/{division}/playoffs/seed")
async def playoffs_seed(request: web.Request):
    """Place the first round. Seats can be left empty while captains pick."""
    season = _season_or_404(request)
    _require_admin(request)
    bracket = db.get_playoff_bracket(season["id"], season["division_id"])
    if not bracket:
        raise web.HTTPFound(f"{season_url(season)}?tab=playoffs")
    data = await request.post()
    seats = [(data.get(f"seat{i}") or "").strip() or None for i in range(bracket["size"])]
    known = {t["captain"] for t in store.season_teams(season, matches=[])}
    if any(c and c not in known for c in seats):
        raise web.HTTPBadRequest(text="That isn't a team in this season.")
    try:
        db.set_playoff_seats(season["id"], season["division_id"], seats)
    except ValueError as e:
        raise web.HTTPFound(f"{season_url(season)}?tab=playoffs&" + urlencode({"notice": str(e)}))
    raise web.HTTPFound(f"{season_url(season)}?tab=playoffs&"
                        + urlencode({"notice": "Bracket saved."}))


@routes.post("/season/{season_id}/{division}/playoffs/delete")
async def playoffs_delete(request: web.Request):
    season = _season_or_404(request)
    _require_admin(request)
    db.delete_playoff_bracket(season["id"], season["division_id"])
    raise web.HTTPFound(f"{season_url(season)}?tab=playoffs&"
                        + urlencode({"notice": "Bracket removed."}))


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
                  stats=stats, ratings=db.displayed_ratings(season["division_id"]),
                  team_names={c: t["name"] for c, t in teams.items()},
                  opponents=[(c, t["name"]) for c, t in sorted(teams.items(), key=lambda kv: kv[1]["name"].lower())],
                  can_edit=_can_edit_team(request, team), notice=request.query.get("notice"),
                  free_agents=store.free_agents(season) if request.get("is_admin") else [])


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


def _roster_redirect(season: dict, captain: str, notice: str):
    return web.HTTPFound(f"{team_url(season, captain)}?" + urlencode({"notice": notice}))


def _roster_target(request: web.Request, data) -> int:
    """The account a roster form names: a pick from the list, or an id typed in."""
    raw = (data.get("account_id") or data.get("player") or "").strip()
    account_id = account_id_from(raw)
    if not account_id:
        raise ValueError("Pick a player, or paste a Dota account id or Steam64 id.")
    return account_id


@routes.post("/season/{season_id}/{division}/team/{captain}/roster/replace")
async def roster_replace(request: web.Request):
    """Hand one player's slot to another, at the same price."""
    season = _season_or_404(request)
    _require_admin(request)
    captain = request.match_info["captain"]
    data = await request.post()
    try:
        out_id = int(data["out"])
        in_id = _roster_target(request, data)
        slot = db.replace_roster_player(season["id"], season["division_id"], out_id, in_id)
    except (KeyError, ValueError) as e:
        raise _roster_redirect(season, captain, str(e) or "Couldn't read that.")
    names = store.player_identities([out_id, in_id])
    raise _roster_redirect(season, captain,
                           f"{names[in_id]['name']} takes {names[out_id]['name']}'s "
                           f"${slot['cost']} slot.")


@routes.post("/season/{season_id}/{division}/team/{captain}/roster/remove")
async def roster_remove(request: web.Request):
    season = _season_or_404(request)
    _require_admin(request)
    captain = request.match_info["captain"]
    data = await request.post()
    try:
        account_id = int(data["account_id"])
    except (KeyError, ValueError):
        raise _roster_redirect(season, captain, "Which player?")
    name = store.player_identities([account_id])[account_id]["name"]
    removed = db.remove_roster_player(season["id"], season["division_id"], account_id)
    raise _roster_redirect(season, captain,
                           f"{name} is off the roster." if removed else "They weren't on it.")


@routes.post("/season/{season_id}/{division}/team/{captain}/roster/add")
async def roster_add(request: web.Request):
    season = _season_or_404(request)
    _require_admin(request)
    captain = request.match_info["captain"]
    data = await request.post()
    try:
        account_id = _roster_target(request, data)
        cost = int(re.sub(r"\D", "", data.get("cost", "")) or 0)
        db.add_roster_player(season["id"], season["division_id"], account_id, captain, cost)
    except ValueError as e:
        raise _roster_redirect(season, captain, str(e) or "Couldn't read that.")
    name = store.player_identities([account_id])[account_id]["name"]
    raise _roster_redirect(season, captain, f"{name} joins at ${cost}.")


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


@routes.post("/season/{season_id}/{division}/team/{captain}/match/{match_id}/opponent")
async def match_opponent(request: web.Request):
    """Say which team played the other side of a match. Three of a team's five
    have to play a side for it to be attributed automatically, so a night of
    stand-ins leaves a real result credited to nobody."""
    season = _season_or_404(request)
    _require_admin(request)
    captain = request.match_info["captain"]
    match_id = int(request.match_info["match_id"])
    form = await request.post()
    side = form.get("side")
    choice = (form.get("captain") or "").strip()
    if side not in ("radiant", "dire"):
        raise web.HTTPBadRequest(text="side must be radiant or dire")
    if choice == "__auto__":
        db.set_match_team_override(match_id, side, None, automatic=True)
        notice = f"Match {match_id}: opponent back to whatever the rosters say."
    elif choice == "__standins__":
        db.set_match_team_override(match_id, side, None, set_by=request["user"]["account_id"])
        notice = f"Match {match_id}: opponent recorded as stand-ins."
    else:
        teams = {t["captain"] for t in store.season_teams(season, matches=[])}
        if choice not in teams:
            raise web.HTTPBadRequest(text=f"{choice!r} isn't a team this season")
        db.set_match_team_override(match_id, side, choice, set_by=request["user"]["account_id"])
        notice = f"Match {match_id}: opponent set to {choice}."
    raise web.HTTPFound(team_url(season, captain) + "?" + urlencode({"notice": notice}))


@routes.post("/season/{season_id}/{division}/team/{captain}/match/record")
async def match_record(request: web.Request):
    """Enter one game that has no Dota match to fetch. Most games that go
    missing were never ticketed, so there is nothing to look up — only who
    played and who won.

    One game, not a night's score: a night can be half ticketed, and the
    missing half is what needs entering."""
    season = _season_or_404(request)
    _require_admin(request)
    captain = request.match_info["captain"]
    back = team_url(season, captain)
    data = await request.post()

    teams = {t["captain"]: t for t in store.season_teams(season, matches=[])}
    opponent = (data.get("opponent") or "").strip()
    if opponent not in teams or opponent == captain:
        raise web.HTTPFound(back + "?" + urlencode({"notice": "Pick which team they played."}))
    result = data.get("result")
    if result not in ("won", "lost"):
        raise web.HTTPFound(back + "?" + urlencode({"notice": "Say who won the game."}))
    try:
        # Midday in the league's own timezone, so the result lands in the week
        # it was played whatever the UTC offset does to the boundaries.
        day = datetime.strptime((data.get("played") or "").strip(), "%Y-%m-%d")
        played_at = int(day.replace(hour=12, tzinfo=LEAGUE_TZ).timestamp())
    except ValueError:
        raise web.HTTPFound(back + "?" + urlencode({"notice": "Give the date it was played."}))

    winner, loser = (captain, opponent) if result == "won" else (opponent, captain)
    db.add_manual_match(division_id=season["division_id"], league_id=season["id"],
                        start_time=played_at, winner=winner, loser=loser,
                        set_by=request["user"]["account_id"])
    verb = "Win" if result == "won" else "Loss"
    raise web.HTTPFound(back + "?" + urlencode({
        "notice": f"{verb} against {teams[opponent]['name']} recorded. "
                  f"Add another if more than one game is missing."}))


@routes.post("/season/{season_id}/{division}/team/{captain}/match/{match_id}/delete")
async def match_delete(request: web.Request):
    """Take a game out of the league's reckoning.

    A hand-entered one is deleted, since nothing would bring it back. A real
    one is excluded instead: the row is a game that happened, and deleting it
    would only mean fetching it again next time the league is pulled.
    """
    season = _season_or_404(request)
    _require_admin(request)
    captain = request.match_info["captain"]
    match_id = int(request.match_info["match_id"])
    reason = ((await request.post()).get("reason") or "").strip()[:200] or None
    if match_id < 0:
        done = db.delete_manual_match(match_id)
        notice = "Game removed." if done else "That game was already gone."
    else:
        done = db.set_match_excluded(match_id, True, reason=reason,
                                     by=request["user"]["account_id"])
        notice = (f"Game {match_id} no longer counts — records, stats and matchups all skip it, "
                  f"and re-fetching the league won't bring it back. Find it again under "
                  f"Matches → Kind → Taken out." if done else f"No game {match_id}.")
    raise web.HTTPFound(team_url(season, captain) + "?" + urlencode({"notice": notice}))


@routes.post("/matches/{match_id}/restore")
async def match_restore(request: web.Request):
    """Put an excluded game back."""
    _require_admin(request)
    match_id = int(request.match_info["match_id"])
    db.set_match_excluded(match_id, False)
    raise web.HTTPFound("/matches?" + urlencode({"kind": "excluded",
                                                 "notice": f"Game {match_id} counts again."}))


@routes.post("/season/{season_id}/{division}/team/{captain}/match/add")
async def match_add(request: web.Request):
    """Pull in a game that was played but never ticketed, by its match id."""
    from fetcher import fetch_and_store_match

    season = _season_or_404(request)
    _require_admin(request)
    captain = request.match_info["captain"]
    raw = ((await request.post()).get("match_id") or "").strip()
    # People paste whole Dotabuff/OpenDota URLs as often as bare ids.
    digits = re.sub(r"\D", "", raw.rsplit("/", 1)[-1] if "/" in raw else raw)
    back = team_url(season, captain)
    if not digits:
        raise web.HTTPFound(back + "?" + urlencode({"notice": "That isn't a match id."}))
    try:
        added = await fetch_and_store_match(int(digits), season["division_id"], league_id=season["id"])
    except ValueError as e:
        raise web.HTTPFound(back + "?" + urlencode({"notice": str(e)}))
    except Exception:
        logger.exception("manual match add failed for %s", digits)
        raise web.HTTPFound(back + "?" + urlencode({
            "notice": "Couldn't reach OpenDota for that match. Try again in a minute."}))
    notice = (f"Match {added['match_id']} " + ("re-fetched" if added["already"] else "added")
              + f" with {added['players']} players. If its sides aren't attributed, "
                f"set the teams on the rows below.")
    raise web.HTTPFound(back + "?" + urlencode({"notice": notice}))


# Looking up an account nobody has rated costs a windrun call, serialised at
# one every five seconds, and the site is public — so anonymous visitors share
# a budget. Admins don't, since they're the ones doing league admin.
_PUBLIC_LOOKUPS_PER_HOUR = 30
_public_lookups: list[float] = []


def _may_rate_unknown(request: web.Request) -> bool:
    if request.get("is_admin"):
        return True
    now = time.time()
    _public_lookups[:] = [t for t in _public_lookups if now - t < 3600]
    if len(_public_lookups) >= _PUBLIC_LOOKUPS_PER_HOUR:
        return False
    _public_lookups.append(now)
    return True


@routes.get("/players")
async def players_page(request: web.Request):
    q = request.query.get("q", "")
    players = store.search_players(q)
    # "Look up" means: go straight to this player's page, the way /player in
    # Discord does — by account id, by Steam64 id, or by a name that matches
    # exactly one person. Filtering as you type is for browsing; this is for
    # when you know who you want.
    if request.query.get("lookup") is not None and q.strip():
        account_id = account_id_from(q)
        if account_id is None and len(players) == 1:
            account_id = players[0]["account_id"]
        if account_id is not None:
            raise web.HTTPFound(f"/player/{account_id}")
        return render(request, "players.html", title="Players", q=q, players=players,
                      notice=("No one by that name. A Dota account id or a Steam64 id goes "
                              "straight to a player, whether or not they've played here."
                              if not players else
                              "That matches more than one player — pick one below."))
    return render(request, "players.html", title="Players", q=q, players=players, notice=None)


@routes.get("/fantasy")
async def fantasy_page(request: web.Request):
    """What the fantasy score is made of, and who earns it where."""
    import fantasy

    from formatters import STAT_LABELS

    q = request.query
    seasons = store.all_seasons()
    divisions = store.divisions()
    # A season row is one season in one division, so the two are picked
    # separately: the season names the league, the division names whose games
    # in it. Listing them together made a season read as "Season 39 · NA".
    season_options = list({s["id"]: s for s in seasons}.values())
    division_id = int(re.sub(r"\D", "", q.get("division", "")) or 0) or None
    season = None
    if q.get("season"):
        matches = [s for s in seasons if str(s["id"]) == q["season"]]
        season = (next((s for s in matches if s["division_id"] == division_id), None)
                  or (matches[0] if matches else None))
    stat = q.get("stat") if q.get("stat") in STAT_LABELS else "fantasy_points"
    # Value and attendance are measured against a draft — what a player cost
    # and how much of their team's season they turned up for — so pooled
    # across seasons there's nothing to measure against and the table comes
    # out empty. Scope them to a season, the way the bot always does.
    draft_relative = stat in store.DRAFT_RELATIVE
    if draft_relative and season is None and seasons:
        season = max(seasons, key=lambda s: (s["season_start"], s["id"]))
    week = re.sub(r"\D", "", q.get("week", ""))
    position = re.sub(r"\D", "", q.get("position", ""))
    weeks = store.weeks_with_games(season) if season else []
    # Without a season chosen, a division still narrows which games count.
    if season is None and division_id:
        store_divisions = [d for d in divisions if d["id"] == division_id]
    else:
        store_divisions = None
    min_games = re.sub(r"\D", "", q.get("min_games", ""))
    table = store.fantasy_table(season, stat=stat,
                                week=int(week) if week and season else None,
                                position=int(position) if position else None,
                                min_games=int(min_games) if min_games else None,
                                only_divisions=store_divisions)
    # Naming a player is a way of getting to their page, not a filter on this
    # one — the working is its own page.
    selected = None
    if q.get("player"):
        account_id = account_id_from(q["player"])
        selected = next((r for r in table["everyone"]
                         if r["account_id"] == account_id
                         or q["player"].lower() in (r["name"] or "").lower()), None)
        if selected:
            keep = {k: v for k, v in q.items() if k in ("season", "week", "position", "stat")}
            raise web.HTTPFound(f"/fantasy/{selected['account_id']}"
                                + ("?" + urlencode(keep) if keep else ""))
    # The bot's labels carry an emoji each, which belongs in a Discord embed
    # rather than in a dropdown.
    stats = sorted(((key, re.sub(r"^[^\w(]+", "", label).strip())
                    for key, label in STAT_LABELS.items()), key=lambda kv: kv[1].lower())
    return render(request, "fantasy.html", title="Fantasy", table=table,
                  seasons=season_options, divisions=divisions, division_id=division_id,
                  season=season, draft_relative=draft_relative, q=q, stats=stats, stat=stat,
                  stat_label=dict(stats).get(stat, stat), weeks=weeks,
                  not_found=bool(q.get("player")),
                  labels=fantasy.LABELS, duration_mean=fantasy.DURATION_MEAN_MIN)


@routes.get("/fantasy/formula")
async def fantasy_formula_page(request: web.Request):
    """The whole scoring formula, with what each part is worth and what the
    league actually earns from it. Declared above /fantasy/{account_id} so the
    word "formula" isn't read as an account id."""
    import fantasy

    table = store.fantasy_table(None)
    rows = sorted(table["field"].values(), key=lambda f: -abs(f["points"]))
    return render(request, "fantasy_formula.html", title="The fantasy formula",
                  rows=rows, players=len(table["rows"]),
                  duration_slope=fantasy.DURATION_SLOPE,
                  duration_mean=fantasy.DURATION_MEAN_MIN,
                  death_base=fantasy.WEIGHTS["death_base"],
                  death_each=fantasy.WEIGHTS["deaths_per_game"])


@routes.get("/fantasy/{account_id}")
async def fantasy_player_page(request: web.Request):
    """One player's fantasy score, taken apart. Its own page: the filters and
    the league-wide tables are for choosing someone, and once you have, they
    are in the way."""
    import fantasy

    try:
        account_id = int(request.match_info["account_id"])
    except ValueError:
        return not_found(request, "Player")
    q = request.query
    seasons = store.all_seasons()
    season = next((s for s in seasons if str(s["id"]) == q.get("season", "")), None)
    week = re.sub(r"\D", "", q.get("week", ""))
    position = re.sub(r"\D", "", q.get("position", ""))
    table = store.fantasy_table(season, week=int(week) if week and season else None,
                                position=int(position) if position else None, min_games=0)
    player = next((r for r in table["everyone"] if r["account_id"] == account_id), None)
    if not player:
        return not_found(request, "Player")
    back = "/fantasy" + ("?" + urlencode({k: v for k, v in q.items()
                                          if k in ("season", "week", "position", "stat")}) if q else "")
    return render(request, "fantasy_player.html", title=player["name"], player=player,
                  season=season, back=back, field=table["field"],
                  rating=store.player_ratings().get(account_id),
                  duration_mean=fantasy.DURATION_MEAN_MIN)


@routes.get("/matches")
async def matches_page(request: web.Request):
    """Every match, filtered. The filters live in the query string so a view
    is a link — which is what the More buttons on a player and a season page
    hand you."""
    q = request.query

    def number(name: str) -> int | None:
        raw = re.sub(r"\D", "", q.get(name, ""))
        return int(raw) if raw else None

    # Games an admin took out are an admin's business, and hiding the option
    # isn't hiding them — the query string is right there.
    kind = q.get("kind", "")
    if kind == "excluded" and not request.get("is_admin"):
        kind = ""
    result = store.search_matches(
        player=q.get("player", ""), season=number("season"), division=number("division"),
        kind=kind, date_from=q.get("from", ""), date_to=q.get("to", ""),
        min_minutes=number("min_minutes"), max_minutes=number("max_minutes"),
        page=number("page") or 1,
    )
    # Keep every filter but the page, so the pager can rebuild the URL.
    filters = {k: v for k, v in q.items() if k not in ("page", "theme") and v}
    return render(request, "matches.html", title="Matches", result=result, filters=filters,
                  query=urlencode(filters), seasons=store.all_seasons(),
                  divisions=store.divisions(), q=q, kind=kind)


@routes.get("/player/{account_id}")
async def player_page(request: web.Request):
    try:
        account_id = int(request.match_info["account_id"])
    except ValueError:
        return not_found(request, "Player")
    import rating_compute

    matches = store.player_matches(account_id)
    ident = store.player_identities([account_id])[account_id]
    site_user = store.get_user(account_id)
    # Someone we've never seen isn't "not found" — they're a Dota account we
    # haven't rated yet, and rating them is the thing a visitor came for. The
    # bot's /player does exactly this.
    unavailable: tuple[str, ...] = ()
    if not matches and not site_user and rating_compute.age_in_days(account_id) is None:
        if _may_rate_unknown(request):
            try:
                unavailable = await rating_compute.refresh_if_stale(account_id, compute_missing=True)
            except Exception:
                logger.exception("couldn't rate unknown player %s", account_id)
                unavailable = ("the rating sources",)
            ident = store.player_identities([account_id])[account_id]
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
            team = store.team_of(s, account_id) or {}   # drafted onto it, or its captain
            # The bot's season stats for this player, so the two agree.
            stats = next((r for r in db.get_all_time_stats(s["division_id"], s["id"])
                          if r["account_id"] == account_id), None) or store.aggregate(rows)
            season_rows.append({"season": s, "stats": stats, "captain": team.get("captain"),
                                "team_name": team.get("name"), "cost": costs.get("cost"),
                                # A captain isn't drafted, so they have no cost —
                                # an empty cell reads as missing data rather than
                                # as the reason there's nothing to show.
                                "is_captain": team.get("captain_account_id") == account_id})
    # Admins get the working: the same inputs and the same breakdown string
    # the bot's /lookup prints, rebuilt from the cache so the page costs
    # nothing. Every number here is one the rating was computed from.
    breakdown = None
    if request.get("is_admin"):
        breakdown = store.rating_breakdown(account_id)
    return render(request, "player.html", title=ident["name"] or str(account_id),
                  account_id=account_id, ident=ident, overall=store.aggregate(matches),
                  season_rows=season_rows, matches=matches[:10], match_total=len(matches),
                  rating=store.player_ratings().get(account_id),
                  breakdown=breakdown, unavailable=unavailable,
                  notice=request.query.get("notice"))


@routes.post("/player/{account_id}/recheck")
async def player_recheck(request: web.Request):
    """Re-fetch and re-rate one player now, instead of waiting for the weekly
    job or the seven-day staleness limit."""
    import rating_compute

    _require_admin(request)
    account_id = int(request.match_info["account_id"])
    try:
        unavailable = await rating_compute.refresh_if_stale(
            account_id, max_age_days=0, compute_missing=True)
    except Exception:
        logger.exception("recheck failed for %s", account_id)
        unavailable = ("the rating sources",)
    notice = (f"Couldn't reach {', '.join(unavailable)}, so nothing was changed — "
              f"a rating from a partial fetch would be a guess. Try again shortly."
              if unavailable else "Rechecked.")
    raise web.HTTPFound(f"/player/{account_id}?" + urlencode({"notice": notice}))


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
                  regions=sorted(REGION_CLUSTERS), game_modes=sorted(GAME_MODE_FILTERS),
                  about=store.get_text("about"), rules=store.get_text("rules"))


@routes.get("/admin/traffic")
async def admin_traffic(request: web.Request):
    """Who is reading the site. Admin-only: it is the league's data, not the
    public's."""
    if not request.get("is_admin"):
        return render(request, "admin_denied.html", title="Admin")
    days = 30
    raw = re.sub(r"\D", "", request.query.get("days", ""))
    if raw and 1 <= int(raw) <= 365:
        days = int(raw)
    return render(request, "admin_traffic.html", title="Traffic",
                  traffic=store.traffic(days), days=days)


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


@routes.post("/admin/season/rollover")
async def admin_roll_over_season(request: web.Request):
    """Point a division at a new season and load its weeks, rosters and teams.
    This was /start_new_season in Discord; league setup belongs where its
    results are read."""
    import season_setup

    _require_admin(request)
    data = await request.post()
    try:
        division = db.get_division(int(data["division_id"]))
        league_id = int(data["league_id"])
        weeks = int(data.get("weeks") or 9)
        first_week = int(data.get("first_week") or 0)
    except (KeyError, TypeError, ValueError):
        raise web.HTTPBadRequest(text="Pick a division and enter a league id.")
    if not division:
        raise web.HTTPBadRequest(text="Unknown division.")

    costs_csv = None
    upload = data.get("costs_file")
    if isinstance(upload, web.FileField):
        raw = upload.file.read()
        if raw:
            costs_csv = raw.decode("utf-8-sig", errors="replace")
    result = await season_setup.roll_over(
        division=division,
        league_id=league_id,
        season_start=(data.get("season_start") or "").strip(),
        weeks=weeks,
        first_week=first_week,
        label=(data.get("label") or "").strip() or None,
        costs_csv=costs_csv,
        costs_url=(data.get("costs_url") or "").strip() or None,
        teams_url=(data.get("teams_url") or "").strip() or None,
        dry_run=bool(data.get("dry_run")),
    )
    return render(request, "season_rollover.html",
                  status=400 if result["problems"] else 200,
                  title="Season setup", division=division, league_id=league_id,
                  result=result, dry_run=bool(data.get("dry_run")))


@routes.post("/admin/division/create")
async def admin_create_division(request: web.Request):
    _require_uberadmin(request)
    data = await request.post()
    name = (data.get("name") or "").strip()[:80]
    slug = re.sub(r"[^a-z0-9-]", "", (data.get("slug") or "").strip().lower())[:40]
    region = data.get("region") or "any"
    game_mode = data.get("game_mode") or "ad"
    if not name or not slug:
        raise web.HTTPBadRequest(text="A division needs a name and a URL name.")
    if region not in REGION_CLUSTERS:
        raise web.HTTPBadRequest(text=f"Unknown region {region!r}.")
    if game_mode not in GAME_MODE_FILTERS:
        raise web.HTTPBadRequest(text=f"Unknown game mode {game_mode!r}.")
    if any(d["slug"] == slug for d in store.divisions()):
        return render(request, "message.html", status=400, title="Division not created",
                      message=f"A division already uses the URL name “{slug}”.")
    guild_raw = re.sub(r"\D", "", data.get("guild_id") or "")
    division_id = db.create_division(name=name, slug=slug, region=region, game_mode=game_mode,
                                     guild_id=int(guild_raw) if guild_raw else None)
    logger.info("Division %s (%s) created by %s", division_id, slug, request["user"]["account_id"])
    raise web.HTTPFound("/admin#divisions")


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
    # Region, mode and Discord server used to be /config's to set. They decide
    # which of a league's games count as this division's, so they belong with
    # the rest of the division.
    fields = {}
    region, mode = data.get("region"), data.get("game_mode")
    if region in REGION_CLUSTERS and region != division["region"]:
        fields["region"] = region
    if mode in GAME_MODE_FILTERS and mode != division["game_mode"]:
        fields["game_mode"] = mode
    if "guild_id" in data:
        guild_raw = re.sub(r"\D", "", data.get("guild_id") or "")
        guild_id = int(guild_raw) if guild_raw else None
        if guild_id != division["guild_id"]:
            fields["guild_id"] = guild_id
    if fields:
        db.update_division(division["id"], **fields)
        logger.info("Division %s updated: %s", division["id"], fields)
    raise web.HTTPFound("/admin#divisions")


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

# Crawlers that identify themselves and are not wanted here. robots.txt asks
# them to stay out, but a crawler caches that file for hours, so asking does
# nothing about the one already walking the site. These are refused outright,
# before any work is done. Everything here names itself honestly in its user
# agent — a scanner pretending to be Chrome is a different problem, and not one
# a list of names can solve.
BLOCKED_AGENTS = (
    "gptbot", "oai-searchbot", "chatgpt-user",     # OpenAI
    "claudebot", "claude-web", "anthropic-ai",     # Anthropic
    "ccbot",                                       # Common Crawl
    "bytespider",                                  # ByteDance
    "google-extended", "applebot-extended",        # the AI-training arms
    "perplexitybot", "amazonbot", "meta-external",
    "dataforseo", "semrush", "ahrefs", "mj12bot", "dotbot",   # SEO crawlers
)


@web.middleware
async def crawler_middleware(request: web.Request, handler):
    """Refuse the crawlers robots.txt already asks to stay away.

    A page here is computed from the whole league's stats, so a crawler
    walking every player is the heaviest load the site can be put under —
    which is exactly what happened, and it made the site unusable for the
    people it is actually for.
    """
    ua = request.headers.get("User-Agent", "").lower()
    # robots.txt is how a crawler learns to stay away of its own accord, so it
    # is the one thing a blocked crawler must still be able to read.
    if request.path != "/robots.txt" and ua and any(name in ua for name in BLOCKED_AGENTS):
        return web.Response(status=403, content_type="text/plain",
                            text="Not open to crawlers. See /robots.txt.\n")
    return await handler(request)


@web.middleware
async def canonical_host_middleware(request: web.Request, handler):
    """www.adcl.gg and adcl.gg are the same site, but only one of them is the
    address. Sending www to the bare name permanently keeps links, bookmarks
    and search results on one host — and, more practically, keeps the login
    cookie on one host, since a session set on one is not sent to the other.

    Only the www form of the canonical host is redirected: the fly.dev name
    goes on working, which is what the bot's own links and the stream cards
    use.
    """
    if BASE_URL:
        canonical = urlparse(BASE_URL).netloc
        if request.host.split(":")[0] == f"www.{canonical}":
            raise web.HTTPMovedPermanently(BASE_URL.rstrip("/") + request.path_qs)
    return await handler(request)


@web.middleware
async def view_middleware(request: web.Request, handler):
    """Count page views, after the fact and never at the cost of the page.

    Only GETs that returned a rendered HTML page count — not assets, not the
    API, not a redirect, not a 404. Recording is wrapped because a counter
    failing is not a reason for a visitor to see an error.
    """
    response = await handler(request)
    try:
        if (request.method == "GET" and response.status == 200
                and response.content_type == "text/html"):
            user = request.get("user")
            # Fly puts the real address in Fly-Client-IP; the socket is its proxy.
            ip = (request.headers.get("Fly-Client-IP")
                  or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                  or (request.remote or ""))
            store.record_view(request.path, ip=ip,
                              user_agent=request.headers.get("User-Agent", ""),
                              referrer=request.headers.get("Referer", ""),
                              account_id=user["account_id"] if user else None)
    except Exception:
        logger.exception("failed to record a page view")
    return response


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPNotFound:
        return not_found(request)


def setup(app: web.Application) -> None:
    """Add the website's routes to the bot's aiohttp app."""
    store.init_site_tables()
    app.middlewares.append(crawler_middleware)
    app.middlewares.append(canonical_host_middleware)
    app.middlewares.append(error_middleware)
    app.middlewares.append(session_middleware)
    app.middlewares.append(view_middleware)
    app.router.add_routes(routes)
    app.router.add_static("/static/", path=str(STATIC_DIR), show_index=False)
    uploads.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    app.router.add_static(uploads.URL_PREFIX, path=str(uploads.UPLOAD_DIR), show_index=False)
