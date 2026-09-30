#!/usr/bin/env bash
# Pull a consistent snapshot of the prod SQLite DB from fly.io.
# Usage: ./scripts/pull_db.sh [destination]
#
# Default destination: ~/dota-bot-prod.db
#
# The live database runs in WAL mode: recent writes sit in dota_stats.db-wal
# until SQLite checkpoints them, so copying dota_stats.db alone can miss
# them. SQLite's backup API on the server writes one self-contained file,
# which is what gets downloaded.

set -euo pipefail

DEST="${1:-$HOME/dota-bot-prod.db}"
SNAPSHOT="/tmp/dota_stats_snapshot.db"

echo "→ Snapshotting /data/dota_stats.db on fly (app: dota-bot) ..."
fly ssh console -a dota-bot -C "python -c \"import sqlite3; s = sqlite3.connect('/data/dota_stats.db'); d = sqlite3.connect('$SNAPSHOT'); s.backup(d); d.close(); print(s.execute('pragma user_version').fetchone()[0])\"" >/dev/null

echo "→ Downloading to $DEST ..."
# fly ssh sftp get refuses to overwrite; delete the existing snapshot first.
rm -f "$DEST"
fly ssh sftp get -a dota-bot "$SNAPSHOT" "$DEST"
fly ssh console -a dota-bot -C "rm -f $SNAPSHOT" >/dev/null

if [[ -f "$DEST" ]] && [[ "$(sqlite3 "$DEST" 'pragma integrity_check')" == "ok" ]]; then
  SIZE=$(du -h "$DEST" | cut -f1)
  echo "✓ Snapshot saved to $DEST ($SIZE), integrity ok"
else
  echo "✗ Pull failed or snapshot is damaged — check $DEST" >&2
  exit 1
fi
