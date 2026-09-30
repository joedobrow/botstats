"""Merge one division into another: everything filed under the first moves
to the second, and the first is deleted.

    python scripts/merge_divisions.py --from us-west-ability-draft --into ability-draft \
        --name NA --slug na            # dry run: show what would move
    ... --yes                          # merge

Divisions are named by slug or id. Moves games, season memberships, draft
costs, weeks, teams, website status and signups, and roster/exclusion
lists. Where both divisions have a row for the same thing (the same player
on both rosters, say) the target's row is kept. The target keeps its own
settings (region, mode, Discord server, current season); --name/--slug
rename it. One transaction: it all happens or none of it does.

Run from the repo root. On fly: back up first (scripts/pull_db.sh), then
`fly ssh console -C "python scripts/merge_divisions.py ... --yes"`.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import DB_PATH  # noqa: E402

# Tables with a division_id column. Missing tables (e.g. the site's, before
# the website first runs) are skipped.
TABLES = ("matches", "season_divisions", "player_costs", "season_weeks", "season_teams",
          "division_roster", "division_exclusions", "site_season_settings", "site_signups")


def find(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM divisions WHERE slug = ? OR CAST(id AS TEXT) = ?", (ref, ref)).fetchone()
    if not row:
        sys.exit(f"No division {ref!r}.")
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="source", required=True, help="division to merge away (slug or id)")
    ap.add_argument("--into", dest="target", required=True, help="division that remains (slug or id)")
    ap.add_argument("--name", help="new name for the merged division")
    ap.add_argument("--slug", help="new URL name for the merged division")
    ap.add_argument("--yes", action="store_true", help="actually merge (default is a dry run)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    src, dst = find(conn, args.source), find(conn, args.target)
    if src["id"] == dst["id"]:
        sys.exit("That's the same division.")
    tables = [t for t in TABLES
              if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]

    print(f"Database: {DB_PATH}")
    print(f"Merge division {src['id']} {src['name']!r} into {dst['id']} {dst['name']!r}"
          + (f", renamed {args.name!r}" if args.name else "") + (f" ({args.slug})" if args.slug else ""))
    for t in tables:
        n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE division_id = ?", (src["id"],)).fetchone()[0]
        print(f"  {t}: {n} rows to move")
    if not args.yes:
        print("Dry run. Re-run with --yes to merge.")
        return

    try:
        conn.execute("BEGIN IMMEDIATE")
        for t in tables:
            # Rows the target already has (same key) stay as they are.
            moved = conn.execute(f"UPDATE OR IGNORE {t} SET division_id = ? WHERE division_id = ?",
                                 (dst["id"], src["id"])).rowcount
            dropped = conn.execute(f"DELETE FROM {t} WHERE division_id = ?", (src["id"],)).rowcount
            print(f"  {t}: moved {moved}" + (f", {dropped} already in the target (kept the target's)" if dropped else ""))
        conn.execute("UPDATE guild_settings SET default_division_id = NULL WHERE default_division_id = ?",
                     (src["id"],))
        conn.execute("DELETE FROM divisions WHERE id = ?", (src["id"],))
        if args.name or args.slug:
            conn.execute("UPDATE divisions SET name = COALESCE(?, name), slug = COALESCE(?, slug) WHERE id = ?",
                         (args.name, args.slug, dst["id"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    print("Merged.")


if __name__ == "__main__":
    main()
