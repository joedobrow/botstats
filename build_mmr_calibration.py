"""
Build a sample of (windrun_rating, ranked_mmr_estimate) pairs for fitting the
conversion model used by /lookup's internal-rating calculation.

v4 pipeline — leaderboards + CSV + match-spider for broad coverage:

  1. Source A: windrun leaderboards per region (top 100 each, no extra calls).
  2. Source B: drafted players from player_costs (CSV-imported windrun rating).
  3. Source C (spider): pick seeds spanning the rating range, fetch each
     seed's /players/{id}/matches to get matchIds (the per-player endpoint
     anonymizes opponents but does expose match_id), then hit
     /matches/{matchId} for each — that endpoint shows every participant's
     steamId AND their windrun rating at match time.
  4. Combine, dedupe, shuffle.
  5. Skip account_ids already in mmr_calibration so re-runs only add new data.
  6. OpenDota filter (3 calls each, throttled ~55/min):
        - /players/{id}                  → rank_tier, leaderboard_rank
        - /players/{id}/wl?game_mode=18  → AD games
        - /players/{id}/wl?lobby_type=7  → ranked games
  7. Filter: rank_tier set, AD ≥ MIN_AD, ranked ≥ MIN_RANKED. Optionally drop
     non-leaderboard Immortals (their MMR estimate floors to 5420).
  8. Stop at TARGET total qualified. Upsert results.

Usage:
    python build_mmr_calibration.py
       [--target 1000] [--min-ad 300] [--min-ranked 300]
       [--regions americas,europe,china,sea]
       [--spider-seeds 30] [--matches-per-seed 10]
       [--skip-noisy-immortals]
"""

import argparse
import asyncio
import logging
import os
import random
from datetime import datetime, timezone

import aiohttp

from db import init_db, _conn
from opendota_lookup import estimate_mmr_from_rank_tier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

WINDRUN_BASE       = "https://api.windrun.io/api/v2"
OPENDOTA_BASE      = "https://api.opendota.com/api"
WINDRUN_RATE_LIMIT = 5.0
# Free tier: 60/min. With API key: 1200/min. We do 3 parallel calls per
# candidate, so 0.3s delay → ~600/min. Falls back to 1.1s if no key.
OPENDOTA_API_KEY   = os.environ.get("OPENDOTA_API_KEY")
OPENDOTA_DELAY     = 0.3 if OPENDOTA_API_KEY else 1.1


def _with_key(url: str) -> str:
    if not OPENDOTA_API_KEY:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}api_key={OPENDOTA_API_KEY}"


async def fetch_windrun_region(session, region: str) -> list[dict]:
    url = f"{WINDRUN_BASE}/leaderboard/{region}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                logger.warning("windrun %s returned %d", region, resp.status)
                return []
            return (await resp.json()).get("data", [])
    except Exception:
        logger.exception("windrun fetch failed for region %s", region)
        return []


async def fetch_windrun_player_matches_list(session, account_id: int) -> list[dict]:
    """List of matches the player participated in. Opponents are anonymized but
    the match_id is exposed."""
    url = f"{WINDRUN_BASE}/players/{account_id}/matches"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=90)) as resp:
            if resp.status != 200:
                logger.warning("windrun matches list %d returned %d", account_id, resp.status)
                return []
            data = await resp.json()
            return data if isinstance(data, list) else (data.get("data") or [])
    except Exception:
        logger.exception("windrun matches list fetch failed for %d", account_id)
        return []


async def fetch_windrun_match(session, match_id: int) -> dict | None:
    """Full match detail — shows every participant's steamId + windrun rating
    at the time of the match."""
    url = f"{WINDRUN_BASE}/matches/{match_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=90)) as resp:
            if resp.status != 200:
                logger.warning("windrun match %d returned %d", match_id, resp.status)
                return None
            data = await resp.json()
            return data.get("data") if isinstance(data, dict) and "data" in data else data
    except Exception:
        logger.exception("windrun match fetch failed for %d", match_id)
        return None


async def fetch_opendota_profile_and_counts(session, account_id: int) -> dict | None:
    base = f"{OPENDOTA_BASE}/players/{account_id}"
    urls = [
        _with_key(base),
        _with_key(f"{base}/wl?game_mode=18&significant=0"),
        _with_key(f"{base}/wl?lobby_type=7&significant=0"),
    ]

    async def fetch(url: str):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception:
            return None

    results = await asyncio.gather(*(fetch(u) for u in urls))
    if any(r is None for r in results):
        return None
    profile, ad_wl, ranked_wl = results
    return {
        "rank_tier":         profile.get("rank_tier"),
        "leaderboard_rank":  profile.get("leaderboard_rank"),
        "ad_games":          (ad_wl.get("win", 0) or 0) + (ad_wl.get("lose", 0) or 0),
        "ranked_games":      (ranked_wl.get("win", 0) or 0) + (ranked_wl.get("lose", 0) or 0),
    }


def load_csv_candidates() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT account_id, mmr AS windrun_rating
            FROM player_costs
            WHERE mmr IS NOT NULL AND account_id > 0
        """).fetchall()
    return [
        {
            "account_id":     r["account_id"],
            "windrun_rating": float(r["windrun_rating"]),
            "region":         "csv",
            "source":         "csv",
        }
        for r in rows
    ]


def load_existing_calibration_ids() -> set[int]:
    """Account IDs already qualified in mmr_calibration — skip re-fetching."""
    with _conn() as conn:
        rows = conn.execute("SELECT account_id FROM mmr_calibration").fetchall()
    return {r["account_id"] for r in rows}


def upsert_calibration(rows: list[dict]):
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        r["fetched_at"] = now
    with _conn() as conn:
        conn.executemany("""
            INSERT INTO mmr_calibration (
                account_id, windrun_rating, rank_tier, leaderboard_rank,
                mmr_estimate, ad_games, ranked_games, region, fetched_at
            ) VALUES (
                :account_id, :windrun_rating, :rank_tier, :leaderboard_rank,
                :mmr_estimate, :ad_games, :ranked_games, :region, :fetched_at
            )
            ON CONFLICT(account_id) DO UPDATE SET
                windrun_rating   = excluded.windrun_rating,
                rank_tier        = excluded.rank_tier,
                leaderboard_rank = excluded.leaderboard_rank,
                mmr_estimate     = excluded.mmr_estimate,
                ad_games         = excluded.ad_games,
                ranked_games     = excluded.ranked_games,
                region           = excluded.region,
                fetched_at       = excluded.fetched_at
        """, rows)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=1000)
    ap.add_argument("--min-ad", type=int, default=300)
    ap.add_argument("--min-ranked", type=int, default=300)
    ap.add_argument("--regions", default="americas,europe,china,sea")
    ap.add_argument("--spider-seeds", type=int, default=30,
                    help="Number of seed players to spider matches from.")
    ap.add_argument("--matches-per-seed", type=int, default=10,
                    help="Number of matches to fetch per spider seed.")
    ap.add_argument("--skip-noisy-immortals", action="store_true")
    args = ap.parse_args()

    init_db()

    existing_ids = load_existing_calibration_ids()
    logger.info("Already qualified in DB: %d", len(existing_ids))

    regions = [r.strip() for r in args.regions.split(",")]
    by_id: dict[int, dict] = {}

    async with aiohttp.ClientSession() as session:
        # === Source A: windrun leaderboards ===
        for i, region in enumerate(regions):
            if i > 0:
                await asyncio.sleep(WINDRUN_RATE_LIMIT)
            entries = await fetch_windrun_region(session, region)
            logger.info("Windrun %s: %d players", region, len(entries))
            for e in entries:
                aid = e.get("steamId")
                if not aid or aid <= 0:
                    continue
                by_id.setdefault(aid, {
                    "account_id":     aid,
                    "windrun_rating": float(e["rating"]),
                    "region":         e.get("region", region),
                    "source":         "leaderboard",
                })

        # === Source B: CSV drafted players ===
        for c in load_csv_candidates():
            if c["account_id"] not in by_id:
                by_id[c["account_id"]] = c

        logger.info("Pre-spider candidate pool: %d", len(by_id))

        # === Source C: spider via match details ===
        # Pick seeds across the rating range. We want low/mid ratings (where
        # diversity is highest) AND a few CSV players (mid-range) to surface
        # below-leaderboard accounts. Sort by rating, take spaced samples.
        pool_sorted = sorted(by_id.values(), key=lambda c: c["windrun_rating"])
        if pool_sorted:
            step = max(1, len(pool_sorted) // args.spider_seeds)
            seeds = pool_sorted[::step][:args.spider_seeds]
            random.shuffle(seeds)
            logger.info(
                "Spider seeds (n=%d): windrun range [%.0f, %.0f]",
                len(seeds), min(s["windrun_rating"] for s in seeds),
                max(s["windrun_rating"] for s in seeds),
            )

            # Step 1: get matchIds per seed
            all_matchids: set[int] = set()
            for i, seed in enumerate(seeds):
                await asyncio.sleep(WINDRUN_RATE_LIMIT)
                matches = await fetch_windrun_player_matches_list(session, seed["account_id"])
                if not matches:
                    logger.warning("[seed %d/%d aid=%d] no matches", i + 1, len(seeds), seed["account_id"])
                    continue
                ids = [m.get("matchId") for m in matches[:args.matches_per_seed]
                       if m.get("matchId")]
                new = [m for m in ids if m not in all_matchids]
                all_matchids.update(new)
                logger.info("[seed %d/%d aid=%d] +%d new matchIds (total %d)",
                            i + 1, len(seeds), seed["account_id"], len(new), len(all_matchids))

            # Step 2: fetch each match — get all 10 players with ratings
            for j, mid in enumerate(all_matchids):
                await asyncio.sleep(WINDRUN_RATE_LIMIT)
                m = await fetch_windrun_match(session, mid)
                if not m:
                    continue
                n_before = len(by_id)
                for side in ("radiant", "dire"):
                    for p in (m.get(side) or []):
                        sid = p.get("steamId")
                        rating = p.get("rating")
                        if not sid or sid <= 0 or rating is None:
                            continue
                        by_id.setdefault(sid, {
                            "account_id":     sid,
                            "windrun_rating": float(rating),
                            "region":         m.get("region", "?"),
                            "source":         "spider",
                        })
                if (j + 1) % 25 == 0 or (j + 1) == len(all_matchids):
                    logger.info("[match %d/%d] pool=%d (+%d so far)",
                                j + 1, len(all_matchids), len(by_id), len(by_id) - n_before)

        candidates = list(by_id.values())
        random.shuffle(candidates)
        logger.info(
            "Final pool: %d unique (excluding %d already in DB)",
            len(candidates), len([c for c in candidates if c["account_id"] in existing_ids]),
        )

        # === OpenDota filter ===
        qualified: list[dict] = []
        skipped = {"already": 0, "hidden": 0, "few_games": 0, "failed": 0, "noisy_immortal": 0}
        seen = 0
        target_new = max(0, args.target - len(existing_ids))
        if target_new == 0:
            logger.info("Target already reached; nothing to do.")
            return
        logger.info("Targeting %d new qualified (DB has %d, target %d)", target_new, len(existing_ids), args.target)

        for c in candidates:
            if len(qualified) >= target_new:
                break
            if c["account_id"] in existing_ids:
                skipped["already"] += 1
                continue
            if seen > 0:
                await asyncio.sleep(OPENDOTA_DELAY)
            seen += 1

            od = await fetch_opendota_profile_and_counts(session, c["account_id"])
            if od is None:
                skipped["failed"] += 1
                continue
            if not od["rank_tier"]:
                skipped["hidden"] += 1
                continue
            if od["ad_games"] < args.min_ad or od["ranked_games"] < args.min_ranked:
                skipped["few_games"] += 1
                continue
            if args.skip_noisy_immortals and od["rank_tier"] == 80 and not od["leaderboard_rank"]:
                skipped["noisy_immortal"] += 1
                continue

            mmr_est = estimate_mmr_from_rank_tier(od["rank_tier"], od["leaderboard_rank"])
            qualified.append({
                "account_id":       c["account_id"],
                "windrun_rating":   c["windrun_rating"],
                "rank_tier":        od["rank_tier"],
                "leaderboard_rank": od["leaderboard_rank"],
                "mmr_estimate":     mmr_est,
                "ad_games":         od["ad_games"],
                "ranked_games":     od["ranked_games"],
                "region":           c.get("region"),
            })
            # Persist every 25 rows so we don't lose work on crash.
            if len(qualified) % 25 == 0:
                upsert_calibration(qualified[-25:])
                logger.info("Checkpoint upsert at %d new qualified", len(qualified))
            logger.info(
                "[%4d/%d] aid=%d src=%-11s wr=%.0f mmr_est=%s rank_tier=%d ad=%d rk=%d",
                len(qualified), target_new,
                c["account_id"], c["source"], c["windrun_rating"], mmr_est, od["rank_tier"],
                od["ad_games"], od["ranked_games"],
            )

        logger.info("Done. new_qualified=%d, seen=%d, skipped: %s",
                    len(qualified), seen, skipped)

    # Final upsert (anything not already checkpointed)
    upsert_calibration(qualified)
    logger.info("Upserted %d rows. mmr_calibration total ≈ %d",
                len(qualified), len(existing_ids) + len(qualified))


if __name__ == "__main__":
    asyncio.run(main())
