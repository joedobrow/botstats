"""What GET /api/player returns: the numbers /player shows, for other tools.

Everything comes from what the bot already has — the rating cache plus any
/set_player overrides — resolved exactly the way /player resolves it:

- name: the override nickname, else the cached name
- internal_rating: the manual rating override if one is set; otherwise the
  cached rating (an alt account's is its main's), nudged by the season's
  fantasy adjustment in the given division — the number /player displays
- windrun_rating: the override if set, else the cached windrun rating
- rank_tier / leaderboard_rank / badge: the override badge if set, else the
  cached one from OpenDota

The response shows results, not workings: no pre-adjustment rating, no
adjustment size, no season, no sign of which overrides are set (alt links
included), and warnings that state missing data plainly rather than how the
rating uses it. The rating's explanation string, which spells out the
model's weights, never leaves the bot.
"""

from __future__ import annotations

from datetime import datetime, timezone

STEAM64_OFFSET = 76561197960265728
STALE_DAYS = 30


def account_id_from(raw: str) -> int | None:
    """A Dotabuff/OpenDota account id, or a Steam64 id converted to one."""
    raw = (raw or "").strip()
    if not raw.isdigit():
        return None
    n = int(raw)
    return n - STEAM64_OFFSET if n > STEAM64_OFFSET else n


def build(account_id: int, guild_id: int | None) -> dict | None:
    """The payload for one player, or None if the bot knows nothing about them."""
    from db import (compute_fantasy_adjusted_ratings, get_division, get_rating_cache_row,
                    get_skill_override, latest_match_fetch)
    from formatters import format_badge

    cache = get_rating_cache_row(account_id) or {}
    override = get_skill_override(account_id) or {}
    if not cache and not override:
        return None

    manual = override.get("internal_rating_override")
    base = manual if manual is not None else cache.get("internal_rating")
    rating = base
    division = get_division(guild_id) if guild_id else None
    if manual is None and base is not None and division:
        # Alts are judged on their main's history, as /player does.
        effective = cache.get("alt_account_for") or account_id
        adj = compute_fantasy_adjusted_ratings(guild_id, division["season_start"]).get(effective)
        if adj:
            rating = adj["adjusted_rating"]

    windrun = override.get("windrun_rating")
    if windrun is None:
        windrun = cache.get("raw_windrun")
    if override.get("rank_tier") is not None:
        rank_tier, lb_rank = override["rank_tier"], override.get("leaderboard_rank")
    else:
        rank_tier, lb_rank = cache.get("rank_tier"), cache.get("leaderboard_rank")

    hidden = bool(cache.get("fh_unavailable"))
    warnings = []
    if hidden:
        warnings.append("Steam match history is private.")
    if base is None:
        warnings.append("Not enough data to rate this player.")
    if manual is None and windrun is None and base is not None:
        warnings.append("No windrun rating found.")
    if manual is None and rank_tier is None and base is not None:
        warnings.append("No ranked badge found.")
    # Game counts as OpenDota reports them. With match history private it
    # returns zeroes, which aren't real counts — and older cache rows stored
    # those zeroes — so report null instead of claiming nobody has played.
    games = {
        "ability_draft_last_year": cache.get("ad_last_year"),
        "ability_draft_all_time": cache.get("ad_all_time"),
        "ranked_last_year": cache.get("ranked_last_year"),
    }
    if hidden:
        games = dict.fromkeys(games)

    updated = cache.get("updated_at")
    if updated:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(updated)).days
            if age > STALE_DAYS:
                warnings.append(f"Cached rating is {age} days old.")
        except ValueError:
            pass

    return {
        "account_id": account_id,
        "name": override.get("nickname") or cache.get("name"),
        "internal_rating": round(rating) if rating is not None else None,
        "windrun_rating": round(windrun) if windrun is not None else None,
        "games": games,
        "rank_tier": rank_tier,
        "leaderboard_rank": lb_rank,
        "badge": format_badge(rank_tier, lb_rank) if rank_tier else None,
        "match_history_hidden": hidden,
        "accurate": base is not None and not hidden,
        "warnings": warnings,
        "updated_at": updated,
        # The newer of: this player's data, and the division's latest match —
        # a rating moves when anyone in the division plays.
        "changed_at": max(filter(None, [updated, latest_match_fetch(guild_id) if guild_id else None]),
                          default=updated),
    }
