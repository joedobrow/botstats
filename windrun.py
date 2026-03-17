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

_last_call_time = 0.0
_rate_lock = asyncio.Lock()


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
        async with aiohttp.ClientSession(timeout=timeout) as session:
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
