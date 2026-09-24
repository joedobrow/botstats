"""Shared logic for computing a player's internal rating from windrun +
OpenDota and caching the result in player_ratings_cache.

Extracted so both bot.py's live-lookup fallbacks and the /api/ratings
backfill path (botstats/server.py) can populate the cache without a human
having to run a Discord command first.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def windrun_games(wr: dict | None) -> int | None:
    """How many Ability Draft games a windrun profile rests on, or None when
    windrun has no record of the account. Cached alongside the rating so the
    MIN_WINDRUN_GAMES test can be re-applied later without re-fetching."""
    if not wr or (wr.get("wins") is None and wr.get("losses") is None):
        return None
    return (wr.get("wins") or 0) + (wr.get("losses") or 0)


async def compute_and_cache_rating(account_id: int) -> int | None:
    """Fetch windrun + OpenDota data for account_id, resolve its internal
    rating, and upsert the result into player_ratings_cache. Returns the
    resolved rating, or None if there wasn't enough signal to compute one
    (in which case nothing meaningful is cached beyond raw stat fields).

    Every account is rated on its own data, second accounts included: when
    someone plays on several (skill_overrides.alt_account_for), their shared
    rating is built from the best evidence across them (rating_group.py), so
    each account's own inputs have to be cached first."""
    from windrun import fetch_player as fetch_windrun_player
    from opendota_lookup import fetch_player_profile, fetch_player_game_counts
    from db import get_skill_override, upsert_rating_cache_row
    import rating_group
    from formatters import (
        _resolve_internal_rating, _rank_to_windrun, windrun_rating_for_formula,
        sanitize_od_counts,
    )

    override = get_skill_override(account_id)

    try:
        results = await asyncio.gather(
            fetch_windrun_player(account_id),
            fetch_player_profile(account_id),
            fetch_player_game_counts(account_id),
            return_exceptions=True,
        )
        wr, od, od_counts = (None if isinstance(r, Exception) else r for r in results)

        raw_wr = (wr or {}).get("rating")
        formula_wr = windrun_rating_for_formula(wr)
        rank_tier = (od or {}).get("rank_tier")
        lb_rank = (od or {}).get("leaderboard_rank")

        eff_wr = override["windrun_rating"] if override and override.get("windrun_rating") is not None else formula_wr
        eff_rank_tier = override["rank_tier"] if override and override.get("rank_tier") is not None else rank_tier
        eff_lb_rank = override["leaderboard_rank"] if override and override.get("rank_tier") is not None else lb_rank
        ranked_in_wr_raw = _rank_to_windrun(eff_rank_tier, eff_lb_rank)

        od_counts = sanitize_od_counts(od, od_counts)
        ad_last = (od_counts or {}).get("last_year_ad")
        ad_all = (od_counts or {}).get("all_time_ad")
        ranked_last = (od_counts or {}).get("last_year_ranked")
        ranked_all = (od_counts or {}).get("all_time_ranked")

        info = None
        if od_counts is not None or (override and override.get("internal_rating_override") is not None):
            info = _resolve_internal_rating(
                override=override,
                ad_last=ad_last, ad_all=ad_all,
                ranked_last=ranked_last, ranked_all=ranked_all,
                wr_rating=eff_wr, ranked_in_wr_raw=ranked_in_wr_raw,
            )

        name = (wr or {}).get("nickname")
        if not name and od:
            name = (od.get("profile") or {}).get("personaname")

        upsert_rating_cache_row({
            "account_id": account_id,
            "name": name,
            "internal_rating": info[0] if info else None,
            "raw_windrun": raw_wr,
            "rank_tier": rank_tier,
            "leaderboard_rank": lb_rank,
            "ad_last_year": ad_last,
            "ad_all_time": ad_all,
            "ranked_last_year": ranked_last,
            "windrun_games": windrun_games(wr),
            "explanation": info[1] if info else None,
            "avatar_url": (
                ((od or {}).get("profile") or {}).get("avatarfull")
                or (wr or {}).get("avatar")
            ),
            "fh_unavailable": bool((od or {}).get("profile", {}).get("fh_unavailable")) if od else None,
        })
        rating_group.recompute(account_id)
        return info[0] if info else None
    except Exception:
        logger.exception("compute_and_cache_rating failed for account %d", account_id)
        return None
