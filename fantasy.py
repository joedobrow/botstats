"""
Fantasy Points Formula
-----------------------

Based on pyopendota's scoring system with custom tweaks.

The formula works on per-game averages so that players who played fewer
games aren't unfairly penalised (or rewarded) just by game count.

Scoring breakdown (per game played):
    Kills                   +0.3   each
    Deaths                  +3.0   base per game, −0.3 each
                                   (0 deaths = +3.0, 10 deaths = 0, 11+ goes negative)
    Assists                 +0.1   each                    [tweak: pyopendota has 0]
    Last Hits               +0.003 each
    GPM                     +0.002 per GPM (no baseline)
    XPM                     +0.001 per XPM (no baseline)  [tweak: not in pyopendota]
    Tower Kills             +1.0   each
    Roshan Kills            +0.5   each
    First Blood             +2.0   if claimed
    Teamfight Participation +3.0   × participation rate (0–1)
    Stuns                   +0.05  per second of stun
    Obs Placed              +0.1   each
    Sen Placed               0     (not scored)
    Obs Kills               +0.4   each
    Sen Kills               +0.2   each
    Camp Stacks             +0.1   each
    Rune Pickups            +0.25  each
    Denies                  +0.02  each                    [tweak: not in pyopendota]
    Hero Healing            +0.008 per 100                 [tweak: not in pyopendota]
    Hero Damage             +0.005 per 100                 [tweak: not in pyopendota]

Total is then rounded to 1 decimal place.
"""

WEIGHTS = {
    "kills_per_game":               0.3,
    "death_base":                   3.0,   # flat bonus per game (eroded by deaths)
    "deaths_per_game":             -0.3,
    "assists_per_game":             0.1,   # tweak: pyopendota has 0
    "last_hits_per_game":           0.003,
    "gpm":                          0.002,
    "xpm":                          0.001,  # tweak: not in pyopendota
    "tower_kills_per_game":         1.0,
    "roshans_killed_per_game":      0.5,
    "firstblood_claimed_per_game":  2.0,
    "teamfight_participation":      3.0,
    "stuns_per_game":               0.05,
    "obs_placed_per_game":          0.1,
    "sen_placed_per_game":          0.0,
    "obs_kills_per_game":           0.4,
    "sen_kills_per_game":           0.2,
    "camps_stacked_per_game":       0.1,
    "rune_pickups_per_game":        0.25,
    "denies_per_game":              0.02,  # tweak: not in pyopendota
    "healing_per_100":              0.008, # tweak: not in pyopendota
    "damage_per_100":               0.005, # tweak: not in pyopendota
}


def calculate_fantasy_points(player: dict) -> float:
    """
    Given an aggregated player dict (as returned by db stats functions),
    compute and return the fantasy point total as a PER-GAME AVERAGE.

    Players with more games are NOT rewarded — this is purely skill-based.
    """
    games = max(player.get("games_played", 1), 1)  # avoid div-by-zero

    # Convert totals to per-game averages
    kills    = player.get("total_kills", 0) / games
    deaths   = player.get("total_deaths", 0) / games
    assists  = player.get("total_assists", 0) / games

    # These are already per-game averages from SQL AVG()
    lh                      = player.get("last_hits", 0) or 0
    denies                  = player.get("denies", 0) or 0
    gpm                     = player.get("gpm", 0) or 0
    xpm                     = player.get("xpm", 0) or 0
    damage                  = player.get("hero_damage", 0) or 0
    healing                 = player.get("hero_healing", 0) or 0
    obs_placed              = player.get("obs_placed", 0) or 0
    sen_placed              = player.get("sen_placed", 0) or 0
    obs_kills               = player.get("observer_kills", 0) or 0
    sen_kills               = player.get("sentry_kills", 0) or 0
    tower_kills             = player.get("tower_kills", 0) or 0
    roshans_killed          = player.get("roshans_killed", 0) or 0
    firstblood_claimed      = player.get("firstblood_claimed", 0) or 0
    teamfight_participation = player.get("teamfight_participation", 0) or 0
    stuns                   = player.get("stuns", 0) or 0
    camps_stacked           = player.get("camps_stacked", 0) or 0
    rune_pickups            = player.get("rune_pickups", 0) or 0

    pts  = kills                   * WEIGHTS["kills_per_game"]
    pts += WEIGHTS["death_base"]                               # +3 per game base
    pts += deaths                  * WEIGHTS["deaths_per_game"]
    pts += assists                 * WEIGHTS["assists_per_game"]
    pts += lh                      * WEIGHTS["last_hits_per_game"]
    pts += denies                  * WEIGHTS["denies_per_game"]
    pts += gpm                     * WEIGHTS["gpm"]
    pts += xpm                     * WEIGHTS["xpm"]
    pts += tower_kills             * WEIGHTS["tower_kills_per_game"]
    pts += roshans_killed          * WEIGHTS["roshans_killed_per_game"]
    pts += firstblood_claimed      * WEIGHTS["firstblood_claimed_per_game"]
    pts += teamfight_participation * WEIGHTS["teamfight_participation"]
    pts += stuns                   * WEIGHTS["stuns_per_game"]
    pts += obs_placed              * WEIGHTS["obs_placed_per_game"]
    pts += sen_placed              * WEIGHTS["sen_placed_per_game"]
    pts += obs_kills               * WEIGHTS["obs_kills_per_game"]
    pts += sen_kills               * WEIGHTS["sen_kills_per_game"]
    pts += camps_stacked           * WEIGHTS["camps_stacked_per_game"]
    pts += rune_pickups            * WEIGHTS["rune_pickups_per_game"]
    pts += damage  / 100           * WEIGHTS["damage_per_100"]
    pts += healing / 100           * WEIGHTS["healing_per_100"]

    return round(pts, 1)
