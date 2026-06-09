"""
Load mmr_calibration data and try several models for the
    windrun_rating = f(ranked_mmr_estimate)
relationship. Compare R² and report.

Models:
  - Linear:        wr = a + b·mmr
  - Quadratic:     wr = a + b·mmr + c·mmr²
  - Log-linear:    wr = a + b·ln(mmr)
  - Piecewise lin: two-segment fit, breakpoint scanned over the data range

Usage:
    python fit_mmr_model.py [--min-games-each 300]
"""

import argparse
import logging
import math

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

from db import _conn, _solve_linear

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def fetch_calibration_rows(min_ad: int, min_ranked: int) -> list[tuple[float, int]]:
    """Returns list of (windrun_rating, mmr_estimate) pairs."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT windrun_rating, mmr_estimate
            FROM mmr_calibration
            WHERE mmr_estimate IS NOT NULL
              AND ad_games     >= ?
              AND ranked_games >= ?
        """, (min_ad, min_ranked)).fetchall()
    return [(r["windrun_rating"], r["mmr_estimate"]) for r in rows]


def r_squared(ys: list[float], yhats: list[float]) -> float:
    mean_y = sum(ys) / len(ys)
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - yh) ** 2 for y, yh in zip(ys, yhats))
    return 1 - ss_res / ss_tot if ss_tot else 0.0


def fit_linear(xs: list[float], ys: list[float]) -> tuple[tuple[float, float], list[float]]:
    n = len(xs)
    sx = sum(xs); sy = sum(ys)
    sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n
    yhats = [a + b * x for x in xs]
    return (a, b), yhats


def fit_quadratic(xs: list[float], ys: list[float]) -> tuple[tuple[float, float, float], list[float]]:
    """Solve 3x3 normal equations for wr = a + b·mmr + c·mmr²."""
    n = len(xs)
    sx  = sum(xs);             sy  = sum(ys)
    sxx = sum(x * x for x in xs); syx = sum(x * y for x, y in zip(xs, ys))
    sxxx = sum(x ** 3 for x in xs); sxxxx = sum(x ** 4 for x in xs)
    syxx = sum(y * x * x for x, y in zip(xs, ys))
    M = [
        [n,   sx,   sxx ],
        [sx,  sxx,  sxxx],
        [sxx, sxxx, sxxxx],
    ]
    rhs = [sy, syx, syxx]
    sol = _solve_linear(M, rhs)
    a, b, c = sol
    yhats = [a + b * x + c * x * x for x in xs]
    return (a, b, c), yhats


def fit_log_linear(xs: list[float], ys: list[float]) -> tuple[tuple[float, float], list[float]]:
    log_xs = [math.log(x) for x in xs]
    coefs, _ = fit_linear(log_xs, ys)
    a, b = coefs
    yhats = [a + b * math.log(x) for x in xs]
    return (a, b), yhats


def fit_piecewise(xs: list[float], ys: list[float]) -> tuple[dict, list[float]]:
    """Two-segment piecewise linear with continuity at the breakpoint.
    Scans candidate breakpoints over the data range; picks the one with the
    lowest SSE."""
    sorted_pairs = sorted(zip(xs, ys))
    sxs = [p[0] for p in sorted_pairs]
    sys_ = [p[1] for p in sorted_pairs]
    # Candidate breakpoints: every 10th percentile of the x distribution
    n = len(sxs)
    candidates = sorted({sxs[int(n * p)] for p in [.2, .3, .4, .5, .6, .7, .8]})
    best = None
    for bp in candidates:
        # Build design matrix for: wr = a + b·x + c·max(0, x - bp)
        # This enforces continuity at bp and lets the slope change beyond it.
        # Solve OLS via the same normal-equations + 3x3 solve.
        x1 = xs
        x2 = [max(0, x - bp) for x in xs]
        n_ = len(xs)
        sx1  = sum(x1);                 sx2  = sum(x2);                 sy = sum(ys)
        sx11 = sum(a * b for a, b in zip(x1, x1))
        sx12 = sum(a * b for a, b in zip(x1, x2))
        sx22 = sum(a * b for a, b in zip(x2, x2))
        sx1y = sum(a * b for a, b in zip(x1, ys))
        sx2y = sum(a * b for a, b in zip(x2, ys))
        M = [
            [n_,  sx1,  sx2 ],
            [sx1, sx11, sx12],
            [sx2, sx12, sx22],
        ]
        rhs = [sy, sx1y, sx2y]
        sol = _solve_linear(M, rhs)
        if sol is None:
            continue
        a, b, c = sol
        yhats = [a + b * x + c * max(0, x - bp) for x in xs]
        sse = sum((y - yh) ** 2 for y, yh in zip(ys, yhats))
        if best is None or sse < best[0]:
            best = (sse, bp, (a, b, c), yhats)
    if best is None:
        return None, None
    sse, bp, (a, b, c), yhats = best
    return {"intercept": a, "slope_lo": b, "slope_delta": c, "breakpoint": bp}, yhats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-ad", type=int, default=300)
    ap.add_argument("--min-ranked", type=int, default=300)
    args = ap.parse_args()

    pairs = fetch_calibration_rows(args.min_ad, args.min_ranked)
    if len(pairs) < 5:
        print(f"Not enough calibration rows: {len(pairs)}. Run build_mmr_calibration.py first.")
        return

    xs = [float(p[1]) for p in pairs]  # mmr_estimate (input)
    ys = [float(p[0]) for p in pairs]  # windrun_rating (output)
    n = len(pairs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    print(f"Sample size: {n}")
    print(f"  mean ranked-mmr estimate: {mean_x:.0f}")
    print(f"  mean windrun rating:      {mean_y:.0f}")
    print(f"  ranges: mmr [{min(xs):.0f}, {max(xs):.0f}]  windrun [{min(ys):.0f}, {max(ys):.0f}]")
    print()

    print(f"{'model':18s}  {'R²':>6s}  formula")
    print("-" * 80)

    # Linear
    (a, b), yhats = fit_linear(xs, ys)
    print(f"{'linear':18s}  {r_squared(ys, yhats):>6.3f}  wr = {a:.2f} + {b:.5f}·mmr")

    # Quadratic
    (a, b, c), yhats = fit_quadratic(xs, ys)
    print(f"{'quadratic':18s}  {r_squared(ys, yhats):>6.3f}  wr = {a:.2f} + {b:.5f}·mmr + {c:.8f}·mmr²")

    # Log-linear
    (a, b), yhats = fit_log_linear(xs, ys)
    print(f"{'log-linear':18s}  {r_squared(ys, yhats):>6.3f}  wr = {a:.2f} + {b:.3f}·ln(mmr)")

    # Piecewise linear (two-segment, continuous)
    coefs, yhats = fit_piecewise(xs, ys)
    if coefs:
        a = coefs["intercept"]
        b = coefs["slope_lo"]
        c = coefs["slope_delta"]
        bp = coefs["breakpoint"]
        b2 = b + c
        print(f"{'piecewise (cts)':18s}  {r_squared(ys, yhats):>6.3f}  "
              f"wr ≤{bp:.0f}: {a:.2f} + {b:.5f}·mmr ; >{bp:.0f}: slope→{b2:.5f}")

    # Show a few sample predictions for the linear baseline vs quadratic
    print()
    print("Sample predictions:")
    print(f"  {'mmr':>6s}  {'actual':>8s}  ...")
    # Buckets every 500 MMR
    sample_mmrs = [3500, 4500, 5500, 6500, 7500, 8500]
    for x in sample_mmrs:
        # average windrun rating among observations within ±200 of this mmr
        nearby = [y for xv, y in zip(xs, ys) if abs(xv - x) <= 200]
        actual = sum(nearby) / len(nearby) if nearby else None
        actual_str = f"{actual:.0f} (n={len(nearby)})" if actual else "n/a"
        print(f"  {x:>6.0f}  {actual_str}")


if __name__ == "__main__":
    main()
