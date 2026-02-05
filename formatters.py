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

# Human-readable labels for stat keys
STAT_LABELS: dict[str, str] = {
    "fantasy_points": "⭐ Fantasy Pts",
    "gpm":            "💰 GPM",
    "kda":            "⚔️  KDA",
    "last_hits":      "🌾 Last Hits",
    "denies":         "🚫 Denies",
    "hero_damage":    "💥 Hero Damage",
    "hero_healing":   "💚 Hero Healing",
    "xpm":            "📈 XPM",
}

MEDAL = ["🥇", "🥈", "🥉"]


def _week_label(week_offset: int) -> str:
    if week_offset == 0:
        return "This Week"
    if week_offset == 1:
        return "Last Week"
    return f"{week_offset} Weeks Ago"


def _sort_key(player: dict, sort_by: str) -> float:
    """Return the numeric value to sort on. Some stats are totals, some averages."""
    return player.get(sort_by, 0) or 0


# ---------------------------------------------------------------------------
# /leaderboard
# ---------------------------------------------------------------------------

def format_leaderboard(stats: list[dict], sort_by: str = "fantasy_points", week_label: str = "Latest Week") -> discord.Embed:
    label = STAT_LABELS.get(sort_by, sort_by)
    sorted_players = sorted(stats, key=lambda p: _sort_key(p, sort_by), reverse=True)

    embed = discord.Embed(
        title=f"📊 Leaderboard — {label}",
        description=f"**{week_label}** · {len(stats)} players across all matches",
        colour=EMBED_COLOUR_GOLD,
    )

    lines = []
    for i, p in enumerate(sorted_players[:10]):  # top 10
        medal = MEDAL[i] if i < 3 else f"**{i+1}.**"
        val = p.get(sort_by, 0)
        # Format nicely depending on type
        if sort_by in ("gpm", "xpm", "kda"):
            val_str = f"{val:.1f}"
        elif sort_by == "fantasy_points":
            val_str = f"{val:.1f} pts"
        else:
            val_str = f"{int(val):,}"

        # Make name a clickable Dotabuff link
        account_id = p.get("account_id", 0)
        if account_id:
            player_link = f"[{p['name']}](https://www.dotabuff.com/players/{account_id})"
        else:
            player_link = f"**{p['name']}**"

        games = p.get("games_played", 0)
        lines.append(f"{medal} {player_link} — {val_str} — {games} game{'s' if games != 1 else ''}")

    embed.add_field(name="\u200b", value="\n".join(lines) or "No data.", inline=False)

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

    # Build Dotabuff URL if we have an account ID
    dotabuff_url = f"https://www.dotabuff.com/players/{account_id}" if account_id else None

    embed = discord.Embed(
        title=f"🎮 {p['name']}",
        url=dotabuff_url,
        description=f"{role_str} · {week_label} · {games} game(s) played · {wins} win(s)",
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
        f"🌾 Last Hits: {p.get('last_hits', 0):,}\n"
        f"🚫 Denies:    {p.get('denies', 0):,}\n"
    )
    embed.add_field(name="Economy", value=econ, inline=True)

    # Impact
    impact = (
        f"💥 Hero Dmg:   {p.get('hero_damage', 0):,}\n"
        f"💚 Hero Heal:  {p.get('hero_healing', 0):,}\n"
        f"⭐ Fantasy Pts: {p.get('fantasy_points', 0):.1f}\n"
    )
    embed.add_field(name="Impact", value=impact, inline=True)

    embed.set_footer(text="Use /leaderboard to compare across all players")
    return embed


# ---------------------------------------------------------------------------
# /roles
# ---------------------------------------------------------------------------

def format_roles_summary(stats: list[dict], week_label: str = "Week 1") -> discord.Embed:
    """Show the best player per role, judged by fantasy points."""
    embed = discord.Embed(
        title="🗺️  Best by Role",
        description=f"**{week_label}** — top fantasy points performer at each position",
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
                f"**{best['name']}**\n"
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
            value=f"**{best['name']}** — ⭐ {best.get('fantasy_points', 0):.1f} pts",
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
        f"⭐ **Best Fantasy Pts:** {top_fp['name']} — {top_fp.get('fantasy_points', 0):.1f} pts\n"
        f"💰 **Highest GPM:**     {top_gpm['name']} — {top_gpm.get('gpm', 0):.0f}\n"
        f"⚔️  **Best KDA:**         {top_kda['name']} — {top_kda.get('kda', 0):.1f}\n"
        f"💥 **Most Hero Dmg:**   {top_dmg['name']} — {top_dmg.get('hero_damage', 0):,}\n"
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
        lines.append(f"{medal} {p['name']} — {val_str}")

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
