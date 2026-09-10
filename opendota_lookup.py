"""
opendota_lookup.py — OpenDota API helpers for /lookup: player profile,
rank-tier decoding, and MMR estimation.
"""

import asyncio
import logging
import math
import os
from collections import Counter

import aiohttp

logger = logging.getLogger(__name__)

OPENDOTA_BASE = "https://api.opendota.com/api"

# Used by /lookup's profile + game-count fetches. Free tier (no key) is
# 60 req/min — we burn 6 per /lookup, so unauthenticated calls fail under
# even light usage. With key: 1200/min.
OPENDOTA_API_KEY = os.environ.get("OPENDOTA_API_KEY")


def _with_key(url: str) -> str:
    if not OPENDOTA_API_KEY:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}api_key={OPENDOTA_API_KEY}"
# ---------------------------------------------------------------------------
# Player profile lookup (used by /lookup for ranked MMR estimate)
# ---------------------------------------------------------------------------

MEDAL_NAMES = {
    1: "Herald",
    2: "Guardian",
    3: "Crusader",
    4: "Archon",
    5: "Legend",
    6: "Ancient",
    7: "Divine",
    8: "Immortal",
}

# Approximate MMR boundaries per medal tier (lo inclusive, hi exclusive).
# Each non-Immortal tier has 5 stars; each star covers 1/5 of the band.
# Immortal has no upper bound — we use a sensible floor + leaderboard-rank
# context when shown.
MMR_BRACKETS = {
    1: (0,    770),    # Herald
    2: (770,  1540),   # Guardian
    3: (1540, 2310),   # Crusader
    4: (2310, 3080),   # Archon
    5: (3080, 3850),   # Legend
    6: (3850, 4620),   # Ancient
    7: (4620, 5420),   # Divine
    8: (5420, None),   # Immortal
}


async def fetch_player_profile(account_id: int) -> dict | None:
    """Fetch /players/{account_id} from OpenDota. Returns the JSON dict or
    None on error. Uses OPENDOTA_API_KEY when available (1200 req/min)."""
    url = _with_key(f"{OPENDOTA_BASE}/players/{account_id}")
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("OpenDota returned %d for %s", resp.status, url)
                    return None
                return await resp.json()
    except Exception:
        logger.exception("OpenDota player fetch failed for %d", account_id)
        return None


def decode_rank_tier(
    rank_tier: int | None,
    leaderboard_rank: int | None = None,
) -> str | None:
    """Turn a numeric rank_tier into a display string like 'Divine 2' or
    'Immortal (Top 1701)'. Returns None if rank_tier is missing/unparseable."""
    if not rank_tier:
        return None
    tier = rank_tier // 10
    stars = rank_tier % 10
    medal = MEDAL_NAMES.get(tier)
    if not medal:
        return None
    if tier == 8:  # Immortal — no stars, optional leaderboard rank
        return f"Immortal (Top {leaderboard_rank})" if leaderboard_rank else "Immortal"
    if stars > 0:
        return f"{medal} {stars}"
    return medal


def estimate_mmr_from_rank_tier(
    rank_tier: int | None,
    leaderboard_rank: int | None = None,
) -> int | None:
    """Midpoint MMR estimate for the given rank_tier. ±154 MMR within a
    star-band. For Immortal players with a leaderboard_rank, fits a rough
    log curve (mmr ≈ 13000 - 880·ln(rank)) — calibrated so top 1 ≈ 13k,
    top 100 ≈ 8.9k, top 1000 ≈ 6.9k, top 5000 ≈ 5.5k.
    """
    if not rank_tier:
        return None
    tier = rank_tier // 10
    stars = rank_tier % 10
    bracket = MMR_BRACKETS.get(tier)
    if not bracket:
        return None
    lo, hi = bracket
    if hi is None:
        # Immortal: log-linear fit on 8 user-supplied NA anchors (R² = 0.99):
        #   (rank, mmr) = (11, 11000), (550, 8400), (1026, 7980), (1063, 8000),
        #                 (1750, 7500), (2000, 7200), (2207, 7349), (4294, 6800)
        #   → mmr = 12745 − 702.1 · ln(rank)
        # Trained on NA leaderboard. EU has a separate (steeper) curve and
        # will be slightly underestimated, but most use is NA-anchored.
        # Clamped to Immortal floor (5420).
        if leaderboard_rank and leaderboard_rank >= 1:
            mmr = 12745 - 702.1 * math.log(leaderboard_rank)
            return max(lo, int(mmr))
        # Non-leaderboard Immortal: NA leaderboard tops out around rank 5000
        # (~6765 MMR), so a generic Immortal sits between the floor (5420)
        # and that cutoff. Midpoint ≈ 6090; round to 6000.
        return 6000
    width = hi - lo
    if stars > 0:
        # Star n covers 1/5 of the band; pick its midpoint.
        return lo + round(width * (2 * stars - 1) / 10)
    return lo + width // 2


async def fetch_player_game_counts(account_id: int) -> dict | None:
    """Fetch all-time + last-year breakdowns from OpenDota's /wl endpoints
    in parallel. Five queries: all-time total, last-year total, all-time
    ranked (lobby_type=7), all-time AD (game_mode=18), last-year AD.

    Note: OpenDota's lobby_type + date filter returns 0/0 (apparent bug),
    so last_year_ranked is ratio-estimated from the other counts.

    The two date=365 queries (last-year total, last-year AD) have also been
    observed to occasionally return a valid-looking but wrong (too-low)
    count with a normal 200 status — no error to catch, just silently bad
    data (e.g. three unrelated long-time AD players all separately landing
    on ad_last_year=30 despite wildly different real activity, confirmed
    wrong by re-querying moments later). Those two are verified by
    re-querying — see fetch_wl_verified.

    Returns:
        {
            'all_time_total':   int,
            'last_year_total':  int,
            'all_time_ranked':  int,
            'last_year_ranked': int,  # ratio-estimated
            'all_time_ad':      int,
            'last_year_ad':     int,
        }
    or None if any request fails.
    """
    base = f"{OPENDOTA_BASE}/players/{account_id}/wl"
    all_time_total_url  = _with_key(f"{base}?significant=0")
    last_year_total_url = _with_key(f"{base}?significant=0&date=365")
    all_time_ranked_url = _with_key(f"{base}?lobby_type=7&significant=0")
    all_time_ad_url     = _with_key(f"{base}?game_mode=18&significant=0")
    last_year_ad_url    = _with_key(f"{base}?game_mode=18&date=365&significant=0")

    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async def fetch_wl(url: str) -> int | None:
                # One retry on transient failure — OpenDota's /wl endpoints
                # occasionally 500 or time out under load.
                for attempt in range(2):
                    try:
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                j = await resp.json()
                                return (j.get("win", 0) or 0) + (j.get("lose", 0) or 0)
                    except Exception:
                        pass
                    if attempt == 0:
                        await asyncio.sleep(0.5)
                return None

            async def fetch_wl_verified(url: str) -> int | None:
                """Like fetch_wl, but for the date=365 queries where a 200
                response can still be silently wrong. Re-queries up to 3
                times; returns as soon as two readings agree. If all three
                disagree, falls back to the largest reading (the observed
                failure mode is under-counting, never over-counting) and
                logs it since that case hasn't been directly confirmed safe."""
                samples = []
                for _ in range(3):
                    value = await fetch_wl(url)
                    if value is None:
                        return None
                    samples.append(value)
                    if len(samples) >= 2 and samples[-1] == samples[-2]:
                        return value
                tally = Counter(samples)
                most_common_value, n = tally.most_common(1)[0]
                if n >= 2:
                    return most_common_value
                logger.warning(
                    "OpenDota date-filtered wl count never agreed for account %d (%s): %s — using max",
                    account_id, url, samples,
                )
                return max(samples)

            all_total, last_total, all_ranked, all_ad, last_ad = await asyncio.gather(
                fetch_wl(all_time_total_url),
                fetch_wl_verified(last_year_total_url),
                fetch_wl(all_time_ranked_url),
                fetch_wl(all_time_ad_url),
                fetch_wl_verified(last_year_ad_url),
            )
        counts = (all_total, last_total, all_ranked, all_ad, last_ad)
        if any(c is None for c in counts):
            logger.warning("OpenDota wl fetch failed for %d", account_id)
            return None
        # OpenDota's lobby_type+date filter is broken (returns 0/0), so we
        # estimate last-year ranked from the *non-AD* slice:
        #   non_ad_last  = last_total - last_ad
        #   ranked_share = all_ranked / (all_total - all_ad)
        #   last_ranked  = round(non_ad_last * ranked_share)
        # The earlier shortcut (last_total * all_ranked / all_total) over-
        # estimated for AD specialists whose recent play has shifted entirely
        # to AD — it treated their few non-AD last-year games as having the
        # same ranked share as their all-time mix.
        non_ad_last = max(0, last_total - last_ad)
        non_ad_all  = max(0, all_total - all_ad)
        ranked_share_non_ad = (all_ranked / non_ad_all) if non_ad_all > 0 else 0
        return {
            "all_time_total":   all_total,
            "last_year_total":  last_total,
            "all_time_ranked":  all_ranked,
            "last_year_ranked": int(round(non_ad_last * ranked_share_non_ad)),
            "all_time_ad":      all_ad,
            "last_year_ad":     last_ad,
        }
    except Exception:
        logger.exception("OpenDota counts fetch failed for %d", account_id)
        return None
