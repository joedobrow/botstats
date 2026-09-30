"""Permanently delete divisions and everything recorded for them.

    python scripts/delete_divisions.py --mode cm            # dry run: show what would go
    python scripts/delete_divisions.py --mode cm --yes      # delete
    python scripts/delete_divisions.py --division na --yes  # one division by slug (or id)

Removes the division, its games (with their player rows, chat and draft
picks), everything filed under it per season (weeks, teams, costs, website
status and signups), its roster and exclusion lists, and any season that no
remaining division plays in. Player rating-cache rows and /set_player
overrides are global, so they stay.

Run from the repo root. On fly: back up first (scripts/pull_db.sh), then
`fly ssh console -C "python scripts/delete_divisions.py --mode cm --yes"`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import DB_PATH, _conn  # noqa: E402

# Tables with a division_id column, and tables keyed by match_id. Missing
# tables (e.g. the site's, before the website first runs) are skipped.
DIVISION_TABLES = ("season_weeks", "season_teams", "player_costs", "site_season_settings",
                   "site_signups", "division_roster", "division_exclusions", "season_divisions")
MATCH_TABLES = ("players", "chat_messages", "draft_picks")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["cm", "ad"], help="delete every division with this game mode")
    ap.add_argument("--division", action="append", default=[], help="delete this division (slug or id)")
    ap.add_argument("--yes", action="store_true", help="actually delete (default is a dry run)")
    args = ap.parse_args()

    with _conn() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        chosen = {r["id"]: r for r in conn.execute("SELECT * FROM divisions")
                  if (args.mode and r["game_mode"] == args.mode)
                  or str(r["id"]) in args.division or r["slug"] in args.division}
        if not chosen:
            sys.exit("No matching divisions.")

        print(f"Database: {DB_PATH}")
        for d in chosen.values():
            n = conn.execute("SELECT COUNT(*) FROM matches WHERE division_id = ?", (d["id"],)).fetchone()[0]
            print(f"  division {d['id']} {d['name']!r} ({d['game_mode']}, Discord server {d['guild_id']}): {n} games")

        if not args.yes:
            print("Dry run. Re-run with --yes to delete.")
            return

        ids = list(chosen)
        marks = ",".join("?" * len(ids))
        match_filter = f"match_id IN (SELECT match_id FROM matches WHERE division_id IN ({marks}))"
        for t in MATCH_TABLES:
            if t in tables:
                cur = conn.execute(f"DELETE FROM {t} WHERE {match_filter}", ids)
                print(f"  {t}: {cur.rowcount} rows")
        cur = conn.execute(f"DELETE FROM matches WHERE division_id IN ({marks})", ids)
        print(f"  matches: {cur.rowcount} rows")
        for t in DIVISION_TABLES:
            if t in tables:
                cur = conn.execute(f"DELETE FROM {t} WHERE division_id IN ({marks})", ids)
                print(f"  {t}: {cur.rowcount} rows")
        cur = conn.execute(f"UPDATE guild_settings SET default_division_id = NULL WHERE default_division_id IN ({marks})", ids)
        cur = conn.execute(f"DELETE FROM divisions WHERE id IN ({marks})", ids)
        print(f"  divisions: {cur.rowcount} rows")
        cur = conn.execute("DELETE FROM seasons WHERE id NOT IN (SELECT season_id FROM season_divisions)")
        print(f"  seasons no division plays in any more: {cur.rowcount} rows")
    print("Deleted.")


if __name__ == "__main__":
    main()
