"""
Windrun.io API client with rate limiting.

The API owner requires at least 5 seconds between requests.
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
REQUEST_TIMEOUT = 90  # API is reportedly very slow

# Windrun.io whitelisted this UA — required to bypass Cloudflare's bot
# challenge on api.windrun.io. Don't change without coordinating with Noxville.
HEADERS = {"User-Agent": "RD2L-EST-WED-Bot (contact: jdobrow@gmail.com)"}

_last_call_time = 0.0
_rate_lock = asyncio.Lock()


async def fetch_player(account_id: int) -> dict | None:
    """
    Fetch player data from windrun.io, respecting the 5-second rate limit.

    Returns the parsed JSON `data` dict (with keys nickname, rating, region,
    overallRank, regionalRank, percentile, wins, losses, avatar, lastMatch),
    or None on error.
    """
    global _last_call_time

    async with _rate_lock:
        elapsed = time.monotonic() - _last_call_time
        if elapsed < RATE_LIMIT_SECONDS:
            wait = RATE_LIMIT_SECONDS - elapsed
            logger.info("Windrun rate limit: waiting %.1fs", wait)
            await asyncio.sleep(wait)
        _last_call_time = time.monotonic()

    url = f"{BASE_URL}/players/{account_id}"
    logger.info("Fetching windrun player %d", account_id)

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Windrun returned %d for %s", resp.status, url)
                    return None
                data = await resp.json()
                return data.get("data") if isinstance(data, dict) else data
    except asyncio.TimeoutError:
        logger.error("Windrun request timed out for player %d", account_id)
        return None
    except Exception:
        logger.exception("Windrun request failed for player %d", account_id)
        return None


async def fetch_player_matches(account_id: int, limit: int = 200) -> list | None:
    """Fetch a player's recent matches (most recent first). Heavy response —
    only call when match-list date filtering is needed (e.g. last-year count).
    Same 5-second rate limit as other windrun calls.
    """
    global _last_call_time

    async with _rate_lock:
        elapsed = time.monotonic() - _last_call_time
        if elapsed < RATE_LIMIT_SECONDS:
            await asyncio.sleep(RATE_LIMIT_SECONDS - elapsed)
        _last_call_time = time.monotonic()

    url = f"{BASE_URL}/players/{account_id}/matches?limit={limit}"
    logger.info("Fetching windrun matches for %d (limit %d)", account_id, limit)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Windrun returned %d for %s", resp.status, url)
                    return None
                data = await resp.json()
                if isinstance(data, dict):
                    return data.get("data") or []
                return data
    except asyncio.TimeoutError:
        logger.error("Windrun matches request timed out for %d", account_id)
        return None
    except Exception:
        logger.exception("Windrun matches request failed for %d", account_id)
        return None


async def fetch_match(match_id: int) -> dict | None:
    """
    Fetch match data from windrun.io, respecting the 5-second rate limit.

    Returns the parsed JSON response or None on error.
    """
    global _last_call_time

    async with _rate_lock:
        elapsed = time.monotonic() - _last_call_time
        if elapsed < RATE_LIMIT_SECONDS:
            wait = RATE_LIMIT_SECONDS - elapsed
            logger.info("Windrun rate limit: waiting %.1fs", wait)
            await asyncio.sleep(wait)
        _last_call_time = time.monotonic()

    url = f"{BASE_URL}/matches/{match_id}"
    logger.info("Fetching windrun match %d", match_id)

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=HEADERS) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Windrun returned %d for %s", resp.status, url)
                    return None
                data = await resp.json()
                return data.get("data") if isinstance(data, dict) else data
    except asyncio.TimeoutError:
        logger.error("Windrun request timed out for match %d", match_id)
        return None
    except Exception:
        logger.exception("Windrun request failed for match %d", match_id)
        return None
