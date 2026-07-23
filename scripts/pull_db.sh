#!/usr/bin/env bash
# Pull a snapshot of the prod SQLite DB from fly.io to inspect locally.
# Usage: ./scripts/pull_db.sh [destination]
#
# Default destination: ~/dota-bot-prod.db
# Open the resulting file in DB Browser for SQLite (or any SQLite GUI).

set -euo pipefail

DEST="${1:-$HOME/dota-bot-prod.db}"
REMOTE_PATH="/data/dota_stats.db"

echo "→ Pulling $REMOTE_PATH from fly (app: dota-bot) to $DEST ..."
# fly ssh sftp get refuses to overwrite; delete the existing snapshot first.
rm -f "$DEST"
fly ssh sftp get "$REMOTE_PATH" "$DEST"

if [[ -f "$DEST" ]]; then
  SIZE=$(du -h "$DEST" | cut -f1)
  echo "✓ Snapshot saved to $DEST ($SIZE)"
  echo "  Open it in DB Browser for SQLite."
else
  echo "✗ Pull failed — no file at $DEST" >&2
  exit 1
fi
