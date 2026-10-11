import asyncio
import os
import discord
from discord import app_commands
from discord.ext import tasks
import logging
from datetime import datetime, timezone, timedelta

from config import (DISCORD_TOKEN, ADMIN_USER_ID, REGION_CLUSTERS, GAME_MODE_FILTERS,
                    LOOKUP_CHANNEL_IDS, SITE_URL)
from fetcher import fetch_and_store_matches_for_division, fetch_and_store_season
from db import (init_db, get_division, get_guild_division, update_division,
                get_all_divisions, get_scold_channel, set_guild_settings, get_guild_settings,
                get_season, upsert_season, set_current_season, season_label, ranked, qualified)
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
    """The division a command is about, or None if this server has none.

    A server can host several divisions. A command that has a `division:`
    option picks one by name or slug; otherwise it's the server's only
    division, or its default one (guild_settings.default_division_id)."""
    return get_guild_division(interaction.guild_id, getattr(interaction.namespace, "division", None))


# Stat/match/quote commands post to the channel by default; pass public:False
# to get the reply as a whisper only you can see. Owner-only commands
# (set_player, sync_roles_channels, nuke) are always private
# and take no public param.
PUBLIC_BY_DEFAULT_PARAM_DESCRIPTION = "Post the result in the channel; set False to only show it to you (default: True)"


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

async def _season_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete for every `season:` param. Values are season ids (the
    season's Dota league id); names are the friendly label."""
    from db import get_seasons, season_label
    try:
        div = _require_division(interaction)
        if not div:
            return []
        seasons = get_seasons(div["id"])
        active = div["current_season_id"]
        out = []
        for s in seasons:
            name = season_label(s)
            if s["id"] == active:
                name += "  (current)"
            if (current.lower() in name.lower() or current in s["season_start"]
                    or current in str(s["id"])):
                out.append(app_commands.Choice(name=name[:100], value=str(s["id"])))
        return out[:25]
    except Exception:
        logger.exception("season autocomplete failed")
        return []


def _resolve_season(interaction: discord.Interaction, season: str | None) -> tuple[int | None, str | None]:
    """Turn a `season:` param into a season id.

    Accepts what autocomplete sends (the id, i.e. the Dota league id), a
    start date, or a season's name. Returns (season_id, error). season=None
    means "the division's current season", which is the pre-existing
    behaviour of every command.
    """
    division = _require_division(interaction)
    if not division:
        return None, "\u26a0\ufe0f No division configured. Ask an admin to run `/config` first."
    if not season:
        return division["current_season_id"], None
    from db import get_seasons, season_label
    seasons = get_seasons(division["id"])
    wanted = season.strip().lower()
    for s in seasons:
        if wanted in (str(s["id"]), s["season_start"], season_label(s).lower()):
            return s["id"], None
    opts = ", ".join(f"`{season_label(s)}`" for s in seasons[:8]) or "none on file"
    return None, f"\u26a0\ufe0f Unknown season `{season}`. Known seasons: {opts}"


SEASON_PARAM_DESCRIPTION = "Which season — defaults to the division's current one"

_REGION_NAMES = {"us_west": "US West", "us_east": "US East", "europe": "Europe", "any": "Any Region"}


@tree.command(name="config", description="[Admin] Point this server's division at a league (divisions are made on the site)")
@app_commands.describe(
    league="Dota league ID of the current season (e.g. 20056 = RD2L Season 39)",
    region="Server region for filtering matches",
    mode="Game mode filter",
    season_start="That season's start date (YYYY-MM-DD format)",
    scold_channel="Channel where all messages get deleted with a scolding reply",
    name="Rename the division, e.g. NA",
)
@app_commands.choices(
    region=[
        app_commands.Choice(name="US West", value="us_west"),
        app_commands.Choice(name="US East", value="us_east"),
        app_commands.Choice(name="Europe", value="europe"),
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
    scold_channel: discord.TextChannel = None,
    name: str = None,
):
    # Only server admins or bot owner can configure
    is_admin = interaction.user.guild_permissions.administrator
    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    if not is_admin and not is_owner:
        await interaction.response.send_message("⚠️ Only server admins can configure divisions.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    current = _require_division(interaction)
    settings = get_guild_settings(guild_id)

    # If no parameters, show current config
    if all(v is None for v in (league, region, mode, season_start, scold_channel, name)):
        if current:
            mode_display = "Captain's Mode" if current["game_mode"] == "cm" else "Ability Draft"
            scold_display = f"<#{settings['scold_channel_id']}>" if settings.get("scold_channel_id") else "None"
            season = get_season(current["current_season_id"]) or {}
            await interaction.response.send_message(
                f"**Current Division Config**\n"
                f"Division: `{current['name']}`\n"
                f"League ID: `{current['current_season_id']}`"
                + (f" ({season_label(season)})" if season else "") + "\n"
                f"Region: `{_REGION_NAMES.get(current['region'], current['region'])}`\n"
                f"Mode: `{mode_display}`\n"
                f"Season Start: `{season.get('season_start', '?')}`\n"
                f"Scold Channel: {scold_display}",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "No division configured for this server.\n"
                f"Create one at {SITE_URL}/admin (Divisions → New division), giving it this "
                f"server's id `{guild_id}`, then run `/config` to point it at a league.",
                ephemeral=True
            )
        return

    # Divisions are created on the site now: a division is a name, a URL name,
    # a region and a game mode, and the region and mode decide which of a
    # league's games belong to it — worth seeing the existing divisions while
    # you pick. This command configures one that already exists.
    if not current:
        await interaction.response.send_message(
            "⚠️ This server has no division yet. Create one at "
            f"{SITE_URL}/admin (Divisions → New division) and put this server's id "
            f"`{guild_id}` in it, then run `/config` again.",
            ephemeral=True
        )
        return
    # Updating - use existing values for missing fields
    league = league or current["current_season_id"]
    region = region or current["region"]
    mode = mode or current["game_mode"]

    # A season is identified by its league id (several divisions can share
    # one): `league` picks or creates the season, `season_start` sets its
    # start date.
    season = get_season(league)
    season_start = season_start or (season["season_start"] if season else None)
    if not season_start:
        await interaction.response.send_message(
            f"⚠️ League `{league}` is a new season, so `season_start` is required.", ephemeral=True)
        return

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
    upsert_season(league, season_start)
    fields = {"region": region, "game_mode": mode}
    if name:
        fields.update(name=name.strip(), slug=_slug(name))
    update_division(current["id"], **fields)
    division_id = current["id"]
    set_current_season(division_id, league)
    if scold_channel:
        set_guild_settings(guild_id, scold_channel_id=scold_channel.id)
    scold_channel_id = scold_channel.id if scold_channel else settings.get("scold_channel_id")

    mode_display = "Captain's Mode" if mode == "cm" else "Ability Draft"
    scold_display = f"<#{scold_channel_id}>" if scold_channel_id else "None"

    await interaction.response.send_message(
        f"✅ Division configured!\n"
        f"Division: `{get_division(division_id)['name']}`\n"
        f"League ID: `{league}`\n"
        f"Region: `{_REGION_NAMES.get(region, region)}`\n"
        f"Mode: `{mode_display}`\n"
        f"Season Start: `{season_start}`\n"
        f"Scold Channel: {scold_display}\n\n"
        f"Run `/refresh_leaderboard` to fetch match data.",
        ephemeral=True
    )


def _slug(name: str) -> str:
    """A division's URL name, e.g. "NA West" -> "na-west"."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "division"


@tree.command(name="leaderboard", description="Show the leaderboard sorted by a stat")
@app_commands.describe(
    season="Which season to look at (defaults to the current one)",
    week="Season week number (1, 2, 3...) or -1 for all-time. Leave blank for the current week.",
    stat="Which stat to sort by",
    pos="Filter by position (1-5, optional)",
    all="Show every player (no top-10 cap, no min-games threshold)",
    min_games="Games needed to rank. Defaults to half the most active player's; 0 ranks everyone.",
    debug="[Owner only] expose value components (cost, diff)",
    public=PUBLIC_BY_DEFAULT_PARAM_DESCRIPTION,
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
    app_commands.Choice(name="Watcher Captures",        value="watcher_captures"),
    app_commands.Choice(name="First Tormentor Time",    value="avg_first_tormentor_time"),
    app_commands.Choice(name="Obs Uptime",              value="obs_uptime"),
    app_commands.Choice(name="Avg Game Length",         value="avg_duration"),
], pos=[
    app_commands.Choice(name="Position 1 (Safe Lane)", value=1),
    app_commands.Choice(name="Position 2 (Mid)", value=2),
    app_commands.Choice(name="Position 3 (Off Lane)", value=3),
    app_commands.Choice(name="Position 4 (Roaming)", value=4),
    app_commands.Choice(name="Position 5 (Hard Support)", value=5),
])
@app_commands.autocomplete(season=_season_autocomplete)
async def leaderboard(interaction: discord.Interaction, stat: app_commands.Choice[str], week: int = None, pos: int = None, all: bool = False, season: str = None, min_games: int = None, debug: bool = False, public: bool = True):
    await interaction.response.defer(ephemeral=not public)

    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    debug = bool(debug) and is_owner

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    season_id, season_err = _resolve_season(interaction, season)
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
            stats = ranked(get_all_time_stats(division["id"], season_id,
                                              cross_season=not draft_relative))
            week_label = "All-Time (this season)" if draft_relative else "All-Time (all seasons)"
        else:
            # Blank week defaults to the current season week (not all-time —
            # pass week:-1 explicitly for that).
            resolved_week = week if week is not None else get_current_season_week(season_id, division["id"])
            stats = get_stats_for_season_week(season_id, division["id"], resolved_week)
            week_label = f"Week {resolved_week}"

        # value/attendance lean on draft data: filter to drafted-only players
        # (cost set) and skip the min-games threshold. Other stats apply a
        # min-games qualification. Either way, top 10 unless all=True.
        draft_only_stats = {"value", "attendance"}
        if stat.value in draft_only_stats:
            stats = [s for s in stats if s.get("cost") is not None]
            threshold = None
            max_games = None
        elif all and min_games is None:
            threshold = None
            max_games = None
        else:
            # The bar is half the most active player's games unless min_games
            # names one; all=True with a min_games still means "no top ten",
            # not "no bar".
            stats, threshold, max_games = qualified(stats, min_games)

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
    public=PUBLIC_BY_DEFAULT_PARAM_DESCRIPTION,
)
@app_commands.autocomplete(season=_season_autocomplete)
async def player(interaction: discord.Interaction, name: str, week: int = None, season: str = None, public: bool = True):
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
        await _send_resolve_error(interaction, err)
        return

    season_id, season_err = _resolve_season(interaction, season)
    if season_err:
        await interaction.followup.send(season_err, ephemeral=True)
        return

    from db import get_stats_for_season_week, get_all_time_stats, get_current_season_week

    try:
        if week == -1:
            # Matches /leaderboard: a player's all-time line spans every
            # season. Cost/value in the Draft block stay season-scoped.
            stats = get_all_time_stats(division["id"], season_id, cross_season=True)
            week_label = "All-Time (all seasons)"
        else:
            # Blank week defaults to the current season week (not all-time —
            # pass week:-1 explicitly for that).
            week = week if week is not None else get_current_season_week(season_id, division["id"])
            stats = get_stats_for_season_week(season_id, division["id"], week)
            week_label = f"Week {week}"

        match_row = next((p for p in stats if p.get("account_id") == account_id), None)

        # Look up the player's internal rating from the cache and apply the
        # same fantasy adjustment that /players and /lookup use. For alt
        # accounts, use the main's account_id for the adjustment lookup so
        # the displayed rating matches the main's exactly.
        from db import get_rating_cache_row, compute_fantasy_adjusted_ratings
        # A rating nobody refreshes is a rating that quietly rots: the weekly
        # job only covers people who play in a division, and most of the cache
        # is people looked up once. Reading one is what keeps it current.
        import rating_compute
        unavailable: tuple[str, ...] = ()
        try:
            # compute_missing: someone asked about this player by name, so
            # answer with a number even if we've never rated them.
            unavailable = await rating_compute.refresh_if_stale(account_id, compute_missing=True)
        except Exception:
            logger.exception("stale-rating refresh failed for %d in /player", account_id)
        # Nothing is rated from a partial fetch, so when a source was down the
        # number below is the last one worked out rather than a checked one.
        stale_note = (f"⚠️ Couldn't reach {', '.join(unavailable)}, so this rating wasn't "
                      f"rechecked just now — try again in a minute." if unavailable else None)
        rating: int | None = None
        cache_row = get_rating_cache_row(account_id)
        if cache_row and cache_row.get("internal_rating") is not None:
            rating = cache_row["internal_rating"]
            adjustments = compute_fantasy_adjusted_ratings(division["current_season_id"], division["id"])
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
                # Nothing cached for them yet. Compute it the way every other
                # writer does and cache it, so the next /player agrees with
                # this one and with /lookup.
                try:
                    import rating_compute
                    rating = await rating_compute.compute_and_cache_rating(
                        account_id, name_fallback=display_name)
                    fresh = get_rating_cache_row(account_id) or {}
                    display_name = display_name or fresh.get("name")
                    avatar_url = avatar_url or fresh.get("avatar_url")
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
            if stale_note:
                embed.add_field(name="⚠️ Rating not rechecked", value=stale_note, inline=False)
            if (cache_row or {}).get("fh_unavailable"):
                embed.add_field(
                    name="⚠️ Match history hidden",
                    value="Steam match history is private, so game counts are unavailable.",
                    inline=False,
                )
            embed.set_footer(text=f"Account ID: {account_id}")
            await interaction.followup.send(embed=embed, ephemeral=not public)
            return

        # Who this player is being measured against (db.comparison_pool).
        from db import comparison_pool
        qualified_pool = comparison_pool(stats, include=[account_id])
        embed = format_player_stats(
            match_row, week_label=week_label, debug=debug, rating=rating,
            qualified_pool=qualified_pool,
        )
        if stale_note:
            embed.add_field(name="⚠️ Rating not rechecked", value=stale_note, inline=False)
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
    public=PUBLIC_BY_DEFAULT_PARAM_DESCRIPTION,
)
async def team_stats(interaction: discord.Interaction, name: str, season: str = None, public: bool = True):
    await interaction.response.defer(ephemeral=not public)

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured.", ephemeral=True)
        return

    season_id, season_err = _resolve_season(interaction, season)
    if season_err:
        await interaction.followup.send(season_err, ephemeral=True)
        return

    account_id, resolved_name, err = await _resolve_player_query(interaction, name)
    if err:
        await _send_resolve_error(interaction, err)
        return

    from db import get_player_costs, get_team_match_aggregates, get_captain_account_ids
    costs = get_player_costs(season_id, division["id"])
    target_cost = costs.get(account_id)
    captain: str | None = None
    if target_cost and target_cost.get("captain"):
        captain = target_cost["captain"]
    else:
        # Maybe they ARE a captain — look up reverse mapping.
        captain_aids = get_captain_account_ids(season_id, division["id"])
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
    team_aggs = get_team_match_aggregates(season_id, division["id"])
    target_agg = team_aggs.get(captain)
    if not target_agg:
        await interaction.followup.send(
            f"⚠️ Team `{captain}` has no game data yet.",
            ephemeral=True,
        )
        return

    from team_metadata import get_team_info
    team_label = get_team_info(captain, season_id, division["id"]).get("team_name") or captain
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
@app_commands.describe(name="Player name (partial), override nickname, or numeric account ID",
                       season=SEASON_PARAM_DESCRIPTION,
                       in_role="Compare against players in the same position, instead of the whole league")
@app_commands.autocomplete(season=_season_autocomplete)
async def player_card(interaction: discord.Interaction, name: str, season: str = None,
                      in_role: bool = False):
    await interaction.response.defer()
    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured.", ephemeral=True)
        return
    season_id, err = _resolve_season(interaction, season)
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    account_id, resolved_name, err = await _resolve_player_query(interaction, name)
    if err:
        await _send_resolve_error(interaction, err)
        return

    from db import (
        get_all_time_stats, get_rating_cache_row, compute_fantasy_adjusted_ratings,
    )
    from formatters import _compute_strengths_weaknesses
    from streamcards import render_player_card, fetch_avatar

    stats = get_all_time_stats(division["id"], season_id)
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
        adjustments = compute_fantasy_adjusted_ratings(season_id, division["id"])
        effective_id = cache_row.get("alt_account_for") or account_id
        adj = adjustments.get(effective_id)
        if adj:
            rating = adj["adjusted_rating"]

    # Strengths/weaknesses (max 3 each), against the rest of the league — or
    # against the player's own position, which is the fairer comparison for
    # anything a support is never going to win on.
    from db import comparison_pool
    pool = comparison_pool(stats, include=[account_id])
    compared_to = None
    position = target.get("role_position")
    if in_role:
        if position is None:
            await interaction.followup.send(
                f"\u26a0\ufe0f `{resolved_name or account_id}` has no position on record, so there's "
                "nobody to compare them with. Leaving `in_role` off compares them with the league.",
                ephemeral=True)
            return
        pool = [s for s in pool if s.get("role_position") == position]
        compared_to = f"pos {position}"
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
        strengths=strengths, weaknesses=weaknesses, compared_to=compared_to,
        avatar=avatar, account_id=account_id,
    )
    await interaction.followup.send(
        file=discord.File(buf, filename=f"player_{account_id}.png"),
    )


@tree.command(name="team_card", description="Render a stream-friendly PNG card for a player's team")
@app_commands.describe(name="Any player on the team — partial name, override nickname, or numeric account ID",
                       season=SEASON_PARAM_DESCRIPTION)
@app_commands.autocomplete(season=_season_autocomplete)
async def team_card(interaction: discord.Interaction, name: str, season: str = None):
    await interaction.response.defer()
    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured.", ephemeral=True)
        return
    season_id, err = _resolve_season(interaction, season)
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    account_id, resolved_name, err = await _resolve_player_query(interaction, name)
    if err:
        await _send_resolve_error(interaction, err)
        return

    from db import (
        get_player_costs, get_team_match_aggregates, get_captain_account_ids,
        get_rating_cache_row, get_all_time_stats,
    )
    from formatters import _compute_strengths_weaknesses, _TEAM_NOTABLE_STATS
    from streamcards import render_team_card, fetch_avatar
    import asyncio

    costs = get_player_costs(season_id, division["id"])
    target_cost = costs.get(account_id)
    captain: str | None = None
    if target_cost and target_cost.get("captain"):
        captain = target_cost["captain"]
    else:
        cap_aids = get_captain_account_ids(season_id, division["id"])
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

    team_aggs = get_team_match_aggregates(season_id, division["id"])
    target_agg = team_aggs.get(captain)
    if not target_agg:
        await interaction.followup.send(f"⚠️ Team `{captain}` has no game data yet.", ephemeral=True)
        return

    # Build member list (in roster order: drafted players then captain).
    cap_aids = get_captain_account_ids(season_id, division["id"])
    captain_aid = cap_aids.get(captain)
    drafted_aids = [aid for aid, c in costs.items() if c.get("captain") == captain]
    roster_aids = drafted_aids + ([captain_aid] if captain_aid else [])

    # Per-player role_position comes from the stats query.
    stats_by_aid = {
        s["account_id"]: s
        for s in get_all_time_stats(division["id"], season_id)
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
    info = get_team_info(captain, season_id, division["id"])
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
    )


@tree.command(name="h2h_card", description="Render a head-to-head PNG card for two teams")
@app_commands.describe(
    season="Which season to look at (defaults to the current one)",
    team1="Any player on team 1 (or the captain's name)",
    team2="Any player on team 2 (or the captain's name)",
)
@app_commands.autocomplete(season=_season_autocomplete)
async def h2h_card(interaction: discord.Interaction, team1: str, team2: str, season: str = None):
    await interaction.response.defer()

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

    season_id, season_err = _resolve_season(interaction, season)
    if season_err:
        await interaction.followup.send(season_err, ephemeral=True)
        return

    async def _resolve_to_captain(player_input: str) -> tuple[str | None, str]:
        """Return (captain_name, error_msg). Captain string sentinel."""
        aid, resolved_name, err = await _resolve_player_query(interaction, player_input)
        if err:
            return None, err
        costs = get_player_costs(season_id, division["id"])
        if costs.get(aid) and costs[aid].get("captain"):
            return costs[aid]["captain"], ""
        cap_aids = get_captain_account_ids(season_id, division["id"])
        for cap_name, cap_aid in cap_aids.items():
            if cap_aid == aid:
                return cap_name, ""
        return None, f"⚠️ `{resolved_name or aid}` isn't on a team this season."

    cap_a, err_a = await _resolve_to_captain(team1)
    if err_a:
        await _send_resolve_error(interaction, err_a); return
    cap_b, err_b = await _resolve_to_captain(team2)
    if err_b:
        await _send_resolve_error(interaction, err_b); return
    if cap_a == cap_b:
        await interaction.followup.send("⚠️ Same team on both sides.", ephemeral=True); return

    team_aggs = get_team_match_aggregates(season_id, division["id"])
    agg_a = team_aggs.get(cap_a)
    agg_b = team_aggs.get(cap_b)
    if not agg_a or not agg_b:
        await interaction.followup.send("⚠️ One of the teams has no match data yet.", ephemeral=True)
        return

    costs = get_player_costs(season_id, division["id"])
    cap_aids = get_captain_account_ids(season_id, division["id"])
    stats_by_aid = {s["account_id"]: s for s in get_all_time_stats(division["id"], season_id)}

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

    history = get_head_to_head_matches(season_id, division["id"], cap_a, cap_b)

    # Map captain → real team name + logo URL (from RD2L). When a team has no
    # registered logo, fall back to the captain's Steam avatar.
    from team_metadata import get_team_info
    info_a = get_team_info(cap_a, season_id, division["id"])
    info_b = get_team_info(cap_b, season_id, division["id"])

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
    )


@tree.command(name="h2h_player", description="Render a head-to-head PNG card comparing two players")
@app_commands.describe(
    player1="First player (partial name, override nickname, or numeric account ID)",
    player2="Second player (partial name, override nickname, or numeric account ID)",
    season=SEASON_PARAM_DESCRIPTION,
)
@app_commands.autocomplete(season=_season_autocomplete)
async def h2h_player(interaction: discord.Interaction, player1: str, player2: str, season: str = None):
    await interaction.response.defer()
    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured.", ephemeral=True)
        return
    season_id, err = _resolve_season(interaction, season)
    if err:
        await interaction.followup.send(err, ephemeral=True)
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
        await _send_resolve_error(interaction, err_a); return
    aid_b, resolved_b, err_b = await _resolve_player_query(interaction, player2)
    if err_b:
        await _send_resolve_error(interaction, err_b); return
    if aid_a == aid_b:
        await interaction.followup.send("⚠️ Same player on both sides.", ephemeral=True); return

    stats = get_all_time_stats(division["id"], season_id)
    row_a = next((s for s in stats if s.get("account_id") == aid_a), None)
    row_b = next((s for s in stats if s.get("account_id") == aid_b), None)
    if row_a is None or row_b is None:
        missing = resolved_a if row_a is None else resolved_b
        await interaction.followup.send(
            f"⚠️ `{missing}` has no league matches here yet.", ephemeral=True,
        )
        return

    # Rank stats by |z-diff| across the league (db.comparison_pool). Both
    # players are in the pool whatever they have played, so a stat is never
    # missing just because one of them is new.
    from db import comparison_pool
    pool = comparison_pool(stats, include=[row_a["account_id"], row_b["account_id"]])
    stat_rows = compute_h2h_stat_rows(row_a, row_b, pool, top_n=8)

    # Ratings: prefer fantasy-adjusted, fall back to raw internal_rating.
    adjustments = compute_fantasy_adjusted_ratings(season_id, division["id"])

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
    )



@tree.command(name="lookup", description="Diagnostic lookup — full rating breakdown (in the admin/bot channels)")
@app_commands.describe(
    query="Player name (partial match) or numeric account ID",
    force_refresh="Skip the cache and re-fetch from windrun/OpenDota",
    public=PUBLIC_BY_DEFAULT_PARAM_DESCRIPTION,
)
async def lookup(interaction: discord.Interaction, query: str, force_refresh: bool = False, public: bool = True):
    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    in_admin_channel = interaction.channel_id in LOOKUP_CHANNEL_IDS
    if not (is_owner or in_admin_channel):
        channels = " or ".join(f"<#{c}>" for c in sorted(LOOKUP_CHANNEL_IDS))
        await interaction.response.send_message(
            f"🔒 `/lookup` shows the full rating breakdown, so it only runs in {channels}. "
            f"Anywhere else, `/player` has the rating itself.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=not public)
    debug = True
    force_refresh = bool(force_refresh) and is_owner

    account_id, player_name, err = await _resolve_player_query(interaction, query)
    if err:
        await _send_resolve_error(interaction, err)
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
        adjustments = compute_fantasy_adjusted_ratings(division["current_season_id"], division["id"])
        effective_id = (override or {}).get("alt_account_for") or account_id
        adj = adjustments.get(effective_id)
        if adj:
            adjustment_pct = adj["pct"]

    # Other accounts this player uses: each keeps its own rating, and the
    # rating shown pools the best of them (rating_group.py).
    from db import account_groups, raw_cache_ratings
    others = [a for a in account_groups().get(account_id, []) if a != account_id]
    linked_accounts = [{"account_id": a, **raw_cache_ratings(others).get(a, {})} for a in others]

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
            linked_accounts=linked_accounts,
            group_rating=cache_row.get("group_rating"),
            group_note=cache_row.get("group_explanation"),
        )
        await interaction.followup.send(embed=embed, ephemeral=not public)
        return

    # One gatherer for every writer: /lookup, /refresh_ratings and the API all
    # take their inputs from rating_compute, so the cached number doesn't
    # depend on which of them ran last (and /player, which only reads, agrees
    # with all three).
    import rating_compute
    inputs = await rating_compute.fetch_inputs(account_id)
    wr, od, od_counts = inputs["windrun"], inputs["opendota"], inputs["od_counts"]
    ad_all_time, ad_last_year = inputs["ad_all_time"], inputs["ad_last_year"]

    # If fetch couldn't produce a fresh rating but we have a stale cache row,
    # use the cached values as a fallback so the user still sees a number.
    has_override_rating = bool(override and override.get("internal_rating_override") is not None)
    have_any_signal = (wr is not None) or (od_counts is not None) or has_override_rating
    fallback_cached_rating = None
    fallback_cached_avatar = None
    fallback_updated_at = None
    is_stale_render = False
    could_not_rate = bool(inputs.get("unavailable")) or not have_any_signal
    if could_not_rate and cache_row:
        fallback_cached_rating = cache_row.get("internal_rating")
        if not (cache_row.get("hide_avatar") or (override and override.get("hide_avatar"))):
            fallback_cached_avatar = cache_row.get("avatar_url")
        fallback_updated_at = cache_row.get("updated_at")
        is_stale_render = True

    # Build a fetch-error message when we couldn't get fresh data.
    fetch_error: str | None = None
    if inputs.get("unavailable"):
        # Nothing is rated from a partial picture — a missing count reads as
        # "activity unknown" and moves the weighting onto the badge, which is
        # how a 3371 became a 3506. Say so and show what was last worked out.
        fetch_error = (f"Couldn't reach {', '.join(inputs['unavailable'])} just now, so this "
                       f"player can't be rated accurately — the rating below is whatever was "
                       f"last worked out, and nothing has been updated. Try again in a minute.")
    elif not have_any_signal:
        failed_apis = [label for label, got in (("windrun", wr), ("OpenDota profile", od),
                                                ("OpenDota game counts", od_counts)) if got is None]
        if failed_apis:
            sources = ", ".join(failed_apis)
            if cache_row:
                fetch_error = f"Couldn't reach: {sources}. Showing previously cached value."
            else:
                fetch_error = f"Couldn't reach: {sources}. No cached value available."

    # Refresh this player's cache row so /players stays current without anyone
    # needing to run /refresh_ratings. Write whenever we got ANY fresh signal —
    # windrun-only or OpenDota-only is still better than leaving the cache
    # stale. This happens before the embed is built so that a player with
    # several accounts gets their shared rating (rating_group) recomputed from
    # what we just fetched, rather than from the previous refresh.
    info = rating_compute.resolve(inputs, override)
    if have_any_signal and not has_override_rating and not inputs.get("unavailable"):
        try:
            rating_compute.cache(inputs, info, player_name)
        except Exception:
            logger.exception("cache write failed for %d after /lookup", account_id)

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
        linked_accounts=linked_accounts,
        rating_info=info,
        **(_group_rating_kwargs(account_id) if linked_accounts else {}),
    )
    await interaction.followup.send(embed=embed, ephemeral=not public)


def _group_rating_kwargs(account_id: int) -> dict:
    """The pooled rating for a player with several accounts, as format_lookup
    kwargs. Read after the cache write-back so it reflects what we just
    fetched; empty for anyone playing a single account."""
    from db import get_rating_cache_row
    row = get_rating_cache_row(account_id) or {}
    if row.get("group_rating") is None:
        return {}
    return {"group_rating": row["group_rating"], "group_note": row.get("group_explanation")}


class _AmbiguousName(str):
    """The "be more specific" message, carrying the picker that goes with it.

    It subclasses str so a caller that just sends it still says something
    sensible; callers that go through _send_resolve_error get a card per
    candidate and a button to choose one.
    """
    embeds: list
    view: "discord.ui.View | None"


class _PickPlayerButton(discord.ui.Button):
    """One candidate. Pressing it runs the command again with that account's
    id in place of the name that was ambiguous."""

    def __init__(self, account_id: int, name: str | None, command, params: dict, param: str,
                 label: str | None = None):
        super().__init__(label=(label or name or str(account_id))[:80],
                         style=discord.ButtonStyle.secondary)
        self.account_id, self.command, self.params, self.param = account_id, command, params, param

    async def callback(self, interaction: discord.Interaction):
        await self.command.callback(interaction, **{**self.params, self.param: str(self.account_id)})


class _PlayerPicker(discord.ui.View):
    """Who a name could have meant, as buttons."""

    def __init__(self, user_id: int, command, params: dict, param: str,
                 candidates: list[tuple[int, str]]):
        super().__init__(timeout=180)
        self.user_id = user_id
        # Two people really can share a handle, and two identical buttons are
        # no choice at all; those ones carry the account id as well.
        seen = [n for _, n in candidates]
        for aid, name in candidates:
            label = f"{name} · {aid}" if seen.count(name) > 1 else name
            self.add_item(_PickPlayerButton(aid, name, command, params, param, label))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your lookup — run it yourself.", ephemeral=True)
            return False
        return True


def _player_card_embed(account_id: int, name: str | None) -> discord.Embed:
    """A candidate at a glance: the name, the account id to paste back in,
    their Steam picture, and the rating, which is usually what tells two
    people with the same handle apart."""
    from db import get_rating_cache_row, get_skill_override
    row = get_rating_cache_row(account_id) or {}
    override = get_skill_override(account_id) or {}
    rating = row.get("group_rating") or row.get("internal_rating")
    embed = discord.Embed(
        title=name or f"#{account_id}",
        description=f"`{account_id}`" + (f" · rating **{rating}**" if rating else ""),
    )
    if not (row.get("hide_avatar") or override.get("hide_avatar")):
        if row.get("avatar_url"):
            embed.set_thumbnail(url=row["avatar_url"])
    return embed


def _ambiguous(interaction: discord.Interaction, q: str,
               candidates: list[tuple[int, str]]) -> _AmbiguousName:
    """The answer when a name matches several people: one card each, and a
    button per candidate when we can tell which option held the name."""
    total = len(candidates)
    candidates = candidates[:10]           # Discord sends at most 10 embeds
    shown = f" — the first {len(candidates)} are below" if total > len(candidates) else ""
    err = _AmbiguousName(f"`{q}` matches {total} players{shown}. Pick one, "
                         f"or run it again with the account id.")
    err.embeds = [_player_card_embed(aid, name) for aid, name in candidates]
    err.view = None
    # The button re-runs this same command, so it has to know which of its
    # options the name came from; two options holding the same text resolve
    # one at a time, which is the only way to tell them apart anyway.
    params = dict(interaction.namespace)
    param = next((k for k, v in params.items() if isinstance(v, str) and v.strip() == q), None)
    if param and isinstance(interaction.command, app_commands.Command):
        err.view = _PlayerPicker(interaction.user.id, interaction.command, params, param, candidates)
    return err


async def _send_resolve_error(interaction: discord.Interaction, err) -> None:
    """Say why a name didn't resolve, with the picker when it was ambiguous."""
    if isinstance(err, _AmbiguousName):
        extra = {"view": err.view} if err.view is not None else {}
        await interaction.followup.send(str(err), embeds=err.embeds, ephemeral=True, **extra)
    else:
        await interaction.followup.send(err, ephemeral=True)


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

    from db import _conn, get_guild_divisions
    # Search every division in this server: a name could be anyone's in it.
    division_ids = [d["id"] for d in get_guild_divisions(interaction.guild_id)] or [-1]
    in_divisions = ",".join("?" * len(division_ids))
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
                      WHERE m.division_id IN ({in_divisions})
                  )
                  OR prc.account_id IN (
                      SELECT account_id FROM division_roster WHERE division_id IN ({in_divisions})
                  )
              )
        """.format(in_divisions=in_divisions), (f"%{q}%", *division_ids, *division_ids)).fetchall():
            candidates.setdefault(r["account_id"], r["name"])
        # This server's players
        for r in conn.execute("""
            SELECT DISTINCT p.account_id, p.name
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.division_id IN ({in_divisions}) AND LOWER(p.name) LIKE LOWER(?)
        """.format(in_divisions=in_divisions), (*division_ids, f"%{q}%")).fetchall():
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
            # Accounts already linked as one person aren't a choice — asking
            # which of poof's two accounts you meant is the same answer
            # twice. Collapse to the main, which is where the stats live.
            from db import alt_links
            links = alt_links()
            people: dict[int, tuple[int, str]] = {}
            for aid, n in unique:
                person = links.get(aid, aid)
                if person not in people or aid == person:
                    people[person] = (person, n)
            unique = list(people.values())
            if len(unique) > 1:
                return None, None, _ambiguous(interaction, q, unique)
    return unique[0][0], unique[0][1], None


@tree.command(name="set_player", description="[Owner] Override a player's nickname, windrun, badge/rank used by /lookup and /players")
@app_commands.describe(
    name="Player name (partial match) or numeric account ID",
    nickname="Custom display name (omit to keep existing)",
    windrun="Manual windrun rating (omit to keep existing)",
    badge='Ranked badge: e.g. "divine 3", "legend 5", "immortal" (omit to keep existing)',
    rank="Immortal leaderboard rank (only meaningful with badge:immortal)",
    rating="Manual internal rating — overrides EVERYTHING; windrun/badge/trust-weights are ignored",
    add_account="Another account this player uses (name/id/nickname/Steam64) — rating becomes the best of their accounts",
    alt_account_for="The reverse: say THIS account is a second account of that player",
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
    add_account: str = None,
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
        await _send_resolve_error(interaction, err)
        return

    from db import (
        upsert_skill_override, delete_skill_override, get_skill_override,
        remove_from_division_roster, division_player_has_matches, _conn,
        exclude_from_division, unexclude_from_division,
    )
    label = f"`{player_name}` (`{account_id}`)" if player_name else f"`{account_id}`"

    # The roster and exclusion lists are per division; the override itself is global.
    division = _require_division(interaction)

    if restore:
        removed = unexclude_from_division(division["id"], account_id) if division else False
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
        roster_removed = remove_from_division_roster(division["id"], account_id) if division else False
        with _conn() as conn:
            cur = conn.execute(
                "DELETE FROM player_ratings_cache WHERE account_id = ?",
                (account_id,),
            )
            cache_removed = cur.rowcount > 0
        if division:
            exclude_from_division(division["id"], account_id)
        had_matches = division_player_has_matches(division["id"], account_id) if division else False

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
        # Note the group before the override goes: clearing it may be what
        # unlinks a second account, and every member then needs re-pooling.
        from db import account_groups
        import rating_group
        was_grouped = account_groups().get(account_id, [account_id])
        removed = delete_skill_override(account_id)
        for aid in was_grouped:
            rating_group.recompute(aid)
        msg = f"✅ Cleared override for {label}." if removed else f"ℹ️ No override existed for {label}."
        await interaction.followup.send(msg, ephemeral=True)
        return

    if (windrun is None and badge is None and rank is None and rating is None
            and alt_account_for is None and add_account is None and discord_handle is None
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

    # add_account is alt_account_for from the other side: "this player also
    # plays on X" rather than "this account belongs to X".
    added_account: tuple[int, str] | None = None
    if add_account is not None:
        other_id, other_name, other_err = await _resolve_player_query(interaction, add_account)
        if other_err or other_id is None:
            await interaction.followup.send(
                f"⚠️ Couldn't resolve `add_account:{add_account}` — {other_err or 'not found'}.",
                ephemeral=True)
            return
        if other_id == account_id:
            await interaction.followup.send(
                "⚠️ `add_account:` can't point at the same account.", ephemeral=True)
            return
        existing_link = (get_skill_override(other_id) or {}).get("alt_account_for")
        if existing_link and existing_link != account_id:
            await interaction.followup.send(
                f"⚠️ `{other_id}` is already linked to `{existing_link}`. Clear that first "
                f"(`/set_player name:{other_id} clear:True`).", ephemeral=True)
            return
        this_link = (get_skill_override(account_id) or {}).get("alt_account_for")
        if this_link:
            await interaction.followup.send(
                f"⚠️ `{account_id}` is itself listed as a second account of `{this_link}` — "
                f"add the new one to `{this_link}` instead, so all their accounts sit in one group.",
                ephemeral=True)
            return
        upsert_skill_override(
            account_id=other_id,
            windrun_rating=None, rank_tier=None, leaderboard_rank=None, note=None,
            alt_account_for=account_id,
            set_by_user_id=interaction.user.id,
            set_by_name=interaction.user.display_name,
        )
        added_account = (other_id, other_name or str(other_id))

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

    # Add the player to this division's roster so they appear in /players even
    # if they haven't played a match yet, then re-rate them from what was last
    # fetched — no API call, and through the same resolver every other writer
    # uses, so an override doesn't leave a number /lookup would disagree with.
    import rating_compute
    from db import add_to_division_roster, get_rating_cache_row
    if division:
        add_to_division_roster(division_id=division["id"], account_id=account_id)

    full = get_skill_override(account_id) or {}
    existing = get_rating_cache_row(account_id) or {}
    from formatters import format_badge
    inputs = rating_compute.inputs_from_cache(account_id)
    info = rating_compute.resolve(inputs, full)
    display_name = (
        nickname or full.get("nickname")
        or existing.get("name") or player_name
        or f"Player_{account_id}"
    )
    inputs["name"] = display_name
    rating_compute.cache(inputs, info or (existing.get("internal_rating"),
                                          existing.get("explanation")))
    # Overrides and account links both change what a group is worth, so
    # re-pool. Both ends, since add_account may have just joined two groups.
    import rating_group
    for aid in {account_id, parsed_alt_id} - {None}:
        rating_group.recompute(aid)

    parts = []
    if nickname is not None:    parts.append(f"nickname=`{nickname}`")
    if windrun is not None:     parts.append(f"windrun=`{windrun}`")
    if badge is not None:       parts.append(f"badge=`{format_badge(parsed_rank_tier, rank)}`")
    if rating is not None:      parts.append(f"rating=`{rating}`")
    if parsed_alt_id is not None: parts.append(f"alt_account_for=`{parsed_alt_id}`")
    if added_account is not None:
        parts.append(f"also plays on `{added_account[0]}` ({added_account[1]}) — "
                     f"rating is now the best of their accounts")
    if discord_handle is not None: parts.append(f"discord_handle=`{discord_handle}`")
    if note is not None:        parts.append(f"note=`{note}`")
    if hide_avatar is not None: parts.append(f"hide_avatar=`{hide_avatar}`")
    rating_note = f" · cached rating: **{info[0]}**" if info else ""
    await interaction.followup.send(
        f"✅ Updated override for {label}: {', '.join(parts)}{rating_note}",
        ephemeral=True,
    )


async def _refresh_ratings_task(aids: list[int], interaction: discord.Interaction):
    """Long-running background task that refreshes player_ratings_cache for
    each account_id, using the same gatherer /lookup and the API use so every
    writer leaves the same number behind."""
    import rating_compute
    from db import get_skill_override

    success = 0
    failures = 0
    for aid in aids:
        try:
            inputs = await rating_compute.fetch_inputs(aid)
            info = rating_compute.resolve(inputs, get_skill_override(aid))
            rating_compute.cache(inputs, info)
            if info:
                success += 1
            else:
                failures += 1
        except Exception:
            logger.exception("refresh failed for account %d", aid)
            failures += 1
        except Exception:
            logger.exception("refresh failed for account %d", aid)
            failures += 1

    # Every account now has fresh inputs, so pool the multi-account players.
    try:
        import rating_group
        rating_group.recompute_all()
    except Exception:
        logger.exception("group rating recompute failed after refresh")

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

    from db import get_division_player_account_ids
    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return
    aids = get_division_player_account_ids(division["id"])
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
    standins="Only players not drafted this season — who's free to fill in",
    debug="[Owner only] show methodology details (residual %, expected vs actual)",
    public=PUBLIC_BY_DEFAULT_PARAM_DESCRIPTION,
)
async def players(interaction: discord.Interaction, show_all: bool = False, standins: bool = False,
                  debug: bool = False, public: bool = True):
    await interaction.response.defer(ephemeral=not public)

    is_owner = ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID
    debug = bool(debug) and is_owner

    from db import get_division_cached_ratings
    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return
    cached = get_division_cached_ratings(division["id"], include_all=show_all)
    if not cached:
        await interaction.followup.send(
            "⚠️ Rating cache empty. Ask the bot owner to refresh it.",
            ephemeral=True,
        )
        return

    rows = [dict(r) for r in cached]

    if standins:
        # Everyone a team already has: the drafted players and the captains,
        # who aren't in player_costs because they did the drafting.
        from db import get_captain_account_ids, get_player_costs
        season_id = division["current_season_id"]
        rostered = set(get_player_costs(season_id, division["id"]))
        rostered |= {aid for aid in get_captain_account_ids(season_id, division["id"]).values() if aid}
        # A person is one or more accounts, and being drafted under any of
        # them puts the whole person on a team. Checking only an account's
        # main gets this backwards for someone drafted under their alt — which
        # is real: Mr.Glock is drafted on an account that is dingus's alt, and
        # a main-only test offered him as a stand-in.
        person = {r["account_id"]: (r.get("alt_account_for") or r["account_id"]) for r in rows}
        drafted_people = {person.get(a, a) for a in rostered}
        rows = [r for r in rows if person[r["account_id"]] not in drafted_people]
        if not rows:
            await interaction.followup.send(
                "⚠️ Everyone in the cache is on a roster this season.", ephemeral=True)
            return

    division = _require_division(interaction)
    if division:
        from db import compute_fantasy_adjusted_ratings
        adjustments = compute_fantasy_adjusted_ratings(division["current_season_id"], division["id"])
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
    embeds = format_players_list(rows, fantasy_adjusted=True, debug=debug,
                                 note="not on a roster" if standins else None)
    # Discord's 6000-char limit is across all embeds in a single message, so
    # we send one followup message per embed instead of cramming them together.
    for embed in embeds:
        await interaction.followup.send(embed=embed, ephemeral=not public)


@tree.command(name="matches", description="Show matches with Dotabuff links")
@app_commands.describe(
    season="Which season to look at (defaults to the current one)",
    week="Season week number (1, 2, 3...) or -1 for all matches. Leave blank for the current week.",
    player="Filter to matches a specific player appeared in (partial name match).",
    public=PUBLIC_BY_DEFAULT_PARAM_DESCRIPTION,
)
@app_commands.autocomplete(season=_season_autocomplete)
async def matches(interaction: discord.Interaction, week: int = None, player: str = None, season: str = None, public: bool = True):
    # Defer immediately to avoid 3-second timeout
    await interaction.response.defer(ephemeral=not public)

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    season_id, season_err = _resolve_season(interaction, season)
    if season_err:
        await interaction.followup.send(season_err, ephemeral=True)
        return

    from db import get_matches_for_season_week, get_latest_matches, get_all_matches, get_match_ids_for_player, get_current_season_week

    if week is None:
        match_list = get_latest_matches(division["id"], season_id)
        week_label = f"Week {get_current_season_week(season_id, division["id"])}"
    elif week == -1:
        match_list = get_all_matches(division["id"])
        week_label = "All Matches"
    else:
        match_list = get_matches_for_season_week(season_id, division["id"], week)
        week_label = f"Week {week}"

    # Apply player filter if specified
    if player:
        player_match_ids = get_match_ids_for_player(division["id"], player)
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
    side_captains = get_match_team_captains(season_id, division["id"], [m["match_id"] for m in match_list])
    team_names: dict[str, str] = {}
    for m in match_list:
        sides = side_captains.get(m["match_id"], {})
        for side in ("radiant", "dire"):
            cap = sides.get(side)
            if cap and cap not in team_names:
                team_names[cap] = get_team_info(cap, season_id, division["id"]).get("team_name") or cap
            m[f"{side}_team"] = team_names.get(cap)

    from formatters import format_matches_list
    embed = format_matches_list(match_list, week_label=week_label)
    await interaction.followup.send(embed=embed, ephemeral=not public)


@tree.command(name="summary", description="Show 10 fantasy-points leaderboards: each position × latest-week and all-time")
@app_commands.autocomplete(season=_season_autocomplete)
@app_commands.describe(
    season="Which season to look at (defaults to the current one)",
    public=PUBLIC_BY_DEFAULT_PARAM_DESCRIPTION,
)
async def summary(interaction: discord.Interaction, season: str = None, public: bool = True):
    await interaction.response.defer(ephemeral=not public)

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    season_id, season_err = _resolve_season(interaction, season)
    if season_err:
        await interaction.followup.send(season_err, ephemeral=True)
        return

    from db import get_stats_for_season_week, get_all_time_stats, get_current_season_week

    try:
        # Same current-week resolver /leaderboard, /player, and /matches use
        # for a blank week — one source of truth instead of a bespoke one.
        current_week = get_current_season_week(season_id, division["id"])
        week_stats_full = get_stats_for_season_week(season_id, division["id"], current_week)
        week_label_base = f"Week {current_week}"
        alltime_stats_full = ranked(get_all_time_stats(division["id"], season_id))

        # Min-games qualification (50% of most-active player's count) applied per scope.
        week_qualified, week_threshold, week_max = qualified(week_stats_full)

        alltime_qualified, alltime_threshold, alltime_max = qualified(alltime_stats_full)

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


@tree.command(name="standings", description="Show the season's table — record and tiebreaker, the way the site ranks them")
@app_commands.autocomplete(season=_season_autocomplete)
@app_commands.describe(season=SEASON_PARAM_DESCRIPTION, public=PUBLIC_BY_DEFAULT_PARAM_DESCRIPTION)
async def standings(interaction: discord.Interaction, season: str = None, public: bool = True):
    await interaction.response.defer(ephemeral=not public)

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("\u26a0\ufe0f No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return
    season_id, season_err = _resolve_season(interaction, season)
    if season_err:
        await interaction.followup.send(season_err, ephemeral=True)
        return

    # The website's own standings, so the two can't disagree about who is top
    # or how a tie is broken.
    from web import store
    from db import get_season, season_label

    teams = store.season_teams({"id": season_id, "division_id": division["id"]})
    if not teams:
        await interaction.followup.send(
            "\u26a0\ufe0f No teams yet \u2014 standings appear once the draft is in.", ephemeral=True)
        return

    lines = []
    for i, t in enumerate(teams, 1):
        lines.append(f"**{i}. {t['name']}** \u2014 {t['wins']}\u2013{t['losses']} "
                     f"\u00b7 {t['buchholz']} tb")
    embed = discord.Embed(
        title=f"{season_label(get_season(season_id))} standings",
        description="\n".join(lines),
        color=0x9A2A1E,
    )
    embed.set_footer(text="Sorted by record, then the combined wins of everyone a team has played (tb).")
    await interaction.followup.send(embed=embed, ephemeral=not public)


@tree.command(name="hall_of_fame", description="Show every season that has been won, and who won it")
@app_commands.describe(public=PUBLIC_BY_DEFAULT_PARAM_DESCRIPTION)
async def hall_of_fame(interaction: discord.Interaction, public: bool = True):
    await interaction.response.defer(ephemeral=not public)

    from web import store

    champions = store.all_champions()
    if not champions:
        await interaction.followup.send("\u26a0\ufe0f No season has been won yet.", ephemeral=True)
        return

    embed = discord.Embed(title="\U0001F3C6 Hall of Fame", color=0x9A2A1E)
    for c in champions[:25]:          # an embed takes 25 fields
        who = ", ".join(m["name"] for m in c["members"])
        embed.add_field(name=f"{c['season']['label']} \u2014 {c['team'] or 'Champions'}",
                        value=who or "\u2014", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=not public)


@tree.command(name="quote", description="Display a random chat message from league matches")
@app_commands.describe(public=PUBLIC_BY_DEFAULT_PARAM_DESCRIPTION)
async def quote(interaction: discord.Interaction, public: bool = True):
    division = _require_division(interaction)
    if not division:
        await interaction.response.send_message("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    from db import get_random_quote

    quote_data = get_random_quote(division["id"])
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


@tree.command(name="hi_vs_low", description="Split 10 players into hi-MMR vs lo-MMR teams and set random requirements")
@app_commands.describe(players="10 comma-separated player names, override nicknames, or account IDs", public=PUBLIC_BY_DEFAULT_PARAM_DESCRIPTION)
async def hi_vs_low(interaction: discord.Interaction, players: str, public: bool = True):
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
        compute_fantasy_adjusted_ratings(division["current_season_id"], division["id"])
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


# /start_new_season used to live here: a rollover driven by a slash command with
# a CSV attachment, setting up a league nobody could see the result of until
# they loaded the site. It's the admin pages' job now — Admin → Season setup,
# which previews before it writes (season_setup.roll_over).



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
        count = await fetch_and_store_matches_for_division(division["id"])
        # Kick off the same windrun + OpenDota rating refresh /refresh_ratings does,
        # in the background so this command returns fast. User gets a second ping
        # when the rating refresh completes (~5s/player due to windrun rate limit).
        from db import get_division_player_account_ids
        aids = get_division_player_account_ids(division["id"])
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
        nuke_data(division["id"])
        count = await fetch_and_store_matches_for_division(division["id"])
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
# Weekly auto-fetch (Thursday 09:00 UTC)
# ---------------------------------------------------------------------------
# League night is Wednesday evening ET and wraps up around 03:30 UTC Thursday.
# 09:00 UTC (5am EDT / 4am EST) leaves OpenDota a few hours to ingest the
# games and still has everything in place before anyone's awake.

@tasks.loop(time=datetime.min.time().replace(hour=9, tzinfo=timezone.utc))
async def weekly_fetch():
    # Only run on Thursdays (weekday() == 3)
    if datetime.now(timezone.utc).weekday() != 3:
        return
    logger.info("Weekly fetch triggered (Thursday 09:00 UTC)")

    # Fetch each division's current season once: divisions sharing a season
    # (one league) are all filled in by the same fetch.
    divisions = get_all_divisions()
    total_count = 0
    for season_id in sorted({d["current_season_id"] for d in divisions if d.get("current_season_id")}):
        try:
            count = await fetch_and_store_season(season_id)
            total_count += count
            logger.info(f"Weekly fetch for league {season_id}: {count} match(es)")
        except Exception:
            logger.exception(f"Weekly fetch failed for league {season_id}")

    logger.info(f"Weekly fetch complete: {total_count} total match(es) across {len(divisions)} division(s)")

    # Then re-rate everyone who has played, while the new results are the
    # freshest thing in the database — the fantasy adjustment is fitted from
    # them, and ratings are on the team pages now, so a stale number is a
    # number a captain reads. Nothing else did this on a schedule: it waited
    # on someone running /refresh_ratings.
    # Wrapped: a rating refresh that throws must not take the match fetch's
    # loop down with it, since the fetch is the part that can't be done by
    # hand afterwards.
    try:
        import rating_compute
        import rating_group
        from db import get_division_player_account_ids
        for division_id in sorted({d["id"] for d in divisions}):
            aids = get_division_player_account_ids(division_id)
            done = 0
            for aid in aids:
                if await rating_compute.compute_and_cache_rating(aid) is not None:
                    done += 1
            logger.info("Weekly rating refresh for division %s: %d of %d rated",
                        division_id, done, len(aids))
        rating_group.recompute_all()
    except Exception:
        logger.exception("Weekly rating refresh failed")


# ---------------------------------------------------------------------------
# Bot startup
# ---------------------------------------------------------------------------

async def _setup_once():
    """Runs once, before the bot connects to Discord: database, weekly
    fetch schedule, slash commands, and the web server. Kept out of on_ready,
    which fires again on every reconnect — and so the website comes up even
    if Discord is unreachable."""
    init_db()

    # Re-pool multi-account players from what's already cached, so a formula
    # change lands on them without waiting for a refresh. No API calls.
    try:
        import rating_group
        n = rating_group.recompute_all()
        if n:
            logger.info("Recomputed %d multi-account rating group(s)", n)
    except Exception:
        logger.exception("Group rating recompute failed at startup")

    # The web server: stream cards, the Sheets API and the league website.
    try:
        from botstats.server import start_web_server
        await start_web_server()
    except Exception:
        logger.exception("Failed to start the web server")

    weekly_fetch.start()

    try:
        synced = await tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s).")
    except Exception:
        logger.exception("Failed to sync commands")


bot.setup_hook = _setup_once


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
