"""
Fit the win-probability curve for the windrun rating system.

For a rating system to be "good," bigger rating gaps should predict bigger
winrate gaps. This script quantifies that by:

  1. Pulling AD matches for a stratified sample of players from the local
     rating cache (varied rating buckets → varied opponents).
  2. For each match, computing (radiant_team_avg_rating - dire_team_avg_rating)
     and the observed radiant_win outcome.
  3. Binning by rating diff, reporting observed winrates per bin.
  4. Fitting the classical Elo logistic P = 1/(1 + 10^(-diff/S)) via 1D
     grid search on S, minimising log loss.
  5. Reporting AUC, Brier score, log loss, and translating S into the
     "rating gap that corresponds to a 75% winrate" for easy comparison
     to chess's 200-Elo-for-75%.

Usage:
    ./scripts/pull_db.sh                       # get a fresh snapshot
    python3 scripts/analyze_rating_curve.py    # runs the analysis

Rate limits: windrun caps at one request per 5s. With ~30 players ×
200 matches each, expect ~2.5 minutes.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sqlite3
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError


WINDRUN_BASE = "https://api.windrun.io/api/v2"
RATE_LIMIT_S = 5.0
_last_call = 0.0

DEFAULT_DB   = str(Path.home() / "dota-bot-prod.db")
DEFAULT_OUT  = str(Path(__file__).parent.parent / "rating_curve_analysis.csv")
PLAYERS_PER_BUCKET = 6           # sampled per rating bucket
MATCH_LIMIT_PER_PLAYER = 200     # windrun API limit
BUCKETS = [
    ("<2500",     None, 2500),
    ("2500-2750", 2500, 2750),
    ("2750-3000", 2750, 3000),
    ("3000-3250", 3000, 3250),
    ("3250+",     3250, None),
]


def _rate_limit() -> None:
    global _last_call
    dt = time.monotonic() - _last_call
    if dt < RATE_LIMIT_S:
        time.sleep(RATE_LIMIT_S - dt)
    _last_call = time.monotonic()


def fetch_windrun_matches(account_id: int) -> list[dict] | None:
    _rate_limit()
    url = f"{WINDRUN_BASE}/players/{account_id}/matches?limit={MATCH_LIMIT_PER_PLAYER}"
    req = Request(url, headers={"User-Agent": "rating-curve-analysis"})
    try:
        with urlopen(req, timeout=90) as r:
            if r.status != 200:
                print(f"  ! {account_id}: HTTP {r.status}", file=sys.stderr)
                return None
            return json.loads(r.read())
    except URLError as e:
        print(f"  ! {account_id}: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ! {account_id}: {type(e).__name__} {e}", file=sys.stderr)
        return None


def pick_players(db_path: str, per_bucket: int) -> list[tuple[int, str, int]]:
    """Return (account_id, name, rating) triples stratified across rating buckets."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    picked: list[tuple[int, str, int]] = []
    seen: set[int] = set()
    for label, lo, hi in BUCKETS:
        where = "internal_rating IS NOT NULL"
        params: list = []
        if lo is not None:
            where += " AND internal_rating >= ?"
            params.append(lo)
        if hi is not None:
            where += " AND internal_rating < ?"
            params.append(hi)
        rows = conn.execute(
            f"SELECT account_id, name, internal_rating FROM player_ratings_cache "
            f"WHERE {where} ORDER BY RANDOM() LIMIT ?",
            (*params, per_bucket),
        ).fetchall()
        for r in rows:
            if r["account_id"] in seen:
                continue
            seen.add(r["account_id"])
            picked.append((r["account_id"], r["name"] or "?", r["internal_rating"]))
        print(f"  bucket {label}: {len(rows)} players")
    conn.close()
    return picked


def match_features(m: dict) -> tuple[float, int] | None:
    """From a windrun match, return (rating_diff, radiant_win) or None if unusable.

    rating_diff = radiant_avg_rating - dire_avg_rating.
    """
    radiant = m.get("radiant") or []
    dire    = m.get("dire") or []
    if len(radiant) != 5 or len(dire) != 5:
        return None
    r_ratings = [p.get("rating") for p in radiant if isinstance(p.get("rating"), (int, float))]
    d_ratings = [p.get("rating") for p in dire    if isinstance(p.get("rating"), (int, float))]
    if len(r_ratings) < 4 or len(d_ratings) < 4:  # tolerate at most one anonymous per side
        return None
    r_avg = sum(r_ratings) / len(r_ratings)
    d_avg = sum(d_ratings) / len(d_ratings)
    if "radiantWin" not in m:
        return None
    return (r_avg - d_avg, 1 if m["radiantWin"] else 0)


def logistic(diff: float, S: float) -> float:
    """Chess-style Elo curve. S is the scale (chess uses S=400)."""
    try:
        return 1.0 / (1.0 + 10 ** (-diff / S))
    except OverflowError:
        return 0.0 if diff < 0 else 1.0


def log_loss(features: list[tuple[float, int]], S: float) -> float:
    total = 0.0
    eps = 1e-9
    for diff, win in features:
        p = max(eps, min(1 - eps, logistic(diff, S)))
        total += -(win * math.log(p) + (1 - win) * math.log(1 - p))
    return total / len(features)


def brier(features: list[tuple[float, int]], S: float) -> float:
    total = 0.0
    for diff, win in features:
        p = logistic(diff, S)
        total += (p - win) ** 2
    return total / len(features)


def auc(features: list[tuple[float, int]]) -> float:
    """Mann-Whitney AUC: fraction of (win, loss) pairs where the diff is
    higher on the winning-radiant side. Chance = 0.5, perfect = 1.0."""
    wins  = [d for d, w in features if w == 1]
    losses = [d for d, w in features if w == 0]
    if not wins or not losses:
        return float("nan")
    # Efficient computation via ranking would be nicer, but n²/2 is fine at
    # the scales we work with (a few thousand matches).
    tot = 0
    concordant = 0.0
    for wd in wins:
        for ld in losses:
            tot += 1
            if wd > ld:
                concordant += 1.0
            elif wd == ld:
                concordant += 0.5
    return concordant / tot


def fit_S(features: list[tuple[float, int]]) -> float:
    """1-D grid search for S that minimises log loss."""
    best_S, best_loss = 400.0, float("inf")
    for S in [50, 75, 100, 125, 150, 175, 200, 225, 250, 275, 300,
              325, 350, 375, 400, 450, 500, 550, 600, 700, 800, 1000]:
        L = log_loss(features, S)
        if L < best_loss:
            best_S, best_loss = float(S), L
    # Local refinement around best.
    lo, hi = best_S * 0.8, best_S * 1.2
    for _ in range(20):
        mid1 = lo + (hi - lo) / 3
        mid2 = hi - (hi - lo) / 3
        if log_loss(features, mid1) < log_loss(features, mid2):
            hi = mid2
        else:
            lo = mid1
    return (lo + hi) / 2


def bin_results(features: list[tuple[float, int]]) -> list[dict]:
    """Bucket by rating diff, return observed winrate per bucket.

    Each row corresponds to `lo <= diff < hi` (except the first bin has
    lo=None and the last has hi=None).
    """
    edges = [
        (None, -600),
        (-600, -400), (-400, -300), (-300, -200), (-200, -100), (-100, -50),
        (-50, 50), (50, 100),
        (100, 200), (200, 300), (300, 400), (400, 600), (600, None),
    ]
    def label(lo, hi):
        if lo is None: return f"<{hi:+d}"
        if hi is None: return f">={lo:+d}"
        return f"{lo:+d} to {hi:+d}"
    counts = [[0, 0] for _ in edges]
    for diff, win in features:
        for i, (lo, hi) in enumerate(edges):
            if (lo is None or diff >= lo) and (hi is None or diff < hi):
                counts[i][0] += 1
                counts[i][1] += win
                break
    out = []
    for (lo, hi), (n, w) in zip(edges, counts):
        rate = (w / n) if n else None
        mid = ((lo or hi) + (hi or lo)) / 2 if (lo is not None and hi is not None) else (lo if lo is not None else hi)
        out.append({"bin": label(lo, hi), "midpoint": mid, "n": n, "wins": w, "winrate": rate})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"local DB snapshot (default {DEFAULT_DB})")
    ap.add_argument("--per-bucket", type=int, default=PLAYERS_PER_BUCKET,
                    help=f"players sampled per rating bucket (default {PLAYERS_PER_BUCKET})")
    ap.add_argument("--spider", type=int, default=0,
                    help="after the initial pass, pull this many more players from the "
                         "matches we found (stratified by observed rating).")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"CSV output (default {DEFAULT_OUT})")
    ap.add_argument("--seed", type=int, default=42, help="random seed for stratified sample")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"! DB not found at {args.db}", file=sys.stderr)
        print("  Run ./scripts/pull_db.sh first.", file=sys.stderr)
        sys.exit(1)
    random.seed(args.seed)

    cache_path = Path(args.out).with_suffix(".features.json")
    features: list[tuple[float, int]] = []
    if cache_path.exists():
        print(f"Loading cached features from {cache_path} …")
        with open(cache_path) as f:
            features = [tuple(row) for row in json.load(f)]
        print(f"→ {len(features)} matches loaded (delete {cache_path.name} to re-fetch).\n")
    else:
        print(f"Picking players from {args.db} …")
        players = pick_players(args.db, args.per_bucket)
        print(f"→ {len(players)} players selected from cache.\n")

        # Fetch initial round + collect steamIds from every match we see, so we
        # can spider outward to more players (diverse ratings, wider match pool).
        seen_matches: set[int] = set()
        seen_players: set[int] = {aid for aid, _, _ in players}
        # (steamId, avg_rating_seen) so we can later stratify the spider set.
        discovered: dict[int, list[float]] = {}

        def _harvest_match(m):
            for side in ("radiant", "dire"):
                for p in (m.get(side) or []):
                    sid = p.get("steamId")
                    r   = p.get("rating")
                    if isinstance(sid, int) and sid > 0 and isinstance(r, (int, float)):
                        discovered.setdefault(sid, []).append(float(r))

        eta_min = (len(players) + args.spider) * 5 / 60
        print(f"Fetching windrun matches (rate-limited 5s/req, ETA ~{eta_min:.1f} min) …")

        def _consume(matches, tag):
            added = 0
            for m in matches:
                mid = m.get("matchId")
                if mid in seen_matches:
                    continue
                seen_matches.add(mid)
                _harvest_match(m)
                f = match_features(m)
                if f is None:
                    continue
                features.append(f)
                added += 1
            print(f"  {tag} → +{added} unique  (running total: {len(features)})")

        for aid, name, rating in players:
            matches = fetch_windrun_matches(aid)
            if not matches:
                continue
            _consume(matches, f"{aid} ({name} @ {rating})")

        # Spider: pick players discovered in fetched matches, stratified by
        # their observed rating so the tails don't get under-sampled.
        spider_pool = [(sid, sum(rs) / len(rs)) for sid, rs in discovered.items()
                       if sid not in seen_players]
        if args.spider > 0 and spider_pool:
            print(f"\nSpidering {args.spider} additional players from collected matches …")
            random.shuffle(spider_pool)
            # Stratify: split into 5 rating quintiles, pick equal from each.
            spider_pool.sort(key=lambda x: x[1])
            n = len(spider_pool)
            per_q = max(1, args.spider // 5)
            spider_pick: list[tuple[int, float]] = []
            for q in range(5):
                start = (n * q) // 5
                end   = (n * (q + 1)) // 5
                chunk = spider_pool[start:end]
                random.shuffle(chunk)
                spider_pick.extend(chunk[:per_q])
            for sid, rating in spider_pick[:args.spider]:
                seen_players.add(sid)
                matches = fetch_windrun_matches(sid)
                if not matches:
                    continue
                _consume(matches, f"{sid} (spidered @ ~{rating:.0f})")

        with open(cache_path, "w") as f:
            json.dump(features, f)
        print(f"\nCollected {len(features)} unique matches. Cached → {cache_path}")

    if len(features) < 100:
        print("! Not enough data to fit reliably. Try again with --per-bucket bigger.")
        sys.exit(1)

    # Bin
    print("\nObserved winrate by rating diff bucket (radiant − dire):")
    print(f"  {'bin':<18} {'n':>6} {'wins':>6} {'obs':>7} {'expected@S=fit':>15}")
    bins = bin_results(features)
    S = fit_S(features)
    for row in bins:
        expected = logistic(row["midpoint"], S)
        obs = "—" if row["winrate"] is None else f"{row['winrate']*100:5.1f}%"
        exp = f"{expected*100:5.1f}%"
        print(f"  {row['bin']:<18} {row['n']:>6d} {row['wins']:>6d} {obs:>7} {exp:>15}")

    # Fit summary
    ll = log_loss(features, S)
    br = brier(features, S)
    auc_val = auc(features[:5000])  # AUC is n² so cap for speed
    diff_for_75 = S * math.log10(3.0)
    print(f"\n─── Fit summary ────────────────────────────────────────────")
    print(f"  Scale factor S:              {S:.1f}  (chess uses 400)")
    print(f"  Rating gap for 75% winrate:  {diff_for_75:.0f}")
    print(f"  Rating gap for 90% winrate:  {S * math.log10(9.0):.0f}")
    print(f"  Log loss:                    {ll:.4f}   (uninformed baseline = 0.6931)")
    print(f"  Brier score:                 {br:.4f}   (uninformed baseline = 0.25)")
    print(f"  AUC:                         {auc_val:.3f}   (uninformed = 0.5, perfect = 1.0)")
    print(f"  N matches:                   {len(features)}")

    # CSV
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["bin", "midpoint", "n", "wins",
                                          "observed_winrate", "expected_winrate_at_midpoint"])
        w.writeheader()
        for row in bins:
            w.writerow({
                "bin": row["bin"],
                "midpoint": row["midpoint"],
                "n": row["n"],
                "wins": row["wins"],
                "observed_winrate": f"{row['winrate']:.4f}" if row["winrate"] is not None else "",
                "expected_winrate_at_midpoint": f"{logistic(row['midpoint'], S):.4f}",
            })
    print(f"\nCSV → {args.out}")


if __name__ == "__main__":
    main()
