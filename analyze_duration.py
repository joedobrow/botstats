"""
Duration vs Fantasy Points Analysis
-------------------------------------
Queries every player-match row from the DB, computes per-match fantasy points,
then fits a linear regression: fantasy_pts ~ game_duration_minutes

Run on Fly.io:
    flyctl ssh console --app dota-bot -C "sh -c 'DB_PATH=/data/dota_stats.db python /app/analyze_duration.py'"
"""

import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "dota_stats.db")

WEIGHTS = {
    "kills":                    0.3,
    "death_base":               3.0,
    "deaths":                  -0.2,
    "assists":                  0.25,
    "last_hits":                0.003,
    "denies":                   0.02,
    "gpm":                      0.002,
    "xpm":                      0.001,
    "tower_kills":              1.0,
    "roshans_killed":           0.5,
    "firstblood_claimed":       2.0,
    "teamfight_participation":  3.0,
    "stuns":                    0.08,
    "obs_placed":               0.1,
    "sen_placed":               0.0,
    "observer_kills":           0.5,
    "sentry_kills":             0.2,
    "camps_stacked":            0.1,
    "rune_pickups":             0.25,
    "hero_damage":              0.01,   # per 100
    "hero_healing":             0.015,  # per 100
}


def per_match_fantasy(row) -> float:
    """Compute fantasy pts for a single match row (all values are per-match, not averaged)."""
    pts  = row["kills"]                   * WEIGHTS["kills"]
    pts += WEIGHTS["death_base"]
    pts += row["deaths"]                  * WEIGHTS["deaths"]
    pts += row["assists"]                 * WEIGHTS["assists"]
    pts += (row["last_hits"] or 0)        * WEIGHTS["last_hits"]
    pts += (row["denies"] or 0)           * WEIGHTS["denies"]
    pts += (row["gpm"] or 0)              * WEIGHTS["gpm"]
    pts += (row["xpm"] or 0)              * WEIGHTS["xpm"]
    pts += (row["tower_kills"] or 0)      * WEIGHTS["tower_kills"]
    pts += (row["roshans_killed"] or 0)   * WEIGHTS["roshans_killed"]
    pts += (row["firstblood_claimed"] or 0) * WEIGHTS["firstblood_claimed"]
    pts += (row["teamfight_participation"] or 0) * WEIGHTS["teamfight_participation"]
    pts += (row["stuns"] or 0)            * WEIGHTS["stuns"]
    pts += (row["obs_placed"] or 0)       * WEIGHTS["obs_placed"]
    pts += (row["sen_placed"] or 0)       * WEIGHTS["sen_placed"]
    pts += (row["observer_kills"] or 0)   * WEIGHTS["observer_kills"]
    pts += (row["sentry_kills"] or 0)     * WEIGHTS["sentry_kills"]
    pts += (row["camps_stacked"] or 0)    * WEIGHTS["camps_stacked"]
    pts += (row["rune_pickups"] or 0)     * WEIGHTS["rune_pickups"]
    pts += ((row["hero_damage"] or 0) / 100)  * WEIGHTS["hero_damage"]
    pts += ((row["hero_healing"] or 0) / 100) * WEIGHTS["hero_healing"]
    return pts


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT
            p.match_id,
            p.duration,
            p.kills,
            p.deaths,
            p.assists,
            p.last_hits,
            p.denies,
            p.gpm,
            p.xpm,
            p.hero_damage,
            p.hero_healing,
            p.obs_placed,
            p.sen_placed,
            p.observer_kills,
            p.sentry_kills,
            p.tower_kills,
            p.roshans_killed,
            p.firstblood_claimed,
            p.teamfight_participation,
            p.stuns,
            p.camps_stacked,
            p.rune_pickups
        FROM players p
        WHERE p.duration > 0
          AND p.account_id != 0
    """).fetchall()

    print(f"Total player-match rows: {len(rows)}")

    durations = []   # in minutes
    pts_list  = []

    for row in rows:
        duration_min = row["duration"] / 60.0
        pts = per_match_fantasy(row)
        durations.append(duration_min)
        pts_list.append(pts)

    n = len(durations)
    if n < 2:
        print("Not enough data for regression.")
        return

    # --- Linear regression (manual, no numpy needed on Fly) ---
    mean_x = sum(durations) / n
    mean_y = sum(pts_list) / n
    var_x  = sum((x - mean_x) ** 2 for x in durations)
    cov_xy = sum((durations[i] - mean_x) * (pts_list[i] - mean_y) for i in range(n))

    slope     = cov_xy / var_x
    intercept = mean_y - slope * mean_x

    # R²
    ss_res = sum((pts_list[i] - (slope * durations[i] + intercept)) ** 2 for i in range(n))
    ss_tot = sum((y - mean_y) ** 2 for y in pts_list)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    print(f"\n=== Linear Regression: fantasy_pts ~ duration_minutes ===")
    print(f"  fantasy_pts = {slope:.4f} * duration_min + {intercept:.4f}")
    print(f"  R²          = {r2:.4f}")
    print(f"\n  Interpretation:")
    print(f"  Each extra minute of game time adds ~{slope:.3f} fantasy pts on average")
    print(f"  At 0 min baseline, expected pts = {intercept:.2f}")

    print(f"\n=== Duration Distribution ===")
    print(f"  Min:  {min(durations):.1f} min")
    print(f"  Max:  {max(durations):.1f} min")
    print(f"  Mean: {mean_x:.1f} min")

    print(f"\n=== Fantasy Pts Distribution ===")
    print(f"  Min:  {min(pts_list):.2f}")
    print(f"  Max:  {max(pts_list):.2f}")
    print(f"  Mean: {mean_y:.2f}")

    print(f"\n=== Normalization Preview ===")
    print(f"  Adjusted pts = raw_pts - {slope:.4f} * (duration_min - {mean_x:.2f})")
    print(f"  i.e. subtract the expected pts gain/loss vs average game length")

    # Show what adjustments look like at various durations
    print(f"\n  Duration → Adjustment")
    for d in [20, 25, 30, 35, 40, 45, 50, 55, 60]:
        adj = -slope * (d - mean_x)
        print(f"    {d} min → {adj:+.2f} pts")

    # Show bucket averages to validate linearity
    print(f"\n=== Actual Mean Pts by Duration Bucket ===")
    buckets = {}
    for i in range(n):
        bucket = int(durations[i] // 5) * 5  # 5-min buckets
        if bucket not in buckets:
            buckets[bucket] = []
        buckets[bucket].append(pts_list[i])
    for b in sorted(buckets):
        vals = buckets[b]
        print(f"    {b:2d}-{b+5} min: n={len(vals):3d}  mean={sum(vals)/len(vals):.2f}  predicted={slope*(b+2.5)+intercept:.2f}")


if __name__ == "__main__":
    main()
