"""
One-shot importer: read the "Drafted Players" sheet export (CSV) and
populate the player_costs table.

CSV format expected — the exact columns the Google Sheets "Drafted Players"
tab uses (see scripts/draft_sheet_excel_helper.gs):

    Account ID, Name, Winner, Cost

Only rows with both Winner and Cost filled in are drafted picks; blank rows
(nobody picked yet, or the draft hasn't happened) are skipped, not errors.

    account_id ← "Account ID" (already a plain Steam32 id, no URL to parse)
    captain    ← "Winner"
    cost       ← "Cost"
    name       ← "Name" (logged only; identity lives in account_id)

Usage:
    python import_costs.py --csv drafted-players.csv \
        --guild-id 1481800158826991718 --season-start 2026-XX-XX

Uses the same DB_PATH as the bot (default /data/dota_stats.db on fly, else
dota_stats.db). Re-running with the same (guild_id, season_start) overwrites
existing rows for that season.

Note: player_costs also has an "mmr" column (a draft-time MMR snapshot used
by a couple of regression-based stats), but the Drafted Players sheet has
no equivalent column, so it's left unset (None) for every imported row —
downstream code already treats it as optional and skips rows missing it.
"""

import argparse
import csv
import logging
import sys

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

from db import upsert_player_costs, init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


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
    """Parse the CSV and return a list of {account_id, cost, captain, mmr, _name} dicts."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for raw in reader:
            account_id = parse_int(raw.get("Account ID"))
            cost = parse_int(raw.get("Cost"))
            captain = (raw.get("Winner") or "").strip() or None
            name = (raw.get("Name") or "").strip()

            if account_id is None:
                logger.warning("Skipping row (no parseable Account ID): %s", raw)
                continue
            if cost is None or captain is None:
                # Not drafted (yet) — the normal state for most rows until
                # the draft actually happens.
                continue

            rows.append({
                "account_id": account_id,
                "cost":       cost,
                "captain":    captain,
                "mmr":        None,
                "_name":      name,
            })
        return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="Path to the Drafted Players sheet export (CSV)")
    ap.add_argument("--guild-id", type=int, required=True, help="Discord guild ID this season belongs to")
    ap.add_argument("--season-start", required=True, help="YYYY-MM-DD; should match divisions.season_start")
    args = ap.parse_args()

    init_db()  # safe to call; ensures the table exists

    rows = load_rows(args.csv)
    if not rows:
        logger.error("No drafted rows found in %s (need both Winner and Cost filled in)", args.csv)
        sys.exit(1)

    logger.info("Parsed %d drafted player rows from %s", len(rows), args.csv)

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
            "  top: %s (account_id=%d) — cost=%d, captain=%s",
            r["_name"], r["account_id"], r["cost"], r["captain"],
        )


if __name__ == "__main__":
    main()
