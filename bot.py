import discord
from discord import app_commands
from discord.ext import tasks
import logging
from datetime import datetime, timezone, timedelta

from config import DISCORD_TOKEN, LEAGUE_ID, STATS_CHANNEL_ID, ADMIN_USER_ID
from fetcher import fetch_and_store_weekly_matches
from db import get_latest_week_stats, get_all_weeks, init_db
from formatters import format_leaderboard, format_player_stats, format_weekly_summary

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

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

    from db import get_stats_for_season_week, get_all_time_stats

    try:
        if week is None or week == -1:
            # All-time stats (default)
            stats = get_all_time_stats()
            week_label = "All-Time"
        else:
            # Specific season week (0, 1, 2, ...)
            stats = get_stats_for_season_week(week)
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

    from db import get_stats_for_season_week, get_all_time_stats

    try:
        if week is None or week == -1:
            stats = get_all_time_stats()
            week_label = "All-Time"
        else:
            stats = get_stats_for_season_week(week)
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

    from db import get_stats_for_season_week, get_all_time_stats

    try:
        if week is None or week == -1:
            stats = get_all_time_stats()
            week_label = "All-Time"
        else:
            stats = get_stats_for_season_week(week)
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

    from db import get_matches_for_season_week, get_latest_matches, get_all_matches

    if week is None:
        # No week specified - show latest
        match_list = get_latest_matches()
        week_label = "Latest Week"
    elif week == -1:
        # All matches
        match_list = get_all_matches()
        week_label = "All Matches"
    else:
        # Specific season week requested
        match_list = get_matches_for_season_week(week)
        week_label = f"Week {week}"

    if not match_list:
        await interaction.followup.send(f"⚠️ No matches found for {week_label.lower()}.", ephemeral=True)
        return

    from formatters import format_matches_list
    embed = format_matches_list(match_list, week_label=week_label)
    await interaction.followup.send(embed=embed)


@tree.command(name="refresh", description="[Admin] Manually trigger a data fetch from OpenDota")
async def refresh(interaction: discord.Interaction):
    # Only allow the guild owner or admins
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("⚠️ Only admins can use this command.", ephemeral=True)
        return
    await interaction.response.send_message("⏳ Fetching match data...", ephemeral=True)
    try:
        count = await fetch_and_store_weekly_matches()
        await interaction.followup.send(f"✅ Done! Fetched and stored {count} match(es).", ephemeral=True)
    except Exception as e:
        logger.exception("Refresh failed")
        await interaction.followup.send(f"❌ Error during fetch: {e}", ephemeral=True)


@tree.command(name="nuke", description="[Owner] Wipe all data and re-fetch from OpenDota")
async def nuke(interaction: discord.Interaction):
    # Restrict to bot owner only (ADMIN_USER_ID), not server admins
    if ADMIN_USER_ID is None or interaction.user.id != ADMIN_USER_ID:
        await interaction.response.send_message("hahaa nice try loser", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        from db import nuke_data
        nuke_data()
        count = await fetch_and_store_weekly_matches()
        await interaction.followup.send(f"✅ Data wiped and re-fetched {count} match(es).", ephemeral=True)
    except Exception as e:
        logger.exception("Nuke failed")
        await interaction.followup.send(f"❌ Error during nuke: {e}", ephemeral=True)


# ---------------------------------------------------------------------------
# Weekly auto-fetch (Monday 6:00 AM UTC)
# ---------------------------------------------------------------------------

@tasks.loop(time=datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0).time())
async def weekly_fetch():
    # Only run on Mondays (weekday() == 0)
    if datetime.now(timezone.utc).weekday() != 0:
        return
    logger.info("Weekly fetch triggered (Monday 06:00 UTC)")
    try:
        count = await fetch_and_store_weekly_matches()
        logger.info(f"Weekly fetch complete: {count} match(es) stored.")

        # Post a summary to the configured channel if set
        if STATS_CHANNEL_ID:
            channel = bot.get_channel(STATS_CHANNEL_ID)
            if channel:
                stats = get_latest_week_stats(week_offset=0)
                if stats:
                    embed = format_weekly_summary(stats)
                    await channel.send(embed=embed)
                    logger.info("Weekly summary posted to channel.")
    except Exception:
        logger.exception("Weekly auto-fetch failed")


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
