"""
One-off script to inspect OpenDota match data for tormentor/watcher fields.
Run on Fly.io:
    flyctl ssh console --app dota-bot -C "sh -c 'DB_PATH=/data/dota_stats.db python /app/inspect_match.py'"
"""
import sqlite3, os, urllib.request, json

DB_PATH = os.environ.get("DB_PATH", "dota_stats.db")
conn = sqlite3.connect(DB_PATH)
row = conn.execute("SELECT match_id FROM matches ORDER BY start_time DESC LIMIT 1").fetchone()
mid = row[0]
print(f"Fetching match {mid}")

url = f"https://api.opendota.com/api/matches/{mid}"
with urllib.request.urlopen(url, timeout=20) as r:
    data = json.loads(r.read())

p = data["players"][0]
print(f"\n=== All player keys ({len(p)}) ===")
for k in sorted(p.keys()):
    print(f"  {k}: {repr(p[k])[:80]}")

print("\n=== Tormentor / watcher / puck / bounty / lotus related ===")
found = False
for k, v in sorted(p.items()):
    kl = k.lower()
    if any(x in kl for x in ["torment", "puck", "watcher", "watch", "shrine", "lotus", "bounty"]):
        print(f"  {k}: {v}")
        found = True
if not found:
    print("  (none found)")

# Check kills_log for tormentor unit names
kills_log = p.get("kills_log", [])
print(f"\n=== kills_log sample (first 10) ===")
for entry in kills_log[:10]:
    print(f"  {entry}")
print(f"\n=== kills_log entries containing 'puck' or 'torment' ===")
for entry in kills_log:
    key = str(entry.get("key", "")).lower()
    if "puck" in key or "torment" in key or "watch" in key:
        print(f"  {entry}")

# Check objectives
objs = data.get("objectives", [])
print(f"\n=== objectives ({len(objs)} total) ===")
for o in objs:
    key = str(o.get("key", "") or o.get("type", "")).lower()
    if any(x in key for x in ["torment", "puck", "watch"]):
        print(f"  MATCH: {o}")
print("First 10 objectives:")
for o in objs[:10]:
    print(f"  {o}")
