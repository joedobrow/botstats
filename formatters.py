"""
Formatters — turn raw stat dicts into Discord Embed objects.
"""

import discord
from config import ROLE_LABELS

# Colour palette
EMBED_COLOUR_GOLD   = discord.Colour(0xFFD700)
EMBED_COLOUR_BLUE   = discord.Colour(0x4A90D9)
EMBED_COLOUR_GREEN  = discord.Colour(0x2ECC71)
EMBED_COLOUR_PURPLE = discord.Colour(0x9B59B6)
EMBED_COLOUR_RED    = discord.Colour(0xE74C3C)

# Human-readable labels for stat keys
STAT_LABELS: dict[str, str] = {
    "fantasy_points":          "⭐ Fantasy Pts",
    "value":                   "💎 Value (Diff vs Cost-Predicted)",
    "attendance":              "📅 Attendance",
    "diff":                    "📈 Fantasy Diff vs. Teammates",
    "gpm":                     "💰 GPM",
    "kda":                     "⚔️  KDA",
    "last_hits":               "🌾 Last Hits",
    "denies":                  "🚫 Denies",
    "hero_damage":             "💥 Hero Damage",
    "avg_pct_damage":          "💥 Damage Share %",
    "hero_healing":            "💚 Hero Healing",
    "xpm":                     "📈 XPM",
    "stuns":                   "😴 Stuns (sec)",
    "stuns_per_min":           "😴 Stuns/min",
    "teamfight_participation":  "⚡ Teamfight Part.",
    "tower_kills":             "🏰 Tower Kills",
    "observer_kills":          "👁️  Observer Kills",
    "roshans_killed":          "🐉 Roshans Killed",
    "camps_stacked":           "📦 Camp Stacks",
    "rune_pickups":            "💎 Rune Pickups",
    "defensive_item_uses":     "🛡️  Defensive Item Uses",
    "firstblood_claimed":          "🩸 First Blood Rate",
    "tormentor_kills":             "💀 Tormentor Kills",
    "watcher_captures":            "👁️  Watcher Captures",
    "avg_first_tormentor_time":    "⏱️  First Tormentor Time",
    "avg_duration":                "⏱️  Avg Game Length",
}

MEDAL = ["🥇", "🥈", "🥉"]


def _display_name(p: dict) -> str:
    """Return p['name'] or 'Player_<account_id>' if name is blank."""
    name = (p.get("name") or "").strip()
    if name:
        return name
    aid = p.get("account_id") or 0
    return f"Player_{aid}"


def _week_label(week_offset: int) -> str:
    if week_offset == 0:
        return "This Week"
    if week_offset == 1:
        return "Last Week"
    return f"{week_offset} Weeks Ago"


def _format_seconds(seconds) -> str:
    """Format a duration in seconds as MM:SS."""
    if not seconds or seconds <= 0:
        return "N/A"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"


# Stats where a lower value is better (leaderboard sorts ascending)
LOWER_IS_BETTER = {"avg_first_tormentor_time", "avg_duration"}


def _sort_key(player: dict, sort_by: str) -> float:
    """Return the numeric value to sort on. Some stats are totals, some averages."""
    val = player.get(sort_by)
    # Treat None (e.g., undrafted player on value sort) as worst possible
    if val is None:
        return float("-inf")
    val = val or 0
    # For lower-is-better stats, invert so sorted(..., reverse=True) still puts best first.
    if sort_by in LOWER_IS_BETTER:
        return -val if val > 0 else float("-inf")
    return val


# ---------------------------------------------------------------------------
# /leaderboard
# ---------------------------------------------------------------------------

def format_leaderboard(
    stats: list[dict],
    sort_by: str = "fantasy_points",
    week_label: str = "Latest Week",
    threshold: int | None = None,
    max_games: int | None = None,
    limit: int | None = 10,
    debug: bool = False,
) -> list[discord.Embed]:
    label = STAT_LABELS.get(sort_by, sort_by)
    sorted_players = sorted(stats, key=lambda p: _sort_key(p, sort_by), reverse=True)

    if threshold is not None and max_games is not None:
        desc = f"**{week_label}** · {len(stats)} qualified players (≥ {threshold} of {max_games} games)"
    elif sort_by in ("value", "attendance"):
        desc = f"**{week_label}** · {len(stats)} drafted players"
    else:
        desc = f"**{week_label}** · {len(stats)} players across all matches"

    title_base = f"📊 Leaderboard — {label}"

    cap = limit if limit is not None else len(sorted_players)
    lines = []
    for i, p in enumerate(sorted_players[:cap]):
        medal = MEDAL[i] if i < 3 else f"**{i+1}.**"
        val = p.get(sort_by)
        if val is None:
            # Undrafted on a value sort — skip rather than show 'None'
            continue
        val = val or 0
        # Format nicely depending on type
        if sort_by == "fantasy_points":
            val_str = f"{val:.1f} pts"
        elif sort_by == "value":
            sign = "+" if val >= 0 else ""
            cost = p.get("cost") or 0
            val_str = f"{sign}{val:.2f} (cost {cost})"
            if debug:
                diff = p.get("diff") or 0
                diff_sign = "+" if diff >= 0 else ""
                val_str += f" · diff {diff_sign}{diff:.2f}"
        elif sort_by == "attendance":
            games = p.get("games_played", 0) or 0
            val_str = f"{val * 100:.0f}% ({games} games)"
        elif sort_by == "diff":
            sign = "+" if val >= 0 else ""
            val_str = f"{sign}{val:.1f} pts"
        elif sort_by == "avg_pct_damage":
            val_str = f"{val * 100:.1f}%"
        elif sort_by == "teamfight_participation":
            val_str = f"{val * 100:.1f}%"
        elif sort_by == "firstblood_claimed":
            val_str = f"{val * 100:.1f}%"
        elif sort_by in ("gpm", "xpm", "kda", "stuns"):
            val_str = f"{val:.1f}"
        elif sort_by == "stuns_per_min":
            val_str = f"{val:.2f}/min"
        elif sort_by == "avg_first_tormentor_time":
            val_str = _format_seconds(val)
        elif sort_by == "avg_duration":
            r_val = p.get("avg_duration_radiant")
            d_val = p.get("avg_duration_dire")
            r_str = _format_seconds(r_val) if r_val else "—"
            d_str = _format_seconds(d_val) if d_val else "—"
            val_str = f"{_format_seconds(val)} (R: {r_str} · D: {d_str})"
        elif sort_by in ("tormentor_kills", "watcher_captures"):
            val_str = f"{val:.2f}"
        else:
            val_str = f"{val:.2f}"

        # Make name a clickable Dotabuff link
        account_id = p.get("account_id", 0)
        display = _display_name(p)
        if account_id:
            player_link = f"[{display}](https://www.dotabuff.com/players/{account_id})"
        else:
            player_link = f"**{display}**"

        games = p.get("games_played", 0)
        lines.append(f"{medal} {player_link} — {val_str} — {games} game{'s' if games != 1 else ''}")

    if not lines:
        empty = discord.Embed(title=title_base, description=desc, colour=EMBED_COLOUR_GOLD)
        empty.add_field(name="\u200b", value="No data.", inline=False)
        empty.set_footer(text="Use /leaderboard <stat> to sort by a different stat \u00b7 /player <name> for full details")
        return [empty]

    EMBED_BUDGET = 5500
    FIELD_BUDGET = 1000

    embeds: list[discord.Embed] = []
    cur_lines: list[str] = []
    cur_total = 0

    def flush():
        nonlocal cur_lines, cur_total
        if not cur_lines:
            return
        page_num = len(embeds) + 1
        title = title_base if page_num == 1 else f"{title_base} (cont.)"
        embed = discord.Embed(
            title=title,
            description=desc if page_num == 1 else None,
            colour=EMBED_COLOUR_GOLD,
        )
        chunk: list[str] = []
        chunk_len = 0
        for line in cur_lines:
            line_len = len(line) + 1
            if chunk and chunk_len + line_len > FIELD_BUDGET:
                embed.add_field(name="\u200b", value="\n".join(chunk), inline=False)
                chunk, chunk_len = [line], line_len
            else:
                chunk.append(line)
                chunk_len += line_len
        if chunk:
            embed.add_field(name="\u200b", value="\n".join(chunk), inline=False)
        embeds.append(embed)
        cur_lines, cur_total = [], 0

    for line in lines:
        line_len = len(line) + 1
        if cur_total + line_len > EMBED_BUDGET:
            flush()
        cur_lines.append(line)
        cur_total += line_len
    flush()

    embeds[-1].set_footer(text="Use /leaderboard <stat> to sort by a different stat \u00b7 /player <name> for full details")
    return embeds



# ---------------------------------------------------------------------------
# /player
# ---------------------------------------------------------------------------

def format_player_stats(
    p: dict,
    week_label: str = "All-Time",
    debug: bool = False,
    rating: int | None = None,
    qualified_pool: list[dict] | None = None,
) -> discord.Embed:
    role_str = ROLE_LABELS.get(p.get("role_position"), "Unknown Role")
    games = p.get("games_played", 0)
    wins  = p.get("wins", 0)
    account_id = p.get("account_id", 0)
    attendance = p.get("attendance")

    # Build Dotabuff URL if we have an account ID
    dotabuff_url = f"https://www.dotabuff.com/players/{account_id}" if account_id else None

    games_str = f"{games} game(s) played"
    if attendance is not None:
        games_str += f" ({attendance * 100:.0f}% attendance)"

    title = f"🎮 {_display_name(p)}"
    if rating is not None:
        title += f"  •  Rating {rating}"

    embed = discord.Embed(
        title=title,
        url=dotabuff_url,
        description=f"{role_str} · {week_label} · {games_str} · {wins} win(s)",
        colour=EMBED_COLOUR_BLUE,
    )

    # Percentile-based Strengths / Weaknesses against the qualified pool.
    if qualified_pool:
        strengths, weaknesses = _compute_strengths_weaknesses(p, qualified_pool)
        if strengths:
            embed.add_field(
                name="🔥 Strengths",
                value="\n".join(strengths[:3]),
                inline=False,
            )
        if weaknesses:
            embed.add_field(
                name="📉 Weaknesses",
                value="\n".join(weaknesses[:3]),
                inline=False,
            )

    # Draft (only if we have cost data for this player this season)
    if p.get("cost"):
        cost = p["cost"]
        captain = p.get("captain") or "Unknown"
        mmr = p.get("mmr")
        mmr_str = f"{mmr}" if mmr else "N/A"
        draft = f"💵 Cost:      {cost}\n"
        if debug:
            value = p.get("value")
            predicted = p.get("predicted_diff")
            if value is not None:
                sign = "+" if value >= 0 else ""
                value_str = f"{sign}{value:.2f} vs expected"
            else:
                value_str = "N/A"
            if predicted is not None:
                psign = "+" if predicted >= 0 else ""
                predicted_str = f"{psign}{predicted:.2f}"
            else:
                predicted_str = "N/A"
            draft += f"🎯 Expected:  {predicted_str} diff\n"
            draft += f"💎 Value:     {value_str}\n"
        draft += f"👑 Captain:   {captain}\n"
        draft += f"📊 Adjusted Windrun Rating: {mmr_str}\n"
        embed.add_field(name="Draft", value=draft, inline=True)

    if account_id:
        embed.add_field(
            name="🔗 Profiles",
            value=(
                f"[Dotabuff](https://www.dotabuff.com/players/{account_id}) "
                f"· [Windrun](https://windrun.io/players/{account_id})"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Account ID: {account_id}")
    else:
        embed.set_footer(text="Use /leaderboard to compare across all players")
    return embed


# ---------------------------------------------------------------------------
# /notable_stats
# ---------------------------------------------------------------------------

# (label, key, higher_is_better, format_func)
_NOTABLE_STATS: list[tuple[str, str, bool, callable]] = [
    ("Fantasy Points",          "fantasy_points",          True,  lambda v: f"{v:.1f}"),
    ("Fantasy Diff vs Teammates","diff",                   True,  lambda v: f"{v:+.1f}"),
    ("GPM",                     "gpm",                     True,  lambda v: f"{v:.0f}"),
    ("XPM",                     "xpm",                     True,  lambda v: f"{v:.0f}"),
    ("KDA",                     "kda",                     True,  lambda v: f"{v:.2f}"),
    ("Last Hits/game",          "last_hits",               True,  lambda v: f"{v:.1f}"),
    ("Denies/game",             "denies",                  True,  lambda v: f"{v:.1f}"),
    ("Hero Damage/game",        "hero_damage",             True,  lambda v: f"{v:,.0f}"),
    ("Damage Share %",          "avg_pct_damage",          True,  lambda v: f"{v*100:.1f}%"),
    ("Hero Healing/game",       "hero_healing",            True,  lambda v: f"{v:,.0f}"),
    ("Teamfight %",             "teamfight_participation", True,  lambda v: f"{v*100:.0f}%"),
    ("Stuns/min",               "stuns_per_min",           True,  lambda v: f"{v:.2f}"),
    ("Tower Kills/game",        "tower_kills",             True,  lambda v: f"{v:.2f}"),
    ("Observer Kills/game",     "observer_kills",          True,  lambda v: f"{v:.2f}"),
    ("Roshans Killed/game",     "roshans_killed",          True,  lambda v: f"{v:.2f}"),
    ("Camp Stacks/game",        "camps_stacked",           True,  lambda v: f"{v:.2f}"),
    ("Rune Pickups/game",       "rune_pickups",            True,  lambda v: f"{v:.2f}"),
    ("Defensive Item Uses/game","defensive_item_uses",     True,  lambda v: f"{v:.2f}"),
    ("Tormentor Kills/game",    "tormentor_kills",         True,  lambda v: f"{v:.2f}"),
    ("Watcher Captures/game",   "watcher_captures",        True,  lambda v: f"{v:.2f}"),
    ("Building Damage/game",    "building_damage",         True,  lambda v: f"{v:,.0f}"),
    ("First Blood Rate",        "firstblood_claimed",      True,  lambda v: f"{v*100:.0f}%"),
    ("Avg Game Length",         "avg_duration",            False, lambda v: _format_seconds(v)),
    ("First Tormentor Time",    "avg_first_tormentor_time",False, lambda v: _format_seconds(v)),
]


# Team-aggregate variant: drop stats that don't combine meaningfully
# (e.g., fantasy diff vs teammates is per-player; damage share % averages
# toward 100/N which says little about the team).
_TEAM_NOTABLE_STATS = [
    (label, key, hib, fmt)
    for (label, key, hib, fmt) in _NOTABLE_STATS
    if key not in {"diff", "avg_pct_damage", "firstblood_claimed"}
]


def format_team_stats(
    team_label: str,
    target_agg: dict,
    all_team_aggs: list[dict],
) -> discord.Embed:
    """Render a team-vs-other-teams strengths/weaknesses comparison."""
    games = target_agg.get("games_played", 0)
    wins = target_agg.get("wins", 0)
    losses = games - wins
    win_pct = (wins / games * 100) if games else 0.0
    n_teams = len(all_team_aggs)

    roster = target_agg.get("members") or []
    roster_line = " · ".join(roster) if roster else ""
    desc_parts = []
    if roster_line:
        desc_parts.append(roster_line)
    desc_parts.append(f"{wins}W-{losses}L ({win_pct:.0f}% winrate)")
    embed = discord.Embed(
        title=f"🛡️ Team {team_label}",
        description="\n".join(desc_parts),
        colour=EMBED_COLOUR_GOLD,
    )

    strengths, weaknesses = _compute_strengths_weaknesses(
        target_agg, all_team_aggs, stat_list=_TEAM_NOTABLE_STATS,
    )
    if strengths:
        embed.add_field(
            name="🔥 Strengths",
            value="\n".join(strengths[:5]),
            inline=False,
        )
    if weaknesses:
        embed.add_field(
            name="📉 Weaknesses",
            value="\n".join(weaknesses[:5]),
            inline=False,
        )

    return embed


def _compute_strengths_weaknesses(
    target: dict,
    qualified_pool: list[dict],
    stat_list: list[tuple[str, str, bool, callable]] | None = None,
) -> tuple[list[str], list[str]]:
    """Return (strengths_lines, weaknesses_lines) sorted by extremeness.
    Each list is already ordered best-to-worst (or worst-to-best for weaknesses).
    Callers typically take [:3] of each.
    """
    if stat_list is None:
        stat_list = _NOTABLE_STATS
    strengths: list[tuple[float, str]] = []
    weaknesses: list[tuple[float, str]] = []
    for label, key, higher_is_better, fmt in stat_list:
        target_val = target.get(key)
        if target_val is None:
            continue
        pool_vals = [p.get(key) for p in qualified_pool if p.get(key) is not None]
        if len(pool_vals) < 3:
            continue
        if higher_is_better:
            better_count = sum(1 for v in pool_vals if v > target_val)
        else:
            better_count = sum(1 for v in pool_vals if v < target_val)
        rank = better_count + 1
        total = len(pool_vals)
        # 1.0 = best in pool, 0.0 = worst
        percentile = 1 - (rank - 1) / max(1, total - 1)
        try:
            val_str = fmt(target_val)
        except Exception:
            val_str = str(target_val)
        line = f"**{label}**: {val_str} (rank {rank}/{total})"
        if percentile >= 0.5:
            strengths.append((percentile, line))
        else:
            weaknesses.append((percentile, line))
    strengths.sort(key=lambda x: -x[0])
    weaknesses.sort(key=lambda x: x[0])
    return [line for _, line in strengths], [line for _, line in weaknesses]


# ---------------------------------------------------------------------------
# /players
# ---------------------------------------------------------------------------

def format_players_list(cached: list[dict], fantasy_adjusted: bool = False, debug: bool = False) -> list[discord.Embed]:
    """Build the /players embed list. Returns multiple embeds when needed —
    Discord caps each embed at 6000 total chars, but allows up to 10 embeds
    per message. We pack ~5500 chars per embed (safety margin)."""
    from datetime import datetime

    sorted_rows = sorted(cached, key=lambda r: -(r.get("internal_rating") or -1))

    timestamps = [r.get("updated_at") for r in cached if r.get("updated_at")]
    refresh_str = "never"
    if timestamps:
        try:
            dt = datetime.fromisoformat(max(timestamps))
            refresh_str = f"<t:{int(dt.timestamp())}:R>"
        except Exception:
            refresh_str = "recently"

    base_title = "🏅 Players by Rating"
    desc_extra = ""
    if debug and fantasy_adjusted:
        base_title += " (fantasy-adjusted, debug)"
        desc_extra = " · ±6% based on fantasy diff residual (actual vs rating-expected)"

    # Build the lines once
    lines: list[str] = []
    rank = 0
    for r in sorted_rows:
        rating = r.get("internal_rating")
        if rating is None:
            continue
        rank += 1
        name = r.get("override_nickname") or _display_name({
            "name": r.get("name"),
            "account_id": r.get("account_id"),
        })
        windrun_url = f"https://windrun.io/players/{r['account_id']}"
        line = f"**{rank}.** [{name}]({windrun_url}) — **{rating}**"
        if debug and fantasy_adjusted and "_pct" in r:
            pct      = r.get("_pct", 0.0)
            residual = r.get("_residual", 0.0)
            line += f" _({pct*100:+.1f}%, {residual:+.1f} vs expected)_"
        lines.append(line)

    if not lines:
        embed = discord.Embed(
            title=base_title,
            description=f"0 players · last refreshed {refresh_str}{desc_extra}",
            colour=EMBED_COLOUR_GOLD,
        )
        embed.add_field(name="​", value="No rated players in cache yet.", inline=False)
        return [embed]

    # Pack lines into embeds, each capped well under the 6000-char total embed limit.
    EMBED_BUDGET = 5500   # safety margin under the 6000 hard cap
    FIELD_BUDGET = 1000   # safety margin under the 1024 per-field cap

    embeds: list[discord.Embed] = []
    cur_lines: list[str] = []
    cur_total = 0

    def flush():
        nonlocal cur_lines, cur_total
        if not cur_lines:
            return
        page_num = len(embeds) + 1
        title = base_title if page_num == 1 else f"{base_title} (cont.)"
        desc = f"{len(sorted_rows)} players · last refreshed {refresh_str}{desc_extra}" if page_num == 1 else None
        embed = discord.Embed(title=title, description=desc, colour=EMBED_COLOUR_GOLD)
        # Split cur_lines into 1000-char fields
        chunk: list[str] = []
        chunk_len = 0
        for line in cur_lines:
            line_len = len(line) + 1
            if chunk and chunk_len + line_len > FIELD_BUDGET:
                embed.add_field(name="​", value="\n".join(chunk), inline=False)
                chunk, chunk_len = [line], line_len
            else:
                chunk.append(line)
                chunk_len += line_len
        if chunk:
            embed.add_field(name="​", value="\n".join(chunk), inline=False)
        embeds.append(embed)
        cur_lines, cur_total = [], 0

    for line in lines:
        line_len = len(line) + 1
        if cur_total + line_len > EMBED_BUDGET:
            flush()
        cur_lines.append(line)
        cur_total += line_len
    flush()

    if debug and embeds:
        embeds[-1].set_footer(text="Owner: run /refresh_ratings to update")
    return embeds


# ---------------------------------------------------------------------------
# /lookup
# ---------------------------------------------------------------------------

def _ranked_mmr_to_windrun(ranked_mmr: int) -> float:
    """Piecewise conversion calibrated against user-supplied anchors at the
    top end (where empirical sample means under-represent skilled AD players):

      MMR ≤ 8000:   wr = 1900 + 0.20·mmr
      MMR > 8000:   wr = 3500 + 0.10·(mmr − 8000)

    Anchors: 8000 → 3500, 10000 → 3700. Lower-MMR slope chosen so the formula
    still hits empirical bucket means around MMR 3000–5000 (~2500–2900 windrun).
    """
    if ranked_mmr <= 8000:
        return 1900 + 0.20 * ranked_mmr
    return 3500 + 0.10 * (ranked_mmr - 8000)


def _windrun_to_ranked_mmr(wr_rating: float) -> int:
    """Inverse of _ranked_mmr_to_windrun. Boundary at wr = 3500 (mmr = 8000)."""
    if wr_rating <= 3500:
        return round(5 * (wr_rating - 1900))
    return round(8000 + 10 * (wr_rating - 3500))


def _resolve_internal_rating(
    override: dict | None,
    ad_last: int | None,
    ad_all: int | None,
    ranked_last: int | None,
    wr_rating: float | None,
    ranked_mmr: int | None,
    ranked_all: int | None = None,
) -> tuple[int, str] | None:
    """If the override sets internal_rating_override, return that directly
    (skipping the windrun/ranked blend). Otherwise compute as normal."""
    if override and override.get("internal_rating_override") is not None:
        return (int(override["internal_rating_override"]), "manual rating override")
    return _internal_rating(ad_last, ad_all, ranked_last, wr_rating, ranked_mmr, ranked_all)


def _ad_inexperience_factor(ad_last_year: int | None) -> float:
    """Discount applied to ranked-eqv to reflect AD inexperience.

    A pure ranked player's MMR doesn't fully translate to AD skill until
    they have meaningful AD experience. Scale from 0.90 (10% penalty) at
    0 AD games last year up to 1.00 (no penalty) at 200+.
    Unknown ad_last_year → no penalty (no data to judge).
    """
    if ad_last_year is None:
        return 1.0
    if ad_last_year >= 200:
        return 1.0
    if ad_last_year <= 0:
        return 0.90
    return 0.90 + (ad_last_year / 200.0) * 0.10


def _lifetime_ad_bonus(ad_all_time: int | None) -> float:
    """Trust bonus rewarding lifetime AD experience, added to base trust.

    A player with many lifetime AD games has 'specialist credibility' that
    complements their last-year activity. Phases in from 500 to 2000 lifetime
    AD games, capped at +15%.
    """
    if ad_all_time is None or ad_all_time < 500:
        return 0.0
    if ad_all_time >= 2000:
        return 0.15
    return (ad_all_time - 500) / 1500 * 0.15


def _base_ad_trust(ad_last_year: int | None) -> float:
    """Map last-year AD game count → windrun-trust fraction in [0, 1].

    Piecewise linear, anchored at:
        <200 → 0%, 200 → 25%, 400 → 50%, 600 → 60%, 800 → 70%,
        1200 → 80%, 1600 → 90%, 2000+ → 100%
    """
    if ad_last_year is None or ad_last_year < 200:
        return 0.0
    if ad_last_year >= 2000:
        return 1.0
    if ad_last_year >= 800:
        # 800 → 0.70, 2000 → 1.00 (slope 0.00025 per game)
        return 0.70 + (ad_last_year - 800) * 0.00025
    if ad_last_year >= 400:
        # 400 → 0.50, 800 → 0.70 (slope 0.0005 per game)
        return 0.50 + (ad_last_year - 400) * 0.0005
    # 200 → 0.25, 400 → 0.50 (slope 0.00125 per game)
    return 0.25 + (ad_last_year - 200) * 0.00125


def _internal_rating(
    ad_last: int | None,
    ad_all: int | None,
    ranked_last: int | None,
    wr_rating: float | None,
    ranked_mmr: int | None,
    ranked_all: int | None = None,
) -> tuple[int, str] | None:
    """Compute the player's internal (true-skill) rating on the windrun scale.

    Conversion: piecewise linear from ranked MMR → windrun (see
    _ranked_mmr_to_windrun).

    Trust model:
      base         = _base_ad_trust(ad_last)                    # 0% to 100%
      lifetime_bonus = _lifetime_ad_bonus(ad_all)               # 0% to +15%
      ad_share     = ad_last / (ad_last + ranked_last)
      adj          = (ad_share − 0.50) × 0.4                    # ±10% near 25%/75%
      trust        = clamp(base + lifetime_bonus + adj, 0, 1)

    Output: trust·windrun + (1 − trust)·ranked_in_wr.

    Returns (rating, explanation) or None if neither source is usable.
    """
    ranked_in_wr_raw = _ranked_mmr_to_windrun(ranked_mmr) if ranked_mmr is not None else None
    # Discount ranked-eqv when AD experience is low — pure-ranked players
    # don't 1:1 translate to AD until they've shown they can actually play.
    inexperience_factor = _ad_inexperience_factor(ad_last)
    ranked_in_wr = ranked_in_wr_raw * inexperience_factor if ranked_in_wr_raw is not None else None

    if wr_rating is None and ranked_in_wr is None:
        return None
    if wr_rating is None:
        return (round(ranked_in_wr), "ranked-converted (no windrun data)")
    if ranked_in_wr is None:
        return (round(wr_rating), "windrun (no ranked data)")

    # If recent ranked activity is too low AND lifetime ranked is also low,
    # the MMR estimate is essentially a stale snapshot — blending it in adds
    # noise. Players with substantial lifetime ranked (500+) have a well-
    # anchored MMR even if they've slowed down recently, so we still blend.
    LOW_RANKED_LAST_THRESHOLD = 50
    LOW_RANKED_ALL_THRESHOLD  = 500
    has_significant_ranked_history = (ranked_all is not None and ranked_all >= LOW_RANKED_ALL_THRESHOLD)
    if (ranked_last is not None
            and ranked_last < LOW_RANKED_LAST_THRESHOLD
            and not has_significant_ranked_history):
        ad_str = f"{ad_last} AD/yr" if ad_last is not None else ""
        ranked_all_str = f", {ranked_all} ranked lifetime" if ranked_all is not None else ""
        explanation = (
            f"100% windrun ({round(wr_rating)}) "
            f"[ranked ignored: only {ranked_last} ranked games last year"
            + ranked_all_str
            + (f", {ad_str}" if ad_str else "")
            + "]"
        )
        return (round(wr_rating), explanation)

    base = _base_ad_trust(ad_last)
    lifetime_bonus = _lifetime_ad_bonus(ad_all)
    adj = 0.0
    share_str = ""
    if ad_last is not None and ranked_last is not None and (ad_last + ranked_last) > 0:
        ad_share = ad_last / (ad_last + ranked_last)
        adj = (ad_share - 0.5) * 0.4
        share_str = f", {ad_share * 100:.0f}% AD share"
    trust = max(0.0, min(1.0, base + lifetime_bonus + adj))

    rating = trust * wr_rating + (1.0 - trust) * ranked_in_wr

    ad_str = f"{ad_last} AD/yr" if ad_last is not None else "AD/yr unknown"
    lifetime_str = f", +{lifetime_bonus * 100:.0f}% lifetime" if lifetime_bonus > 0 else ""
    penalty_str = (
        f", −{(1 - inexperience_factor) * 100:.0f}% AD penalty"
        if inexperience_factor < 1.0 else ""
    )
    explanation = (
        f"{trust * 100:.0f}% windrun ({round(wr_rating)}) + "
        f"{(1 - trust) * 100:.0f}% ranked-eqv ({round(ranked_in_wr)})  "
        f"[{ad_str}{share_str}{lifetime_str}{penalty_str}]"
    )
    return (round(rating), explanation)


def format_lookup(
    account_id: int,
    fallback_name: str | None = None,
    windrun: dict | None = None,
    opendota: dict | None = None,
    ad_all_time: int | None = None,
    ad_last_year: int | None = None,
    od_counts: dict | None = None,
    override: dict | None = None,
    debug: bool = False,
    cached_rating: int | None = None,
    cached_avatar: str | None = None,
    updated_at: str | None = None,
    is_stale: bool = False,
    adjustment_pct: float | None = None,
    fetch_error: str | None = None,
) -> discord.Embed:
    """Build the /lookup embed. `windrun` is the raw dict from windrun.io's
    /players/{id} endpoint (or None). `opendota` is the raw dict from
    OpenDota's /players/{id} endpoint (or None). `ad_*` come from windrun's
    match list; `od_counts` is the dict from fetch_player_game_counts().
    `override` is a row from skill_overrides (or None); when set, its values
    take precedence over API data for display + internal-rating calc."""
    # Override nickname (if any) takes precedence over anything from the APIs.
    name = (override or {}).get("nickname")
    if not name and windrun:
        name = windrun.get("nickname")
    if not name and opendota:
        name = (opendota.get("profile") or {}).get("personaname")
    name = name or fallback_name or f"Player_{account_id}"

    dotabuff_url = f"https://www.dotabuff.com/players/{account_id}"
    windrun_url  = f"https://windrun.io/players/{account_id}"

    title = f"🔍 {name}"
    if debug:
        title += " (debug)"
    embed = discord.Embed(title=title, url=dotabuff_url, colour=EMBED_COLOUR_BLUE)

    # Avatar (when available) shown in both modes — visual identity only.
    # Prefer fresh windrun avatar; fall back to cached avatar if needed.
    avatar_to_use = (windrun or {}).get("avatar") or cached_avatar
    if avatar_to_use:
        embed.set_thumbnail(url=avatar_to_use)

    # Prominent fetch-failure warning (used by /lookup force_refresh).
    if fetch_error:
        embed.add_field(name="⚠️ Refresh failed", value=fetch_error, inline=False)

    # Resolve overrides up front so display + internal-rating both see them.
    override_wr  = (override or {}).get("windrun_rating")
    override_mmr = (override or {}).get("ranked_mmr")
    effective_wr_rating = override_wr if override_wr is not None else (windrun or {}).get("rating")

    # Compute MMR estimate regardless of mode — internal rating needs it.
    from opendota_lookup import decode_rank_tier, estimate_mmr_from_rank_tier
    rank_tier = (opendota or {}).get("rank_tier")
    leaderboard_rank = (opendota or {}).get("leaderboard_rank")
    api_mmr_est = estimate_mmr_from_rank_tier(rank_tier, leaderboard_rank)
    mmr_est = override_mmr if override_mmr is not None else api_mmr_est
    ranked_last = (od_counts or {}).get("last_year_ranked")

    # --- Debug-only fields (everything that exposes methodology / raw inputs) ---
    if debug:
        if windrun:
            rating       = override_wr if override_wr is not None else windrun.get("rating")
            region       = (windrun.get("region") or "").title() or "?"
            overall_rank = windrun.get("overallRank")
            regional_rank = windrun.get("regionalRank")

            rating_str = f"**{rating:.0f}**" if isinstance(rating, (int, float)) else "Unknown"
            if override_wr is not None:
                rating_str += " ⚠️"
            rank_bits = []
            if regional_rank:
                rank_bits.append(f"#{regional_rank} {region}")
            if overall_rank:
                rank_bits.append(f"#{overall_rank} overall")
            rank_line = " · ".join(rank_bits) if rank_bits else "Unranked"

            embed.add_field(name="🌬️ Windrun", value=f"{rating_str} — {rank_line}", inline=False)
        elif override_wr is not None:
            embed.add_field(
                name="🌬️ Windrun",
                value=f"**{override_wr:.0f}** ⚠️ _override_ (no windrun.io data)",
                inline=False,
            )
        else:
            embed.add_field(
                name="🌬️ Windrun",
                value="Unknown (windrun lookup failed or player not on windrun)",
                inline=False,
            )

        medal_str = decode_rank_tier(rank_tier, leaderboard_rank)
        if override_mmr is not None:
            base = medal_str or "Manual"
            mmr_value = f"**{base}** (~{override_mmr} MMR) ⚠️"
        elif medal_str and api_mmr_est is not None:
            is_immortal = rank_tier and rank_tier // 10 == 8
            if is_immortal and not leaderboard_rank:
                mmr_value = f"**{medal_str}** (~{api_mmr_est}+ MMR)"
            else:
                mmr_value = f"**{medal_str}** (~{api_mmr_est} MMR)"
        elif medal_str:
            mmr_value = f"**{medal_str}**"
        else:
            mmr_value = "Unknown"
        embed.add_field(name="🏆 Ranked MMR", value=mmr_value, inline=True)

        def _fmt_count(last_yr, all_time):
            ly = "?" if last_yr is None else str(last_yr)
            at = "?" if all_time is None else str(all_time)
            return f"**{ly}** last year · **{at}** all-time"
        ranked_total = (od_counts or {}).get("all_time_ranked")
        embed.add_field(
            name="📊 Games Played",
            value=(
                f"🎯 **AD:** {_fmt_count(ad_last_year, ad_all_time)}\n"
                f"⚔️ **Ranked:** {_fmt_count(ranked_last, ranked_total)}"
            ),
            inline=False,
        )

    # --- Internal rating (always shown) ---
    rating: int | None = None
    explanation: str | None = None
    have_signal = (
        od_counts is not None
        or windrun is not None
        or (override and override.get("internal_rating_override") is not None)
    )
    if have_signal:
        ranked_all_time = (od_counts or {}).get("all_time_ranked")
        rating_info = _resolve_internal_rating(
            override=override,
            ad_last=ad_last_year,
            ad_all=ad_all_time,
            ranked_last=ranked_last,
            wr_rating=effective_wr_rating,
            ranked_mmr=mmr_est,
            ranked_all=ranked_all_time,
        )
        if rating_info:
            rating, explanation = rating_info

    # Fall back to cached rating if fresh compute didn't yield one.
    if rating is None and cached_rating is not None:
        rating = cached_rating

    if rating is None:
        embed.add_field(
            name="✨ Internal Rating",
            value="⚠️ Couldn't compute right now — try again later.",
            inline=False,
        )
    else:
        # Apply the fantasy-performance adjustment so /lookup matches /players.
        base_rating = rating
        if adjustment_pct is not None:
            rating = round(base_rating * (1 + adjustment_pct))

        if debug and explanation:
            value = f"**{rating}** _(windrun-equiv)_"
            if adjustment_pct is not None:
                value += f"\n_base {base_rating} · fantasy adj {adjustment_pct*100:+.1f}%_"
            value += f"\n_{explanation}_"
        else:
            value = f"**{rating}**"
        if is_stale:
            value += " _(cached)_"
        embed.add_field(name="✨ Internal Rating", value=value, inline=False)

    profiles_value = f"[Dotabuff]({dotabuff_url}) · [Windrun]({windrun_url})"
    if updated_at:
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(updated_at)
            profiles_value += f"\nLast updated <t:{int(dt.timestamp())}:R>"
        except Exception:
            pass
    embed.add_field(name="🔗 Profiles", value=profiles_value, inline=False)
    embed.set_footer(text=f"Account ID: {account_id}")
    return embed


# ---------------------------------------------------------------------------
# /suggested_cost
# ---------------------------------------------------------------------------
# /roles
# ---------------------------------------------------------------------------
# Weekly auto-post summary
# ---------------------------------------------------------------------------
# /summary (compact leaderboard for embedding multiple in one message)
# ---------------------------------------------------------------------------

def format_compact_leaderboard(stats: list[dict], pos: int, sort_by: str = "fantasy_points") -> str:
    """Return a compact text block for a single position's top 3 players."""
    # Filter by position
    filtered = [s for s in stats if s.get("role_position") == pos]
    if not filtered:
        return "*No data*"

    sorted_players = sorted(filtered, key=lambda p: _sort_key(p, sort_by), reverse=True)

    lines = []
    for i, p in enumerate(sorted_players[:3]):  # top 3 only
        medal = MEDAL[i]
        val = p.get(sort_by, 0)
        if sort_by == "fantasy_points":
            val_str = f"{val:.1f}"
        else:
            val_str = f"{val:.1f}"
        lines.append(f"{medal} {_display_name(p)} — {val_str}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /matches
# ---------------------------------------------------------------------------

def format_matches_list(matches: list[dict], week_label: str = "Latest Week") -> discord.Embed:
    """Format a list of matches with Dotabuff links."""
    from datetime import datetime

    embed = discord.Embed(
        title=f"🎮 Matches — {week_label}",
        description=f"{len(matches)} match(es) found",
        colour=EMBED_COLOUR_BLUE,
    )

    lines = []
    for m in matches:
        match_id = m["match_id"]
        # Convert unix timestamp to readable date
        match_time = datetime.fromtimestamp(m["start_time"]).strftime("%b %d, %I:%M %p")
        duration_min = m["duration"] // 60

        winner = "Radiant" if m["radiant_win"] else "Dire"
        score = f"{m['radiant_score']}-{m['dire_score']}"

        dotabuff_link = f"https://www.dotabuff.com/matches/{match_id}"
        opendota_link = f"https://www.opendota.com/matches/{match_id}"

        lines.append(
            f"**{match_time}** ({duration_min}m) — {winner} won {score}\n"
            f"[Dotabuff]({dotabuff_link}) · [OpenDota]({opendota_link})"
        )

    # Split into multiple fields if content is too long (Discord limit: 1024 chars per field)
    if not lines:
        embed.add_field(name="\u200b", value="No matches.", inline=False)
    else:
        current_chunk = []
        current_length = 0
        field_num = 1

        for line in lines:
            line_length = len(line) + 2  # +2 for the "\n\n" separator
            if current_length + line_length > 1000 and current_chunk:
                # Add current chunk as a field and start a new one
                embed.add_field(
                    name=f"Matches" if field_num == 1 else "\u200b",
                    value="\n\n".join(current_chunk),
                    inline=False
                )
                current_chunk = [line]
                current_length = len(line)
                field_num += 1
            else:
                current_chunk.append(line)
                current_length += line_length

        # Add the last chunk
        if current_chunk:
            embed.add_field(
                name=f"Matches" if field_num == 1 else "\u200b",
                value="\n\n".join(current_chunk),
                inline=False
            )

    embed.set_footer(text="Click the links to view full match details")
    return embed
