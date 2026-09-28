"""One way to work out a player's internal rating, for everything that needs one.

/player reads the cache; /lookup, /refresh_ratings and the API write it. Those
writers each used to gather their own inputs, and they drifted: for a player
with private Steam match history, /refresh_ratings had no Ability Draft counts
at all and rated them on their badge alone, while /lookup filled the counts in
from windrun and rated them lower. Same player, two numbers, and whichever
command ran last was the one /player showed (Yoxir, 2747 on Tuesday and 2629 on
Wednesday, without playing a game).

So the gathering lives here, once. `fetch_inputs` does every API call and
decides what each number means; `resolve` turns that into a rating; `cache`
stores it. A caller that also wants to display the data — /lookup — gets the
same inputs object back rather than fetching its own.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# windrun's match list returns this many at most, whatever limit we ask for
# (500 requested, 30 returned, the oldest of them inside the year). So a full
# list means "at least this many", never "exactly this many" — read as exact it
# becomes an inactivity penalty for someone whose 31st game we simply can't see.
WINDRUN_MATCH_CAP = 30


def windrun_games(wr: dict | None) -> int | None:
    """How many Ability Draft games a windrun profile rests on, or None when
    windrun has no record of the account. Cached alongside the rating so the
    MIN_WINDRUN_GAMES test can be re-applied later without re-fetching."""
    if not wr or (wr.get("wins") is None and wr.get("losses") is None):
        return None
    return (wr.get("wins") or 0) + (wr.get("losses") or 0)


def _windrun_games_last_year(matches: list | None) -> tuple[int, bool]:
    """(games in the last 365 days, whether the list came back full)."""
    if not matches:
        return 0, False
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)
    count = 0
    for m in matches:
        ts = m.get("gameStart")
        if not ts:
            continue
        try:
            if datetime.fromisoformat(ts.replace("Z", "+00:00")) > cutoff:
                count += 1
        except ValueError:
            pass
    return count, len(matches) >= WINDRUN_MATCH_CAP


async def fetch_inputs(account_id: int) -> dict:
    """Everything the rating formula needs about one account, fetched once.

    Ability Draft counts come from OpenDota where it can see them. Lifetime
    games fall back to windrun's own win/loss record, which costs nothing —
    it's in the profile we already fetch — and is what a player with private
    match history has instead of nothing.

    Last year's count is OpenDota's, with windrun's recent matches as a floor
    for accounts OpenDota has only partly indexed. That floor is only worth a
    second windrun call when it could raise the answer, so it's skipped when
    OpenDota already reports at least as many as windrun could return. When the
    list comes back full it's a lower bound, so it can raise a known count but
    never stand in for one: an unknown count stays unknown rather than becoming
    a penalty (see WINDRUN_MATCH_CAP).
    """
    from windrun import fetch_player as fetch_windrun_player, fetch_player_matches
    from opendota_lookup import fetch_player_profile, fetch_player_game_counts
    from formatters import sanitize_od_counts

    results = await asyncio.gather(
        fetch_windrun_player(account_id),
        fetch_player_profile(account_id),
        fetch_player_game_counts(account_id),
        return_exceptions=True,
    )
    for label, r in zip(("windrun", "opendota", "opendota-counts"), results):
        if isinstance(r, Exception):
            logger.warning("%s fetch failed for %d: %s", label, account_id, r)
    wr, od, od_counts = (None if isinstance(r, Exception) else r for r in results)

    # Private match history makes OpenDota report 0 for every count, which
    # means "can't see", not "hasn't played".
    od_counts = sanitize_od_counts(od, od_counts)
    od_last = (od_counts or {}).get("last_year_ad")
    od_all = (od_counts or {}).get("all_time_ad")

    wr_lifetime = windrun_games(wr)
    ad_all_time = max(x for x in (od_all, wr_lifetime) if x is not None) \
        if (od_all is not None or wr_lifetime is not None) else None

    ad_last_year = od_last
    if wr is not None and (od_last is None or od_last < WINDRUN_MATCH_CAP):
        recent, saturated = _windrun_games_last_year(await fetch_player_matches(account_id))
        if od_last is not None:
            ad_last_year = max(od_last, recent)
        elif recent and not saturated:
            ad_last_year = recent

    return {
        "account_id": account_id,
        "windrun": wr,
        "opendota": od,
        "od_counts": od_counts,
        "raw_windrun": (wr or {}).get("rating"),
        "windrun_for_formula": trusted_windrun((wr or {}).get("rating"), wr_lifetime),
        "windrun_games": wr_lifetime,
        "rank_tier": (od or {}).get("rank_tier"),
        "leaderboard_rank": (od or {}).get("leaderboard_rank"),
        "ad_last_year": ad_last_year,
        "ad_all_time": ad_all_time,
        "ranked_last_year": (od_counts or {}).get("last_year_ranked"),
        "ranked_all_time": (od_counts or {}).get("all_time_ranked"),
        "name": ((wr or {}).get("nickname")
                 or ((od or {}).get("profile") or {}).get("personaname")),
        "avatar_url": (((od or {}).get("profile") or {}).get("avatarfull")
                       or (wr or {}).get("avatar")),
        "fh_unavailable": (bool((od.get("profile") or {}).get("fh_unavailable"))
                           if od else None),
        "reached_any_api": wr is not None or od is not None or od_counts is not None,
        # Which fields we actually reached the source for, so "unknown" can be
        # written as unknown instead of leaving a stale number behind (see
        # db.upsert_rating_cache_row). A source that was down leaves its
        # fields out, and the cache keeps what it had.
        "authoritative": (
            (("raw_windrun", "windrun_games") if wr is not None else ())
            + (("rank_tier", "leaderboard_rank", "fh_unavailable") if od is not None else ())
            + (("ad_last_year", "ad_all_time", "ranked_last_year", "ranked_all_time")
               if od_counts is not None else ())
        ),
    }


def trusted_windrun(rating: float | None, games: int | None) -> float | None:
    """A windrun rating, or None when too few games stand behind it. Takes the
    numbers rather than a profile, so a rating rebuilt from the cache is judged
    exactly as the freshly fetched one was.

    An unknown game count means the row was cached before the count was, so the
    rating is trusted rather than thrown away — the next refresh fills it in.
    """
    from formatters import MIN_WINDRUN_GAMES

    if rating is None:
        return None
    return rating if games is None or games >= MIN_WINDRUN_GAMES else None


def inputs_from_cache(account_id: int) -> dict:
    """The same inputs, rebuilt from what was last fetched. For /set_player,
    which changes an override and has to re-rate without calling any API."""
    from db import get_rating_cache_row

    row = get_rating_cache_row(account_id) or {}
    return {
        "account_id": account_id,
        "windrun_for_formula": trusted_windrun(row.get("raw_windrun"), row.get("windrun_games")),
        "raw_windrun": row.get("raw_windrun"),
        "windrun_games": row.get("windrun_games"),
        "rank_tier": row.get("rank_tier"),
        "leaderboard_rank": row.get("leaderboard_rank"),
        "ad_last_year": row.get("ad_last_year"),
        "ad_all_time": row.get("ad_all_time"),
        "ranked_last_year": row.get("ranked_last_year"),
        "ranked_all_time": row.get("ranked_all_time"),
        "name": row.get("name"),
        "avatar_url": row.get("avatar_url"),
        "fh_unavailable": row.get("fh_unavailable"),
        "reached_any_api": row.get("raw_windrun") is not None or row.get("rank_tier") is not None,
    }


def effective_inputs(inputs: dict, override: dict | None) -> tuple[float | None, float | None]:
    """(windrun rating, ranked windrun-equivalent) after any manual override."""
    from formatters import _rank_to_windrun

    override = override or {}
    wr_rating = (override["windrun_rating"] if override.get("windrun_rating") is not None
                 else inputs["windrun_for_formula"])
    if override.get("rank_tier") is not None:
        tier, lb = override["rank_tier"], override.get("leaderboard_rank")
    else:
        tier, lb = inputs["rank_tier"], inputs["leaderboard_rank"]
    return wr_rating, _rank_to_windrun(tier, lb)


def resolve(inputs: dict, override: dict | None) -> tuple[int, str] | None:
    """The internal rating for these inputs, or None when nothing was reachable."""
    from formatters import _resolve_internal_rating

    manual = (override or {}).get("internal_rating_override") is not None
    if not manual and not inputs["reached_any_api"]:
        return None
    wr_rating, ranked_in_wr_raw = effective_inputs(inputs, override)
    return _resolve_internal_rating(
        override=override,
        ad_last=inputs["ad_last_year"],
        ad_all=inputs["ad_all_time"],
        ranked_last=inputs["ranked_last_year"],
        ranked_all=inputs["ranked_all_time"],
        wr_rating=wr_rating,
        ranked_in_wr_raw=ranked_in_wr_raw,
    )


def cache(inputs: dict, info: tuple[int, str] | None, name_fallback: str | None = None) -> None:
    """Store these inputs and their rating, and re-pool any second accounts."""
    import rating_group
    from db import upsert_rating_cache_row

    upsert_rating_cache_row({
        "account_id":       inputs["account_id"],
        "name":             inputs["name"] or name_fallback,
        "internal_rating":  info[0] if info else None,
        "raw_windrun":      inputs["raw_windrun"],
        "rank_tier":        inputs["rank_tier"],
        "leaderboard_rank": inputs["leaderboard_rank"],
        "ad_last_year":     inputs["ad_last_year"],
        "ad_all_time":      inputs["ad_all_time"],
        "ranked_last_year": inputs["ranked_last_year"],
        "ranked_all_time":  inputs["ranked_all_time"],
        "windrun_games":    inputs["windrun_games"],
        "explanation":      info[1] if info else None,
        "avatar_url":       inputs["avatar_url"],
        "fh_unavailable":   inputs["fh_unavailable"],
    }, authoritative=inputs.get("authoritative", ()))
    rating_group.recompute(inputs["account_id"])


async def compute_and_cache_rating(account_id: int, name_fallback: str | None = None) -> int | None:
    """Fetch, resolve and cache one account's rating. Returns the rating, or
    None when nothing could be computed."""
    from db import get_skill_override

    try:
        inputs = await fetch_inputs(account_id)
        info = resolve(inputs, get_skill_override(account_id))
        cache(inputs, info, name_fallback)
        return info[0] if info else None
    except Exception:
        logger.exception("compute_and_cache_rating failed for account %d", account_id)
        return None
