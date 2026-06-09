"""
Crunch players.csv: keep key columns, add windrun link and MMR.

Usage:
    python3 crunch_players.py
    Reads players.csv, writes players_crunched.csv
"""

import csv
import json
import re
import time
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INPUT_FILE = DATA_DIR / "players.csv"
OUTPUT_FILE = DATA_DIR / "players_crunched.csv"

WINDRUN_API_BASE = "https://api.windrun.io/api/v2"
WINDRUN_PROFILE_BASE = "https://windrun.io/players"
RATE_LIMIT_SECONDS = 5.0

KEEP_COLUMNS = [
    "name",
    "draft_mmr",
    "statement",
    "dotabuff",
    "Role: Position 1",
    "Role: Position 2",
    "Role: Position 3",
    "Role: Position 4",
    "Role: Position 5",
]


def extract_steam_id(dotabuff_url: str):
    """Extract steam ID from a dotabuff URL like https://www.dotabuff.com/players/12345."""
    match = re.search(r"dotabuff\.com/players/(\d+)", dotabuff_url)
    return match.group(1) if match else None


def fetch_windrun_mmr(steam_id: str) -> str:
    """Fetch a player's windrun rating via the API. Returns rounded rating or empty on failure."""
    url = f"{WINDRUN_API_BASE}/players/{steam_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
        player_data = data.get("data", data)
        if isinstance(player_data, dict):
            rating = player_data.get("rating")
            if rating is not None:
                return str(round(rating))
        return ""
    except Exception as e:
        print(f"  Error fetching windrun for {steam_id}: {e}")
        return ""


def main():
    with open(INPUT_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"Read {len(rows)} players from {INPUT_FILE}")

    output_rows = []
    last_request_time = 0.0

    for i, row in enumerate(rows):
        name = row.get("name", "")
        dotabuff = row.get("dotabuff", "")
        steam_id = extract_steam_id(dotabuff)

        windrun_link = ""
        windrun_mmr = ""

        if steam_id:
            windrun_link = f"{WINDRUN_PROFILE_BASE}/{steam_id}"

            # Rate limit: 5 seconds between requests per windrun policy
            elapsed = time.monotonic() - last_request_time
            if elapsed < RATE_LIMIT_SECONDS:
                time.sleep(RATE_LIMIT_SECONDS - elapsed)

            print(f"[{i+1}/{len(rows)}] Fetching windrun rating for {name} ({steam_id})...")
            windrun_mmr = fetch_windrun_mmr(steam_id)
            last_request_time = time.monotonic()

            if windrun_mmr:
                print(f"  Rating: {windrun_mmr}")
            else:
                print(f"  No rating found")
        else:
            print(f"[{i+1}/{len(rows)}] No steam ID found for {name}")

        out_row = {col: row.get(col, "") for col in KEEP_COLUMNS}
        out_row["windrun_link"] = windrun_link
        out_row["windrun_mmr"] = windrun_mmr
        output_rows.append(out_row)

    out_columns = KEEP_COLUMNS + ["windrun_link", "windrun_mmr"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_columns)
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"\nWrote {len(output_rows)} players to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
