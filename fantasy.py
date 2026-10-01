"""
Fantasy Points Formula
-----------------------

Based on pyopendota's scoring system with custom tweaks.

The formula works on per-game averages so that players who played fewer
games aren't unfairly penalised (or rewarded) just by game count.

Scoring breakdown (per game played):
    Kills                   +0.3   each
    Deaths                  +3.0   base per game, −0.2 each
                                   (0 deaths = +3.0, 15 deaths = 0, 16+ goes negative)
    Assists                 +0.25  each                    [tweak: pyopendota has 0]
    Last Hits               +0.003 each
    GPM                     +0.003 per GPM (no baseline)
    XPM                     +0.002 per XPM (no baseline)  [tweak: not in pyopendota]
    Teamfight Participation +5.0   × participation rate (0–1)
    Stuns                   +0.08  per second of stun
    Obs Placed              +0.1   each
    Sen Placed               0     (not scored)
    Obs Kills               +0.5   each
    Sen Kills               +0.2   each
    Camp Stacks             +0.1   each
    Rune Pickups            +0.25  each
    Denies                  +0.02  each                    [tweak: not in pyopendota]
    Hero Healing            +0.015 per 100                 [tweak: not in pyopendota]
    Hero Damage             +0.01  per 100                 [tweak: not in pyopendota]
    Building Damage         +0.01  per 100                 [from tower_damage]
    Defensive Item Uses     +0.05  each                    [tweak: not in pyopendota]
        — sum of pipe / crimson / lotus / glimmer / force /
          pavise / solar crest / heaven's halberd / linken's
          activations from OpenDota's item_uses

    REMOVED in favor of more granular signals:
    - Tower Kills / First Blood / Roshan Kills (zero-weight; building damage
      now captures objective contribution).

Duration normalisation:
    A linear regression over all stored matches found:
        fantasy_pts = 0.3678 × duration_min + 5.67   (R² = 0.34)
    We remove this bias so longer games don't unfairly inflate scores:
        adjustment = −0.3678 × (avg_duration_min − 41.7)
    Players whose games averaged longer than 41.7 min are penalised;
    shorter-game players receive a bonus.

Total is then rounded to 1 decimal place.
"""

# Duration normalisation coefficients (derived from linear regression over all stored matches)
# fantasy_pts = DURATION_SLOPE * duration_min + DURATION_INTERCEPT   (R² = 0.34)
# Re-run analyze_duration.py periodically as more matches accumulate to refresh these.
DURATION_SLOPE    = 0.3678   # pts per minute
DURATION_MEAN_MIN = 41.7     # average game length in minutes (the "neutral" point)

WEIGHTS = {
    "kills_per_game":               0.3,
    "death_base":                   3.0,   # flat bonus per game (eroded by deaths)
    "deaths_per_game":             -0.2,
    "assists_per_game":             0.25,  # tweak: pyopendota has 0
    "last_hits_per_game":           0.003,
    "gpm":                          0.003,
    "xpm":                          0.002,  # tweak: not in pyopendota
    "teamfight_participation":      5.0,
    "stuns_per_game":               0.08,
    "obs_placed_per_game":          0.1,
    "sen_placed_per_game":          0.0,
    "obs_kills_per_game":           0.5,
    "sen_kills_per_game":           0.2,
    "camps_stacked_per_game":       0.1,
    "rune_pickups_per_game":        0.25,
    "denies_per_game":              0.02,  # tweak: not in pyopendota
    "healing_per_100":              0.015, # tweak: not in pyopendota
    "damage_per_100":               0.01,  # tweak: not in pyopendota
    "building_damage_per_100":      0.01,  # tower_damage from OpenDota
    "defensive_item_uses_per_game": 0.05,  # tweak: not in pyopendota
}


# What each component is called, and how to read the number behind it, for
# anywhere the score is explained rather than just totalled.
LABELS = {
    "death_base":                   ("Staying alive", "base, per game"),
    "kills_per_game":               ("Kills", "per game"),
    "deaths_per_game":              ("Deaths", "per game"),
    "assists_per_game":             ("Assists", "per game"),
    "last_hits_per_game":           ("Last hits", "per game"),
    "denies_per_game":              ("Denies", "per game"),
    "gpm":                          ("Gold per minute", ""),
    "xpm":                          ("Experience per minute", ""),
    "teamfight_participation":      ("Teamfight participation", "share of fights"),
    "stuns_per_game":               ("Stun duration", "seconds per game"),
    "obs_placed_per_game":          ("Observer wards placed", "per game"),
    "sen_placed_per_game":          ("Sentry wards placed", "per game"),
    "obs_kills_per_game":           ("Observer wards killed", "per game"),
    "sen_kills_per_game":           ("Sentry wards killed", "per game"),
    "camps_stacked_per_game":       ("Camps stacked", "per game"),
    "rune_pickups_per_game":        ("Runes taken", "per game"),
    "damage_per_100":               ("Hero damage", "per game"),
    "healing_per_100":              ("Hero healing", "per game"),
    "building_damage_per_100":      ("Building damage", "per game"),
    "defensive_item_uses_per_game": ("Defensive items used", "per game"),
    "duration":                     ("Game length adjustment", "average minutes"),
}


def breakdown(player: dict) -> list[dict]:
    """Every component of a player's fantasy score, as
    [{key, label, unit, value, weight, points}] summing to the total.

    The score is one number made of twenty-one, and which of them a player
    earns is the interesting part — so this is what explains it, and
    calculate_fantasy_points totals it rather than repeating the arithmetic.
    """
    games = max(player.get("games_played", 1), 1)
    per_game = lambda key: (player.get(key, 0) or 0) / games
    avg = lambda key: player.get(key, 0) or 0          # already per-game from SQL AVG()

    values = {
        "death_base":                   1.0,
        "kills_per_game":               per_game("total_kills"),
        "deaths_per_game":              per_game("total_deaths"),
        "assists_per_game":             per_game("total_assists"),
        "last_hits_per_game":           avg("last_hits"),
        "denies_per_game":              avg("denies"),
        "gpm":                          avg("gpm"),
        "xpm":                          avg("xpm"),
        "teamfight_participation":      avg("teamfight_participation"),
        "stuns_per_game":               avg("stuns"),
        "obs_placed_per_game":          avg("obs_placed"),
        "sen_placed_per_game":          avg("sen_placed"),
        "obs_kills_per_game":           avg("observer_kills"),
        "sen_kills_per_game":           avg("sentry_kills"),
        "camps_stacked_per_game":       avg("camps_stacked"),
        "rune_pickups_per_game":        avg("rune_pickups"),
        "damage_per_100":               avg("hero_damage") / 100,
        "healing_per_100":              avg("hero_healing") / 100,
        "building_damage_per_100":      avg("building_damage") / 100,
        "defensive_item_uses_per_game": avg("defensive_item_uses"),
    }
    rows = []
    for key, value in values.items():
        label, unit = LABELS[key]
        # Hero damage and healing are scored per 100 but read per game.
        shown = value * 100 if key.endswith("_per_100") else value
        rows.append({"key": key, "label": label, "unit": unit, "value": shown,
                     "weight": WEIGHTS[key], "points": value * WEIGHTS[key]})

    avg_duration_min = (player.get("avg_duration") or (DURATION_MEAN_MIN * 60)) / 60.0
    label, unit = LABELS["duration"]
    rows.append({"key": "duration", "label": label, "unit": unit,
                 "value": avg_duration_min, "weight": -DURATION_SLOPE,
                 "points": -DURATION_SLOPE * (avg_duration_min - DURATION_MEAN_MIN)})
    return rows


def calculate_fantasy_points(player: dict) -> float:
    """
    Given an aggregated player dict (as returned by db stats functions),
    compute and return the fantasy point total as a PER-GAME AVERAGE.

    Players with more games are NOT rewarded — this is purely skill-based.
    """
    return round(sum(c["points"] for c in breakdown(player)), 1)
