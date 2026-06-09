"""
One-shot importer: read a draft-sheet CSV and populate the player_costs table.

CSV format expected (column header names with the trailing colon, as exported
from the league's Google Sheet):

    Winner:, Cost:, Discord ID:, Dotabuff Link:, MMR:, Comfort (Pos 1):, ..., Player statement:

Only the first four columns + MMR are stored:
    captain ← "Winner:"
    cost    ← "Cost:"
    name    ← "Discord ID:"          (logged only; identity lives in account_id)
    account_id ← parsed from "Dotabuff Link:"  (last numeric segment of the URL)
    mmr     ← "MMR:"

Usage:
    python import_costs.py --csv draft-sheet.csv \
        --guild-id 1481800158826991718 --season-start 2026-04-28

Uses the same DB_PATH as the bot (default /data/dota_stats.db on fly, else
dota_stats.db). Re-running with the same (guild_id, season_start) overwrites
existing rows for that season.
"""

import argparse
import csv
import logging
import re
import sys

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

from db import upsert_player_costs, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DOTABUFF_RE = re.compile(r"dotabuff\.com/players/(\d+)")


def parse_account_id(url: str) -> int | None:
    """Extract the trailing account_id from a Dotabuff player URL."""
    if not url:
        return None
    m = DOTABUFF_RE.search(url)
    return int(m.group(1)) if m else None


def parse_int(s: str | None) -> int | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def load_rows(csv_path: str) -> list[dict]:
    """Parse the CSV and return a list of {account_id, cost, captain, mmr, name} dicts."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for raw in reader:
            url = raw.get("Dotabuff Link:") or ""
            account_id = parse_account_id(url)
            cost = parse_int(raw.get("Cost:"))
            captain = (raw.get("Winner:") or "").strip() or None
            mmr = parse_int(raw.get("MMR:"))
            name = (raw.get("Discord ID:") or "").strip()

            if account_id is None:
                logger.warning("Skipping row (no parseable account_id): %s", raw)
                continue
            if cost is None:
                logger.warning("Skipping %s (no cost)", name or account_id)
                continue

            rows.append({
                "account_id": account_id,
                "cost":       cost,
                "captain":    captain,
                "mmr":        mmr,
                "_name":      name,  # not stored, just for logging
            })
        return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="Path to draft-sheet CSV")
    ap.add_argument("--guild-id", type=int, required=True, help="Discord guild ID this season belongs to")
    ap.add_argument("--season-start", required=True, help="YYYY-MM-DD; should match divisions.season_start")
    args = ap.parse_args()

    init_db()  # safe to call; ensures the table exists

    rows = load_rows(args.csv)
    if not rows:
        logger.error("No valid rows found in %s", args.csv)
        sys.exit(1)

    logger.info("Parsed %d player rows from %s", len(rows), args.csv)

    # Strip the local-only _name field before persisting
    payload = [{k: v for k, v in r.items() if k != "_name"} for r in rows]
    upsert_player_costs(args.guild_id, args.season_start, payload)

    logger.info(
        "Upserted %d player costs for guild=%d season_start=%s",
        len(payload), args.guild_id, args.season_start,
    )

    # Preview a few rows
    for r in sorted(rows, key=lambda r: -(r["cost"] or 0))[:5]:
        logger.info(
            "  top: %s (account_id=%d) — cost=%d, captain=%s, mmr=%s",
            r["_name"], r["account_id"], r["cost"], r["captain"], r["mmr"],
        )


if __name__ == "__main__":
    main()
