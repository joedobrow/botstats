"""
Team identity (real team name + logo) for a captain, per season.

Backed by the `season_teams` DB table — populated by Admin → Season setup from
the RD2L teams page, so rolling a new season is a data change, not a code
change.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Logos uploaded on the website are stored with a site-relative path
# (/uploads/...). The bot downloads logos over HTTP to draw its cards, so
# resolve those against its own web server.
_LOCAL_WEB = os.environ.get("LOCAL_WEB_URL", "http://127.0.0.1:8080")


def _absolute(url: str | None) -> str | None:
    return f"{_LOCAL_WEB}{url}" if url and url.startswith("/") else url


def get_team_info(captain: str | None, season_id: int | None = None,
                  division_id: int | None = None) -> dict:
    """Return {team_name, logo_url} for a captain.

    Reads season_teams for the given season and division. Falls back to the captain
    string as the team name (and no logo) when we have no identity on file —
    which is the correct display for an un-branded team, not an error.
    """
    if not captain:
        return {"team_name": captain or "", "logo_url": None}
    if season_id and division_id:
        try:
            from db import get_season_teams
            info = get_season_teams(season_id, division_id).get(captain.strip().lower())
            if info:
                return dict(info, logo_url=_absolute(info.get("logo_url")))
        except Exception:
            logger.exception("season_teams lookup failed for %s", captain)
    return {"team_name": captain, "logo_url": None}
