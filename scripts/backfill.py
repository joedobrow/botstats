"""
Backfill script — fills in missing player stat columns for existing matches.

Fetches full match data from OpenDota for every match already in the DB
and updates the new columns that weren't collected at original fetch time:
    tower_kills, roshans_killed, firstblood_claimed, teamfight_participation,
    stuns, camps_stacked, rune_pickups

Also backfills ward stats (obs_placed, sen_placed, observer_kills, sentry_kills)
for any matches that predate those columns.

Run once locally:
    python backfill.py

Uses the same DB_PATH and OPENDOTA_API_KEY as the bot.
"""

import asyncio
import aiohttp
import logging
import sqlite3
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "dota_stats.db")
OPENDOTA_API_KEY = os.environ.get("OPENDOTA_API_KEY")
BASE_URL = "https://api.opendota.com/api"

# Keep in sync with fetcher.DEFENSIVE_ITEM_KEYS
DEFENSIVE_ITEM_KEYS = (
    "pipe", "crimson_guard", "lotus_orb", "glimmer_cape", "force_staff",
    "pavise", "solar_crest", "heavens_halberd", "sphere",
)


def _sum_defensive_item_uses(player: dict) -> int:
    uses = player.get("item_uses") or {}
    return sum(uses.get(k, 0) or 0 for k in DEFENSIVE_ITEM_KEYS)


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def get_all_match_ids() -> list[int]:
    with _conn() as conn:
        rows = conn.execute("SELECT DISTINCT match_id FROM matches ORDER BY match_id").fetchall()
    return [r["match_id"] for r in rows]


def update_players(match_id: int, players_data: list[dict], objectives: list[dict]):
    """UPDATE existing player rows with the new stat columns."""
    # Compute team first tormentor times from objectives
    team_first_tormentor: dict[int, int] = {}
    for obj in objectives:
        if obj.get("type") == "CHAT_MESSAGE_MINIBOSS_KILL":
            team = obj.get("team")
            t = obj.get("time", 0)
            if team and (team not in team_first_tormentor or t < team_first_tormentor[team]):
                team_first_tormentor[team] = t

    with _conn() as conn:
        for p in players_data:
            slot = p.get("player_slot", 0)
            is_radiant = slot < 128
            team_num = 2 if is_radiant else 3
            killed_dict  = p.get("killed") or {}
            ability_uses = p.get("ability_uses") or {}

            conn.execute("""
                UPDATE players SET
                    obs_placed              = :obs_placed,
                    sen_placed              = :sen_placed,
                    observer_kills          = :observer_kills,
                    sentry_kills            = :sentry_kills,
                    tower_kills             = :tower_kills,
                    roshans_killed          = :roshans_killed,
                    firstblood_claimed      = :firstblood_claimed,
                    teamfight_participation = :teamfight_participation,
                    stuns                   = :stuns,
                    camps_stacked           = :camps_stacked,
                    rune_pickups            = :rune_pickups,
                    tormentor_kills         = :tormentor_kills,
                    watcher_captures        = :watcher_captures,
                    team_first_tormentor_time = :team_first_tormentor_time,
                    defensive_item_uses     = :defensive_item_uses,
                    building_damage         = :building_damage
                WHERE match_id = :match_id
                  AND account_id = :account_id
            """, {
                "match_id":               match_id,
                "account_id":             p.get("account_id", 0),
                "obs_placed":             p.get("obs_placed", 0) or 0,
                "sen_placed":             p.get("sen_placed", 0) or 0,
                "observer_kills":         p.get("observer_kills", 0) or 0,
                "sentry_kills":           p.get("sentry_kills", 0) or 0,
                "tower_kills":            p.get("towers_killed", 0) or 0,
                "roshans_killed":         p.get("roshans_killed", 0) or 0,
                "firstblood_claimed":     1 if p.get("firstblood_claimed") else 0,
                "teamfight_participation":p.get("teamfight_participation", 0) or 0,
                "stuns":                  p.get("stuns", 0) or 0,
                "camps_stacked":          p.get("camps_stacked", 0) or 0,
                "rune_pickups":           p.get("rune_pickups", 0) or 0,
                "tormentor_kills":        killed_dict.get("npc_dota_miniboss", 0),
                "watcher_captures":       ability_uses.get("ability_lamp_use", 0),
                "team_first_tormentor_time": team_first_tormentor.get(team_num, -1),
                "defensive_item_uses":    _sum_defensive_item_uses(p),
                "building_damage":        p.get("tower_damage", 0) or 0,
            })


async def fetch_match(session: aiohttp.ClientSession, match_id: int) -> dict | None:
    url = f"{BASE_URL}/matches/{match_id}"
    headers = {}
    if OPENDOTA_API_KEY:
        headers["Authorization"] = f"Bearer {OPENDOTA_API_KEY}"
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                logger.warning("Match %d returned HTTP %d", match_id, resp.status)
                return None
            return await resp.json()
    except Exception:
        logger.exception("Failed to fetch match %d", match_id)
        return None


async def main():
    match_ids = get_all_match_ids()
    logger.info("Found %d matches to backfill", len(match_ids))

    ok = 0
    failed = 0

    async with aiohttp.ClientSession() as session:
        for i, match_id in enumerate(match_ids, 1):
            logger.info("[%d/%d] Fetching match %d ...", i, len(match_ids), match_id)
            data = await fetch_match(session, match_id)

            if not data:
                failed += 1
            else:
                players = data.get("players", [])
                objectives = data.get("objectives", [])
                update_players(match_id, players, objectives)
                ok += 1
                logger.info("  Updated %d players", len(players))

            # Respect rate limit: 60 req/min unauthenticated
            await asyncio.sleep(1.1)

    logger.info("Done. %d updated, %d failed.", ok, failed)


if __name__ == "__main__":
    asyncio.run(main())
