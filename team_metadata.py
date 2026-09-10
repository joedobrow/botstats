"""
Team identity (real team name + logo) for a captain, per season.

Backed by the `season_teams` DB table — populated by /start_new_season from
the RD2L teams page, so rolling a new season is a data change, not a code
change. The S38 dict below is kept only as the seed for that table (see
_seed_s38_season_teams in db.py) and as a last-resort fallback.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# Legacy S38 data. Seeded into season_teams on first startup after that table
# was added; not consulted at runtime once the DB has rows. Don't add to this.
_LEGACY_S38_TEAMS: dict[str, dict] = {
    "bonedini":           {"team_name": "Bonedini",                                              "logo_url": None},
    "thefoxinthebox":     {"team_name": "Bottom Feeders",                                        "logo_url": "https://i.ibb.co/WvrNJPZH/bottom-feeders-logo.png"},
    "yokozuna hoshoryu":  {"team_name": "Gym Esports (For Aniki)",                               "logo_url": "https://i.ibb.co/hJBrGwNx/GYMesports.png"},
    "redground":          {"team_name": "Hobo Alt Accounts",                                     "logo_url": "https://i.ibb.co/Qvxv33dr/dawg.jpg"},
    "rainmaker":          {"team_name": "Invictus Blaming",                                      "logo_url": "https://i.ibb.co/XkGTwKg7/ibm.png"},
    "sofaking":           {"team_name": "Order of the Round Sofa",                               "logo_url": "https://i.ibb.co/n8bvJVz6/sofaking.png"},
    "loves muffins":      {"team_name": "Out of the Box Thinkers",                               "logo_url": "https://i.ibb.co/bjW4zBs2/muffs.png"},
    "icicle":             {"team_name": "Prestige Worldwide: The First Word in Entertainment",   "logo_url": "https://i.ibb.co/2YpWfHmP/pw.png"},
    "space chicken":      {"team_name": "Space Chicken",                                         "logo_url": None},
    "dingus":             {"team_name": "Turtle Club",                                           "logo_url": "https://i.ibb.co/8LmYj1Kx/turtle.png"},
}


def get_team_info(captain: str | None,
                  guild_id: int | None = None,
                  season_start: str | None = None) -> dict:
    """Return {team_name, logo_url} for a captain.

    Reads season_teams for the given guild/season. Falls back to the captain
    string as the team name (and no logo) when we have no identity on file —
    which is the correct display for an un-branded team, not an error.
    """
    if not captain:
        return {"team_name": captain or "", "logo_url": None}
    key = captain.strip().lower()

    if guild_id and season_start:
        try:
            from db import get_season_teams
            info = get_season_teams(guild_id, season_start).get(key)
            if info:
                return info
        except Exception:
            logger.exception("season_teams lookup failed for %s", captain)

    info = _LEGACY_S38_TEAMS.get(key)
    if info:
        return info
    return {"team_name": captain, "logo_url": None}
