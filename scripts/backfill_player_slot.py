"""
One-off backfill: fills in players.player_slot for matches that predate
the column. Only touches rows where player_slot = -1.

Run on prod:
    DB_PATH=/data/dota_stats.db python3 scripts/backfill_player_slot.py
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


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def get_match_ids_needing_backfill() -> list[int]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT match_id FROM players
            WHERE player_slot < 0
            ORDER BY match_id
        """).fetchall()
    return [r["match_id"] for r in rows]


def update_player_slots(match_id: int, players_data: list[dict]) -> int:
    """Update player_slot for each player in this match. Returns rows touched."""
    touched = 0
    with _conn() as conn:
        for p in players_data:
            aid = p.get("account_id", 0) or 0
            slot = p.get("player_slot")
            if slot is None or aid == 0:
                continue
            cur = conn.execute(
                "UPDATE players SET player_slot = ? "
                "WHERE match_id = ? AND account_id = ? AND player_slot < 0",
                (slot, match_id, aid),
            )
            touched += cur.rowcount
    return touched


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
    match_ids = get_match_ids_needing_backfill()
    logger.info("Found %d matches needing player_slot backfill", len(match_ids))

    ok = 0
    failed = 0
    total_rows_touched = 0
    rate = 0.6 if OPENDOTA_API_KEY else 1.1

    async with aiohttp.ClientSession() as session:
        for i, match_id in enumerate(match_ids, 1):
            logger.info("[%d/%d] match %d ...", i, len(match_ids), match_id)
            data = await fetch_match(session, match_id)
            if not data:
                failed += 1
            else:
                touched = update_player_slots(match_id, data.get("players", []))
                total_rows_touched += touched
                ok += 1
                logger.info("  updated %d player rows", touched)
            await asyncio.sleep(rate)

    logger.info(
        "Done. %d matches ok, %d failed. Total player rows touched: %d",
        ok, failed, total_rows_touched,
    )


if __name__ == "__main__":
    asyncio.run(main())
