"""Rolling a division over to a new season.

A season is a Dota league id, a start date, a set of week boundaries, the
rosters that were drafted into it and the teams those rosters belong to. Every
write here is an upsert, so running it twice against the same league is a
repair rather than a mistake: it tops up whatever didn't land the first time —
a roster CSV that failed to parse, say — and leaves matches and weeks alone.

This used to live in the /start_new_season Discord command, which meant league
setup happened in a text box with a file attachment, out of sight of the site
that shows the result. It's here so the admin pages can own it, and so nothing
is written until every source has parsed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MAX_WEEKS = 30


async def _league_name(league_id: int) -> str | None:
    """The league's own name from OpenDota, to save typing one out."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"https://api.opendota.com/api/leagues/{league_id}") as r:
                if r.status == 200:
                    return ((await r.json()) or {}).get("name") or None
    except Exception:
        logger.info("league name lookup failed for %d", league_id, exc_info=False)
    return None


async def roll_over(
    *,
    division: dict,
    league_id: int,
    season_start: str,
    weeks: int = 9,
    first_week: int = 0,
    label: str | None = None,
    costs_csv: str | None = None,
    costs_url: str | None = None,
    teams_url: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Point a division at a new season, and load its weeks, rosters and teams.

    Returns {applied, same_season, label, summary, problems, weeks}. `summary`
    and `problems` are lines meant for whoever pressed the button; when
    `problems` is non-empty nothing is written, whether or not dry_run was set.
    """
    from db import (_season_week_start, generate_season_weeks, get_player_costs,
                    get_season, season_label, set_current_season, upsert_player_costs,
                    upsert_season, upsert_season_teams)
    from rd2l import (fetch_drafted_players_csv, fetch_team_identities,
                      parse_drafted_players_csv)

    problems: list[str] = []
    try:
        datetime.strptime(season_start, "%Y-%m-%d")
    except ValueError:
        problems.append("Start date must be YYYY-MM-DD.")
    if first_week not in (0, 1):
        problems.append("First week must be 0 or 1.")
    if not 1 <= weeks <= MAX_WEEKS:
        problems.append(f"Weeks must be between 1 and {MAX_WEEKS}.")
    if costs_csv and costs_url:
        problems.append("Give either a roster CSV or a roster URL, not both.")
    if problems:
        return {"applied": False, "same_season": False, "label": label,
                "summary": [], "problems": problems, "weeks": []}

    season_id = league_id                      # a season's id is its league id
    previous = get_season(division["current_season_id"]) or {}
    same_season = division["current_season_id"] == season_id

    # --- gather every source before writing anything ---
    drafted, identities = [], []
    if costs_csv:
        try:
            drafted = parse_drafted_players_csv(costs_csv)
            if not drafted:
                problems.append("The roster CSV parsed but held no drafted rows "
                                "(every row missing Winner or Cost).")
        except Exception as e:
            problems.append(f"The roster CSV failed: {e}")
    elif costs_url:
        try:
            drafted = await fetch_drafted_players_csv(costs_url)
            if not drafted:
                problems.append("The roster CSV parsed but held no drafted rows "
                                "(every row missing Winner or Cost).")
        except Exception as e:
            problems.append(f"The roster CSV failed: {e}")
    if teams_url:
        try:
            identities = await fetch_team_identities(teams_url)
            if not identities:
                problems.append("The teams page parsed but yielded no teams.")
        except Exception as e:
            problems.append(f"The teams page failed: {e}")

    label = label or await _league_name(league_id)

    # Pure date maths, no writes — safe to show before deciding.
    week_rows = []
    for n in range(first_week, first_week + weeks):
        start, end = _season_week_start(n - first_week + 1, season_start)
        week_rows.append({
            "number": n,
            "start": datetime.fromtimestamp(start, timezone.utc).strftime("%b %-d"),
            "end": datetime.fromtimestamp(end, timezone.utc).strftime("%b %-d"),
        })

    teams_in_costs = len({d["captain"].strip().lower() for d in drafted})
    summary = [
        f"League {division['current_season_id'] or '—'} → {league_id}" + (f" ({label})" if label else ""),
        f"Start {previous.get('season_start') or '—'} → {season_start}",
        f"Weeks {first_week}–{first_week + weeks - 1}"
        f" ({week_rows[0]['start']} → {week_rows[-1]['end']})",
        f"Rosters {len(drafted)} drafted players across {teams_in_costs} teams"
        + ("" if (costs_csv or costs_url) else " — no source given, skipped")
        + (" (captains are added separately, so this is 4 a team)" if drafted else ""),
        f"Teams {len(identities)} names and logos"
        + ("" if teams_url else " — no source given, existing kept"),
    ]

    if problems or dry_run:
        return {"applied": False, "same_season": same_season, "label": label,
                "summary": summary, "problems": problems, "weeks": week_rows}

    # --- writes: the season row first, since everything else points at it ---
    upsert_season(season_id, season_start, label=label)
    set_current_season(division["id"], season_id)
    generate_season_weeks(season_id, division["id"], weeks, first_week=first_week)
    if drafted:
        upsert_player_costs(season_id, division["id"], [
            {"account_id": d["account_id"], "cost": d["cost"], "captain": d["captain"]}
            for d in drafted])
    if identities:
        upsert_season_teams(season_id, division["id"], identities)
    logger.info("Season %s rolled over for division %s (%d rosters, %d teams)",
                season_id, division["id"], len(drafted), len(identities))

    if same_season:
        summary.append(f"League {league_id} now holds "
                       f"{len(get_player_costs(season_id, division['id']))} roster rows. "
                       f"Existing matches and weeks were left alone.")
    else:
        kept = len(get_player_costs(division["current_season_id"], division["id"]))
        summary.append(f"The previous season "
                       f"({season_label(previous) if previous else division['current_season_id']}) "
                       f"kept its {kept} roster rows and all its matches — nothing was deleted.")
    return {"applied": True, "same_season": same_season, "label": label,
            "summary": summary, "problems": [], "weeks": week_rows}
