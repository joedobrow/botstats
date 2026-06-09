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
LOWER_IS_BETTER = {"avg_first_tormentor_time"}


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
) -> discord.Embed:
    label = STAT_LABELS.get(sort_by, sort_by)
    sorted_players = sorted(stats, key=lambda p: _sort_key(p, sort_by), reverse=True)

    if threshold is not None and max_games is not None:
        desc = f"**{week_label}** · {len(stats)} qualified players (≥ {threshold} of {max_games} games)"
    elif sort_by in ("value", "attendance"):
        desc = f"**{week_label}** · {len(stats)} drafted players"
    else:
        desc = f"**{week_label}** · {len(stats)} players across all matches"

    embed = discord.Embed(
        title=f"📊 Leaderboard — {label}",
        description=desc,
        colour=EMBED_COLOUR_GOLD,
    )

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
            cost = p.get("cost") or 0
            diff = p.get("diff") or 0
            sign = "+" if val >= 0 else ""
            diff_sign = "+" if diff >= 0 else ""
            val_str = f"{sign}{val:.2f} (cost {cost}, diff {diff_sign}{diff:.2f})"
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
        embed.add_field(name="\u200b", value="No data.", inline=False)
    else:
        # Split into multiple fields so we stay under Discord's 1024-char field limit.
        chunk: list[str] = []
        chunk_len = 0
        for line in lines:
            line_len = len(line) + 1  # +1 for the newline
            if chunk and chunk_len + line_len > 1000:
                embed.add_field(name="\u200b", value="\n".join(chunk), inline=False)
                chunk, chunk_len = [line], line_len
            else:
                chunk.append(line)
                chunk_len += line_len
        if chunk:
            embed.add_field(name="\u200b", value="\n".join(chunk), inline=False)

    embed.set_footer(text="Use /leaderboard <stat> to sort by a different stat · /player <name> for full details")
    return embed


# ---------------------------------------------------------------------------
# /player
# ---------------------------------------------------------------------------

def format_player_stats(p: dict, week_label: str = "All-Time") -> discord.Embed:
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

    embed = discord.Embed(
        title=f"🎮 {_display_name(p)}",
        url=dotabuff_url,
        description=f"{role_str} · {week_label} · {games_str} · {wins} win(s)",
        colour=EMBED_COLOUR_BLUE,
    )

    # Left column — combat & economy
    combat = (
        f"⚔️  Kills:      {p.get('total_kills', 0)}\n"
        f"💀 Deaths:    {p.get('total_deaths', 0)}\n"
        f"🤝 Assists:   {p.get('total_assists', 0)}\n"
        f"📊 KDA:        {p.get('kda', 0):.2f}\n"
    )
    embed.add_field(name="Combat", value=combat, inline=True)

    # Right column — economy & utility
    econ = (
        f"💰 GPM:        {p.get('gpm', 0):.0f}\n"
        f"📈 XPM:        {p.get('xpm', 0):.0f}\n"
        f"🌾 Last Hits: {p.get('last_hits', 0):,.2f}\n"
        f"🚫 Denies:    {p.get('denies', 0):,.2f}\n"
    )
    embed.add_field(name="Economy", value=econ, inline=True)

    # Impact
    pct_dmg = (p.get("avg_pct_damage") or 0) * 100
    impact = (
        f"💥 Hero Dmg:   {p.get('hero_damage', 0):,.0f}\n"
        f"💥 Dmg Share:  {pct_dmg:.1f}%\n"
        f"💚 Hero Heal:  {p.get('hero_healing', 0):,.0f}\n"
        f"⭐ Fantasy Pts: {p.get('fantasy_points', 0):.1f}\n"
    )
    embed.add_field(name="Impact", value=impact, inline=True)

    # Utility
    utility = (
        f"😴 Stuns:      {p.get('stuns_per_min', 0) or 0:.2f}/min\n"
        f"⚡ Teamfight:  {(p.get('teamfight_participation') or 0)*100:.0f}%\n"
        f"🏰 Towers:     {p.get('tower_kills', 0):.2f}\n"
        f"👁️  Obs Kills:  {p.get('observer_kills', 0):.2f}\n"
        f"📦 Stacks:     {p.get('camps_stacked', 0):.2f}\n"
        f"💎 Runes:      {p.get('rune_pickups', 0):.2f}\n"
    )
    embed.add_field(name="Utility", value=utility, inline=True)

    # Draft (only if we have cost data for this player this season)
    if p.get("cost"):
        cost = p["cost"]
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
        captain = p.get("captain") or "Unknown"
        mmr = p.get("mmr")
        mmr_str = f"{mmr}" if mmr else "N/A"
        draft = (
            f"💵 Cost:      {cost}\n"
            f"🎯 Expected:  {predicted_str} diff\n"
            f"💎 Value:     {value_str}\n"
            f"👑 Captain:   {captain}\n"
            f"📊 MMR:       {mmr_str}\n"
        )
        embed.add_field(name="Draft", value=draft, inline=True)

    embed.set_footer(text="Use /leaderboard to compare across all players")
    return embed


# ---------------------------------------------------------------------------
# /playerdiff
# ---------------------------------------------------------------------------

def format_player_diff(d: dict, week_label: str = "All-Time") -> discord.Embed:
    diff = d["diff"]
    sign = "+" if diff >= 0 else ""
    colour = EMBED_COLOUR_GREEN if diff >= 0 else EMBED_COLOUR_RED

    account_id = d.get("account_id", 0)
    dotabuff_url = f"https://www.dotabuff.com/players/{account_id}" if account_id else None

    embed = discord.Embed(
        title=f"📊 {_display_name(d)} vs. teammates",
        url=dotabuff_url,
        description=f"{week_label} · {d['games_played']} game(s)",
        colour=colour,
    )

    embed.add_field(
        name="Fantasy Points",
        value=(
            f"⭐ **{_display_name(d)}:** {d['player_avg_fp']:.1f}\n"
            f"👥 Teammate avg: {d['teammate_avg_fp']:.1f}\n"
            f"📈 **Diff:** {sign}{diff:.1f}"
        ),
        inline=False,
    )

    embed.add_field(
        name="Team Rank",
        value=(
            f"🥇 Top of team: {d['top_of_team_count']} game(s)\n"
            f"🪦 Bottom of team: {d['bottom_of_team_count']} game(s)"
        ),
        inline=False,
    )

    embed.set_footer(text="Per-match average vs. the 4 same-side teammates")
    return embed


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
    ranked_in_wr = _ranked_mmr_to_windrun(ranked_mmr) if ranked_mmr is not None else None

    if wr_rating is None and ranked_in_wr is None:
        return None
    if wr_rating is None:
        return (round(ranked_in_wr), "ranked-converted (no windrun data)")
    if ranked_in_wr is None:
        return (round(wr_rating), "windrun (no ranked data)")

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
    explanation = (
        f"{trust * 100:.0f}% windrun ({round(wr_rating)}) + "
        f"{(1 - trust) * 100:.0f}% ranked-eqv ({round(ranked_in_wr)})  "
        f"[{ad_str}{share_str}{lifetime_str}]"
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
) -> discord.Embed:
    """Build the /lookup embed. `windrun` is the raw dict from windrun.io's
    /players/{id} endpoint (or None). `opendota` is the raw dict from
    OpenDota's /players/{id} endpoint (or None). `ad_*` come from windrun's
    match list; `od_counts` is the dict from fetch_player_game_counts().
    `override` is a row from skill_overrides (or None); when set, its values
    take precedence over API data for display + internal-rating calc."""
    name = None
    if windrun:
        name = windrun.get("nickname")
    if not name and opendota:
        name = (opendota.get("profile") or {}).get("personaname")
    name = name or fallback_name or f"Player_{account_id}"

    dotabuff_url = f"https://www.dotabuff.com/players/{account_id}"
    windrun_url  = f"https://windrun.io/players/{account_id}"

    embed = discord.Embed(
        title=f"🔍 {name}",
        url=dotabuff_url,
        colour=EMBED_COLOUR_BLUE,
    )

    # Resolve overrides up front so display + internal-rating both see them.
    override_wr  = (override or {}).get("windrun_rating")
    override_mmr = (override or {}).get("ranked_mmr")

    if windrun:
        avatar = windrun.get("avatar")
        if avatar:
            embed.set_thumbnail(url=avatar)

        rating       = override_wr if override_wr is not None else windrun.get("rating")
        region       = (windrun.get("region") or "").title() or "?"
        overall_rank = windrun.get("overallRank")
        regional_rank = windrun.get("regionalRank")
        percentile   = windrun.get("percentile")
        wins         = windrun.get("wins") or 0
        losses       = windrun.get("losses") or 0

        rating_str = f"**{rating:.0f}**" if isinstance(rating, (int, float)) else "Unknown"
        if override_wr is not None:
            rating_str += " ⚠️"
        rank_bits = []
        if regional_rank:
            rank_bits.append(f"#{regional_rank} {region}")
        if overall_rank:
            rank_bits.append(f"#{overall_rank} overall")
        rank_line = " · ".join(rank_bits) if rank_bits else "Unranked"
        pct_str = f"_{percentile * 100:.2f}th percentile_" if isinstance(percentile, (int, float)) else ""

        wr_lines = [f"{rating_str} — {rank_line}"]
        if pct_str:
            wr_lines.append(pct_str)
        embed.add_field(name="🌬️ Windrun", value="\n".join(wr_lines), inline=False)
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

    # Ranked MMR — derived from OpenDota's rank_tier plus leaderboard_rank for
    # Immortals (log curve). Manual override (if set) replaces the estimate.
    from opendota_lookup import decode_rank_tier, estimate_mmr_from_rank_tier
    rank_tier = (opendota or {}).get("rank_tier")
    leaderboard_rank = (opendota or {}).get("leaderboard_rank")
    medal_str = decode_rank_tier(rank_tier, leaderboard_rank)
    api_mmr_est = estimate_mmr_from_rank_tier(rank_tier, leaderboard_rank)
    mmr_est = override_mmr if override_mmr is not None else api_mmr_est

    if override_mmr is not None:
        # Manual override: prefer the medal label if we have it, else just the number.
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

    # Game counts — Ability Draft (from windrun) and Ranked (from OpenDota).
    def _fmt_count(last_yr, all_time):
        ly = "?" if last_yr is None else str(last_yr)
        at = "?" if all_time is None else str(all_time)
        return f"**{ly}** last year · **{at}** all-time"
    ranked_last  = (od_counts or {}).get("last_year_ranked")
    ranked_total = (od_counts or {}).get("all_time_ranked")
    embed.add_field(
        name="📊 Games Played",
        value=(
            f"🎯 **AD:** {_fmt_count(ad_last_year, ad_all_time)}\n"
            f"⚔️ **Ranked:** {_fmt_count(ranked_last, ranked_total)}"
        ),
        inline=False,
    )

    # Internal rating — windrun-equiv true-skill estimate using trust-weighted average.
    effective_wr_rating = override_wr if override_wr is not None else (windrun or {}).get("rating")
    rating_info = _internal_rating(
        ad_last=ad_last_year,
        ad_all=ad_all_time,
        ranked_last=ranked_last,
        wr_rating=effective_wr_rating,
        ranked_mmr=mmr_est,
    )
    if rating_info:
        rating, explanation = rating_info
        embed.add_field(
            name="✨ Internal Rating",
            value=f"**{rating}** _(windrun-equiv)_\n_{explanation}_",
            inline=False,
        )

    # Override metadata footer intentionally omitted — the ⚠️ on overridden
    # values is enough of a memory aid without leaking internal notes publicly.
    embed.add_field(
        name="🔗 Profiles",
        value=f"[Dotabuff]({dotabuff_url}) · [Windrun]({windrun_url})",
        inline=False,
    )
    embed.set_footer(text=f"Account ID: {account_id}")
    return embed


# ---------------------------------------------------------------------------
# /suggested_cost
# ---------------------------------------------------------------------------

def format_suggested_costs(
    suggestions: list[dict],
    fit: tuple[float, float, float, float],
    mmr_weight: float = 1.0,
    single: bool = False,
) -> discord.Embed:
    """Render the suggested-cost list. `fit` is (a, b_d, b_f, b_m) for the
    forward model: cost = a + b_d·diff + b_f·fp + b_m·mmr. `mmr_weight`
    multiplies b_m (intercept is re-centered in the caller)."""
    a, b_d, b_f, b_m = fit
    if single:
        title = f"💰 Suggested Cost — {_display_name(suggestions[0])}"
    else:
        title = "💰 Suggested Costs"

    weight_str = (
        f" (MMR weight ×{mmr_weight:g})"
        if abs(mmr_weight - 1.0) > 1e-9 else ""
    )
    embed = discord.Embed(
        title=title,
        description=(
            f"Forward fit: cost ≈ {a:.2f} + {b_d:.3f}·diff + {b_f:.4f}·fp + "
            f"{b_m:.5f}·mmr{weight_str}\n"
            f"Drafted players only · MMR is windrun rating from the draft sheet."
        ),
        colour=EMBED_COLOUR_GOLD,
    )

    # Sort by suggested cost descending so the priciest projections are first.
    sorted_s = sorted(suggestions, key=lambda p: -p["suggested_cost"])

    lines = []
    for p in sorted_s:
        sug = p["suggested_cost"]
        actual = p.get("actual_cost")
        if actual is not None:
            delta = sug - actual
            sign = "+" if delta >= 0 else ""
            tail = f"(actual {actual}, {sign}{delta})"
        else:
            tail = "(undrafted)"
        diff = p.get("diff", 0) or 0
        fp = p.get("fp", 0) or 0
        diff_sign = "+" if diff >= 0 else ""
        mmr = p.get("mmr")
        lines.append(
            f"**{_display_name(p)}** — **{sug}** {tail} · "
            f"{p['games_played']}g · {fp:.1f} fp · {diff_sign}{diff:.2f} diff · mmr {mmr}"
        )

    if not lines:
        embed.add_field(name="​", value="No data.", inline=False)
    else:
        # Pack into ≤1000-char fields to dodge Discord's 1024 limit.
        chunk: list[str] = []
        chunk_len = 0
        for line in lines:
            line_len = len(line) + 1
            if chunk and chunk_len + line_len > 1000:
                embed.add_field(name="​", value="\n".join(chunk), inline=False)
                chunk, chunk_len = [line], line_len
            else:
                chunk.append(line)
                chunk_len += line_len
        if chunk:
            embed.add_field(name="​", value="\n".join(chunk), inline=False)

    embed.set_footer(text="Higher suggestion = produced more FP than their cost predicts")
    return embed


# ---------------------------------------------------------------------------
# /roles
# ---------------------------------------------------------------------------

def format_roles_summary(
    stats: list[dict],
    week_label: str = "Week 1",
    threshold: int | None = None,
    max_games: int | None = None,
) -> discord.Embed:
    """Show the best player per role, judged by fantasy points."""
    if threshold is not None and max_games is not None:
        desc = f"**{week_label}** — top fantasy points performer at each position (≥ {threshold} of {max_games} games)"
    else:
        desc = f"**{week_label}** — top fantasy points performer at each position"

    embed = discord.Embed(
        title="🗺️  Best by Role",
        description=desc,
        colour=EMBED_COLOUR_PURPLE,
    )

    # Group by role_position
    by_role: dict[int, list[dict]] = {}
    for p in stats:
        role = p.get("role_position") or 0
        by_role.setdefault(role, []).append(p)

    for pos in [1, 2, 3, 4, 5]:
        label = ROLE_LABELS.get(pos, f"Position {pos}")
        players = sorted(by_role.get(pos, []), key=lambda x: x.get("fantasy_points", 0), reverse=True)
        if players:
            best = players[0]
            value = (
                f"**{_display_name(best)}**\n"
                f"⭐ {best.get('fantasy_points', 0):.1f} pts · "
                f"KDA {best.get('kda', 0):.1f} · "
                f"GPM {best.get('gpm', 0):.0f}"
            )
        else:
            value = "*No data*"
        embed.add_field(name=label, value=value, inline=True)

    # If there are players with no role data
    unkeyed = by_role.get(0, [])
    if unkeyed:
        best = sorted(unkeyed, key=lambda x: x.get("fantasy_points", 0), reverse=True)[0]
        embed.add_field(
            name="❓ Unknown Role",
            value=f"**{_display_name(best)}** — ⭐ {best.get('fantasy_points', 0):.1f} pts",
            inline=True,
        )

    embed.set_footer(text="Roles are assigned by positional slot in the match")
    return embed


# ---------------------------------------------------------------------------
# Weekly auto-post summary
# ---------------------------------------------------------------------------

def format_weekly_summary(stats: list[dict]) -> discord.Embed:
    """A concise summary embed that gets auto-posted on Monday mornings."""
    if not stats:
        return discord.Embed(title="📊 Weekly Summary", description="No matches found this week.", colour=EMBED_COLOUR_GREEN)

    # Top players by different stats
    top_fp   = max(stats, key=lambda p: p.get("fantasy_points", 0))
    top_gpm  = max(stats, key=lambda p: p.get("gpm", 0))
    top_kda  = max(stats, key=lambda p: p.get("kda", 0))
    top_dmg  = max(stats, key=lambda p: p.get("hero_damage", 0))

    total_games = max(p.get("games_played", 0) for p in stats)  # all played same matches

    embed = discord.Embed(
        title="📊 Weekly Stats Summary",
        description=f"**{total_games} match(es)** tracked this week across {len(stats)} players.",
        colour=EMBED_COLOUR_GREEN,
    )

    highlights = (
        f"⭐ **Best Fantasy Pts:** {_display_name(top_fp)} — {top_fp.get('fantasy_points', 0):.1f} pts\n"
        f"💰 **Highest GPM:**     {_display_name(top_gpm)} — {top_gpm.get('gpm', 0):.0f}\n"
        f"⚔️  **Best KDA:**         {_display_name(top_kda)} — {top_kda.get('kda', 0):.1f}\n"
        f"💥 **Most Hero Dmg:**   {_display_name(top_dmg)} — {top_dmg.get('hero_damage', 0):,}\n"
    )
    embed.add_field(name="🏆 Highlights", value=highlights, inline=False)

    embed.set_footer(text="Use /leaderboard or /player for full details")
    return embed


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
