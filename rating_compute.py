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


async def compute_and_cache_rating(account_id: int) -> int | None:
    """Fetch windrun + OpenDota data for account_id, resolve its internal
    rating, and upsert the result into player_ratings_cache. Returns the
    resolved rating, or None if there wasn't enough signal to compute one
    (in which case nothing meaningful is cached beyond raw stat fields).

    If this account is set up as an alt (skill_overrides.alt_account_for),
    its rating is deferred entirely to the main account's — mirrors
    get_rating_cache_row's read-time substitution, but at write time, so a
    freshly-backfilled alt is never cached with its own (wrong) rating."""
    from windrun import fetch_player as fetch_windrun_player
    from opendota_lookup import fetch_player_profile, fetch_player_game_counts
    from db import get_skill_override, get_rating_cache_row, upsert_rating_cache_row
    from formatters import (
        _resolve_internal_rating, _rank_to_windrun, windrun_rating_for_formula,
        sanitize_od_counts,
    )

    override = get_skill_override(account_id)
    main_id = override.get("alt_account_for") if override else None

    try:
        if main_id and main_id != account_id:
            main_row = get_rating_cache_row(main_id)
            if not main_row or main_row.get("internal_rating") is None:
                await compute_and_cache_rating(main_id)
                main_row = get_rating_cache_row(main_id)
            main_rating = main_row.get("internal_rating") if main_row else None
            main_name = main_row.get("name") if main_row else None

            # Still fetch the alt's own profile for name + avatar (display
            # only) — never used to compute its rating.
            alt_results = await asyncio.gather(
                fetch_windrun_player(account_id),
                fetch_player_profile(account_id),
                return_exceptions=True,
            )
            alt_wr, alt_od = (None if isinstance(r, Exception) else r for r in alt_results)
            alt_name = (alt_wr or {}).get("nickname") or ((alt_od or {}).get("profile") or {}).get("personaname")
            alt_avatar = ((alt_od or {}).get("profile") or {}).get("avatarfull") or (alt_wr or {}).get("avatar")

            upsert_rating_cache_row({
                "account_id": account_id,
                "name": alt_name,
                "internal_rating": main_rating,
                "explanation": (
                    f"linked to {main_name or ('#' + str(main_id))} (alt account)"
                    if main_rating is not None else None
                ),
                "avatar_url": alt_avatar,
            })
            return main_rating

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
            "explanation": info[1] if info else None,
            "avatar_url": (
                ((od or {}).get("profile") or {}).get("avatarfull")
                or (wr or {}).get("avatar")
            ),
            "fh_unavailable": bool((od or {}).get("profile", {}).get("fh_unavailable")) if od else None,
        })
        return info[0] if info else None
    except Exception:
        logger.exception("compute_and_cache_rating failed for account %d", account_id)
        return None
