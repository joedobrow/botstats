"""
Fantasy Points Formula
-----------------------

Designed to reward well-rounded play across all roles.  The weights are
tunable — adjust the values in WEIGHTS below to match your group's taste.

The formula works on per-game averages so that players who played fewer
games aren't unfairly penalised (or rewarded) just by game count.

Scoring breakdown (per game played):
    Kills           +3.0  pts each
    Deaths          -2.5  pts each
    Assists         +2.0  pts each
    Last Hits       +0.02 pts each  (rewards farming)
    Denies          +0.5  pts each  (high-impact deny)
    GPM             +2.0  pts per 100 GPM above 300 (baseline farming)
    XPM             +1.8  pts per 100 XPM above 400
    Hero Damage     +0.05 pts per 100 damage
    Hero Healing    +0.08 pts per 100 healing (supports get love)
    Obs Placed      +0.5  pts each  (vision wins games)
    Sen Placed      +0.5  pts each  (dewarding)
    Obs Kills       +1.0  pts each  (deward bonus)
    Sen Kills       +1.0  pts each  (deward bonus)

Total is then rounded to 1 decimal place.
"""

WEIGHTS = {
    "kills_per_game":       3.0,
    "deaths_per_game":     -2.5,
    "assists_per_game":     2.0,
    "last_hits_per_game":   0.02,
    "denies_per_game":      0.5,
    "gpm_bonus_per_100":    2.0,   # per 100 GPM above GPM_BASELINE
    "xpm_bonus_per_100":    1.8,   # per 100 XPM above XPM_BASELINE
    "damage_per_100":       0.05,
    "healing_per_100":      0.08,
    "obs_placed_per_game":  0.5,
    "sen_placed_per_game":  0.5,
    "obs_kills_per_game":   1.0,
    "sen_kills_per_game":   1.0,
}

GPM_BASELINE = 300   # GPM below this doesn't score bonus points
XPM_BASELINE = 400   # XPM below this doesn't score bonus points


def calculate_fantasy_points(player: dict) -> float:
    """
    Given an aggregated player dict (as returned by db.get_latest_week_stats),
    compute and return the fantasy point total as a PER-GAME AVERAGE.

    Players with more games are NOT rewarded — this is purely skill-based.
    """
    games = max(player.get("games_played", 1), 1)  # avoid div-by-zero

    # Convert totals to per-game averages
    kills    = player.get("total_kills", 0) / games
    deaths   = player.get("total_deaths", 0) / games
    assists  = player.get("total_assists", 0) / games

    # These are already per-game averages from SQL AVG()
    lh         = player.get("last_hits", 0)
    denies     = player.get("denies", 0)
    gpm        = player.get("gpm", 0)
    xpm        = player.get("xpm", 0)
    damage     = player.get("hero_damage", 0)
    healing    = player.get("hero_healing", 0)
    obs_placed = player.get("obs_placed", 0) or 0
    sen_placed = player.get("sen_placed", 0) or 0
    obs_kills  = player.get("observer_kills", 0) or 0
    sen_kills  = player.get("sentry_kills", 0) or 0

    pts  = kills   * WEIGHTS["kills_per_game"]
    pts += deaths  * WEIGHTS["deaths_per_game"]
    pts += assists * WEIGHTS["assists_per_game"]
    pts += lh      * WEIGHTS["last_hits_per_game"]
    pts += denies  * WEIGHTS["denies_per_game"]
    pts += max(gpm - GPM_BASELINE, 0) / 100 * WEIGHTS["gpm_bonus_per_100"]
    pts += max(xpm - XPM_BASELINE, 0) / 100 * WEIGHTS["xpm_bonus_per_100"]
    pts += damage     / 100 * WEIGHTS["damage_per_100"]
    pts += healing    / 100 * WEIGHTS["healing_per_100"]
    pts += obs_placed * WEIGHTS["obs_placed_per_game"]
    pts += sen_placed * WEIGHTS["sen_placed_per_game"]
    pts += obs_kills  * WEIGHTS["obs_kills_per_game"]
    pts += sen_kills  * WEIGHTS["sen_kills_per_game"]

    return round(pts, 1)
