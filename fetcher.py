"""
Fetcher — talks to the OpenDota API.

Flow:
  1. GET /leagues/{league_id}/matches  →  list of recent matches for the league
  2. Filter out ability draft and non-US-West matches
  3. For each surviving match_id, GET /matches/{match_id}  →  full player data
  4. Persist everything to SQLite via db.py
"""

import aiohttp
import logging
from datetime import datetime, timezone

from config import LEAGUE_ID, OPENDOTA_API_KEY, EXCLUDED_GAME_MODES, US_WEST_CLUSTERS
from db import upsert_match, upsert_players, match_exists

logger = logging.getLogger(__name__)

BASE_URL = "https://api.opendota.com/api"


def _headers() -> dict:
    """Return auth header if we have an API key."""
    if OPENDOTA_API_KEY:
        return {"Authorization": f"Bearer {OPENDOTA_API_KEY}"}
    return {}


async def _get(session: aiohttp.ClientSession, path: str) -> dict | list | None:
    """Make a GET request, return parsed JSON or None on error."""
    url = f"{BASE_URL}{path}"
    async with session.get(url, headers=_headers()) as resp:
        if resp.status != 200:
            logger.warning("OpenDota returned %d for %s", resp.status, url)
            return None
        return await resp.json()


def _determine_role(player: dict) -> int | None:
    """
    Determine positional role (1-5) from a player object.

    OpenDota's player_slot encodes side + position:
        Radiant: slots 0-4  → positions 1-5
        Dire:    slots 128-132 → positions 1-5
    """
    slot = player.get("player_slot", 0)
    if slot < 128:
        return slot + 1       # Radiant: 0→1, 1→2, …, 4→5
    else:
        return slot - 128 + 1 # Dire: 128→1, 129→2, …, 132→5


async def fetch_and_store_weekly_matches() -> int:
    """
    Main entry point.  Fetches recent league matches, filters, fetches full
    details, and stores them.  Returns the number of NEW matches stored.
    """
    stored = 0

    async with aiohttp.ClientSession() as session:
        # --- Step 1: get league match IDs (works for amateur leagues) ---
        match_ids = await _get(session, f"/leagues/{LEAGUE_ID}/matchIds")
        if not match_ids:
            logger.warning("No match IDs returned for league %d", LEAGUE_ID)
            return 0

        logger.info("Fetched %d match IDs for league", len(match_ids))

        # --- Step 2: we'll filter AFTER fetching full match details ---
        # (can't filter on summary data since /matchIds only gives us IDs)
        candidates = match_ids

        # --- Step 3: fetch full details for each match ID and filter ---
        for match_id_str in candidates:
            mid = int(match_id_str)

            # Skip if we already have this match stored
            if match_exists(mid):
                logger.debug("Match %d already in DB, skipping", mid)
                continue

            full = await _get(session, f"/matches/{mid}")
            if not full:
                logger.warning("Failed to fetch full data for match %d", mid)
                continue

            # --- Apply filters now that we have full match data ---
            game_mode = full.get("game_mode", 0)
            cluster = full.get("cluster", 0)

            if game_mode in EXCLUDED_GAME_MODES:
                logger.info("FILTERED OUT: Match %d — game_mode=%d (excluded)", mid, game_mode)
                continue
            if cluster not in US_WEST_CLUSTERS:
                logger.info("FILTERED OUT: Match %d — cluster=%d (not US West)", mid, cluster)
                continue
            
            # Log matches that PASS the filter
            logger.info("STORING: Match %d — cluster=%d, game_mode=%d", mid, cluster, game_mode)

            # --- Persist match row ---
            radiant_win = 1 if full.get("radiant_win") else 0
            match_row = {
                "match_id":     full["match_id"],
                "league_id":    full.get("leagueid", LEAGUE_ID),
                "start_time":   full.get("start_time", 0),
                "duration":     full.get("duration", 0),
                "game_mode":    full.get("game_mode", 0),
                "cluster":      full.get("cluster", 0),
                "radiant_win":  radiant_win,
                "radiant_score": full.get("radiant_score", 0),
                "dire_score":   full.get("dire_score", 0),
                "fetched_at":   datetime.now(timezone.utc).isoformat(),
            }
            upsert_match(match_row)

            # --- Persist player rows ---
            duration = full.get("duration", 0)
            players = []
            for p in full.get("players", []):
                is_radiant = (p.get("player_slot", 0) < 128)
                won = 1 if (is_radiant and radiant_win) or (not is_radiant and not radiant_win) else 0

                players.append({
                    "match_id":       mid,
                    "account_id":     p.get("account_id", 0),
                    "name":           p.get("personaname") or p.get("name") or f"Player_{p.get('account_id', '?')}",
                    "hero_id":        p.get("hero_id", 0),
                    "team_side":      "radiant" if is_radiant else "dire",
                    "role_position":  _determine_role(p),
                    "kills":          p.get("kills", 0),
                    "deaths":         p.get("deaths", 0),
                    "assists":        p.get("assists", 0),
                    "gpm":            p.get("gold_per_min", 0),
                    "xpm":            p.get("xp_per_min", 0),
                    "last_hits":      p.get("last_hits", 0),
                    "denies":         p.get("denies", 0),
                    "hero_damage":    p.get("hero_damage", 0),
                    "hero_healing":   p.get("hero_healing", 0),
                    "gold_spent":     p.get("gold_spent", 0),
                    "duration":       duration,
                    "won":            won,
                })

            upsert_players(players)
            stored += 1
            logger.info("Stored match %d (%d players)", mid, len(players))

    return stored
