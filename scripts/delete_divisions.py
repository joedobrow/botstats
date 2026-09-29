"""Permanently delete divisions and everything recorded for them.

    python scripts/delete_divisions.py --mode cm            # dry run: show what would go
    python scripts/delete_divisions.py --mode cm --yes      # delete
    python scripts/delete_divisions.py --guild 123 --yes    # one division by guild id

Removes the division's config (so the weekly fetch stops pulling its
matches), its seasons, weeks, teams, costs, rosters, website settings and
signups, and its matches with their player rows, chat and draft picks.
Player rating-cache rows and /set_player overrides are global, so they stay.

Run from the repo root. On fly: back up first (scripts/pull_db.sh), then
`fly ssh console -C "python scripts/delete_divisions.py --mode cm --yes"`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import DB_PATH, _conn  # noqa: E402

# Tables keyed by guild_id that belong to a division. Missing tables (e.g.
# the site's, before the website first runs) are skipped.
GUILD_TABLES = (
    "seasons", "season_weeks", "season_teams", "player_costs", "guild_roster",
    "guild_exclusions", "site_divisions", "site_season_settings", "site_signups",
    "divisions",
)
MATCH_TABLES = ("players", "chat_messages", "draft_picks")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["cm", "ad"], help="delete every division with this game mode")
    ap.add_argument("--guild", type=int, action="append", default=[], help="delete this guild id")
    ap.add_argument("--yes", action="store_true", help="actually delete (default is a dry run)")
    args = ap.parse_args()

    with _conn() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        guilds = set(args.guild)
        if args.mode:
            guilds |= {r[0] for r in conn.execute(
                "SELECT guild_id FROM divisions WHERE game_mode = ?", (args.mode,))}
        if not guilds:
            sys.exit("No matching divisions.")

        print(f"Database: {DB_PATH}")
        for g in sorted(guilds):
            n = conn.execute("SELECT COUNT(*) FROM matches WHERE guild_id = ?", (g,)).fetchone()[0]
            div = conn.execute("SELECT league_id, game_mode FROM divisions WHERE guild_id = ?", (g,)).fetchone()
            print(f"  guild {g}: {n} matches"
                  + (f", league {div[0]}, mode {div[1]}" if div else ", no division row"))

        if not args.yes:
            print("Dry run. Re-run with --yes to delete.")
            return

        marks = ",".join("?" * len(guilds))
        ids = list(guilds)
        match_filter = f"match_id IN (SELECT match_id FROM matches WHERE guild_id IN ({marks}))"
        for t in MATCH_TABLES:
            if t in tables:
                cur = conn.execute(f"DELETE FROM {t} WHERE {match_filter}", ids)
                print(f"  {t}: {cur.rowcount} rows")
        cur = conn.execute(f"DELETE FROM matches WHERE guild_id IN ({marks})", ids)
        print(f"  matches: {cur.rowcount} rows")
        for t in GUILD_TABLES:
            if t in tables:
                cur = conn.execute(f"DELETE FROM {t} WHERE guild_id IN ({marks})", ids)
                print(f"  {t}: {cur.rowcount} rows")
    print("Deleted.")


if __name__ == "__main__":
    main()
