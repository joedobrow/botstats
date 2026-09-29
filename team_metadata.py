"""
Team identity (real team name + logo) for a captain, per season.

Backed by the `season_teams` DB table — populated by /start_new_season from
the RD2L teams page, so rolling a new season is a data change, not a code
change.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def get_team_info(captain: str | None, season_id: int | None = None) -> dict:
    """Return {team_name, logo_url} for a captain.

    Reads season_teams for the given season. Falls back to the captain
    string as the team name (and no logo) when we have no identity on file —
    which is the correct display for an un-branded team, not an error.
    """
    if not captain:
        return {"team_name": captain or "", "logo_url": None}
    if season_id:
        try:
            from db import get_season_teams
            info = get_season_teams(season_id).get(captain.strip().lower())
            if info:
                return info
        except Exception:
            logger.exception("season_teams lookup failed for %s", captain)
    return {"team_name": captain, "logo_url": None}
