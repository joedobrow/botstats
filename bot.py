import asyncio
import os
import discord
from discord import app_commands
from discord.ext import tasks
import logging
from datetime import datetime, timezone, timedelta

from config import DISCORD_TOKEN, ADMIN_USER_ID, REGION_CLUSTERS, GAME_MODE_FILTERS, LOOKUP_CHANNEL_IDS
from fetcher import fetch_and_store_matches_for_division
from db import init_db, get_division, upsert_division, get_all_divisions, get_scold_channel
from formatters import format_leaderboard, format_player_stats, format_team_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # required for /sync_roles_channels' guild.fetch_members
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


# ---------------------------------------------------------------------------
# Helper to check division config
# ---------------------------------------------------------------------------

def _require_division(interaction: discord.Interaction):
    """Get division for this guild, or None if not configured."""
    return get_division(interaction.guild_id)


# Commands reply privately (ephemeral) by default; public=True posts to the channel instead
PUBLIC_PARAM_DESCRIPTION = "Post the result in the channel instead of only showing it to you (default: False)"


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

async def _season_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete for every `season:` param. Values are the season_start date
    (the key everything else is stored under); names are the friendly label."""
    from db import get_seasons, season_label, get_division
    try:
        seasons = get_seasons(interaction.guild_id)
        div = get_division(interaction.guild_id)
        active = div["season_start"] if div else None
        out = []
        for s in seasons:
            name = season_label(s)
            if s["season_start"] == active:
                name += "  (current)"
            if current.lower() in name.lower() or current in s["season_start"]:
                out.append(app_commands.Choice(name=name[:100], value=s["season_start"]))
        return out[:25]
    except Exception:
        logger.exception("season autocomplete failed")
        return []


def _resolve_season(interaction: discord.Interaction, season: str | None) -> tuple[str | None, str | None]:
    """Turn a `season:` param into a season_start string.

    Returns (season_start, error). season=None means "the division's current
    season", which is the pre-existing behaviour of every command.
    """
    division = _require_division(interaction)
    if not division:
        return None, "\u26a0\ufe0f No division configured. Ask an admin to run `/config` first."
    if not season:
        return division["season_start"], None
    from db import get_seasons
    known = {s["season_start"] for s in get_seasons(interaction.guild_id)}
    if season not in known:
        opts = ", ".join(f"`{k}`" for k in sorted(known, reverse=True)[:8]) or "none on file"
        return None, f"\u26a0\ufe0f Unknown season `{season}`. Known seasons: {opts}"
    return season, None


@tree.command(name="config", description="[Admin] Configure this server's division settings")
@app_commands.describe(
    league="OpenDota league ID (find it in the league URL)",
    region="Server region for filtering matches",
    mode="Game mode filter",
    season_start="Season start date (YYYY-MM-DD format)",
    scold_channel="Channel where all messages get deleted with a scolding reply"
)
@app_commands.choices(
    region=[
        app_commands.Choice(name="US West", value="us_west"),
        app_commands.Choice(name="US East", value="us_east"),
        app_commands.Choice(name="Any Region", value="any"),
    ],
    mode=[
        app_commands.Choice(name="Captain's Mode (exclude Ability Draft)", value="cm"),
        app_commands.Choice(name="Ability Draft only", value="ad"),
    ]
)
async def config(
    interaction: discord.Interaction,
    league: int = None,
    region: str = None,
    mode: str = None,
    season_start: str = None,
    scold_channel: discord.TextChannel = None
):
    # Only server admins or bot owner can configure
    is_admin = interaction.user.guild_permissions.administrator
    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    if not is_admin and not is_owner:
        await interaction.response.send_message("⚠️ Only server admins can configure divisions.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    current = get_division(guild_id)

    # If no parameters, show current config
    if league is None and region is None and mode is None and season_start is None and scold_channel is None:
        if current:
            region_display = {"us_west": "US West", "us_east": "US East", "any": "Any Region"}.get(current["region"], current["region"])
            mode_display = "Captain's Mode" if current["game_mode"] == "cm" else "Ability Draft"
            scold_display = f"<#{current['scold_channel_id']}>" if current.get("scold_channel_id") else "None"
            await interaction.response.send_message(
                f"**Current Division Config**\n"
                f"League ID: `{current['league_id']}`\n"
                f"Region: `{region_display}`\n"
                f"Mode: `{mode_display}`\n"
                f"Season Start: `{current['season_start']}`\n"
                f"Scold Channel: {scold_display}",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "No division configured for this server.\n"
                "Use `/config league:<id> region:<region> mode:<mode> season_start:<date>` to set up.",
                ephemeral=True
            )
        return

    # Creating or updating - need all fields for new config
    if not current:
        # New config - all fields required
        if not all([league, region, mode, season_start]):
            await interaction.response.send_message(
                "⚠️ For initial setup, all fields are required:\n"
                "`/config league:<id> region:<us_west|us_east> mode:<cm|ad> season_start:<YYYY-MM-DD>`",
                ephemeral=True
            )
            return
    else:
        # Updating - use existing values for missing fields
        league = league or current["league_id"]
        region = region or current["region"]
        mode = mode or current["game_mode"]
        season_start = season_start or current["season_start"]

    # Resolve scold channel ID (use provided, or keep existing)
    scold_channel_id = scold_channel.id if scold_channel else (current.get("scold_channel_id") if current else None)

    # Validate season_start format
    try:
        datetime.strptime(season_start, "%Y-%m-%d")
    except ValueError:
        await interaction.response.send_message(
            "⚠️ Invalid date format. Use YYYY-MM-DD (e.g., 2026-01-26)",
            ephemeral=True
        )
        return

    # Save config
    upsert_division(guild_id, league, region, mode, season_start, scold_channel_id)

    region_display = {"us_west": "US West", "us_east": "US East", "any": "Any Region"}.get(region, region)
    mode_display = "Captain's Mode" if mode == "cm" else "Ability Draft"
    scold_display = f"<#{scold_channel_id}>" if scold_channel_id else "None"

    await interaction.response.send_message(
        f"✅ Division configured!\n"
        f"League ID: `{league}`\n"
        f"Region: `{region_display}`\n"
        f"Mode: `{mode_display}`\n"
        f"Season Start: `{season_start}`\n"
        f"Scold Channel: {scold_display}\n\n"
        f"Run `/refresh_leaderboard` to fetch match data.",
        ephemeral=True
    )


def _can_manage_weeks(interaction: discord.Interaction) -> bool:
    is_admin = interaction.user.guild_permissions.administrator
    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    return bool(is_admin or is_owner)


@tree.command(name="generate_weeks", description="[Admin] Bulk-create season week boundaries as standard 7-day blocks")
@app_commands.describe(weeks="Number of weeks to generate (starting from week 1)")
async def generate_weeks(interaction: discord.Interaction, weeks: int):
    if not _can_manage_weeks(interaction):
        await interaction.response.send_message("⚠️ Only server admins can manage season weeks.", ephemeral=True)
        return

    division = _require_division(interaction)
    if not division:
        await interaction.response.send_message("⚠️ No division configured. Run `/config` first.", ephemeral=True)
        return
    if weeks < 1 or weeks > 30:
        await interaction.response.send_message("⚠️ weeks must be between 1 and 30.", ephemeral=True)
        return

    from db import generate_season_weeks
    generate_season_weeks(interaction.guild_id, division["season_start"], weeks)
    await interaction.response.send_message(
        f"✅ Generated weeks 1–{weeks} as standard 7-day blocks from season start `{division['season_start']}`.\n"
        f"Use `/set_week` to adjust any individual week (e.g. a bye), and `/list_weeks` to review.",
        ephemeral=True,
    )


@tree.command(name="set_week", description="[Admin] Define or override one season week's date range")
@app_commands.describe(
    week="Week number (0 = pre-season, 1+ = normal weeks)",
    start="Start date, YYYY-MM-DD (inclusive)",
    end="End date, YYYY-MM-DD (inclusive)",
)
async def set_week(interaction: discord.Interaction, week: int, start: str, end: str):
    if not _can_manage_weeks(interaction):
        await interaction.response.send_message("⚠️ Only server admins can manage season weeks.", ephemeral=True)
        return

    division = _require_division(interaction)
    if not division:
        await interaction.response.send_message("⚠️ No division configured. Run `/config` first.", ephemeral=True)
        return

    try:
        start_dt = datetime.strptime(start, "%Y-%m-%d").replace(hour=0, minute=0, second=0, tzinfo=timezone.utc)
        end_dt = datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    except ValueError:
        await interaction.response.send_message("⚠️ Invalid date format. Use YYYY-MM-DD.", ephemeral=True)
        return
    if end_dt <= start_dt:
        await interaction.response.send_message("⚠️ end must be after start.", ephemeral=True)
        return

    from db import upsert_season_week
    upsert_season_week(
        interaction.guild_id, division["season_start"], week,
        int(start_dt.timestamp()), int(end_dt.timestamp()),
    )
    await interaction.response.send_message(f"✅ Week {week} set to `{start}` → `{end}`.", ephemeral=True)


@tree.command(name="delete_week", description="[Admin] Remove one season week's explicit definition")
@app_commands.describe(week="Week number to remove")
async def delete_week(interaction: discord.Interaction, week: int):
    if not _can_manage_weeks(interaction):
        await interaction.response.send_message("⚠️ Only server admins can manage season weeks.", ephemeral=True)
        return

    division = _require_division(interaction)
    if not division:
        await interaction.response.send_message("⚠️ No division configured. Run `/config` first.", ephemeral=True)
        return

    from db import delete_season_week
    removed = delete_season_week(interaction.guild_id, division["season_start"], week)
    if removed:
        await interaction.response.send_message(f"✅ Removed week {week}'s explicit definition.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ Week {week} wasn't explicitly defined.", ephemeral=True)


@tree.command(name="list_weeks", description="Show every explicitly-defined season week and its date range")
async def list_weeks(interaction: discord.Interaction):
    division = _require_division(interaction)
    if not division:
        await interaction.response.send_message("⚠️ No division configured. Run `/config` first.", ephemeral=True)
        return

    from db import get_season_weeks, get_current_season_week
    weeks = get_season_weeks(interaction.guild_id, division["season_start"])
    if not weeks:
        await interaction.response.send_message(
            "No explicit weeks defined yet — week boundaries are calculated automatically from season_start.\n"
            "Run `/generate_weeks weeks:<N>` to define them explicitly (needed to handle bye weeks).",
            ephemeral=True,
        )
        return

    current = get_current_season_week(interaction.guild_id, division["season_start"])
    lines = []
    for w in weeks:
        # Week boundaries are stored as UTC midnight Monday -> UTC Sunday
        # 23:59, so their date label is only correct rendered in UTC. Match
        # timestamps (real moments) are shown in LEAGUE_TZ elsewhere.
        start_d = datetime.fromtimestamp(w["start_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        end_d = datetime.fromtimestamp(w["end_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        marker = " ← current" if w["week_number"] == current else ""
        lines.append(f"Week {w['week_number']}: `{start_d}` → `{end_d}`{marker}")

    await interaction.response.send_message("**Season Weeks**\n" + "\n".join(lines), ephemeral=True)


@tree.command(name="leaderboard", description="Show the leaderboard sorted by a stat")
@app_commands.describe(
    season="Which season to look at (defaults to the current one)",
    week="Season week number (1, 2, 3...) or -1 for all-time. Leave blank for the current week.",
    stat="Which stat to sort by",
    pos="Filter by position (1-5, optional)",
    all="Show every player (no top-10 cap, no min-games threshold)",
    debug="[Owner only] expose value components (cost, diff)",
    public=PUBLIC_PARAM_DESCRIPTION,
)
@app_commands.choices(stat=[
    app_commands.Choice(name="Fantasy Points",          value="fantasy_points"),
    app_commands.Choice(name="Value (FP / Draft Cost)", value="value"),
    app_commands.Choice(name="Attendance",              value="attendance"),
    app_commands.Choice(name="Fantasy Diff vs. Teammates", value="diff"),
    app_commands.Choice(name="GPM",                     value="gpm"),
    app_commands.Choice(name="KDA",                     value="kda"),
    app_commands.Choice(name="Last Hits",               value="last_hits"),
    app_commands.Choice(name="Denies",                  value="denies"),
    app_commands.Choice(name="Damage Dealt",            value="hero_damage"),
    app_commands.Choice(name="Damage Share %",          value="avg_pct_damage"),
    app_commands.Choice(name="Healing Done",            value="hero_healing"),
    app_commands.Choice(name="XPM",                     value="xpm"),
    app_commands.Choice(name="Stuns (per min)",          value="stuns_per_min"),
    app_commands.Choice(name="Teamfight Participation", value="teamfight_participation"),
    app_commands.Choice(name="Defensive Item Uses",     value="defensive_item_uses"),
    app_commands.Choice(name="Tower Kills",             value="tower_kills"),
    app_commands.Choice(name="Observer Kills/min",      value="observer_kills_per_min"),
    app_commands.Choice(name="Roshans Killed",          value="roshans_killed"),
    app_commands.Choice(name="Camp Stacks",             value="camps_stacked"),
    app_commands.Choice(name="Rune Pickups",            value="rune_pickups"),
    app_commands.Choice(name="First Blood Rate",        value="firstblood_claimed"),
    app_commands.Choice(name="Tormentor Kills",         value="tormentor_kills"),
    app_commands.Choice(name="Watcher Captures",        value="watcher_captures"),
    app_commands.Choice(name="First Tormentor Time",    value="avg_first_tormentor_time"),
    app_commands.Choice(name="Avg Game Length",         value="avg_duration"),
], pos=[
    app_commands.Choice(name="Position 1 (Safe Lane)", value=1),
    app_commands.Choice(name="Position 2 (Mid)", value=2),
    app_commands.Choice(name="Position 3 (Off Lane)", value=3),
    app_commands.Choice(name="Position 4 (Roaming)", value=4),
    app_commands.Choice(name="Position 5 (Hard Support)", value=5),
])
@app_commands.autocomplete(season=_season_autocomplete)
async def leaderboard(interaction: discord.Interaction, stat: app_commands.Choice[str], week: int = None, pos: int = None, all: bool = False, season: str = None, debug: bool = False, public: bool = False):
    await interaction.response.defer(ephemeral=not public)

    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    debug = bool(debug) and is_owner

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    season_start, season_err = _resolve_season(interaction, season)
    if season_err:
        await interaction.followup.send(season_err, ephemeral=True)
        return

    from db import get_stats_for_season_week, get_all_time_stats, get_current_season_week

    try:
        if week == -1:
            # All-time spans every season the guild has recorded. The two
            # draft-relative stats stay season-scoped: a "deserved cost" or an
            # attendance rate only means anything measured against one draft.
            draft_relative = stat.value in ("value", "attendance")
            stats = get_all_time_stats(guild_id, season_start,
                                       cross_season=not draft_relative)
            week_label = "All-Time (this season)" if draft_relative else "All-Time (all seasons)"
        else:
            # Blank week defaults to the current season week (not all-time —
            # pass week:-1 explicitly for that).
            resolved_week = week if week is not None else get_current_season_week(guild_id, season_start)
            stats = get_stats_for_season_week(guild_id, resolved_week, season_start)
            week_label = f"Week {resolved_week}"

        # value/attendance lean on draft data: filter to drafted-only players
        # (cost set) and skip the min-games threshold. Other stats apply a
        # min-games qualification. Either way, top 10 unless all=True.
        draft_only_stats = {"value", "attendance"}
        if stat.value in draft_only_stats:
            stats = [s for s in stats if s.get("cost") is not None]
            threshold = None
            max_games = None
        elif all:
            threshold = None
            max_games = None
        else:
            # Min-games qualification: 50% of the most-active player's games, rounded down
            max_games = max((s.get("games_played", 0) or 0) for s in stats) if stats else 0
            threshold = max_games // 2
            stats = [s for s in stats if (s.get("games_played", 0) or 0) >= threshold]

        limit = None if all else 10

        # Filter by position if specified
        if pos is not None:
            stats = [s for s in stats if s.get("role_position") == pos]
            week_label += f" (Position {pos})"

        if not stats:
            await interaction.followup.send(f"⚠️ No data found for {week_label.lower()}. If this seems wrong, the request may have timed out — please try again.", ephemeral=True)
            return

        embeds = format_leaderboard(stats, sort_by=stat.value, week_label=week_label, threshold=threshold, max_games=max_games, limit=limit, debug=debug)
        for embed in embeds:
            await interaction.followup.send(embed=embed, ephemeral=not public)
    except Exception as e:
        logger.exception(f"Error in leaderboard command for week {week}")
        await interaction.followup.send(f"❌ Error loading leaderboard: {str(e)}", ephemeral=True)


@tree.command(name="player", description="Show detailed stats for a specific player")
@app_commands.describe(
    season="Which season to look at (defaults to the current one)",
    name="Player name (partial match), override nickname, or numeric account ID",
    week="Season week number (1, 2, 3...) or -1 for all-time. Leave blank for the current week.",
    public=PUBLIC_PARAM_DESCRIPTION,
)
@app_commands.autocomplete(season=_season_autocomplete)
async def player(interaction: discord.Interaction, name: str, week: int = None, season: str = None, public: bool = False):
    await interaction.response.defer(ephemeral=not public)
    debug = False

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    # Resolve name → account_id using the same logic as /lookup, so anyone
    # findable in /lookup is findable here.
    account_id, resolved_name, err = await _resolve_player_query(interaction, name)
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    guild_id = interaction.guild_id
    season_start, season_err = _resolve_season(interaction, season)
    if season_err:
        await interaction.followup.send(season_err, ephemeral=True)
        return

    from db import get_stats_for_season_week, get_all_time_stats, get_current_season_week

    try:
        if week == -1:
            # Matches /leaderboard: a player's all-time line spans every
            # season. Cost/value in the Draft block stay season-scoped.
            stats = get_all_time_stats(guild_id, season_start, cross_season=True)
            week_label = "All-Time (all seasons)"
        else:
            # Blank week defaults to the current season week (not all-time —
            # pass week:-1 explicitly for that).
            week = week if week is not None else get_current_season_week(guild_id, season_start)
            stats = get_stats_for_season_week(guild_id, week, season_start)
            week_label = f"Week {week}"

        match_row = next((p for p in stats if p.get("account_id") == account_id), None)

        # Look up the player's internal rating from the cache and apply the
        # same fantasy adjustment that /players and /lookup use. For alt
        # accounts, use the main's account_id for the adjustment lookup so
        # the displayed rating matches the main's exactly.
        from db import get_rating_cache_row, compute_fantasy_adjusted_ratings
        rating: int | None = None
        cache_row = get_rating_cache_row(account_id)
        if cache_row and cache_row.get("internal_rating") is not None:
            rating = cache_row["internal_rating"]
            adjustments = compute_fantasy_adjusted_ratings(
                interaction.guild_id, division["season_start"],
            )
            effective_id = cache_row.get("alt_account_for") or account_id
            adj = adjustments.get(effective_id)
            if adj:
                rating = adj["adjusted_rating"]

        if match_row is None:
            # No matches in this guild — still surface the rating, links, and
            # ID. If we have no cached rating, fall back to a live API lookup
            # (best-effort; nothing saved to the DB).
            avatar_url: str | None = (cache_row or {}).get("avatar_url")
            display_name = (
                (cache_row or {}).get("override_nickname")
                or (cache_row or {}).get("name")
                or resolved_name
            )

            if rating is None:
                try:
                    from windrun import fetch_player as fetch_windrun_player
                    from opendota_lookup import fetch_player_profile, fetch_player_game_counts
                    from db import get_skill_override
                    from formatters import _resolve_internal_rating, _rank_to_windrun
                    import asyncio as _asyncio
                    results = await _asyncio.gather(
                        fetch_windrun_player(account_id),
                        fetch_player_profile(account_id),
                        fetch_player_game_counts(account_id),
                        return_exceptions=True,
                    )
                    wr, od, od_counts = (
                        None if isinstance(r, Exception) else r for r in results
                    )
                    from formatters import windrun_rating_for_formula, sanitize_od_counts
                    od_counts = sanitize_od_counts(od, od_counts)
                    override = get_skill_override(account_id)
                    raw_wr = (wr or {}).get("rating")
                    formula_wr = windrun_rating_for_formula(wr)
                    rank_tier = (od or {}).get("rank_tier")
                    lb_rank   = (od or {}).get("leaderboard_rank")
                    eff_wr  = (override["windrun_rating"]
                               if override and override.get("windrun_rating") is not None
                               else formula_wr)
                    eff_rank_tier = override["rank_tier"] if override and override.get("rank_tier") is not None else rank_tier
                    eff_lb_rank   = override["leaderboard_rank"] if override and override.get("rank_tier") is not None else lb_rank
                    ranked_in_wr_raw = _rank_to_windrun(eff_rank_tier, eff_lb_rank)
                    ad_last = (od_counts or {}).get("last_year_ad")
                    ad_all  = (od_counts or {}).get("all_time_ad")
                    ranked_last = (od_counts or {}).get("last_year_ranked")
                    ranked_all  = (od_counts or {}).get("all_time_ranked")
                    info = _resolve_internal_rating(
                        override=override,
                        ad_last=ad_last, ad_all=ad_all,
                        ranked_last=ranked_last, ranked_all=ranked_all,
                        wr_rating=eff_wr, ranked_in_wr_raw=ranked_in_wr_raw,
                    )
                    if info:
                        rating = info[0]
                    if not display_name:
                        display_name = (
                            (wr or {}).get("nickname")
                            or ((od or {}).get("profile") or {}).get("personaname")
                        )
                    if not avatar_url:
                        avatar_url = (
                            ((od or {}).get("profile") or {}).get("avatarfull")
                            or (wr or {}).get("avatar")
                        )
                    # Cache this live lookup so /api/ratings (and future /player
                    # calls) don't need to recompute it — mirrors /lookup's
                    # write-back for players with no cached row yet.
                    if info:
                        from db import upsert_rating_cache_row
                        upsert_rating_cache_row({
                            "account_id":       account_id,
                            "name":             display_name,
                            "internal_rating":  info[0],
                            "raw_windrun":      raw_wr,
                            "rank_tier":        rank_tier,
                            "leaderboard_rank": lb_rank,
                            "ad_last_year":     ad_last,
                            "ad_all_time":      ad_all,
                            "ranked_last_year": ranked_last,
                            "avatar_url":       avatar_url,
                        })
                except Exception:
                    logger.exception("Live lookup failed in /player for %d", account_id)

            display = display_name or f"Account {account_id}"
            from formatters import EMBED_COLOUR_BLUE
            title = f"🎮 {display}"
            if rating is not None:
                title += f"  •  Rating {rating}"
            dotabuff_url = f"https://www.dotabuff.com/players/{account_id}"
            windrun_url  = f"https://windrun.io/players/{account_id}"
            embed = discord.Embed(
                title=title,
                url=dotabuff_url,
                colour=EMBED_COLOUR_BLUE,
            )
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)
            embed.add_field(
                name="🔗 Profiles",
                value=f"[Dotabuff]({dotabuff_url}) · [Windrun]({windrun_url})",
                inline=False,
            )
            if (cache_row or {}).get("fh_unavailable"):
                embed.add_field(
                    name="⚠️ Match history hidden",
                    value="Steam match history is private, so game counts are unavailable.",
                    inline=False,
                )
            embed.set_footer(text=f"Account ID: {account_id}")
            await interaction.followup.send(embed=embed, ephemeral=not public)
            return

        # Pool of league players with enough games to give meaningful percentiles.
        qualified_pool = [s for s in stats if (s.get("games_played") or 0) >= 5]
        embed = format_player_stats(
            match_row, week_label=week_label, debug=debug, rating=rating,
            qualified_pool=qualified_pool,
        )
        if (cache_row or {}).get("fh_unavailable"):
            embed.add_field(
                name="⚠️ Match history hidden",
                value="Steam match history is private, so OpenDota game counts are unavailable.",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=not public)
    except Exception as e:
        logger.exception(f"Error in player command for week {week}")
        await interaction.followup.send(f"❌ Error loading player stats: {str(e)}", ephemeral=True)


@tree.command(name="team_stats", description="Show strengths/weaknesses of a player's team vs the rest of the league")
@app_commands.describe(name="Any player on the team — partial name, override nickname, or numeric account ID")
@app_commands.autocomplete(season=_season_autocomplete)
@app_commands.describe(
    season="Which season to look at (defaults to the current one)",
    public=PUBLIC_PARAM_DESCRIPTION,
)
async def team_stats(interaction: discord.Interaction, name: str, season: str = None, public: bool = False):
    await interaction.response.defer(ephemeral=not public)

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured.", ephemeral=True)
        return

    season_start, season_err = _resolve_season(interaction, season)
    if season_err:
        await interaction.followup.send(season_err, ephemeral=True)
        return

    account_id, resolved_name, err = await _resolve_player_query(interaction, name)
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    from db import get_player_costs, get_team_match_aggregates, get_captain_account_ids
    costs = get_player_costs(interaction.guild_id, season_start)
    target_cost = costs.get(account_id)
    captain: str | None = None
    if target_cost and target_cost.get("captain"):
        captain = target_cost["captain"]
    else:
        # Maybe they ARE a captain — look up reverse mapping.
        captain_aids = get_captain_account_ids(
            interaction.guild_id, season_start,
        )
        for cap_name, cap_aid in captain_aids.items():
            if cap_aid == account_id:
                captain = cap_name
                break

    if not captain:
        await interaction.followup.send(
            f"⚠️ `{resolved_name or account_id}` isn't on a team this season — not drafted and not a known captain.",
            ephemeral=True,
        )
        return
    team_aggs = get_team_match_aggregates(interaction.guild_id, season_start)
    target_agg = team_aggs.get(captain)
    if not target_agg:
        await interaction.followup.send(
            f"⚠️ Team `{captain}` has no game data yet.",
            ephemeral=True,
        )
        return

    from team_metadata import get_team_info
    team_label = get_team_info(captain, interaction.guild_id, season_start).get("team_name") or captain
    embed = format_team_stats(
        team_label=team_label,
        target_agg=target_agg,
        all_team_aggs=list(team_aggs.values()),
    )
    await interaction.followup.send(embed=embed, ephemeral=not public)


# Shortened stat labels for stream-card display only. Falls back to the
# long label when no shortening is defined.
_CARD_SHORT_LABELS: dict[str, str] = {
    "Fantasy Points":            "Fantasy Pts",
    "Fantasy Diff vs Teammates": "FP Diff",
    "GPM":                       "GPM",
    "XPM":                       "XPM",
    "KDA":                       "KDA",
    "Last Hits/game":            "Avg Last Hits",
    "Denies/game":               "Avg Denies",
    "Hero Damage/game":          "Avg Hero Dmg",
    "Damage Share %":            "Dmg Share %",
    "Hero Healing/game":         "Avg Hero Heal",
    "Teamfight %":               "TF Part %",
    "Stuns/Min":                 "Stuns/Min",
    "Tower Kills/game":          "Avg Towers",
    "Obs Kills/Min":             "Obs Kills/Min",
    "Roshans Killed/game":       "Avg Roshes",
    "Camp Stacks/game":          "Avg Stacks",
    "Rune Pickups/game":         "Avg Runes",
    "Defensive Item Uses/game":  "Avg Def Items",
    "Tormentor Kills/game":      "Avg Tormentors",
    "Watcher Captures/game":     "Avg Watchers",
    "Building Damage/game":      "Avg Bld Dmg",
    "First Blood Rate":          "First Blood %",
    "Avg Game Length":           "Avg Game Len",
    "First Tormentor Time":      "1st Torm Time",
}


def _clean_card_line(line: str) -> str:
    """Strip markdown, drop the word 'rank', and shorten the stat label
    for the streamcard display."""
    s = line.replace("**", "").replace("(rank ", "(")
    if ":" in s:
        label, rest = s.split(":", 1)
        short = _CARD_SHORT_LABELS.get(label.strip(), label.strip())
        s = f"{short}:{rest}"
    return s


@tree.command(name="player_card", description="Render a stream-friendly PNG card for a player")
@app_commands.describe(name="Player name (partial), override nickname, or numeric account ID", public=PUBLIC_PARAM_DESCRIPTION)
async def player_card(interaction: discord.Interaction, name: str, public: bool = False):
    await interaction.response.defer(ephemeral=not public)
    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured.", ephemeral=True)
        return

    account_id, resolved_name, err = await _resolve_player_query(interaction, name)
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    from db import (
        get_all_time_stats, get_rating_cache_row, compute_fantasy_adjusted_ratings,
    )
    from formatters import _compute_strengths_weaknesses
    from streamcards import render_player_card, fetch_avatar

    stats = get_all_time_stats(interaction.guild_id, division["season_start"])
    target = next((s for s in stats if s.get("account_id") == account_id), None)
    if target is None:
        await interaction.followup.send(
            f"⚠️ `{resolved_name or account_id}` has no league matches here yet.",
            ephemeral=True,
        )
        return

    # Rating (fantasy-adjusted, matches /player + /lookup). Alts inherit the
    # main's adjustment so cards show them at the same rating.
    rating: int | None = None
    cache_row = get_rating_cache_row(account_id)
    if cache_row and cache_row.get("internal_rating") is not None:
        rating = cache_row["internal_rating"]
        adjustments = compute_fantasy_adjusted_ratings(
            interaction.guild_id, division["season_start"],
        )
        effective_id = cache_row.get("alt_account_for") or account_id
        adj = adjustments.get(effective_id)
        if adj:
            rating = adj["adjusted_rating"]

    # Strengths/weaknesses (max 3 each)
    pool = [s for s in stats if (s.get("games_played") or 0) >= 5]
    strengths, weaknesses = _compute_strengths_weaknesses(target, pool)
    strengths = [_clean_card_line(s) for s in strengths[:3]]
    weaknesses = [_clean_card_line(s) for s in weaknesses[:3]]

    cr = cache_row or {}
    avatar_url = None if cr.get("hide_avatar") else cr.get("avatar_url")
    avatar = await fetch_avatar(avatar_url)

    games = target.get("games_played", 0) or 0
    wins  = target.get("wins", 0) or 0
    losses = games - wins
    from config import ROLE_LABELS as _ROLE
    full_role = _ROLE.get(target.get("role_position"), "—")
    # Strip the "(Pos N)" suffix for a cleaner card label.
    role = full_role.split(" (")[0]
    display_name = (
        ((cache_row or {}).get("override_nickname"))
        or target.get("name") or resolved_name or f"Player_{account_id}"
    )

    buf = render_player_card(
        name=display_name,
        rating=rating,
        role=role,
        wins=wins, losses=losses, games=games,
        strengths=strengths, weaknesses=weaknesses,
        avatar=avatar, account_id=account_id,
    )
    await interaction.followup.send(
        file=discord.File(buf, filename=f"player_{account_id}.png"),
        ephemeral=not public,
    )


@tree.command(name="team_card", description="Render a stream-friendly PNG card for a player's team")
@app_commands.describe(name="Any player on the team — partial name, override nickname, or numeric account ID", public=PUBLIC_PARAM_DESCRIPTION)
async def team_card(interaction: discord.Interaction, name: str, public: bool = False):
    await interaction.response.defer(ephemeral=not public)
    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured.", ephemeral=True)
        return

    account_id, resolved_name, err = await _resolve_player_query(interaction, name)
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    from db import (
        get_player_costs, get_team_match_aggregates, get_captain_account_ids,
        get_rating_cache_row, get_all_time_stats,
    )
    from formatters import _compute_strengths_weaknesses, _TEAM_NOTABLE_STATS
    from streamcards import render_team_card, fetch_avatar
    import asyncio

    costs = get_player_costs(interaction.guild_id, division["season_start"])
    target_cost = costs.get(account_id)
    captain: str | None = None
    if target_cost and target_cost.get("captain"):
        captain = target_cost["captain"]
    else:
        cap_aids = get_captain_account_ids(interaction.guild_id, division["season_start"])
        for cap_name, cap_aid in cap_aids.items():
            if cap_aid == account_id:
                captain = cap_name
                break
    if not captain:
        await interaction.followup.send(
            f"⚠️ `{resolved_name or account_id}` isn't on a team this season.",
            ephemeral=True,
        )
        return

    team_aggs = get_team_match_aggregates(interaction.guild_id, division["season_start"])
    target_agg = team_aggs.get(captain)
    if not target_agg:
        await interaction.followup.send(f"⚠️ Team `{captain}` has no game data yet.", ephemeral=True)
        return

    # Build member list (in roster order: drafted players then captain).
    cap_aids = get_captain_account_ids(interaction.guild_id, division["season_start"])
    captain_aid = cap_aids.get(captain)
    drafted_aids = [aid for aid, c in costs.items() if c.get("captain") == captain]
    roster_aids = drafted_aids + ([captain_aid] if captain_aid else [])

    # Per-player role_position comes from the stats query.
    stats_by_aid = {
        s["account_id"]: s
        for s in get_all_time_stats(interaction.guild_id, division["season_start"])
    }

    # Pull cache rows for names + avatars in one go.
    members: list[dict] = []
    avatar_tasks = []
    for aid in roster_aids:
        cr = get_rating_cache_row(aid) or {}
        s = stats_by_aid.get(aid) or {}
        av_url = None if cr.get("hide_avatar") else cr.get("avatar_url")
        members.append({
            "account_id": aid,
            "name": cr.get("override_nickname") or cr.get("name") or f"Player_{aid}",
            "is_captain": aid == captain_aid,
            "role_position": s.get("role_position"),
            "avatar_url": av_url,
            "avatar": None,
        })
        avatar_tasks.append(fetch_avatar(av_url))
    avatars = await asyncio.gather(*avatar_tasks)
    for m, av in zip(members, avatars):
        m["avatar"] = av

    # Strengths/weaknesses, same data path as /team_stats.
    strengths, weaknesses = _compute_strengths_weaknesses(
        target_agg, list(team_aggs.values()), stat_list=_TEAM_NOTABLE_STATS,
    )
    strengths = [_clean_card_line(s) for s in strengths[:3]]
    weaknesses = [_clean_card_line(s) for s in weaknesses[:3]]

    games = target_agg.get("games_played", 0) or 0
    wins  = target_agg.get("wins", 0) or 0
    losses = games - wins

    from team_metadata import get_team_info
    info = get_team_info(captain, interaction.guild_id, division["season_start"])
    team_label = info.get("team_name") or captain
    # Logo URL: RD2L first, captain avatar as fallback.
    logo_url = info.get("logo_url")
    if not logo_url:
        for m in members:
            if m.get("is_captain") and m.get("avatar_url"):
                logo_url = m["avatar_url"]
                break
    team_logo = await fetch_avatar(logo_url, size=160)

    buf = render_team_card(
        team_label=team_label,
        members=members,
        wins=wins, losses=losses, games=games,
        strengths=strengths, weaknesses=weaknesses,
        avg_duration=target_agg.get("avg_duration"),
        team_logo=team_logo,
    )
    await interaction.followup.send(
        file=discord.File(buf, filename=f"team_{captain}.png"),
        ephemeral=not public,
    )


@tree.command(name="h2h_card", description="Render a head-to-head PNG card for two teams")
@app_commands.describe(
    season="Which season to look at (defaults to the current one)",
    team1="Any player on team 1 (or the captain's name)",
    team2="Any player on team 2 (or the captain's name)",
    public=PUBLIC_PARAM_DESCRIPTION,
)
@app_commands.autocomplete(season=_season_autocomplete)
async def h2h_card(interaction: discord.Interaction, team1: str, team2: str, season: str = None, public: bool = False):
    await interaction.response.defer(ephemeral=not public)

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured.", ephemeral=True)
        return

    from db import (
        get_player_costs, get_captain_account_ids, get_team_match_aggregates,
        get_rating_cache_row, get_all_time_stats, get_head_to_head_matches,
    )
    from streamcards import render_h2h_card, fetch_avatar
    import asyncio

    guild_id = interaction.guild_id
    season_start, season_err = _resolve_season(interaction, season)
    if season_err:
        await interaction.followup.send(season_err, ephemeral=True)
        return

    async def _resolve_to_captain(player_input: str) -> tuple[str | None, str]:
        """Return (captain_name, error_msg). Captain string sentinel."""
        aid, resolved_name, err = await _resolve_player_query(interaction, player_input)
        if err:
            return None, err
        costs = get_player_costs(guild_id, season_start)
        if costs.get(aid) and costs[aid].get("captain"):
            return costs[aid]["captain"], ""
        cap_aids = get_captain_account_ids(guild_id, season_start)
        for cap_name, cap_aid in cap_aids.items():
            if cap_aid == aid:
                return cap_name, ""
        return None, f"⚠️ `{resolved_name or aid}` isn't on a team this season."

    cap_a, err_a = await _resolve_to_captain(team1)
    if err_a:
        await interaction.followup.send(err_a, ephemeral=True); return
    cap_b, err_b = await _resolve_to_captain(team2)
    if err_b:
        await interaction.followup.send(err_b, ephemeral=True); return
    if cap_a == cap_b:
        await interaction.followup.send("⚠️ Same team on both sides.", ephemeral=True); return

    team_aggs = get_team_match_aggregates(guild_id, season_start)
    agg_a = team_aggs.get(cap_a)
    agg_b = team_aggs.get(cap_b)
    if not agg_a or not agg_b:
        await interaction.followup.send("⚠️ One of the teams has no match data yet.", ephemeral=True)
        return

    costs = get_player_costs(guild_id, season_start)
    cap_aids = get_captain_account_ids(guild_id, season_start)
    stats_by_aid = {s["account_id"]: s for s in get_all_time_stats(guild_id, season_start)}

    async def _build_members(captain: str) -> list[dict]:
        cap_aid = cap_aids.get(captain)
        drafted_aids = [aid for aid, c in costs.items() if c.get("captain") == captain]
        roster_aids = drafted_aids + ([cap_aid] if cap_aid else [])
        members: list[dict] = []
        tasks = []
        for aid in roster_aids:
            cr = get_rating_cache_row(aid) or {}
            s = stats_by_aid.get(aid) or {}
            av_url = None if cr.get("hide_avatar") else cr.get("avatar_url")
            members.append({
                "account_id": aid,
                "name": cr.get("override_nickname") or cr.get("name") or f"Player_{aid}",
                "is_captain": aid == cap_aid,
                "role_position": s.get("role_position"),
                "avatar_url": av_url,
                "avatar": None,
            })
            tasks.append(fetch_avatar(av_url))
        avatars = await asyncio.gather(*tasks)
        for m, av in zip(members, avatars):
            m["avatar"] = av
        return members

    members_a, members_b = await asyncio.gather(_build_members(cap_a), _build_members(cap_b))

    record_a = (int(agg_a.get("wins", 0) or 0), int((agg_a.get("games_played", 0) or 0) - (agg_a.get("wins", 0) or 0)))
    record_b = (int(agg_b.get("wins", 0) or 0), int((agg_b.get("games_played", 0) or 0) - (agg_b.get("wins", 0) or 0)))

    history = get_head_to_head_matches(guild_id, season_start, cap_a, cap_b)

    # Map captain → real team name + logo URL (from RD2L). When a team has no
    # registered logo, fall back to the captain's Steam avatar.
    from team_metadata import get_team_info
    info_a = get_team_info(cap_a, guild_id, season_start)
    info_b = get_team_info(cap_b, guild_id, season_start)

    def _logo_url_for(info: dict, members: list[dict]) -> str | None:
        if info.get("logo_url"):
            return info["logo_url"]
        for m in members:
            if m.get("is_captain") and m.get("avatar_url"):
                return m["avatar_url"]
        return None

    logo_a, logo_b = await asyncio.gather(
        fetch_avatar(_logo_url_for(info_a, members_a), size=160),
        fetch_avatar(_logo_url_for(info_b, members_b), size=160),
    )

    buf = render_h2h_card(
        team_a_label=info_a["team_name"], team_a_record=record_a,
        team_a_members=members_a, team_a_agg=agg_a,
        team_b_label=info_b["team_name"], team_b_record=record_b,
        team_b_members=members_b, team_b_agg=agg_b,
        history=history,
        team_a_logo=logo_a, team_b_logo=logo_b,
    )
    await interaction.followup.send(
        file=discord.File(buf, filename=f"h2h_{cap_a}_vs_{cap_b}.png"),
        ephemeral=not public,
    )


@tree.command(name="h2h_player", description="Render a head-to-head PNG card comparing two players")
@app_commands.describe(
    player1="First player (partial name, override nickname, or numeric account ID)",
    player2="Second player (partial name, override nickname, or numeric account ID)",
    public=PUBLIC_PARAM_DESCRIPTION,
)
async def h2h_player(interaction: discord.Interaction, player1: str, player2: str, public: bool = False):
    await interaction.response.defer(ephemeral=not public)
    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured.", ephemeral=True)
        return

    from db import (
        get_all_time_stats, get_rating_cache_row, compute_fantasy_adjusted_ratings,
    )
    from streamcards import (
        render_h2h_player_card, fetch_avatar, compute_h2h_stat_rows,
    )
    import asyncio

    aid_a, resolved_a, err_a = await _resolve_player_query(interaction, player1)
    if err_a:
        await interaction.followup.send(err_a, ephemeral=True); return
    aid_b, resolved_b, err_b = await _resolve_player_query(interaction, player2)
    if err_b:
        await interaction.followup.send(err_b, ephemeral=True); return
    if aid_a == aid_b:
        await interaction.followup.send("⚠️ Same player on both sides.", ephemeral=True); return

    stats = get_all_time_stats(interaction.guild_id, division["season_start"])
    row_a = next((s for s in stats if s.get("account_id") == aid_a), None)
    row_b = next((s for s in stats if s.get("account_id") == aid_b), None)
    if row_a is None or row_b is None:
        missing = resolved_a if row_a is None else resolved_b
        await interaction.followup.send(
            f"⚠️ `{missing}` has no league matches here yet.", ephemeral=True,
        )
        return

    # Rank stats by |z-diff| across a 5-game-minimum pool. Both target players
    # are included in the pool regardless of games played so we never miss
    # a stat because they're new.
    pool = [s for s in stats if (s.get("games_played") or 0) >= 5]
    pool_ids = {s["account_id"] for s in pool}
    for r in (row_a, row_b):
        if r["account_id"] not in pool_ids:
            pool.append(r)
    stat_rows = compute_h2h_stat_rows(row_a, row_b, pool, top_n=8)

    # Ratings: prefer fantasy-adjusted, fall back to raw internal_rating.
    adjustments = compute_fantasy_adjusted_ratings(
        interaction.guild_id, division["season_start"],
    )

    def _rating_for(aid: int) -> int | None:
        cr = get_rating_cache_row(aid) or {}
        raw = cr.get("internal_rating")
        if raw is None:
            return None
        effective_id = cr.get("alt_account_for") or aid
        adj = adjustments.get(effective_id)
        return adj["adjusted_rating"] if adj else raw

    rating_a = _rating_for(aid_a)
    rating_b = _rating_for(aid_b)

    from config import ROLE_LABELS as _ROLE

    def _display(row: dict, aid: int, fallback_name: str) -> tuple[str, str, int, int]:
        cr = get_rating_cache_row(aid) or {}
        games = row.get("games_played", 0) or 0
        wins = row.get("wins", 0) or 0
        losses = games - wins
        full_role = _ROLE.get(row.get("role_position"), "—")
        role = full_role.split(" (")[0]
        name = (
            cr.get("override_nickname")
            or row.get("name")
            or fallback_name
            or f"Player_{aid}"
        )
        return name, role, wins, losses

    name_a, role_a, wins_a, losses_a = _display(row_a, aid_a, resolved_a)
    name_b, role_b, wins_b, losses_b = _display(row_b, aid_b, resolved_b)

    cr_a = get_rating_cache_row(aid_a) or {}
    cr_b = get_rating_cache_row(aid_b) or {}
    av_a_url = None if cr_a.get("hide_avatar") else cr_a.get("avatar_url")
    av_b_url = None if cr_b.get("hide_avatar") else cr_b.get("avatar_url")
    avatar_a, avatar_b = await asyncio.gather(
        fetch_avatar(av_a_url), fetch_avatar(av_b_url),
    )

    buf = render_h2h_player_card(
        name_a=name_a, rating_a=rating_a, role_a=role_a,
        wins_a=wins_a, losses_a=losses_a, avatar_a=avatar_a,
        name_b=name_b, rating_b=rating_b, role_b=role_b,
        wins_b=wins_b, losses_b=losses_b, avatar_b=avatar_b,
        stat_rows=stat_rows,
    )
    await interaction.followup.send(
        file=discord.File(buf, filename=f"h2h_{aid_a}_vs_{aid_b}.png"),
        ephemeral=not public,
    )



@tree.command(name="lookup", description="[Owner] Diagnostic lookup — full methodology breakdown for a player")
@app_commands.describe(
    query="Player name (partial match) or numeric account ID",
    force_refresh="Skip the cache and re-fetch from windrun/OpenDota",
    public=PUBLIC_PARAM_DESCRIPTION,
)
async def lookup(interaction: discord.Interaction, query: str, force_refresh: bool = False, public: bool = False):
    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    in_admin_channel = interaction.channel_id in LOOKUP_CHANNEL_IDS
    if not (is_owner or in_admin_channel):
        await interaction.response.send_message(
            "🔒 This command is for admins only — please use `/player` to look up a player.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=not public)
    debug = True
    force_refresh = bool(force_refresh) and is_owner

    account_id, player_name, err = await _resolve_player_query(interaction, query)
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    # Cache-first: if we have a fresh cached row and the caller isn't in
    # debug mode (which needs live raw data), skip the API calls entirely.
    from db import get_skill_override, get_rating_cache_row, compute_fantasy_adjusted_ratings
    from datetime import datetime, timezone
    override = get_skill_override(account_id)
    cache_row = get_rating_cache_row(account_id)
    is_fresh = False
    cache_age_days: float | None = None
    if cache_row and cache_row.get("updated_at"):
        try:
            ts = datetime.fromisoformat(cache_row["updated_at"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            cache_age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400
            is_fresh = cache_age_days < 5
        except Exception:
            is_fresh = False

    # Look up the per-guild fantasy adjustment so /lookup matches /players.
    # Alt accounts inherit the main's adjustment so the number matches.
    adjustment_pct: float | None = None
    division = _require_division(interaction)
    if division:
        adjustments = compute_fantasy_adjusted_ratings(
            interaction.guild_id, division["season_start"],
        )
        effective_id = (override or {}).get("alt_account_for") or account_id
        adj = adjustments.get(effective_id)
        if adj:
            adjustment_pct = adj["pct"]

    from formatters import format_lookup
    if cache_row and is_fresh and not debug and not force_refresh:
        name = (
            (override or {}).get("nickname")
            or cache_row.get("name")
            or player_name
            or f"Player_{account_id}"
        )
        embed = format_lookup(
            account_id=account_id,
            fallback_name=name,
            override=override,
            cached_rating=cache_row.get("internal_rating"),
            cached_avatar=None if (cache_row.get("hide_avatar") or (override and override.get("hide_avatar"))) else cache_row.get("avatar_url"),
            updated_at=cache_row.get("updated_at"),
            adjustment_pct=adjustment_pct,
        )
        await interaction.followup.send(embed=embed, ephemeral=not public)
        return

    # Fetch in parallel: windrun (profile + matches) and OpenDota (profile + counts).
    # AD counts: prefer windrun since it's AD-native and indexes everyone with
    # a league game (OpenDota requires public match history). OpenDota still
    # provides the ranked counts that windrun can't.
    from windrun import (
        fetch_player as fetch_windrun_player,
        fetch_player_matches as fetch_windrun_matches,
    )
    from opendota_lookup import fetch_player_profile, fetch_player_game_counts
    import asyncio

    wr_task         = asyncio.create_task(fetch_windrun_player(account_id))
    wr_matches_task = asyncio.create_task(fetch_windrun_matches(account_id, limit=500))
    od_task         = asyncio.create_task(fetch_player_profile(account_id))
    od_counts_task  = asyncio.create_task(fetch_player_game_counts(account_id))

    results = await asyncio.gather(
        wr_task, wr_matches_task, od_task, od_counts_task, return_exceptions=True,
    )
    wr, wr_matches, od, od_counts = (None if isinstance(r, Exception) else r for r in results)
    # If Steam match history is hidden, OpenDota's /wl counts are all 0/0 —
    # convert those bogus 0s to None so display shows "hidden" and the rating
    # formula doesn't apply an inexperience penalty for unverifiable history.
    from formatters import sanitize_od_counts as _sanitize_od_counts
    od_counts = _sanitize_od_counts(od, od_counts)
    for label, r in zip(("windrun", "windrun-matches", "opendota", "opendota-counts"), results):
        if isinstance(r, Exception):
            logger.exception("%s fetch failed for %d", label, account_id, exc_info=r)

    # AD all-time: take max of the two sources. They sometimes diverge —
    # OpenDota's game_mode=18 filter can include AD variants windrun doesn't
    # track, and windrun can miss matches for partially-indexed players.
    # Max keeps the all-time count consistent with last-year (which is also
    # OpenDota-or-windrun whichever is higher).
    wr_lifetime: int | None = None
    if wr and (wr.get("wins") is not None or wr.get("losses") is not None):
        wr_lifetime = (wr.get("wins") or 0) + (wr.get("losses") or 0)
    od_all_time = (od_counts or {}).get("all_time_ad")
    if wr_lifetime is not None and od_all_time is not None:
        ad_all_time = max(wr_lifetime, od_all_time)
    else:
        ad_all_time = wr_lifetime if wr_lifetime is not None else od_all_time

    # AD last-year: OpenDota's actual count is the authoritative source when
    # available. Windrun's /matches caps at ~30 returned items, so we use it
    # as a floor — if OpenDota's count is lower than windrun's recent activity
    # implies, OpenDota is undercounting (often because the account is only
    # partially indexed). Take max(opendota, windrun_recent_count).
    windrun_last_year_count = 0
    if wr_matches:
        from datetime import datetime, timezone as _tz, timedelta
        cutoff = datetime.now(_tz.utc) - timedelta(days=365)
        for m in wr_matches:
            ts = m.get("gameStart")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt > cutoff:
                    windrun_last_year_count += 1
            except Exception:
                pass
    od_last_year = (od_counts or {}).get("last_year_ad")
    if od_last_year is not None or windrun_last_year_count > 0:
        ad_last_year = max(od_last_year or 0, windrun_last_year_count)
    else:
        ad_last_year = None

    # If fetch couldn't produce a fresh rating but we have a stale cache row,
    # use the cached values as a fallback so the user still sees a number.
    has_override_rating = bool(override and override.get("internal_rating_override") is not None)
    have_any_signal = (wr is not None) or (od_counts is not None) or has_override_rating
    fallback_cached_rating = None
    fallback_cached_avatar = None
    fallback_updated_at = None
    is_stale_render = False
    if not have_any_signal and cache_row:
        fallback_cached_rating = cache_row.get("internal_rating")
        if not (cache_row.get("hide_avatar") or (override and override.get("hide_avatar"))):
            fallback_cached_avatar = cache_row.get("avatar_url")
        fallback_updated_at = cache_row.get("updated_at")
        is_stale_render = True

    # Build a fetch-error message when we couldn't get fresh data.
    fetch_error: str | None = None
    if not have_any_signal:
        failed_apis: list[str] = []
        if isinstance(results[0], Exception) or results[0] is None:
            failed_apis.append("windrun")
        if isinstance(results[2], Exception) or results[2] is None:
            failed_apis.append("OpenDota profile")
        if isinstance(results[3], Exception) or results[3] is None:
            failed_apis.append("OpenDota game counts")
        if failed_apis:
            sources = ", ".join(failed_apis)
            if cache_row:
                fetch_error = f"Couldn't reach: {sources}. Showing previously cached value."
            else:
                fetch_error = f"Couldn't reach: {sources}. No cached value available."

    embed = format_lookup(
        account_id=account_id,
        fallback_name=player_name,
        windrun=wr,
        opendota=od,
        ad_all_time=ad_all_time,
        ad_last_year=ad_last_year,
        od_counts=od_counts,
        override=override,
        debug=debug,
        cached_rating=fallback_cached_rating,
        cached_avatar=fallback_cached_avatar,
        updated_at=fallback_updated_at,
        is_stale=is_stale_render,
        adjustment_pct=adjustment_pct,
        fetch_error=fetch_error,
    )
    await interaction.followup.send(embed=embed, ephemeral=not public)

    # Side-effect: refresh this player's cache row so /players stays current
    # without anyone needing to run /refresh_ratings. Write whenever we got
    # ANY fresh signal — windrun-only or OpenDota-only is still better than
    # leaving the cache stale.
    if have_any_signal and not has_override_rating:
        try:
            from db import upsert_rating_cache_row
            from formatters import _resolve_internal_rating, windrun_rating_for_formula, _rank_to_windrun
            raw_wr = (wr or {}).get("rating")
            formula_wr = windrun_rating_for_formula(wr)
            rank_tier = (od or {}).get("rank_tier")
            lb_rank = (od or {}).get("leaderboard_rank")
            eff_wr  = override["windrun_rating"] if override and override.get("windrun_rating") is not None else formula_wr
            eff_rank_tier = override["rank_tier"] if override and override.get("rank_tier") is not None else rank_tier
            eff_lb_rank   = override["leaderboard_rank"] if override and override.get("rank_tier") is not None else lb_rank
            ranked_in_wr_raw = _rank_to_windrun(eff_rank_tier, eff_lb_rank)
            ranked_last_year = (od_counts or {}).get("last_year_ranked")
            ranked_all_time  = (od_counts or {}).get("all_time_ranked")
            info = _resolve_internal_rating(
                override=override,
                ad_last=ad_last_year,
                ad_all=ad_all_time,
                ranked_last=ranked_last_year,
                wr_rating=eff_wr,
                ranked_in_wr_raw=ranked_in_wr_raw,
                ranked_all=ranked_all_time,
            )
            name = (wr or {}).get("nickname") or ((od or {}).get("profile") or {}).get("personaname") or player_name
            upsert_rating_cache_row({
                "account_id":       account_id,
                "name":             name,
                "internal_rating":  info[0] if info else None,
                "raw_windrun":      raw_wr,
                "rank_tier":        rank_tier,
                "leaderboard_rank": lb_rank,
                "ad_last_year":     ad_last_year,
                "ad_all_time":      ad_all_time,
                "ranked_last_year": ranked_last_year,
                "explanation":      info[1] if info else None,
                "avatar_url":       (
                    ((od or {}).get("profile") or {}).get("avatarfull")
                    or (wr or {}).get("avatar")
                ),
                "fh_unavailable":   bool((od or {}).get("profile", {}).get("fh_unavailable")) if od else None,
            })
        except Exception:
            logger.exception("cache write failed for %d after /lookup", account_id)


async def _resolve_player_query(interaction: discord.Interaction, q: str) -> tuple[int | None, str | None, str | None]:
    """Shared name-or-account-id resolver for /lookup, /set_skill, etc.

    Returns (account_id, player_name, error_msg). error_msg is set if we
    can't resolve uniquely and the caller should bail out.
    """
    q = (q or "").strip()
    if not q:
        return None, None, "⚠️ Empty query."
    if q.isdigit():
        n = int(q)
        # Steam64 IDs (17 digits, > offset) — convert to steam32/account_id
        # by subtracting the steam base. Users often paste these from a
        # Steam profile URL instead of the friend-ID from Dotabuff.
        STEAM64_OFFSET = 76561197960265728
        if n > STEAM64_OFFSET:
            n -= STEAM64_OFFSET
        return n, None, None

    from db import _conn
    guild_id = interaction.guild_id
    candidates: dict[int, str] = {}
    with _conn() as conn:
        # Override nicknames first (global)
        for r in conn.execute("""
            SELECT account_id, nickname FROM skill_overrides
            WHERE nickname IS NOT NULL AND LOWER(nickname) LIKE LOWER(?)
        """, (f"%{q}%",)).fetchall():
            candidates[r["account_id"]] = r["nickname"]
        # Cached names — covers manually-added players who haven't played a
        # league match yet (they appear in /players, so /lookup should find them too).
        for r in conn.execute("""
            SELECT prc.account_id, prc.name
            FROM player_ratings_cache prc
            WHERE prc.name IS NOT NULL
              AND LOWER(prc.name) LIKE LOWER(?)
              AND (
                  prc.account_id IN (
                      SELECT DISTINCT p.account_id
                      FROM players p JOIN matches m ON p.match_id = m.match_id
                      WHERE m.guild_id = ?
                  )
                  OR prc.account_id IN (
                      SELECT account_id FROM guild_roster WHERE guild_id = ?
                  )
              )
        """, (f"%{q}%", guild_id, guild_id)).fetchall():
            candidates.setdefault(r["account_id"], r["name"])
        # Guild players
        for r in conn.execute("""
            SELECT DISTINCT p.account_id, p.name
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.guild_id = ? AND LOWER(p.name) LIKE LOWER(?)
        """, (guild_id, f"%{q}%")).fetchall():
            candidates.setdefault(r["account_id"], r["name"])
        # Fall back to global players
        if not candidates:
            for r in conn.execute("""
                SELECT DISTINCT account_id, name
                FROM players
                WHERE LOWER(name) LIKE LOWER(?)
            """, (f"%{q}%",)).fetchall():
                candidates.setdefault(r["account_id"], r["name"])
    unique = list(candidates.items())
    if not unique:
        return None, None, f"⚠️ No player matching `{q}`. Try a different spelling or use the numeric account ID."
    if len(unique) > 1:
        ql = q.lower()
        exact = [(aid, n) for aid, n in unique if (n or "").lower() == ql]
        if len(exact) == 1:
            unique = exact
        else:
            names = ", ".join(f"`{n}`" for _, n in unique[:10])
            return None, None, f"Multiple matches: {names}\nBe more specific."
    return unique[0][0], unique[0][1], None


@tree.command(name="set_player", description="[Owner] Override a player's nickname, windrun, badge/rank used by /lookup and /players")
@app_commands.describe(
    name="Player name (partial match) or numeric account ID",
    nickname="Custom display name (omit to keep existing)",
    windrun="Manual windrun rating (omit to keep existing)",
    badge='Ranked badge: e.g. "divine 3", "legend 5", "immortal" (omit to keep existing)',
    rank="Immortal leaderboard rank (only meaningful with badge:immortal)",
    rating="Manual internal rating — overrides EVERYTHING; windrun/badge/trust-weights are ignored",
    alt_account_for="Main account this alt belongs to (name/id/nickname/Steam64) — will inherit main's rating",
    discord_handle="Override Discord handle for /sync_roles_channels (use when rd2l has wrong info)",
    note="Optional note (e.g. 'smurf — real rank is 10k')",
    hide_avatar="True to hide their Steam avatar everywhere (inappropriate profile pic). False to unhide.",
    clear="Set True to remove any existing override for this player",
    delete="Set True to remove the player entirely AND permanently exclude them from /players",
    restore="Set True to undo a prior delete (removes this guild's exclusion entry for them)",
)
async def set_player(
    interaction: discord.Interaction,
    name: str,
    nickname: str = None,
    windrun: float = None,
    badge: str = None,
    rank: int = None,
    rating: int = None,
    alt_account_for: str = None,
    discord_handle: str = None,
    note: str = None,
    hide_avatar: bool = None,
    clear: bool = False,
    delete: bool = False,
    restore: bool = False,
):
    if not (ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID):
        await interaction.response.send_message("⚠️ Bot owner only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    account_id, player_name, err = await _resolve_player_query(interaction, name)
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    from db import (
        upsert_skill_override, delete_skill_override, get_skill_override,
        remove_from_guild_roster, guild_player_has_matches, _conn,
        exclude_from_guild, unexclude_from_guild,
    )
    label = f"`{player_name}` (`{account_id}`)" if player_name else f"`{account_id}`"

    if restore:
        removed = unexclude_from_guild(interaction.guild_id, account_id)
        if removed:
            await interaction.followup.send(
                f"♻️ Restored {label} — exclusion removed. They'll reappear in /players "
                f"after the next `/refresh_ratings` or `/lookup`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"ℹ️ {label} wasn't excluded here.",
                ephemeral=True,
            )
        return

    if delete:
        override_removed = delete_skill_override(account_id)
        roster_removed = remove_from_guild_roster(interaction.guild_id, account_id)
        with _conn() as conn:
            cur = conn.execute(
                "DELETE FROM player_ratings_cache WHERE account_id = ?",
                (account_id,),
            )
            cache_removed = cur.rowcount > 0
        exclude_from_guild(interaction.guild_id, account_id)
        had_matches = guild_player_has_matches(interaction.guild_id, account_id)

        bits = ["excluded from /players"]
        if override_removed: bits.append("override")
        if roster_removed:   bits.append("roster entry")
        if cache_removed:    bits.append("cache row")

        extra = (
            " (cache may repopulate after `/refresh_ratings` or `/lookup`, but the "
            "exclusion keeps them hidden from /players. Use `restore:True` to undo.)"
            if had_matches else " Use `restore:True` to undo."
        )
        await interaction.followup.send(
            f"🗑️ Deleted for {label}: {', '.join(bits)}.{extra}",
            ephemeral=True,
        )
        return

    if clear:
        removed = delete_skill_override(account_id)
        msg = f"✅ Cleared override for {label}." if removed else f"ℹ️ No override existed for {label}."
        await interaction.followup.send(msg, ephemeral=True)
        return

    if (windrun is None and badge is None and rank is None and rating is None
            and alt_account_for is None and discord_handle is None
            and note is None and nickname is None and hide_avatar is None):
        # Show current state
        from formatters import format_badge
        existing = get_skill_override(account_id)
        if existing:
            badge_str = format_badge(existing.get("rank_tier"), existing.get("leaderboard_rank")) \
                if existing.get("rank_tier") else "None"
            await interaction.followup.send(
                f"Current override for {label}:\n"
                f"• nickname: `{existing.get('nickname')}`\n"
                f"• windrun: `{existing.get('windrun_rating')}`\n"
                f"• badge: `{badge_str}`\n"
                f"• rating: `{existing.get('internal_rating_override')}`\n"
                f"• alt_account_for: `{existing.get('alt_account_for')}`\n"
                f"• discord_handle: `{existing.get('discord_override')}`\n"
                f"• note: `{existing.get('note')}`\n"
                f"• set by `{existing.get('set_by_name')}` at `{existing.get('set_at')}`",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"No override set for {label}.\n"
                'Use `nickname:`, `windrun:`, `badge:` (e.g. "divine 3" or "immortal" + optional `rank:`), '
                '`rating:`, and/or `alt_account_for:` to set one. `clear:True` to remove.',
                ephemeral=True,
            )
        return

    # Parse badge → rank_tier. Hard-error on invalid combos so mistakes surface
    # right away instead of silently ignoring the input.
    from formatters import parse_badge
    parsed_rank_tier: int | None = None
    if badge is not None:
        parsed_rank_tier = parse_badge(badge)
        if parsed_rank_tier is None:
            await interaction.followup.send(
                f'⚠️ Bad badge `{badge}`. Expected e.g. `"divine 3"`, `"legend 5"`, or `"immortal"`.',
                ephemeral=True,
            )
            return
    if rank is not None and parsed_rank_tier != 80:
        await interaction.followup.send(
            "⚠️ `rank:` is only valid with `badge:immortal`.",
            ephemeral=True,
        )
        return

    # Resolve alt_account_for: accepts the same forms as the `name` arg
    # (partial name, nickname, account_id, Steam64). Refuse self-links.
    parsed_alt_id: int | None = None
    if alt_account_for is not None:
        parsed_alt_id, alt_resolved_name, alt_err = await _resolve_player_query(interaction, alt_account_for)
        if alt_err or parsed_alt_id is None:
            await interaction.followup.send(
                f"⚠️ Couldn't resolve `alt_account_for:{alt_account_for}` — {alt_err or 'not found'}.",
                ephemeral=True,
            )
            return
        if parsed_alt_id == account_id:
            await interaction.followup.send(
                "⚠️ `alt_account_for:` can't point at the same player.",
                ephemeral=True,
            )
            return

    upsert_skill_override(
        account_id=account_id,
        windrun_rating=windrun,
        rank_tier=parsed_rank_tier,
        leaderboard_rank=rank,
        note=note,
        nickname=nickname,
        internal_rating_override=rating,
        hide_avatar=hide_avatar,
        alt_account_for=parsed_alt_id,
        discord_override=discord_handle,
        set_by_user_id=interaction.user.id,
        set_by_name=interaction.user.display_name,
    )

    # Add the player to this guild's roster so they appear in /players even
    # if they haven't played a match yet. Then seed/refresh their cache row
    # using the effective override values (no API call — best effort, will
    # get enriched next time someone runs /lookup on them).
    from db import add_to_guild_roster, get_rating_cache_row, upsert_rating_cache_row
    from formatters import _resolve_internal_rating
    add_to_guild_roster(guild_id=interaction.guild_id, account_id=account_id)

    full = get_skill_override(account_id) or {}
    existing = get_rating_cache_row(account_id) or {}
    from formatters import _rank_to_windrun, format_badge
    eff_wr  = full.get("windrun_rating") if full.get("windrun_rating") is not None else existing.get("raw_windrun")
    eff_rank_tier = full.get("rank_tier") if full.get("rank_tier") is not None else existing.get("rank_tier")
    eff_lb_rank   = full.get("leaderboard_rank") if full.get("rank_tier") is not None else existing.get("leaderboard_rank")
    ranked_in_wr_raw = _rank_to_windrun(eff_rank_tier, eff_lb_rank)
    info = _resolve_internal_rating(
        override=full,
        ad_last=existing.get("ad_last_year"),
        ad_all=None,
        ranked_last=existing.get("ranked_last_year"),
        wr_rating=eff_wr,
        ranked_in_wr_raw=ranked_in_wr_raw,
    )
    display_name = (
        nickname or full.get("nickname")
        or existing.get("name") or player_name
        or f"Player_{account_id}"
    )
    upsert_rating_cache_row({
        "account_id":       account_id,
        "name":             display_name,
        "internal_rating":  info[0] if info else existing.get("internal_rating"),
        "raw_windrun":      eff_wr,
        "rank_tier":        existing.get("rank_tier"),
        "leaderboard_rank": existing.get("leaderboard_rank"),
        "ad_last_year":     existing.get("ad_last_year"),
        "ad_all_time":      existing.get("ad_all_time"),
        "ranked_last_year": existing.get("ranked_last_year"),
        "explanation":      info[1] if info else existing.get("explanation"),
        "avatar_url":       existing.get("avatar_url"),
    })

    parts = []
    if nickname is not None:    parts.append(f"nickname=`{nickname}`")
    if windrun is not None:     parts.append(f"windrun=`{windrun}`")
    if badge is not None:       parts.append(f"badge=`{format_badge(parsed_rank_tier, rank)}`")
    if rating is not None:      parts.append(f"rating=`{rating}`")
    if parsed_alt_id is not None: parts.append(f"alt_account_for=`{parsed_alt_id}`")
    if discord_handle is not None: parts.append(f"discord_handle=`{discord_handle}`")
    if note is not None:        parts.append(f"note=`{note}`")
    if hide_avatar is not None: parts.append(f"hide_avatar=`{hide_avatar}`")
    rating_note = f" · cached rating: **{info[0]}**" if info else ""
    await interaction.followup.send(
        f"✅ Updated override for {label}: {', '.join(parts)}{rating_note}",
        ephemeral=True,
    )


async def _refresh_ratings_task(aids: list[int], interaction: discord.Interaction):
    """Long-running background task that fetches windrun + OpenDota for each
    account_id and populates player_ratings_cache."""
    from windrun import fetch_player as fetch_windrun_player
    from opendota_lookup import fetch_player_profile, fetch_player_game_counts
    from db import get_skill_override, upsert_rating_cache_row
    from formatters import _resolve_internal_rating, _rank_to_windrun

    success = 0
    failures = 0
    for i, aid in enumerate(aids):
        try:
            results = await asyncio.gather(
                fetch_windrun_player(aid),
                fetch_player_profile(aid),
                fetch_player_game_counts(aid),
                return_exceptions=True,
            )
            wr_data, od, od_counts = (None if isinstance(r, Exception) else r for r in results)

            raw_wr   = (wr_data or {}).get("rating")
            from formatters import windrun_rating_for_formula
            formula_wr = windrun_rating_for_formula(wr_data)
            rank_tier = (od or {}).get("rank_tier")
            lb_rank   = (od or {}).get("leaderboard_rank")

            override = get_skill_override(aid)
            eff_wr  = override["windrun_rating"] if override and override.get("windrun_rating") is not None else formula_wr
            eff_rank_tier = override["rank_tier"] if override and override.get("rank_tier") is not None else rank_tier
            eff_lb_rank   = override["leaderboard_rank"] if override and override.get("rank_tier") is not None else lb_rank
            ranked_in_wr_raw = _rank_to_windrun(eff_rank_tier, eff_lb_rank)

            from formatters import sanitize_od_counts
            od_counts = sanitize_od_counts(od, od_counts)
            ad_last     = (od_counts or {}).get("last_year_ad")
            ad_all      = (od_counts or {}).get("all_time_ad")
            ranked_last = (od_counts or {}).get("last_year_ranked")
            ranked_all  = (od_counts or {}).get("all_time_ranked")

            info = None
            if od_counts is not None or (override and override.get("internal_rating_override") is not None):
                info = _resolve_internal_rating(
                    override=override,
                    ad_last=ad_last, ad_all=ad_all, ranked_last=ranked_last,
                    wr_rating=eff_wr, ranked_in_wr_raw=ranked_in_wr_raw, ranked_all=ranked_all,
                )

            name = (wr_data or {}).get("nickname")
            if not name and od:
                name = (od.get("profile") or {}).get("personaname")

            upsert_rating_cache_row({
                "account_id":       aid,
                "name":             name,
                "internal_rating":  info[0] if info else None,
                "raw_windrun":      raw_wr,
                "rank_tier":        rank_tier,
                "leaderboard_rank": lb_rank,
                "ad_last_year":     ad_last,
                "ad_all_time":      ad_all,
                "ranked_last_year": ranked_last,
                "explanation":      info[1] if info else None,
                "avatar_url":       (
                    ((od or {}).get("profile") or {}).get("avatarfull")
                    or (wr_data or {}).get("avatar")
                ),
                "fh_unavailable":   bool((od or {}).get("profile", {}).get("fh_unavailable")) if od else None,
            })
            if info:
                success += 1
            else:
                failures += 1
        except Exception:
            logger.exception("refresh failed for account %d", aid)
            failures += 1

    # Try a final followup. Will silently fail if interaction has expired (>15 min).
    try:
        await interaction.followup.send(
            f"✅ Ratings cache refreshed: {success} succeeded, {failures} failed. "
            f"Run `/players` to see the list.",
            ephemeral=True,
        )
    except Exception:
        logger.info("refresh complete: %d ok, %d fail (followup skipped)", success, failures)


@tree.command(name="refresh_ratings", description="[Owner] Refresh the internal rating cache for all players in this guild")
async def refresh_ratings(interaction: discord.Interaction):
    if not (ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID):
        await interaction.response.send_message("⚠️ Bot owner only.", ephemeral=True)
        return

    from db import get_guild_player_account_ids
    aids = get_guild_player_account_ids(interaction.guild_id)
    if not aids:
        await interaction.response.send_message("No players found in this guild yet.", ephemeral=True)
        return

    eta_min = max(1, round(len(aids) * 5 / 60))
    await interaction.response.send_message(
        f"⏳ Refreshing ratings for {len(aids)} players. ETA: ~{eta_min} min "
        f"(windrun's 5s rate limit dominates). I'll ping you here when it's done.",
        ephemeral=True,
    )
    asyncio.create_task(_refresh_ratings_task(aids, interaction))


@tree.command(name="players", description="Show all guild players ordered by rating")
@app_commands.describe(
    show_all="Include every cached player, not just those in this server's matches/roster",
    debug="[Owner only] show methodology details (residual %, expected vs actual)",
    public=PUBLIC_PARAM_DESCRIPTION,
)
async def players(interaction: discord.Interaction, show_all: bool = False, debug: bool = False, public: bool = False):
    await interaction.response.defer(ephemeral=not public)

    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    debug = bool(debug) and is_owner

    from db import get_guild_cached_ratings
    cached = get_guild_cached_ratings(interaction.guild_id, include_all=show_all)
    if not cached:
        await interaction.followup.send(
            "⚠️ Rating cache empty. Ask the bot owner to refresh it.",
            ephemeral=True,
        )
        return

    rows = [dict(r) for r in cached]
    division = _require_division(interaction)
    if division:
        from db import compute_fantasy_adjusted_ratings
        adjustments = compute_fantasy_adjusted_ratings(
            interaction.guild_id, division["season_start"],
        )
        for r in rows:
            # Alt accounts route the fantasy adjustment through the main so
            # /players displays them at the same rating.
            effective_id = r.get("alt_account_for") or r["account_id"]
            adj = adjustments.get(effective_id)
            if not adj:
                continue
            r["_diff"]     = adj["diff"]
            r["_expected"] = adj["expected"]
            r["_residual"] = adj["residual"]
            r["_pct"]      = adj["pct"]
            r["internal_rating"] = adj["adjusted_rating"]

    from formatters import format_players_list
    embeds = format_players_list(rows, fantasy_adjusted=True, debug=debug)
    # Discord's 6000-char limit is across all embeds in a single message, so
    # we send one followup message per embed instead of cramming them together.
    for embed in embeds:
        await interaction.followup.send(embed=embed, ephemeral=not public)


@tree.command(name="matches", description="Show matches with Dotabuff links")
@app_commands.describe(
    season="Which season to look at (defaults to the current one)",
    week="Season week number (1, 2, 3...) or -1 for all matches. Leave blank for the current week.",
    player="Filter to matches a specific player appeared in (partial name match).",
    public=PUBLIC_PARAM_DESCRIPTION,
)
@app_commands.autocomplete(season=_season_autocomplete)
async def matches(interaction: discord.Interaction, week: int = None, player: str = None, season: str = None, public: bool = False):
    # Defer immediately to avoid 3-second timeout
    await interaction.response.defer(ephemeral=not public)

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    season_start, season_err = _resolve_season(interaction, season)
    if season_err:
        await interaction.followup.send(season_err, ephemeral=True)
        return

    from db import get_matches_for_season_week, get_latest_matches, get_all_matches, get_match_ids_for_player, get_current_season_week

    if week is None:
        match_list = get_latest_matches(guild_id, season_start)
        week_label = f"Week {get_current_season_week(guild_id, season_start)}"
    elif week == -1:
        match_list = get_all_matches(guild_id)
        week_label = "All Matches"
    else:
        match_list = get_matches_for_season_week(guild_id, week, season_start)
        week_label = f"Week {week}"

    # Apply player filter if specified
    if player:
        player_match_ids = get_match_ids_for_player(guild_id, player)
        match_list = [m for m in match_list if m["match_id"] in player_match_ids]
        week_label += f" · {player}"

    if not match_list:
        msg = f"⚠️ No matches found for {week_label.lower()}."
        if player:
            msg += f" (No matches found for player matching '{player}'.)"
        await interaction.followup.send(msg, ephemeral=True)
        return

    # Label each side with its league team; sides we can't attribute to a
    # team fall back to Radiant/Dire in the formatter.
    from db import get_match_team_captains
    from team_metadata import get_team_info
    side_captains = get_match_team_captains(guild_id, season_start, [m["match_id"] for m in match_list])
    team_names: dict[str, str] = {}
    for m in match_list:
        sides = side_captains.get(m["match_id"], {})
        for side in ("radiant", "dire"):
            cap = sides.get(side)
            if cap and cap not in team_names:
                team_names[cap] = get_team_info(cap, guild_id, season_start).get("team_name") or cap
            m[f"{side}_team"] = team_names.get(cap)

    from formatters import format_matches_list
    embed = format_matches_list(match_list, week_label=week_label)
    await interaction.followup.send(embed=embed, ephemeral=not public)


@tree.command(name="summary", description="Show 10 fantasy-points leaderboards: each position × latest-week and all-time")
@app_commands.autocomplete(season=_season_autocomplete)
@app_commands.describe(
    season="Which season to look at (defaults to the current one)",
    public=PUBLIC_PARAM_DESCRIPTION,
)
async def summary(interaction: discord.Interaction, season: str = None, public: bool = False):
    await interaction.response.defer(ephemeral=not public)

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    season_start, season_err = _resolve_season(interaction, season)
    if season_err:
        await interaction.followup.send(season_err, ephemeral=True)
        return

    from db import get_stats_for_season_week, get_all_time_stats, get_current_season_week

    try:
        # Same current-week resolver /leaderboard, /player, and /matches use
        # for a blank week — one source of truth instead of a bespoke one.
        current_week = get_current_season_week(guild_id, season_start)
        week_stats_full = get_stats_for_season_week(guild_id, current_week, season_start)
        week_label_base = f"Week {current_week}"
        alltime_stats_full = get_all_time_stats(guild_id, season_start)

        # Min-games qualification (50% of most-active player's count) applied per scope.
        week_max = max((s.get("games_played", 0) or 0) for s in week_stats_full) if week_stats_full else 0
        week_threshold = week_max // 2
        week_qualified = [s for s in week_stats_full if (s.get("games_played", 0) or 0) >= week_threshold]

        alltime_max = max((s.get("games_played", 0) or 0) for s in alltime_stats_full) if alltime_stats_full else 0
        alltime_threshold = alltime_max // 2
        alltime_qualified = [s for s in alltime_stats_full if (s.get("games_played", 0) or 0) >= alltime_threshold]

        # Send 10 full leaderboards: for each position (1..5), latest-week then all-time.
        for pos in [1, 2, 3, 4, 5]:
            for scope_label, stats, thresh, gmax in (
                (week_label_base, week_qualified,    week_threshold,    week_max),
                ("All-Time",       alltime_qualified, alltime_threshold, alltime_max),
            ):
                pos_stats = [s for s in stats if s.get("role_position") == pos]
                if not pos_stats:
                    continue
                label = f"{scope_label} (Position {pos})"
                embeds = format_leaderboard(
                    pos_stats,
                    sort_by="fantasy_points",
                    week_label=label,
                    threshold=thresh,
                    max_games=gmax,
                    limit=10,
                )
                for embed in embeds:
                    await interaction.followup.send(embed=embed, ephemeral=not public)
    except Exception as e:
        logger.exception("Error in summary command")
        await interaction.followup.send(f"❌ Error loading summary: {str(e)}", ephemeral=True)


@tree.command(name="quote", description="Display a random chat message from league matches")
@app_commands.describe(public=PUBLIC_PARAM_DESCRIPTION)
async def quote(interaction: discord.Interaction, public: bool = False):
    division = _require_division(interaction)
    if not division:
        await interaction.response.send_message("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    from db import get_random_quote

    quote_data = get_random_quote(interaction.guild_id)
    if not quote_data:
        await interaction.response.send_message("No chat messages found yet. Try again after a `/refresh_leaderboard`!", ephemeral=True)
        return

    player_name = quote_data.get("player_name", "Unknown")
    message = quote_data.get("message", "")
    match_id = quote_data.get("match_id", 0)
    time_secs = quote_data.get("time", 0)

    # Format time as MM:SS (can be negative for pre-game)
    sign = "-" if time_secs < 0 else ""
    abs_time = abs(time_secs)
    time_str = f"{sign}{abs_time // 60}:{abs_time % 60:02d}"

    dotabuff_link = f"https://www.dotabuff.com/matches/{match_id}"

    await interaction.response.send_message(
        f"💬 **\"{message}\"**\n"
        f"— *{player_name}* at {time_str} ([match]({dotabuff_link}))",
        ephemeral=not public,
    )


@tree.command(name="draftorder", description="Show the Ability Draft pick order for a match")
@app_commands.describe(match_id="The Dota 2 match ID (from Windrun or Dotabuff)", public=PUBLIC_PARAM_DESCRIPTION)
async def draftorder(interaction: discord.Interaction, match_id: str, public: bool = False):
    await interaction.response.defer(ephemeral=not public)

    try:
        mid = int(match_id.strip())
    except ValueError:
        await interaction.followup.send("⚠️ Invalid match ID. Please provide a numeric match ID.", ephemeral=True)
        return

    from windrun import fetch_match
    from draftorder import generate_draft_image

    try:
        data = await fetch_match(mid)
        if not data or not data.get("picks") or not data.get("radiant") or not data.get("dire"):
            await interaction.followup.send(
                f"⏳ Match `{mid}` hasn't been parsed by [windrun.io](https://windrun.io) yet. "
                "Check back in a few minutes — league games are parsed with priority, but there's "
                "still some lag on fresh matches.",
                ephemeral=True,
            )
            return

        image_bytes = await generate_draft_image(data)
        file = discord.File(image_bytes, filename=f"draft_{mid}.png")
        await interaction.followup.send(file=file, ephemeral=not public)
    except Exception as e:
        logger.exception("Error in draftorder command for match %s", match_id)
        await interaction.followup.send(f"❌ Error generating draft order: {e}", ephemeral=True)


@tree.command(name="hi_vs_low", description="Split 10 players into hi-MMR vs lo-MMR teams and set random requirements")
@app_commands.describe(players="10 comma-separated player names, override nicknames, or account IDs", public=PUBLIC_PARAM_DESCRIPTION)
async def hi_vs_low(interaction: discord.Interaction, players: str, public: bool = False):
    await interaction.response.defer(ephemeral=not public)

    raw = [p.strip() for p in players.split(",") if p.strip()]
    if len(raw) != 10:
        await interaction.followup.send(
            f"⚠️ Need exactly 10 players (got {len(raw)}). Comma-separate them.",
            ephemeral=True,
        )
        return

    division = _require_division(interaction)
    from db import get_rating_cache_row, compute_fantasy_adjusted_ratings
    adjustments = (
        compute_fantasy_adjusted_ratings(interaction.guild_id, division["season_start"])
        if division else {}
    )

    resolved: list[dict] = []
    errors: list[str] = []
    for token in raw:
        aid, name, err = await _resolve_player_query(interaction, token)
        if err or aid is None:
            errors.append(f"`{token}`: {err or 'not found'}")
            continue
        cr = get_rating_cache_row(aid) or {}
        base = cr.get("internal_rating")
        if base is None:
            errors.append(f"`{token}`: no cached rating (try `/lookup {token}` first)")
            continue
        adj = adjustments.get(aid)
        rating = adj["adjusted_rating"] if adj else base
        display = cr.get("override_nickname") or cr.get("name") or name or f"Player_{aid}"
        resolved.append({"account_id": aid, "name": display, "rating": rating})

    if errors:
        await interaction.followup.send(
            "⚠️ Couldn't resolve everyone:\n" + "\n".join(errors),
            ephemeral=True,
        )
        return

    # Highest 5 → HIGH team; lowest 5 → LOW team.
    resolved.sort(key=lambda p: -p["rating"])
    high = resolved[:5]
    low  = resolved[5:]
    high_avg = sum(p["rating"] for p in high) / 5
    low_avg  = sum(p["rating"] for p in low) / 5
    diff = high_avg - low_avg

    if diff < 200:
        randoms = 0
        rule = f"Diff {diff:.0f} < 200 — no randoms required, teams are close enough."
    elif diff < 500:
        randoms = 1
        rule = "Diff 200–500 → HIGH team randoms their **1st** ability."
    elif diff < 800:
        randoms = 2
        rule = "Diff 500–800 → HIGH team randoms their **first 2** abilities."
    else:
        randoms = 3
        rule = "Diff > 800 → HIGH team randoms their **first 3** abilities."

    from formatters import EMBED_COLOUR_BLUE
    title = f"🎲 Hi vs Low  •  avg diff {diff:+.0f}"
    embed = discord.Embed(title=title, description=rule, colour=EMBED_COLOUR_BLUE)

    def _team_field(team: list[dict], avg: float) -> str:
        return "\n".join(f"**{p['rating']}**  {p['name']}" for p in team) + f"\n\n_avg: {avg:.0f}_"

    embed.add_field(name=f"🔴 HIGH  ({randoms} random{'s' if randoms != 1 else ''})",
                    value=_team_field(high, high_avg), inline=True)
    embed.add_field(name="🔵 LOW",
                    value=_team_field(low, low_avg), inline=True)
    await interaction.followup.send(embed=embed, ephemeral=not public)




@tree.command(name="tipjar", description="Support the bot creator")
async def tipjar(interaction: discord.Interaction):
    await interaction.response.send_message(
        "If you're enjoying the bot, consider tipping the creator!\n"
        "https://venmo.com/Joe-Dobrow",
        ephemeral=True,
    )


def _season_week_start_preview(week_number: int, season_start_date: str) -> tuple[int, int]:
    """Week boundaries without touching the DB — used by /start_new_season's
    dry run so a preview never writes. Mirrors db._season_week_start."""
    ss = datetime.strptime(season_start_date, "%Y-%m-%d").replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    monday = ss - timedelta(days=ss.weekday())
    target = monday + timedelta(weeks=week_number - 1)
    return int(target.timestamp()), int((target + timedelta(days=7) - timedelta(seconds=1)).timestamp())


@tree.command(name="start_new_season",
              description="[Owner] Roll this division over to a new season: league, dates, weeks, rosters, teams")
@app_commands.describe(
    league="OpenDota league ID for the new season (e.g. 20056 = RD2L Season 39)",
    season_start="First match day of the new season, YYYY-MM-DD",
    weeks="How many weeks to generate",
    first_week="Number the first week 0 (scrim week) or 1. Default 0.",
    costs_url="Published CSV of the Drafted Players tab (Account ID / Name / Winner / Cost)",
    costs_file="Upload the Drafted Players tab as CSV (File \u2192 Download \u2192 .csv). Use instead of costs_url.",
    label="Display name for this season, e.g. \"Season 39\" (auto-detected from the league if omitted)",
    teams_url="RD2L division teams page, for team names + logos",
    dry_run="Preview everything without writing anything",
)
async def start_new_season(
    interaction: discord.Interaction,
    league: int,
    season_start: str,
    weeks: int = 9,
    first_week: int = 0,
    costs_url: str = None,
    costs_file: discord.Attachment = None,
    label: str = None,
    teams_url: str = None,
    dry_run: bool = False,
):
    if not (ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID):
        await interaction.response.send_message("\u26a0\ufe0f Bot owner only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    try:
        datetime.strptime(season_start, "%Y-%m-%d")
    except ValueError:
        await interaction.followup.send("\u26a0\ufe0f `season_start` must be YYYY-MM-DD.", ephemeral=True)
        return
    if first_week not in (0, 1):
        await interaction.followup.send("\u26a0\ufe0f `first_week` must be 0 or 1.", ephemeral=True)
        return
    if not 1 <= weeks <= 30:
        await interaction.followup.send("\u26a0\ufe0f `weeks` must be between 1 and 30.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    current = get_division(guild_id)
    if not current:
        await interaction.followup.send(
            "\u26a0\ufe0f No division configured here yet \u2014 run `/config` first.", ephemeral=True)
        return
    if current["season_start"] == season_start and not dry_run:
        await interaction.followup.send(
            f"\u26a0\ufe0f `{season_start}` is already the active season. Nothing to roll over.",
            ephemeral=True)
        return

    from db import (upsert_division, generate_season_weeks, get_season_weeks,
                    upsert_player_costs, upsert_season_teams, get_player_costs,
                    upsert_season)
    from rd2l import (fetch_drafted_players_csv, parse_drafted_players_csv,
                      fetch_team_identities)

    # ---- gather remote data first; nothing is written until all of it parses
    drafted, identities, problems = [], [], []
    if costs_url and costs_file:
        await interaction.followup.send(
            "\u26a0\ufe0f Give either `costs_url` or `costs_file`, not both.", ephemeral=True)
        return
    if costs_file:
        try:
            raw = (await costs_file.read()).decode("utf-8-sig", errors="replace")
            drafted = parse_drafted_players_csv(raw)
            if not drafted:
                problems.append("costs file parsed but contained no drafted rows "
                                "(every row missing Winner or Cost)")
        except Exception as e:
            problems.append(f"costs file failed: {e}")
    elif costs_url:
        try:
            drafted = await fetch_drafted_players_csv(costs_url)
            if not drafted:
                problems.append("costs CSV parsed but contained no drafted rows "
                                "(every row missing Winner or Cost)")
        except Exception as e:
            problems.append(f"costs CSV failed: {e}")
    if teams_url:
        try:
            identities = await fetch_team_identities(teams_url)
            if not identities:
                problems.append("teams page parsed but yielded no teams")
        except Exception as e:
            problems.append(f"teams page failed: {e}")

    season_label_val = label
    if not season_label_val:
        try:
            import aiohttp as _aio
            async with _aio.ClientSession(timeout=_aio.ClientTimeout(total=10)) as _s:
                async with _s.get(f"https://api.opendota.com/api/leagues/{league}") as _r:
                    if _r.status == 200:
                        season_label_val = ((await _r.json()) or {}).get("name") or None
        except Exception:
            logger.info("league name lookup failed for %d", league, exc_info=False)

    weeks_preview = []
    for n in range(first_week, first_week + weeks):
        st, en = _season_week_start_preview(n - first_week + 1, season_start)
        weeks_preview.append((n, st, en))

    def _d(ts):
        # UTC: these are week-boundary constructs, not moments — see /list_weeks.
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%b %d")

    teams_in_costs = len({d["captain"].strip().lower() for d in drafted})
    lines = [
        f"**League** `{current['league_id']}` \u2192 `{league}`"
        + (f"  ({season_label_val})" if season_label_val else ""),
        f"**Season start** `{current['season_start']}` \u2192 `{season_start}`",
        f"**Weeks** {first_week}\u2013{first_week + weeks - 1} "
        f"({_d(weeks_preview[0][1])} \u2192 {_d(weeks_preview[-1][2])})",
        f"**Rosters** {len(drafted)} drafted players across {teams_in_costs} teams"
        + ("" if costs_url else "  _(no costs_url \u2014 skipped)_"),
        f"**Team identities** {len(identities)} teams"
        + ("" if teams_url else "  _(no teams_url \u2014 skipped)_"),
    ]
    if problems:
        lines.append("")
        lines += [f"\u26a0\ufe0f {p}" for p in problems]

    if dry_run or problems:
        head = "\U0001f50d **Dry run** \u2014 nothing written." if dry_run \
               else "\u274c **Aborted** \u2014 nothing written (fix the errors below and re-run)."
        wk = "\n".join(f"\u2022 Week {n}: {_d(st)} \u2192 {_d(en)}" for n, st, en in weeks_preview[:4])
        more = f"\n\u2022 \u2026 through Week {weeks_preview[-1][0]}" if len(weeks_preview) > 4 else ""
        await interaction.followup.send(
            head + "\n\n" + "\n".join(lines) + "\n\n**Week boundaries**\n" + wk + more,
            ephemeral=True)
        return

    # ---- writes
    upsert_division(guild_id, league, current["region"], current["game_mode"],
                    season_start, current.get("scold_channel_id"))
    generate_season_weeks(guild_id, season_start, weeks, first_week=first_week)
    if drafted:
        upsert_player_costs(guild_id, season_start, [
            {"account_id": d["account_id"], "cost": d["cost"], "captain": d["captain"]}
            for d in drafted
        ])
    if identities:
        upsert_season_teams(guild_id, season_start, identities)
    upsert_season(guild_id, season_start, league_id=league, label=season_label_val)

    prev_costs = len(get_player_costs(guild_id, current["season_start"]))
    await interaction.followup.send(
        "\u2705 **Season rolled over.**\n\n" + "\n".join(lines) +
        f"\n\nPrevious season `{current['season_start']}` kept its {prev_costs} roster rows "
        "and all its matches \u2014 nothing was deleted.\n"
        "Next: run `/refresh_leaderboard` to pull matches under the new league ID, "
        "then `/sync_roles_channels` for Discord roles.",
        ephemeral=True)


@tree.command(name="sync_roles_channels",
              description="[Owner] Sync players/captains roles + rebuild Captain Rooms voice channels from RD2L")
@app_commands.describe(
    players_url="RD2L players page URL (falls back to env RD2L_PLAYERS_URL)",
    captains_url="RD2L captains page URL (falls back to env RD2L_CAPTAINS_URL)",
    players_role="Role name to sync for players (default: 'players')",
    captains_role="Role name to sync for captains (default: 'captains')",
    category="Category to rebuild voice channels in (default: 'Captain Rooms')",
    dry_run="Preview matches + unmatched without applying any changes",
)
async def sync_roles_channels(
    interaction: discord.Interaction,
    players_url: str = None,
    captains_url: str = None,
    players_role: str = "players",
    captains_role: str = "captains",
    category: str = "Captain Rooms",
    dry_run: bool = False,
):
    is_admin = interaction.user.guild_permissions.administrator
    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    if not (is_admin or is_owner):
        await interaction.response.send_message("⚠️ Admins/owner only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    p_url = players_url or os.environ.get("RD2L_PLAYERS_URL")
    c_url = captains_url or os.environ.get("RD2L_CAPTAINS_URL")
    if not p_url or not c_url:
        await interaction.followup.send(
            "⚠️ Missing URLs. Set fly secrets `RD2L_PLAYERS_URL` + "
            "`RD2L_CAPTAINS_URL`, or pass `players_url:` / `captains_url:`.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    def _find_ci(items, name):
        low = name.lower()
        return next((x for x in items if x.name.lower() == low), None)
    p_role = _find_ci(guild.roles, players_role)
    c_role = _find_ci(guild.roles, captains_role)
    cat = _find_ci(guild.categories, category)
    missing = []
    if not p_role:  missing.append(f"role `{players_role}`")
    if not c_role:  missing.append(f"role `{captains_role}`")
    if not cat:     missing.append(f"category `{category}`")
    # Dry-run just needs to preview matches — no need to gate on missing
    # roles/category since we won't touch them. Real run hard-errors.
    if missing and not dry_run:
        await interaction.followup.send(f"⚠️ Not found in this guild: {', '.join(missing)}", ephemeral=True)
        return

    from rd2l import fetch_roster_from_page, fetch_discord_handles

    try:
        await interaction.edit_original_response(content="⏳ Fetching rosters from rd2l.gg …")
        players = await fetch_roster_from_page(p_url, label="players")
        captains = await fetch_roster_from_page(c_url, label="captains")

        # Captains sometimes appear only on the captains page and not in the
        # players CSV — treat both as separate rosters. Union the account_ids
        # so we only scrape each profile once.
        all_ids = sorted({p["account_id"] for p in players} | {c["account_id"] for c in captains})
        await interaction.edit_original_response(
            content=f"⏳ Scraping Discord handles for {len(all_ids)} players (~{len(all_ids)*0.5:.0f}s) …"
        )
        handles = await fetch_discord_handles(all_ids)
    except Exception as e:
        logger.exception("rd2l fetch failed")
        await interaction.followup.send(f"❌ rd2l fetch failed: {e}", ephemeral=True)
        return

    # Load per-account discord overrides in one query so we can prefer them
    # over the scraped rd2l handle. Set via /set_player discord_handle:...
    from db import _conn as _db_conn
    all_ids_list = list(all_ids)
    overrides: dict[int, str] = {}
    if all_ids_list:
        placeholders = ",".join("?" * len(all_ids_list))
        with _db_conn() as conn:
            for r in conn.execute(
                f"SELECT account_id, discord_override FROM skill_overrides "
                f"WHERE discord_override IS NOT NULL AND discord_override != '' "
                f"AND account_id IN ({placeholders})",
                tuple(all_ids_list),
            ).fetchall():
                overrides[r["account_id"]] = r["discord_override"]

    for p in players:
        p["discord_handle"] = overrides.get(p["account_id"]) or handles.get(p["account_id"])
        p["overridden"] = p["account_id"] in overrides
    for c in captains:
        c["discord_handle"] = overrides.get(c["account_id"]) or handles.get(c["account_id"])
        c["overridden"] = c["account_id"] in overrides

    # Build a handle→Member map. Uses guild.fetch_members which requires
    # the `members` privileged intent both in code (Intents.members) AND
    # enabled in the Discord Developer Portal.
    await interaction.edit_original_response(content="⏳ Loading guild members …")
    try:
        members = [m async for m in guild.fetch_members(limit=None)]
    except discord.ClientException as e:
        await interaction.followup.send(
            f"❌ Can't load guild members: {e}\n"
            "Enable **Server Members Intent** for this bot at "
            "https://discord.com/developers/applications → your bot → Bot tab.",
            ephemeral=True,
        )
        return

    import re as _re
    def _norm(s: str) -> str:
        return (s or "").strip().lower().lstrip("@")
    handle_map: dict[str, discord.Member] = {}
    for m in members:
        # New-style username (no #discriminator), old-style user#1234, nick.
        for key in filter(None, [m.name, f"{m.name}#{m.discriminator}", m.global_name, m.nick]):
            handle_map.setdefault(_norm(key), m)

    def _candidates(handle: str) -> list[str]:
        """Extract every reasonable string to try as a match key from a scraped
        rd2l handle. Handles old-style `user#1234`, parenthetical notes, stray
        punctuation, and space-before-#."""
        if not handle:
            return []
        raw = handle.strip()
        cands: list[str] = [raw]
        # Collapse whitespace around # (e.g. "redground #4230" → "redground#4230")
        cands.append(_re.sub(r"\s*#\s*", "#", raw))
        # Strip trailing "#1234" (or with stray chars before it like "?#3582")
        cands.append(_re.sub(r"\s*[^\w]*#\s*\d+.*$", "", raw))
        # Bare stripped-of-# (username only) — handles migrated users where
        # rd2l still has the old discriminator on file.
        for c in list(cands):
            if "#" in c:
                cands.append(c.split("#", 1)[0])
        # Content inside parens (e.g. "Name (real_handle)" → "real_handle")
        for m in _re.finditer(r"\(([^)]+)\)", raw):
            cands.append(m.group(1))
        # Same string with the parenthetical stripped
        no_paren = _re.sub(r"\s*\([^)]*\)\s*", " ", raw).strip()
        if no_paren:
            cands.append(no_paren)
        # Trim trailing punctuation on each candidate
        cands = [_re.sub(r"[?!.,\s]+$", "", c).strip() for c in cands]
        return [c for c in dict.fromkeys(cands) if c]  # dedupe, preserve order

    def _match(handle: str | None) -> discord.Member | None:
        if not handle:
            return None
        for c in _candidates(handle):
            hit = handle_map.get(_norm(c))
            if hit:
                return hit
        return None

    matched_p, unmatched_p = [], []
    for p in players:
        m = _match(p["discord_handle"])
        (matched_p if m else unmatched_p).append((p, m))
    matched_c, unmatched_c = [], []
    for c in captains:
        m = _match(c["discord_handle"])
        (matched_c if m else unmatched_c).append((c, m))

    def _fmt_unmatched(rows):
        def _line(r):
            handle = r["discord_handle"] or "none scraped"
            src = " (override)" if r.get("overridden") else ""
            return f"• `{r['name']}` (id `{r['account_id']}`) — handle: `{handle}`{src}"
        return "\n".join(_line(r) for r, _ in rows) or "_(none)_"

    if dry_run:
        setup_note = f"\n⚠️ Not found (would block real run): {', '.join(missing)}" if missing else ""
        summary = (
            f"🔍 **Dry run** — no changes applied.\n"
            f"Players: {len(matched_p)} matched / {len(unmatched_p)} unmatched\n"
            f"Captains: {len(matched_c)} matched / {len(unmatched_c)} unmatched"
            f"{setup_note}\n\n"
            f"**Unmatched players:**\n{_fmt_unmatched(unmatched_p)}\n\n"
            f"**Unmatched captains:**\n{_fmt_unmatched(unmatched_c)}"
        )
        await interaction.followup.send(summary[:2000], ephemeral=True)
        return

    # Apply roles: strip from everyone, then add to matched. Doing this via
    # the member iteration + per-role add/remove avoids replacing anyone's
    # full role set (only the target role changes per member).
    err_role_remove = 0
    err_role_add_p = 0
    err_role_add_c = 0
    err_ch_delete = 0
    err_ch_create = 0
    first_error: str | None = None
    def _note_err(exc):
        nonlocal first_error
        if first_error is None:
            first_error = f"{type(exc).__name__}: {exc}"

    await interaction.edit_original_response(content="⏳ Removing existing roles …")
    for m in members:
        try:
            to_remove = [r for r in (p_role, c_role) if r in m.roles]
            if to_remove:
                await m.remove_roles(*to_remove, reason="/sync_roles_channels: reset")
        except Exception as e:
            err_role_remove += 1
            _note_err(e)
            logger.exception("role removal failed for %s", m)

    await interaction.edit_original_response(content="⏳ Adding roles to matched members …")
    for _, m in matched_p:
        try:
            await m.add_roles(p_role, reason="/sync_roles_channels: players")
        except Exception as e:
            err_role_add_p += 1
            _note_err(e)
            logger.exception("add players role failed for %s", m)
    for _, m in matched_c:
        try:
            await m.add_roles(c_role, reason="/sync_roles_channels: captains")
        except Exception as e:
            err_role_add_c += 1
            _note_err(e)
            logger.exception("add captains role failed for %s", m)

    # Rebuild Captain Rooms: nuke everything in the category, then create one
    # voice channel per captain (alphabetical, captain's own name).
    await interaction.edit_original_response(content=f"⏳ Rebuilding `{category}` channels …")
    for ch in list(cat.channels):
        try:
            await ch.delete(reason="/sync_roles_channels: rebuild")
        except Exception as e:
            err_ch_delete += 1
            _note_err(e)
            logger.exception("channel delete failed for %s", ch)

    for c in sorted(captains, key=lambda x: x["name"].lower()):
        try:
            await guild.create_voice_channel(name=c["name"], category=cat,
                                             reason="/sync_roles_channels: per-captain room")
        except Exception as e:
            err_ch_create += 1
            _note_err(e)
            logger.exception("channel create failed for captain %s", c["name"])

    total_errs = err_role_remove + err_role_add_p + err_role_add_c + err_ch_delete + err_ch_create
    header = "✅ Sync complete." if total_errs == 0 else f"⚠️ Sync finished with {total_errs} error(s)."
    summary = (
        f"{header}\n"
        f"• Players: {len(matched_p) - err_role_add_p} roled / {len(unmatched_p)} unmatched"
        f"{f' / {err_role_add_p} failed' if err_role_add_p else ''}\n"
        f"• Captains: {len(matched_c) - err_role_add_c} roled / {len(unmatched_c)} unmatched"
        f"{f' / {err_role_add_c} failed' if err_role_add_c else ''}\n"
        f"• Role-remove failures: {err_role_remove}\n"
        f"• Rebuilt {len(captains) - err_ch_create} voice channels in `{category}`"
        f"{f' ({err_ch_delete} delete + {err_ch_create} create failures)' if (err_ch_delete or err_ch_create) else ''}\n"
    )
    if first_error:
        summary += f"\n**First error:** `{first_error}`"
        if "Missing Permissions" in first_error or "Forbidden" in first_error:
            summary += (
                "\n_Fix: grant the bot's role **Manage Roles** + **Manage Channels**, "
                "and drag its role **above** the `players` / `captains` roles in Server Settings → Roles._"
            )
        summary += "\n"
    if unmatched_p:
        summary += f"\n**Unmatched players:**\n{_fmt_unmatched(unmatched_p)}\n"
    if unmatched_c:
        summary += f"\n**Unmatched captains:**\n{_fmt_unmatched(unmatched_c)}"
    await interaction.followup.send(summary[:2000], ephemeral=True)


@tree.command(name="refresh_leaderboard", description="[Admin] Manually trigger a data fetch from OpenDota")
async def refresh_leaderboard(interaction: discord.Interaction):
    # Server admins or bot owner can refresh
    is_admin = interaction.user.guild_permissions.administrator
    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    if not is_admin and not is_owner:
        await interaction.response.send_message("⚠️ Only admins can use this command.", ephemeral=True)
        return

    division = _require_division(interaction)
    if not division:
        await interaction.response.send_message("⚠️ No division configured. Use `/config` first.", ephemeral=True)
        return

    await interaction.response.send_message("⏳ Fetching match data...", ephemeral=True)
    try:
        count = await fetch_and_store_matches_for_division(interaction.guild_id)
        # Kick off the same windrun + OpenDota rating refresh /refresh_ratings does,
        # in the background so this command returns fast. User gets a second ping
        # when the rating refresh completes (~5s/player due to windrun rate limit).
        from db import get_guild_player_account_ids
        aids = get_guild_player_account_ids(interaction.guild_id)
        eta_note = ""
        if aids:
            eta_min = max(1, round(len(aids) * 5 / 60))
            asyncio.create_task(_refresh_ratings_task(aids, interaction))
            eta_note = (
                f" Refreshing internal ratings for {len(aids)} players "
                f"in the background (~{eta_min} min); I'll ping you here when done."
            )
        await interaction.followup.send(
            f"✅ Fetched {count} new match(es).{eta_note}",
            ephemeral=True,
        )
    except Exception as e:
        logger.exception("Refresh failed")
        await interaction.followup.send(f"❌ Error during fetch: {e}", ephemeral=True)


@tree.command(name="nuke", description="[Admin] Wipe all data for this server and re-fetch")
async def nuke(interaction: discord.Interaction):
    # Server admins or bot owner can nuke their own server's data
    is_admin = interaction.user.guild_permissions.administrator
    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    if not is_admin and not is_owner:
        await interaction.response.send_message("hahaa nice try loser", ephemeral=True)
        return

    division = _require_division(interaction)
    if not division:
        await interaction.response.send_message("⚠️ No division configured. Use `/config` first.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        from db import nuke_data
        nuke_data(interaction.guild_id)
        count = await fetch_and_store_matches_for_division(interaction.guild_id)
        await interaction.followup.send(f"✅ Data wiped and re-fetched {count} match(es).", ephemeral=True)
    except Exception as e:
        logger.exception("Nuke failed")
        await interaction.followup.send(f"❌ Error during nuke: {e}", ephemeral=True)


# ---------------------------------------------------------------------------
# Scold channel — delete messages and reply with a scolding
# ---------------------------------------------------------------------------

import random

SCOLD_MESSAGES = [
    "No posting here. Your message has been deleted.",
    "This channel is read-only. Nice try though.",
    "Nope. Message deleted.",
    "You can look, but you can't post.",
    "This is a no-posting zone. Message removed.",
    "Not here. Your message has been banished.",
    "Read-only channel. Your message didn't make it.",
    "Denied. This channel is for viewing only.",
]


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # --- "badmin" correction (all channels) ---
    if "badmin" in message.content.lower():
        await message.reply("I think you meant to say 'goodmin'")

    # --- Scold channel (delete + scold) ---
    scold_channel_id = get_scold_channel(message.guild.id)
    if scold_channel_id and message.channel.id == scold_channel_id:
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention} {random.choice(SCOLD_MESSAGES)}",
                delete_after=5,
            )
        except discord.Forbidden:
            logger.warning("Missing permissions to delete message in scold channel %d", scold_channel_id)
        except Exception:
            logger.exception("Error in scold channel handler")


# ---------------------------------------------------------------------------
# Weekly auto-fetch (Monday 6:00 AM UTC)
# ---------------------------------------------------------------------------

@tasks.loop(time=datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0).time())
async def weekly_fetch():
    # Only run on Mondays (weekday() == 0)
    if datetime.now(timezone.utc).weekday() != 0:
        return
    logger.info("Weekly fetch triggered (Monday 06:00 UTC)")

    # Fetch for all configured divisions
    divisions = get_all_divisions()
    total_count = 0
    for div in divisions:
        try:
            count = await fetch_and_store_matches_for_division(div["guild_id"])
            total_count += count
            logger.info(f"Weekly fetch for guild {div['guild_id']}: {count} match(es)")
        except Exception:
            logger.exception(f"Weekly fetch failed for guild {div['guild_id']}")

    logger.info(f"Weekly fetch complete: {total_count} total match(es) across {len(divisions)} division(s)")


# ---------------------------------------------------------------------------
# Bot startup
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Initialize database
    init_db()

    try:
        synced = await tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s).")
    except Exception:
        logger.exception("Failed to sync commands")
    weekly_fetch.start()

    # Start the AD helper web server
    try:
        from botstats.server import start_web_server
        await start_web_server()
    except Exception:
        logger.exception("Failed to start AD helper web server")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
