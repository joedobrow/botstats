"""
Static mapping of in-DB captain names to RD2L team metadata (real team name +
logo URL). Source: https://rd2l.gg/seasons/.../divisions/.../teams (S38).

To refresh, re-scrape the RD2L teams page and overwrite this dict. Keys are
matched case-insensitively against the `captain` field in player_costs.
"""

from __future__ import annotations


# Lower-cased captain name (as it appears in player_costs) → metadata.
_TEAMS_BY_CAPTAIN: dict[str, dict] = {
    "bonedini":           {"team_name": "Bonedini",                                              "logo_url": None},
    "thefoxinthebox":     {"team_name": "Bottom Feeders",                                        "logo_url": "https://i.ibb.co/WvrNJPZH/bottom-feeders-logo.png"},
    "yokozuna hoshoryu":  {"team_name": "Gym Esports (For Aniki)",                               "logo_url": "https://i.ibb.co/hJBrGwNx/GYMesports.png"},
    "redground":          {"team_name": "Hobo Alt Accounts",                                     "logo_url": "https://i.ibb.co/Qvxv33dr/dawg.jpg"},
    "rainmaker":          {"team_name": "Invictus Blaming",                                      "logo_url": "https://i.ibb.co/XkGTwKg7/ibm.png"},
    "sofaking":           {"team_name": "Order of the Round Sofa",                               "logo_url": "https://i.ibb.co/n8bvJVz6/sofaking.png"},
    "loves muffins":      {"team_name": "Out of the Box Thinkers",                               "logo_url": "https://i.ibb.co/bjW4zBs2/muffs.png"},
    "icicle":             {"team_name": "Prestige Worldwide: The First Word in Entertainment",   "logo_url": "https://i.ibb.co/2YpWfHmP/pw.png"},
    "space chicken":      {"team_name": "Space Chicken",                                         "logo_url": None},
    # RD2L lists "Dr. Sulaiman Al Habib" as the Turtle Club captain; our DB
    # has him as "Dingus". Alias-mapped here.
    "dingus":             {"team_name": "Turtle Club",                                           "logo_url": "https://i.ibb.co/8LmYj1Kx/turtle.png"},
}


def get_team_info(captain: str | None) -> dict:
    """Return {team_name, logo_url} for the captain string. Falls back to
    using the captain string as the team name and no logo if unknown."""
    if not captain:
        return {"team_name": captain or "", "logo_url": None}
    info = _TEAMS_BY_CAPTAIN.get(captain.strip().lower())
    if info:
        return info
    return {"team_name": captain, "logo_url": None}
