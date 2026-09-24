"""One rating for a player who uses several accounts.

Each account is rated on its own data, but no single account need hold the
best evidence about the player. A smurf can have the sharper Ability Draft
record while the main holds the real ranked badge — the smurf is Divine only
because that account never climbed. Rating whole accounts and taking the best
one throws half of that away.

So the accounts are pooled per input rather than per result:

  windrun rating  the best any of their accounts has earned
  ranked badge    the best badge any of their accounts holds
  game counts     added up, since the person really played them all

and the usual formula runs once on that. The season fantasy adjustment is
applied afterwards, to this result.

Both maxima are guarded, because taking the best of two noisy numbers lands
above either on its own: a windrun rating only counts once it rests on
MIN_WINDRUN_GAMES games, and a badge is the account's current medal, which
isn't something a short lucky run produces.

The result is stored on every account in the group (player_ratings_cache
.group_rating), so reads stay a column lookup.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _account_inputs(account_id: int) -> dict:
    """What one account contributes, with its own overrides applied — the
    same resolution rating_compute does, but from the cache rather than the
    APIs."""
    from db import get_rating_cache_row, get_skill_override
    from formatters import MIN_WINDRUN_GAMES

    full = get_rating_cache_row(account_id) or {}
    override = get_skill_override(account_id) or {}

    windrun = override.get("windrun_rating")
    if windrun is None:
        games = full.get("windrun_games")
        # Unknown game count means the row predates windrun_games; trust the
        # rating that was cached rather than dropping evidence we once had.
        if full.get("raw_windrun") is not None and (games is None or games >= MIN_WINDRUN_GAMES):
            windrun = full.get("raw_windrun")

    if override.get("rank_tier") is not None:
        rank_tier, lb_rank = override["rank_tier"], override.get("leaderboard_rank")
    else:
        rank_tier, lb_rank = full.get("rank_tier"), full.get("leaderboard_rank")

    return {
        "account_id": account_id,
        "name": override.get("nickname") or full.get("name") or f"#{account_id}",
        "windrun": windrun,
        "rank_tier": rank_tier,
        "leaderboard_rank": lb_rank,
        "ad_last_year": full.get("ad_last_year"),
        "ad_all_time": full.get("ad_all_time"),
        "ranked_last_year": full.get("ranked_last_year"),
        "manual": override.get("internal_rating_override"),
        # own_rating, not internal_rating: the latter is already the group's
        # own last answer, and feeding that back in would ratchet.
        "own": full.get("own_rating"),
    }


def _summed(members: list[dict], key: str) -> int | None:
    """Total games across accounts; None when no account knows (private
    match history), so the formula treats it as unknown rather than zero."""
    known = [m[key] for m in members if m.get(key) is not None]
    return sum(known) if known else None


def compute(account_ids: list[int]) -> tuple[int | None, str | None]:
    """(rating, explanation) for a group of accounts belonging to one player."""
    from formatters import _rank_to_windrun, _resolve_internal_rating

    members = [_account_inputs(a) for a in account_ids]

    manual = [m for m in members if m["manual"] is not None]
    if manual:
        best = max(manual, key=lambda m: m["manual"])
        return best["manual"], f"manual rating override on {best['name']}"

    rated = [m for m in members if m["windrun"] is not None]
    best_wr = max(rated, key=lambda m: m["windrun"]) if rated else None
    badged = [m for m in members if m["rank_tier"]]
    best_badge = max(badged, key=lambda m: _rank_to_windrun(m["rank_tier"], m["leaderboard_rank"])) if badged else None

    info = _resolve_internal_rating(
        override=None,
        ad_last=_summed(members, "ad_last_year"),
        ad_all=_summed(members, "ad_all_time"),
        ranked_last=_summed(members, "ranked_last_year"),
        ranked_all=None,
        wr_rating=best_wr["windrun"] if best_wr else None,
        ranked_in_wr_raw=(_rank_to_windrun(best_badge["rank_tier"], best_badge["leaderboard_rank"])
                          if best_badge else None),
    )
    if not info:
        return None, None

    rating, explanation = info
    floor = max((m["own"] for m in members if m["own"] is not None), default=None)
    if floor is not None and floor > rating:
        # Pooling only ever adds evidence — a better windrun rating, a better
        # badge, more games — so it can only raise a rating. Landing lower
        # means an input is missing: a row cached before windrun_games was
        # recorded, say. Never make a player worth less than one account of
        # theirs alone; the next refresh fills the gap in.
        logger.info("group %s: pooled %s below best single account %s, keeping the account",
                    [m["account_id"] for m in members], rating, floor)
        return floor, "best single account (pooled inputs incomplete)"

    sources = []
    if best_wr:
        sources.append(f"windrun from {best_wr['name']}")
    if best_badge:
        sources.append(f"badge from {best_badge['name']}")
    across = f" [across {len(members)} accounts: {', '.join(sources)}]" if sources else ""
    return rating, (explanation or "") + across


def recompute_all() -> int:
    """Refresh every group. Cheap (no API calls, a handful of groups), so it
    runs on boot and after a bulk rating refresh: a formula change or a stale
    row then fixes itself without anyone re-linking accounts."""
    from db import account_groups, set_group_rating

    groups = {tuple(g) for g in account_groups().values()}
    for group in groups:
        rating, explanation = compute(list(group))
        set_group_rating(list(group), rating, explanation)
        logger.info("group rating for %s: %s (%s)", list(group), rating, explanation)
    return len(groups)


def recompute(account_id: int) -> tuple[int | None, str | None]:
    """Refresh the shared rating for whatever group `account_id` belongs to,
    and store it on every account in it. A player with one account has no
    group rating: any stale one is cleared so reads fall back to their own."""
    from db import account_groups, set_group_rating

    group = account_groups().get(account_id)
    if not group:
        set_group_rating([account_id], None, None)
        return None, None
    rating, explanation = compute(group)
    set_group_rating(group, rating, explanation)
    logger.info("group rating for %s: %s (%s)", group, rating, explanation)
    return rating, explanation
