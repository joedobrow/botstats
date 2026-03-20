"""
Windrun.io API client for ability draft statistics.

Fetches ability-high-skill data from the v2 API.
Rate limited to 5 seconds between requests (per windrun policy).
"""

from __future__ import annotations

import asyncio
import time
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://api.windrun.io/api/v2"
RATE_LIMIT_SECONDS = 5.0
REQUEST_TIMEOUT = 90

_last_call_time = 0.0
_rate_lock = asyncio.Lock()


async def _rate_limited_get(session: aiohttp.ClientSession, url: str) -> Optional[dict]:
    global _last_call_time

    async with _rate_lock:
        elapsed = time.monotonic() - _last_call_time
        if elapsed < RATE_LIMIT_SECONDS:
            await asyncio.sleep(RATE_LIMIT_SECONDS - elapsed)
        _last_call_time = time.monotonic()

    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.warning("Windrun returned %d for %s", resp.status, url)
                return None
            return await resp.json()
    except asyncio.TimeoutError:
        logger.error("Windrun request timed out for %s", url)
        return None
    except Exception:
        logger.exception("Windrun request failed for %s", url)
        return None


async def fetch_ability_high_skill() -> Optional[list[dict]]:
    """
    Fetch ability-high-skill data from windrun.io API.

    Returns list of ability stat dicts:
        [{"abilityId": int, "ownerHero": int, "winrate": float,
          "avgPickPosition": float, "numPicks": int, "pickRate": float, ...}, ...]
    """
    url = f"{BASE_URL}/ability-high-skill"
    logger.info("Fetching windrun ability-high-skill")

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        data = await _rate_limited_get(session, url)

    if not data:
        return None

    # Navigate: data -> data -> highSkillData -> abilityStats
    inner = data.get("data", {})
    if isinstance(inner, dict):
        hs_data = inner.get("highSkillData", inner)
        stats = hs_data.get("abilityStats", [])
        if isinstance(stats, list) and stats:
            return stats

    logger.warning("Unexpected windrun response structure")
    return None
