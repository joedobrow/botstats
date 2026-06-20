import asyncio
import discord
from discord import app_commands
from discord.ext import tasks
import logging
from datetime import datetime, timezone, timedelta

from config import DISCORD_TOKEN, ADMIN_USER_ID, REGION_CLUSTERS, GAME_MODE_FILTERS, LOOKUP_CHANNEL_IDS
from fetcher import fetch_and_store_matches_for_division, fetch_and_store_drafts_for_guild
from db import init_db, get_division, upsert_division, get_all_divisions, get_scold_channel
from formatters import format_leaderboard, format_player_stats, format_team_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


# ---------------------------------------------------------------------------
# Helper to check division config
# ---------------------------------------------------------------------------

def _require_division(interaction: discord.Interaction):
    """Get division for this guild, or None if not configured."""
    return get_division(interaction.guild_id)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

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


@tree.command(name="leaderboard", description="Show the leaderboard sorted by a stat")
@app_commands.describe(
    week="Season week number (1, 2, 3...) or -1 for all-time. Leave blank for all-time.",
    stat="Which stat to sort by",
    pos="Filter by position (1-5, optional)",
    all="Show every player (no top-10 cap, no min-games threshold)",
    debug="[Owner only] expose value components (cost, diff)",
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
    app_commands.Choice(name="Observer Kills",          value="observer_kills"),
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
async def leaderboard(interaction: discord.Interaction, stat: app_commands.Choice[str], week: int = None, pos: int = None, all: bool = False, debug: bool = False):
    await interaction.response.defer()

    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    debug = bool(debug) and is_owner

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    season_start = division["season_start"]

    from db import get_stats_for_season_week, get_all_time_stats

    try:
        if week is None or week == -1:
            # All-time stats (default)
            stats = get_all_time_stats(guild_id, season_start)
            week_label = "All-Time"
        else:
            # Specific season week (0, 1, 2, ...)
            stats = get_stats_for_season_week(guild_id, week, season_start)
            week_label = f"Week {week}"

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
            await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.exception(f"Error in leaderboard command for week {week}")
        await interaction.followup.send(f"❌ Error loading leaderboard: {str(e)}", ephemeral=True)


@tree.command(name="player", description="Show detailed stats for a specific player")
@app_commands.describe(
    name="Player name (partial match), override nickname, or numeric account ID",
    week="Season week number (1, 2, 3...) or -1 for all-time. Leave blank for all-time.",
    debug="[Owner only] expose expected/value rows in the Draft block",
)
async def player(interaction: discord.Interaction, name: str, week: int = None, debug: bool = False):
    await interaction.response.defer()

    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    debug = bool(debug) and is_owner

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
    season_start = division["season_start"]

    from db import get_stats_for_season_week, get_all_time_stats

    try:
        if week is None or week == -1:
            stats = get_all_time_stats(guild_id, season_start)
            week_label = "All-Time"
        else:
            stats = get_stats_for_season_week(guild_id, week, season_start)
            week_label = f"Week {week}"

        match_row = next((p for p in stats if p.get("account_id") == account_id), None)

        # Look up the player's internal rating from the cache and apply the
        # same fantasy adjustment that /players and /lookup use.
        from db import get_rating_cache_row, compute_fantasy_adjusted_ratings
        rating: int | None = None
        cache_row = get_rating_cache_row(account_id)
        if cache_row and cache_row.get("internal_rating") is not None:
            rating = cache_row["internal_rating"]
            adjustments = compute_fantasy_adjusted_ratings(
                interaction.guild_id, division["season_start"],
            )
            adj = adjustments.get(account_id)
            if adj:
                rating = adj["adjusted_rating"]

        if match_row is None:
            # No matches in this guild — still surface the rating, links, and ID.
            display = (
                (cache_row or {}).get("override_nickname")
                or (cache_row or {}).get("name")
                or resolved_name
                or f"Account {account_id}"
            )
            from formatters import EMBED_COLOUR_BLUE
            title = f"🎮 {display}"
            if rating is not None:
                title += f"  •  Rating {rating}"
            dotabuff_url = f"https://www.dotabuff.com/players/{account_id}"
            windrun_url  = f"https://windrun.io/players/{account_id}"
            embed = discord.Embed(
                title=title,
                url=dotabuff_url,
                description=f"No matches in this server for {week_label.lower()}.",
                colour=EMBED_COLOUR_BLUE,
            )
            embed.add_field(
                name="🔗 Profiles",
                value=f"[Dotabuff]({dotabuff_url}) · [Windrun]({windrun_url})",
                inline=False,
            )
            embed.set_footer(text=f"Account ID: {account_id}")
            await interaction.followup.send(embed=embed)
            return

        # Pool of league players with enough games to give meaningful percentiles.
        qualified_pool = [s for s in stats if (s.get("games_played") or 0) >= 5]
        embed = format_player_stats(
            match_row, week_label=week_label, debug=debug, rating=rating,
            qualified_pool=qualified_pool,
        )
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.exception(f"Error in player command for week {week}")
        await interaction.followup.send(f"❌ Error loading player stats: {str(e)}", ephemeral=True)


@tree.command(name="team_stats", description="Show strengths/weaknesses of a player's team vs the rest of the league")
@app_commands.describe(name="Any player on the team — partial name, override nickname, or numeric account ID")
async def team_stats(interaction: discord.Interaction, name: str):
    await interaction.response.defer()

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured.", ephemeral=True)
        return

    account_id, resolved_name, err = await _resolve_player_query(interaction, name)
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    from db import get_player_costs, get_team_match_aggregates, get_captain_account_ids
    costs = get_player_costs(interaction.guild_id, division["season_start"])
    target_cost = costs.get(account_id)
    captain: str | None = None
    if target_cost and target_cost.get("captain"):
        captain = target_cost["captain"]
    else:
        # Maybe they ARE a captain — look up reverse mapping.
        captain_aids = get_captain_account_ids(
            interaction.guild_id, division["season_start"],
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
    team_aggs = get_team_match_aggregates(interaction.guild_id, division["season_start"])
    target_agg = team_aggs.get(captain)
    if not target_agg:
        await interaction.followup.send(
            f"⚠️ Team `{captain}` has no game data yet.",
            ephemeral=True,
        )
        return

    embed = format_team_stats(
        team_label=captain,  # captain name used as team label until proper team names are wired in
        target_agg=target_agg,
        all_team_aggs=list(team_aggs.values()),
    )
    await interaction.followup.send(embed=embed)


@tree.command(name="lookup", description="[Owner] Diagnostic lookup — full methodology breakdown for a player")
@app_commands.describe(
    query="Player name (partial match) or numeric account ID",
    force_refresh="Skip the cache and re-fetch from windrun/OpenDota",
)
async def lookup(interaction: discord.Interaction, query: str, force_refresh: bool = False):
    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    in_admin_channel = interaction.channel_id in LOOKUP_CHANNEL_IDS
    if not (is_owner or in_admin_channel):
        await interaction.response.send_message(
            "🔒 This command is for admins only — please use `/player` to look up a player.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
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
    adjustment_pct: float | None = None
    division = _require_division(interaction)
    if division:
        adjustments = compute_fantasy_adjusted_ratings(
            interaction.guild_id, division["season_start"],
        )
        adj = adjustments.get(account_id)
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
            cached_avatar=cache_row.get("avatar_url"),
            updated_at=cache_row.get("updated_at"),
            adjustment_pct=adjustment_pct,
        )
        await interaction.followup.send(embed=embed)
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
    await interaction.followup.send(embed=embed)

    # Side-effect: refresh this player's cache row so /players stays current
    # without anyone needing to run /refresh_ratings. Write whenever we got
    # ANY fresh signal — windrun-only or OpenDota-only is still better than
    # leaving the cache stale.
    if have_any_signal and not has_override_rating:
        try:
            from opendota_lookup import estimate_mmr_from_rank_tier
            from db import upsert_rating_cache_row
            from formatters import _resolve_internal_rating
            raw_wr = (wr or {}).get("rating")
            rank_tier = (od or {}).get("rank_tier")
            lb_rank = (od or {}).get("leaderboard_rank")
            mmr_est = estimate_mmr_from_rank_tier(rank_tier, lb_rank)
            eff_wr  = override["windrun_rating"] if override and override.get("windrun_rating") is not None else raw_wr
            eff_mmr = override["ranked_mmr"]     if override and override.get("ranked_mmr") is not None     else mmr_est
            ranked_last_year = (od_counts or {}).get("last_year_ranked")
            ranked_all_time  = (od_counts or {}).get("all_time_ranked")
            info = _resolve_internal_rating(
                override=override,
                ad_last=ad_last_year,
                ad_all=ad_all_time,
                ranked_last=ranked_last_year,
                wr_rating=eff_wr,
                ranked_mmr=eff_mmr,
                ranked_all=ranked_all_time,
            )
            name = (wr or {}).get("nickname") or ((od or {}).get("profile") or {}).get("personaname") or player_name
            upsert_rating_cache_row({
                "account_id":       account_id,
                "name":             name,
                "internal_rating":  info[0] if info else None,
                "raw_windrun":      raw_wr,
                "mmr_estimate":     mmr_est,
                "ad_last_year":     ad_last_year,
                "ad_all_time":      ad_all_time,
                "ranked_last_year": ranked_last_year,
                "explanation":      info[1] if info else None,
                "avatar_url":       (wr or {}).get("avatar"),
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
        return int(q), None, None

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


@tree.command(name="set_player", description="[Owner] Override a player's nickname, windrun, or ranked MMR used by /lookup and /players")
@app_commands.describe(
    name="Player name (partial match) or numeric account ID",
    nickname="Custom display name (omit to keep existing)",
    windrun="Manual windrun rating (omit to keep existing)",
    ranked_mmr="Manual ranked MMR (omit to keep existing)",
    rating="Manual internal rating — overrides EVERYTHING; windrun/ranked_mmr/trust-weights are ignored",
    note="Optional note (e.g. 'smurf — real rank is 10k')",
    clear="Set True to remove any existing override for this player",
    delete="Set True to remove the player entirely AND permanently exclude them from /players",
    restore="Set True to undo a prior delete (removes this guild's exclusion entry for them)",
)
async def set_player(
    interaction: discord.Interaction,
    name: str,
    nickname: str = None,
    windrun: float = None,
    ranked_mmr: int = None,
    rating: int = None,
    note: str = None,
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

    if windrun is None and ranked_mmr is None and rating is None and note is None and nickname is None:
        # Show current state
        existing = get_skill_override(account_id)
        if existing:
            await interaction.followup.send(
                f"Current override for {label}:\n"
                f"• nickname: `{existing.get('nickname')}`\n"
                f"• windrun: `{existing.get('windrun_rating')}`\n"
                f"• ranked_mmr: `{existing.get('ranked_mmr')}`\n"
                f"• rating: `{existing.get('internal_rating_override')}`\n"
                f"• note: `{existing.get('note')}`\n"
                f"• set by `{existing.get('set_by_name')}` at `{existing.get('set_at')}`",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"No override set for {label}.\n"
                "Use `nickname:`, `windrun:`, `ranked_mmr:`, and/or `rating:` to set one. `clear:True` to remove.",
                ephemeral=True,
            )
        return

    upsert_skill_override(
        account_id=account_id,
        windrun_rating=windrun,
        ranked_mmr=ranked_mmr,
        note=note,
        nickname=nickname,
        internal_rating_override=rating,
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
    eff_wr  = full.get("windrun_rating") if full.get("windrun_rating") is not None else existing.get("raw_windrun")
    eff_mmr = full.get("ranked_mmr")     if full.get("ranked_mmr")     is not None else existing.get("mmr_estimate")
    info = _resolve_internal_rating(
        override=full,
        ad_last=existing.get("ad_last_year"),
        ad_all=None,
        ranked_last=existing.get("ranked_last_year"),
        wr_rating=eff_wr,
        ranked_mmr=eff_mmr,
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
        "mmr_estimate":     eff_mmr,
        "ad_last_year":     existing.get("ad_last_year"),
        "ranked_last_year": existing.get("ranked_last_year"),
        "explanation":      info[1] if info else existing.get("explanation"),
        "avatar_url":       existing.get("avatar_url"),
    })

    parts = []
    if nickname is not None:   parts.append(f"nickname=`{nickname}`")
    if windrun is not None:    parts.append(f"windrun=`{windrun}`")
    if ranked_mmr is not None: parts.append(f"ranked_mmr=`{ranked_mmr}`")
    if rating is not None:     parts.append(f"rating=`{rating}`")
    if note is not None:       parts.append(f"note=`{note}`")
    rating_note = f" · cached rating: **{info[0]}**" if info else ""
    await interaction.followup.send(
        f"✅ Updated override for {label}: {', '.join(parts)}{rating_note}",
        ephemeral=True,
    )


async def _refresh_ratings_task(aids: list[int], interaction: discord.Interaction):
    """Long-running background task that fetches windrun + OpenDota for each
    account_id and populates player_ratings_cache."""
    from windrun import fetch_player as fetch_windrun_player
    from opendota_lookup import (
        fetch_player_profile, fetch_player_game_counts, estimate_mmr_from_rank_tier,
    )
    from db import get_skill_override, upsert_rating_cache_row
    from formatters import _resolve_internal_rating

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
            rank_tier = (od or {}).get("rank_tier")
            lb_rank   = (od or {}).get("leaderboard_rank")
            mmr_est   = estimate_mmr_from_rank_tier(rank_tier, lb_rank)

            override = get_skill_override(aid)
            eff_wr  = override["windrun_rating"] if override and override.get("windrun_rating") is not None else raw_wr
            eff_mmr = override["ranked_mmr"]     if override and override.get("ranked_mmr") is not None     else mmr_est

            ad_last     = (od_counts or {}).get("last_year_ad")
            ad_all      = (od_counts or {}).get("all_time_ad")
            ranked_last = (od_counts or {}).get("last_year_ranked")
            ranked_all  = (od_counts or {}).get("all_time_ranked")

            info = None
            if od_counts is not None or (override and override.get("internal_rating_override") is not None):
                info = _resolve_internal_rating(
                    override=override,
                    ad_last=ad_last, ad_all=ad_all, ranked_last=ranked_last,
                    wr_rating=eff_wr, ranked_mmr=eff_mmr, ranked_all=ranked_all,
                )

            name = (wr_data or {}).get("nickname")
            if not name and od:
                name = (od.get("profile") or {}).get("personaname")

            upsert_rating_cache_row({
                "account_id":       aid,
                "name":             name,
                "internal_rating":  info[0] if info else None,
                "raw_windrun":      raw_wr,
                "mmr_estimate":     mmr_est,
                "ad_last_year":     ad_last,
                "ranked_last_year": ranked_last,
                "explanation":      info[1] if info else None,
                "avatar_url":       (wr_data or {}).get("avatar"),
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
)
async def players(interaction: discord.Interaction, show_all: bool = False, debug: bool = False):
    await interaction.response.defer()

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
            adj = adjustments.get(r["account_id"])
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
        await interaction.followup.send(embed=embed)


@tree.command(name="matches", description="Show matches with Dotabuff links")
@app_commands.describe(
    week="Season week number (1, 2, 3...) or -1 for all matches. Leave blank for latest week.",
    player="Filter to matches a specific player appeared in (partial name match)."
)
async def matches(interaction: discord.Interaction, week: int = None, player: str = None):
    # Defer immediately to avoid 3-second timeout
    await interaction.response.defer()

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    season_start = division["season_start"]

    from db import get_matches_for_season_week, get_latest_matches, get_all_matches, get_match_ids_for_player

    if week is None:
        match_list = get_latest_matches(guild_id)
        week_label = "Latest Week"
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

    from formatters import format_matches_list
    embed = format_matches_list(match_list, week_label=week_label)
    await interaction.followup.send(embed=embed)


@tree.command(name="summary", description="Show 10 fantasy-points leaderboards: each position × latest-week and all-time")
async def summary(interaction: discord.Interaction):
    await interaction.response.defer()

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    season_start = division["season_start"]

    from db import get_stats_for_season_week, get_all_time_stats, get_latest_season_week

    try:
        latest_week = get_latest_season_week(guild_id, season_start)

        # Fetch both stat sets once (each is one DB query, much cheaper than
        # re-fetching per position).
        if latest_week:
            week_stats_full = get_stats_for_season_week(guild_id, latest_week, season_start)
            week_label_base = f"Week {latest_week}"
        else:
            week_stats_full = []
            week_label_base = "Latest Week"
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
                    await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.exception("Error in summary command")
        await interaction.followup.send(f"❌ Error loading summary: {str(e)}", ephemeral=True)


@tree.command(name="quote", description="Display a random chat message from league matches")
async def quote(interaction: discord.Interaction):
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
        f"— *{player_name}* at {time_str} ([match]({dotabuff_link}))"
    )


@tree.command(name="draftorder", description="Show the Ability Draft pick order for a match")
@app_commands.describe(match_id="The Dota 2 match ID (from Windrun or Dotabuff)")
async def draftorder(interaction: discord.Interaction, match_id: str):
    await interaction.response.defer()

    # Validate match ID
    try:
        mid = int(match_id.strip())
    except ValueError:
        await interaction.followup.send("⚠️ Invalid match ID. Please provide a numeric match ID.", ephemeral=True)
        return

    from db import get_draft_picks_for_display, get_match_replay_info, upsert_draft_picks
    from draftorder import generate_draft_image_from_db

    try:
        # 1) Try our local DB first — same-day data from replay parsing.
        draft_data = get_draft_picks_for_display(mid)
        if draft_data:
            # If any cached picks are still "unknown" (OpenDota was still
            # parsing at the time of first /draftorder), retry resolution.
            if any(p.get("ability_name") in ("", "unknown") for p in draft_data["picks"]):
                try:
                    from opendota_lookup import resolve_picks_with_opendota
                    from db import _conn
                    with _conn() as conn:
                        db_picks = conn.execute("""
                            SELECT pick_order, player_id, pick_type, ability_name, m_n_ability_id
                            FROM draft_picks WHERE match_id = ?
                            ORDER BY pick_order
                        """, (mid,)).fetchall()
                    # Reconstruct parser-shaped pick dicts. We don't have
                    # hero_id stored on draft_picks rows; OpenDota provides
                    # it directly via player_slot, so we leave it 0 here.
                    pseudo = [
                        {
                            "order":          r["pick_order"],
                            "player_id":      r["player_id"],
                            "type":           r["pick_type"],
                            "ability_name":   r["ability_name"],
                            "m_n_ability_id": r["m_n_ability_id"],
                            "hero_id":        0,
                        }
                        for r in db_picks
                    ]
                    resolved = await resolve_picks_with_opendota(mid, pseudo)
                    updates = []
                    for r, new in zip(db_picks, resolved):
                        if (new["ability_name"] not in ("", "unknown")
                                and r["ability_name"] != new["ability_name"]):
                            updates.append({
                                "pick_order":     new["order"],
                                "player_id":      new["player_id"],
                                "ability_name":   new["ability_name"],
                                "pick_type":      new["type"],
                                "m_n_ability_id": new["m_n_ability_id"],
                                "ability_id":     0,
                            })
                    if updates:
                        upsert_draft_picks(mid, updates)
                        logger.info("Match %d: backfilled %d picks via OpenDota", mid, len(updates))
                        draft_data = get_draft_picks_for_display(mid)
                except Exception:
                    logger.exception("OpenDota re-resolve failed for %d", mid)
            logger.info("Serving draftorder for match %d from DB (%d picks)", mid, len(draft_data["picks"]))
            image_bytes = await generate_draft_image_from_db(mid, draft_data)
            file = discord.File(image_bytes, filename=f"draft_{mid}.png")
            await interaction.followup.send(file=file)
            return

        # 2) On-demand parse for ANY match. We import the match metadata
        # first (so /draftorder rendering has player names + KDA), then
        # parse the replay. fetch_draft_picks falls back to Steam Web API
        # for replay_salt + cluster if we don't have them stored. Discord
        # allows up to 15 min after defer(); typical parse is 30-90s.
        try:
            await interaction.edit_original_response(
                content="⏳ Parsing replay (this can take 1-2 minutes for a fresh match)…"
            )
        except Exception:
            pass  # progress message is nice-to-have, don't fail on it

        # Use guild_id=0 ("external one-off") so non-league matches that
        # users look up via /draftorder don't pollute /matches for the guild.
        # If this match later gets pulled in by the weekly league refresh,
        # it'll be inserted again under the real guild_id (PK is composite).
        from fetcher import import_match
        ok = await import_match(mid, guild_id=0)
        if not ok:
            logger.warning("import_match(%d) failed; replay parse may also fail", mid)
        cluster, replay_salt = get_match_replay_info(mid)
        logger.info(
            "On-demand parse for %d (cluster=%d, salt=%d)", mid, cluster, replay_salt,
        )
        from replay import fetch_draft_picks
        from opendota_lookup import resolve_picks_with_opendota
        picks = await fetch_draft_picks(mid, cluster, replay_salt)

        # If the parse failed because the replay isn't downloadable yet
        # (replay_salt not in OpenDota AND Steam API also unavailable),
        # give a useful "try again later" message.
        if not picks and not replay_salt:
            try:
                await interaction.edit_original_response(content="")
            except Exception:
                pass
            await interaction.followup.send(
                f"⏳ Match `{mid}` is too new — Valve hasn't released the replay yet.\n"
                "Replay data typically becomes available within 12-24 hours after a match ends. Try again later.",
                ephemeral=True,
            )
            return

        if picks:
            try:
                resolved = await resolve_picks_with_opendota(mid, picks)
            except Exception:
                logger.exception("OpenDota resolve failed for match %d (continuing)", mid)
                resolved = picks
            normalized = [
                {
                    "pick_order":     p["order"],
                    "player_id":      p["player_id"],
                    "ability_name":   p["ability_name"],
                    "pick_type":      p.get("type", "ability"),
                    "m_n_ability_id": p.get("m_n_ability_id", 0),
                    "ability_id":     0,
                }
                for p in resolved
            ]
            upsert_draft_picks(mid, normalized)
            draft_data = get_draft_picks_for_display(mid)
            if draft_data:
                image_bytes = await generate_draft_image_from_db(mid, draft_data)
                file = discord.File(image_bytes, filename=f"draft_{mid}.png")
                try:
                    await interaction.edit_original_response(content="")
                except Exception:
                    pass
                await interaction.followup.send(file=file)
                return
        logger.warning(
            "On-demand parse for %d yielded no picks; falling back to windrun", mid,
        )

        # 3) Replay unavailable AND we couldn't parse — give a clear error.
        # We deliberately do NOT fall back to windrun.io: their data has
        # multi-day lag and ours is now self-sufficient via OpenDota.
        try:
            await interaction.edit_original_response(content="")
        except Exception:
            pass
        await interaction.followup.send(
            f"⚠️ Could not parse a draft for match `{mid}`. "
            "The replay may be unavailable from Valve, or this isn't an Ability Draft game.",
            ephemeral=True,
        )

    except Exception as e:
        logger.exception("Error in draftorder command for match %s", match_id)
        await interaction.followup.send(f"❌ Error generating draft order: {e}", ephemeral=True)


@tree.command(name="convert", description="Convert between windrun rating and ranked MMR")
@app_commands.describe(
    windrun="Windrun rating to convert to MMR",
    mmr="Ranked MMR to convert to windrun",
)
async def convert(interaction: discord.Interaction, windrun: int = None, mmr: int = None):
    if (windrun is None) == (mmr is None):
        await interaction.response.send_message(
            "⚠️ Provide exactly one of `windrun` or `mmr`.",
            ephemeral=True,
        )
        return
    from formatters import _ranked_mmr_to_windrun, _windrun_to_ranked_mmr, EMBED_COLOUR_BLUE
    if windrun is not None:
        out_mmr = _windrun_to_ranked_mmr(windrun)
        embed = discord.Embed(
            title="🔁 Convert",
            description=f"**{windrun}** windrun ≈ **{out_mmr}** ranked MMR",
            colour=EMBED_COLOUR_BLUE,
        )
    else:
        out_wr = _ranked_mmr_to_windrun(mmr)
        embed = discord.Embed(
            title="🔁 Convert",
            description=f"**{mmr}** ranked MMR ≈ **{round(out_wr)}** windrun",
            colour=EMBED_COLOUR_BLUE,
        )
    await interaction.response.send_message(embed=embed)


@tree.command(name="tipjar", description="Support the bot creator")
async def tipjar(interaction: discord.Interaction):
    await interaction.response.send_message(
        "If you're enjoying the bot, consider tipping the creator!\n"
        "https://venmo.com/Joe-Dobrow",
        ephemeral=True,
    )


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
        draft_count = await fetch_and_store_drafts_for_guild(interaction.guild_id)
        msg = f"✅ Done! Fetched {count} new match(es)"
        if draft_count:
            msg += f", parsed {draft_count} draft(s)"
        msg += "."
        await interaction.followup.send(msg, ephemeral=True)
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
            draft_count = await fetch_and_store_drafts_for_guild(div["guild_id"])
            if draft_count:
                logger.info(f"Weekly draft fetch for guild {div['guild_id']}: {draft_count} draft(s)")
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
