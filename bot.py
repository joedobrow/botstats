import discord
from discord import app_commands
from discord.ext import tasks
import logging
from datetime import datetime, timezone, timedelta

from config import DISCORD_TOKEN, ADMIN_USER_ID, REGION_CLUSTERS, GAME_MODE_FILTERS
from fetcher import fetch_and_store_matches_for_division
from db import init_db, get_division, upsert_division, get_all_divisions, get_scold_channel
from formatters import format_leaderboard, format_player_stats, format_weekly_summary

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
            region_display = "US West" if current["region"] == "us_west" else "US East"
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

    region_display = "US West" if region == "us_west" else "US East"
    mode_display = "Captain's Mode" if mode == "cm" else "Ability Draft"
    scold_display = f"<#{scold_channel_id}>" if scold_channel_id else "None"

    await interaction.response.send_message(
        f"✅ Division configured!\n"
        f"League ID: `{league}`\n"
        f"Region: `{region_display}`\n"
        f"Mode: `{mode_display}`\n"
        f"Season Start: `{season_start}`\n"
        f"Scold Channel: {scold_display}\n\n"
        f"Run `/refresh` to fetch match data.",
        ephemeral=True
    )


@tree.command(name="leaderboard", description="Show the leaderboard sorted by a stat")
@app_commands.describe(
    week="Season week number (1, 2, 3...) or -1 for all-time. Leave blank for all-time.",
    stat="Which stat to sort by",
    pos="Filter by position (1-5, optional)"
)
@app_commands.choices(stat=[
    app_commands.Choice(name="Fantasy Points", value="fantasy_points"),
    app_commands.Choice(name="GPM", value="gpm"),
    app_commands.Choice(name="KDA", value="kda"),
    app_commands.Choice(name="Last Hits", value="last_hits"),
    app_commands.Choice(name="Denies", value="denies"),
    app_commands.Choice(name="Damage Dealt", value="hero_damage"),
    app_commands.Choice(name="Healing Done", value="hero_healing"),
    app_commands.Choice(name="XPM", value="xpm"),
], pos=[
    app_commands.Choice(name="Position 1 (Safe Lane)", value=1),
    app_commands.Choice(name="Position 2 (Mid)", value=2),
    app_commands.Choice(name="Position 3 (Off Lane)", value=3),
    app_commands.Choice(name="Position 4 (Roaming)", value=4),
    app_commands.Choice(name="Position 5 (Hard Support)", value=5),
])
async def leaderboard(interaction: discord.Interaction, stat: app_commands.Choice[str], week: int = None, pos: int = None):
    await interaction.response.defer()

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
            stats = get_all_time_stats(guild_id)
            week_label = "All-Time"
        else:
            # Specific season week (0, 1, 2, ...)
            stats = get_stats_for_season_week(guild_id, week, season_start)
            week_label = f"Week {week}"

        # Filter by position if specified
        if pos is not None:
            stats = [s for s in stats if s.get("role_position") == pos]
            week_label += f" (Position {pos})"

        if not stats:
            await interaction.followup.send(f"⚠️ No data found for {week_label.lower()}. If this seems wrong, the request may have timed out — please try again.", ephemeral=True)
            return

        embed = format_leaderboard(stats, sort_by=stat.value, week_label=week_label)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.exception(f"Error in leaderboard command for week {week}")
        await interaction.followup.send(f"❌ Error loading leaderboard: {str(e)}", ephemeral=True)


@tree.command(name="player", description="Show detailed stats for a specific player")
@app_commands.describe(
    name="Player name (partial match is fine)",
    week="Season week number (1, 2, 3...) or -1 for all-time. Leave blank for all-time."
)
async def player(interaction: discord.Interaction, name: str, week: int = None):
    await interaction.response.defer()

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    season_start = division["season_start"]

    from db import get_stats_for_season_week, get_all_time_stats

    try:
        if week is None or week == -1:
            stats = get_all_time_stats(guild_id)
            week_label = "All-Time"
        else:
            stats = get_stats_for_season_week(guild_id, week, season_start)
            week_label = f"Week {week}"

        if not stats:
            await interaction.followup.send(f"⚠️ No data found for {week_label.lower()}.", ephemeral=True)
            return

        # Case-insensitive partial match
        matches = [p for p in stats if name.lower() in p["name"].lower()]
        if not matches:
            await interaction.followup.send(f"⚠️ No player matching \"{name}\" found for {week_label.lower()}.", ephemeral=True)
            return
        if len(matches) > 1:
            names = ", ".join(f"`{p['name']}`" for p in matches[:10])
            await interaction.followup.send(f"Multiple matches found: {names}\nPlease be more specific.", ephemeral=True)
            return

        embed = format_player_stats(matches[0], week_label=week_label)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.exception(f"Error in player command for week {week}")
        await interaction.followup.send(f"❌ Error loading player stats: {str(e)}", ephemeral=True)


@tree.command(name="roles", description="Show stats grouped by role")
@app_commands.describe(week="Season week number (1, 2, 3...) or -1 for all-time. Leave blank for all-time.")
async def roles(interaction: discord.Interaction, week: int = None):
    await interaction.response.defer()

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    season_start = division["season_start"]

    from db import get_stats_for_season_week, get_all_time_stats

    try:
        if week is None or week == -1:
            stats = get_all_time_stats(guild_id)
            week_label = "All-Time"
        else:
            stats = get_stats_for_season_week(guild_id, week, season_start)
            week_label = f"Week {week}"

        logger.info(f"Roles command: week={week}, found {len(stats) if stats else 0} players")

        if not stats:
            await interaction.followup.send(f"⚠️ No data found for {week_label.lower()}. If this seems wrong, the request may have timed out — please try again.", ephemeral=True)
            return

        # Group by role, show best player per role per stat
        from formatters import format_roles_summary
        embed = format_roles_summary(stats, week_label=week_label)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.exception(f"Error in roles command for week {week}")
        await interaction.followup.send(f"❌ Error loading roles: {str(e)}", ephemeral=True)


@tree.command(name="matches", description="Show matches with Dotabuff links")
@app_commands.describe(week="Season week number (1, 2, 3...) or -1 for all matches. Leave blank for latest week.")
async def matches(interaction: discord.Interaction, week: int = None):
    # Defer immediately to avoid 3-second timeout
    await interaction.response.defer()

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    season_start = division["season_start"]

    from db import get_matches_for_season_week, get_latest_matches, get_all_matches

    if week is None:
        # No week specified - show latest
        match_list = get_latest_matches(guild_id)
        week_label = "Latest Week"
    elif week == -1:
        # All matches
        match_list = get_all_matches(guild_id)
        week_label = "All Matches"
    else:
        # Specific season week requested
        match_list = get_matches_for_season_week(guild_id, week, season_start)
        week_label = f"Week {week}"

    if not match_list:
        await interaction.followup.send(f"⚠️ No matches found for {week_label.lower()}.", ephemeral=True)
        return

    from formatters import format_matches_list
    embed = format_matches_list(match_list, week_label=week_label)
    await interaction.followup.send(embed=embed)


@tree.command(name="summary", description="Show fantasy point leaderboards by position for latest week and all-time")
async def summary(interaction: discord.Interaction):
    await interaction.response.defer()

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    season_start = division["season_start"]

    from db import get_stats_for_season_week, get_all_time_stats, get_latest_season_week
    from formatters import format_compact_leaderboard, EMBED_COLOUR_GOLD, EMBED_COLOUR_BLUE
    from config import ROLE_LABELS

    try:
        # Get the latest week number
        latest_week = get_latest_season_week(guild_id, season_start)
        if latest_week:
            week_stats = get_stats_for_season_week(guild_id, latest_week, season_start)
            week_label = f"Week {latest_week}"
        else:
            week_stats = []
            week_label = "Latest Week"

        all_time_stats = get_all_time_stats(guild_id)

        # Create embeds
        embeds = []

        # Latest week embed
        week_embed = discord.Embed(
            title=f"📊 {week_label} — Fantasy Points by Position",
            colour=EMBED_COLOUR_GOLD,
        )
        for pos in [1, 2, 3, 4, 5]:
            label = ROLE_LABELS.get(pos, f"Pos {pos}")
            content = format_compact_leaderboard(week_stats, pos, "fantasy_points")
            week_embed.add_field(name=label, value=content, inline=True)
        embeds.append(week_embed)

        # All-time embed
        alltime_embed = discord.Embed(
            title="📊 All-Time — Fantasy Points by Position",
            colour=EMBED_COLOUR_BLUE,
        )
        for pos in [1, 2, 3, 4, 5]:
            label = ROLE_LABELS.get(pos, f"Pos {pos}")
            content = format_compact_leaderboard(all_time_stats, pos, "fantasy_points")
            alltime_embed.add_field(name=label, value=content, inline=True)
        embeds.append(alltime_embed)

        await interaction.followup.send(embeds=embeds)
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
        await interaction.response.send_message("No chat messages found yet. Try again after a `/refresh`!", ephemeral=True)
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


@tree.command(name="tipjar", description="Support the bot creator")
async def tipjar(interaction: discord.Interaction):
    await interaction.response.send_message(
        "If you're enjoying the bot, consider tipping the creator!\n"
        "https://venmo.com/Joe-Dobrow",
        ephemeral=True,
    )


@tree.command(name="refresh", description="[Admin] Manually trigger a data fetch from OpenDota")
async def refresh(interaction: discord.Interaction):
    # Only allow the guild owner or admins
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("⚠️ Only admins can use this command.", ephemeral=True)
        return

    division = _require_division(interaction)
    if not division:
        await interaction.response.send_message("⚠️ No division configured. Use `/config` first.", ephemeral=True)
        return

    await interaction.response.send_message("⏳ Fetching match data...", ephemeral=True)
    try:
        count = await fetch_and_store_matches_for_division(interaction.guild_id)
        await interaction.followup.send(f"✅ Done! Fetched and stored {count} match(es).", ephemeral=True)
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

    scold_channel_id = get_scold_channel(message.guild.id)
    if not scold_channel_id or message.channel.id != scold_channel_id:
        return

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


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
