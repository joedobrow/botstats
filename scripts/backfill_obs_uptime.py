"""Fill in observer ward uptime for matches already stored.

The columns were added after these matches were fetched, so every row reads
zero until the logs are pulled again. OpenDota keeps them on any parsed match,
so this re-reads each one and writes only the two uptime columns — nothing
else in the row is touched, so a re-run can't disturb anything that's right.

    python3 scripts/backfill_obs_uptime.py            # every match
    python3 scripts/backfill_obs_uptime.py --limit 20 # a sample first
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp

import db
from fetcher import _obs_uptime

BASE_URL = "https://api.opendota.com/api"
RATE_LIMIT_SLEEP = 1.1          # the free tier allows 60 a minute


async def fetch(session: aiohttp.ClientSession, match_id: int) -> dict | None:
    key = os.environ.get("OPENDOTA_API_KEY")
    url = f"{BASE_URL}/matches/{match_id}" + (f"?api_key={key}" if key else "")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                print(f"  {match_id}: HTTP {r.status}")
                return None
            return await r.json()
    except Exception as e:
        print(f"  {match_id}: {e}")
        return None


async def main(limit: int | None, dry_run: bool) -> None:
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT match_id FROM matches WHERE match_id > 0 ORDER BY start_time DESC"
        ).fetchall()
    ids = [r[0] for r in rows][:limit]
    print(f"{len(ids)} matches to read")

    done = wards = skipped = 0
    async with aiohttp.ClientSession() as session:
        for i, match_id in enumerate(ids, 1):
            data = await fetch(session, match_id)
            await asyncio.sleep(RATE_LIMIT_SLEEP)
            if not data or data.get("version") is None:
                skipped += 1
                print(f"  {match_id}: not parsed by OpenDota, no ward logs")
                continue
            updates = []
            for p in data.get("players", []):
                total, count = _obs_uptime(p)
                updates.append((total, count, match_id, p.get("player_slot", -1)))
                wards += count
            if not dry_run:
                with db._conn() as conn:
                    conn.executemany(
                        "UPDATE players SET obs_uptime_total = ?, obs_uptime_count = ? "
                        "WHERE match_id = ? AND player_slot = ?", updates)
            done += 1
            if i % 20 == 0 or i == len(ids):
                print(f"  {i}/{len(ids)} read, {wards} wards so far")
    print(f"\n{'would update' if dry_run else 'updated'} {done} matches, "
          f"{wards} wards timed, {skipped} unparsed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.limit, a.dry_run))
