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
from db import upsert_match, upsert_players, upsert_chat_messages, match_exists

logger = logging.getLogger(__name__)

BASE_URL = "https://api.opendota.com/api"


def _headers(use_auth: bool = True) -> dict:
    """Return auth header if we have an API key and use_auth is True."""
    if use_auth and OPENDOTA_API_KEY:
        return {"Authorization": f"Bearer {OPENDOTA_API_KEY}"}
    return {}


async def _get(session: aiohttp.ClientSession, path: str, use_auth: bool = True) -> dict | list | None:
    """Make a GET request, return parsed JSON or None on error."""
    url = f"{BASE_URL}{path}"
    async with session.get(url, headers=_headers(use_auth)) as resp:
        if resp.status != 200:
            logger.warning("OpenDota returned %d for %s", resp.status, url)
            return None
        return await resp.json()


def _farm_priority(player: dict) -> float:
    """
    Return a farm-priority score for sorting cores vs supports.

    Uses CS at 10 minutes (lh_t[10]) when available — this is the cleanest
    signal for distinguishing cores from supports in the laning phase.
    Falls back to GPM when parsed replay data isn't available.
    """
    lh_t = player.get("lh_t")
    if lh_t and len(lh_t) > 10:
        return lh_t[10]
    return player.get("gold_per_min", 0)


def _assign_team_roles(team_players: list[dict]) -> dict[int, int]:
    """
    Assign positions 1-5 to a team of 5 players using lane_role + CS@10 heuristic.

    OpenDota's lane_role only gives us lane info (1=safe, 2=mid, 3=off, 4=jungle).
    It does NOT distinguish pos 4 vs 5. We use CS at 10 minutes (lh_t[10]) within
    shared lanes to split cores from supports, falling back to GPM when parsed
    replay data isn't available:
        - lane_role 2 (mid)       → Position 2
        - lane_role 3 (off lane)  → Position 3 (highest CS@10), Position 4 (lower)
        - lane_role 1 (safe lane) → Position 1 (highest CS@10), Position 5 (lower)
        - lane_role 4 (jungle)    → fills remaining slots by CS@10
        - Anyone left over        → fills remaining slots by CS@10

    Returns a dict mapping player_slot → position (1-5).
    """
    if len(team_players) != 5:
        # Can't assign roles if we don't have exactly 5 players
        return {p.get("player_slot", 0): None for p in team_players}

    assigned: dict[int, int] = {}     # player_slot → position
    remaining_players = list(team_players)
    taken_positions: set[int] = set()

    # --- Mid lane (lane_role=2) → Position 2 ---
    mid_players = [p for p in remaining_players if p.get("lane_role") == 2]
    if mid_players:
        mid = max(mid_players, key=_farm_priority)
        assigned[mid.get("player_slot", 0)] = 2
        taken_positions.add(2)
        remaining_players.remove(mid)

    # --- Off lane (lane_role=3) → Position 3 (core), Position 4 (support) ---
    off_players = sorted(
        [p for p in remaining_players if p.get("lane_role") == 3],
        key=_farm_priority, reverse=True,
    )
    for i, p in enumerate(off_players):
        if i == 0 and 3 not in taken_positions:
            assigned[p.get("player_slot", 0)] = 3
            taken_positions.add(3)
        elif 4 not in taken_positions:
            assigned[p.get("player_slot", 0)] = 4
            taken_positions.add(4)
        remaining_players.remove(p)

    # --- Safe lane (lane_role=1) → Position 1 (carry), Position 5 (support) ---
    safe_players = sorted(
        [p for p in remaining_players if p.get("lane_role") == 1],
        key=_farm_priority, reverse=True,
    )
    for i, p in enumerate(safe_players):
        if i == 0 and 1 not in taken_positions:
            assigned[p.get("player_slot", 0)] = 1
            taken_positions.add(1)
        elif 5 not in taken_positions:
            assigned[p.get("player_slot", 0)] = 5
            taken_positions.add(5)
        remaining_players.remove(p)

    # --- Jungle / roaming (lane_role=4) and anyone left → fill remaining slots ---
    remaining_players.sort(key=_farm_priority, reverse=True)
    open_positions = sorted(set(range(1, 6)) - taken_positions)
    for p, pos in zip(remaining_players, open_positions):
        assigned[p.get("player_slot", 0)] = pos

    return assigned


async def fetch_and_store_weekly_matches() -> int:
    """
    Main entry point.  Fetches recent league matches, filters, fetches full
    details, and stores them.  Returns the number of NEW matches stored.
    """
    stored = 0

    async with aiohttp.ClientSession() as session:
        # --- Step 1: get league match IDs (works for amateur leagues) ---
        match_ids = await _get(session, f"/leagues/{LEAGUE_ID}/matchIds", use_auth=False)
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

            # Don't use API key - causes 400s for amateur league matches
            # Add small delay to avoid hitting rate limits (60 req/min = 1 per second)
            import asyncio
            await asyncio.sleep(1.1)
            
            full = await _get(session, f"/matches/{mid}", use_auth=False)
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
            all_players = full.get("players", [])

            # Assign roles per team (need full team context for GPM heuristic)
            radiant_team = [p for p in all_players if p.get("player_slot", 0) < 128]
            dire_team = [p for p in all_players if p.get("player_slot", 0) >= 128]
            role_map = {}
            role_map.update(_assign_team_roles(radiant_team))
            role_map.update(_assign_team_roles(dire_team))

            players = []
            for p in all_players:
                slot = p.get("player_slot", 0)
                is_radiant = slot < 128
                won = 1 if (is_radiant and radiant_win) or (not is_radiant and not radiant_win) else 0

                players.append({
                    "match_id":       mid,
                    "account_id":     p.get("account_id", 0),
                    "name":           p.get("personaname") or p.get("name") or f"Player_{p.get('account_id', '?')}",
                    "hero_id":        p.get("hero_id", 0),
                    "team_side":      "radiant" if is_radiant else "dire",
                    "role_position":  role_map.get(slot),
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

            # --- Persist chat messages (typed only, not chatwheel) ---
            chat_data = full.get("chat", [])
            # Build a map of player_slot -> player_name for this match
            slot_to_name = {p.get("player_slot", 0): p.get("personaname") or p.get("name") or "Unknown" for p in all_players}

            chat_messages = []
            for msg in chat_data:
                if msg.get("type") == "chat":  # Only typed messages, not chatwheel
                    slot = msg.get("player_slot", msg.get("slot", 0))
                    chat_messages.append({
                        "match_id": mid,
                        "player_slot": slot,
                        "player_name": slot_to_name.get(slot, "Unknown"),
                        "time": msg.get("time", 0),
                        "message": msg.get("key", ""),
                    })

            if chat_messages:
                upsert_chat_messages(chat_messages)
                logger.info("Stored %d chat messages for match %d", len(chat_messages), mid)

            stored += 1
            logger.info("Stored match %d (%d players)", mid, len(players))

    return stored
