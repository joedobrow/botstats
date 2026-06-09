import discord
from discord import app_commands
from discord.ext import tasks
import logging
from datetime import datetime, timezone, timedelta

from config import DISCORD_TOKEN, ADMIN_USER_ID, REGION_CLUSTERS, GAME_MODE_FILTERS
from fetcher import fetch_and_store_matches_for_division, fetch_and_store_drafts_for_guild
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
            stats = get_all_time_stats(guild_id, season_start)
            week_label = "All-Time"
        else:
            # Specific season week (0, 1, 2, ...)
            stats = get_stats_for_season_week(guild_id, week, season_start)
            week_label = f"Week {week}"

        # value/attendance lean on draft data: show every drafted player (cost set),
        # skip the min-games threshold, and don't cap the list.
        draft_only_stats = {"value", "attendance"}
        if stat.value in draft_only_stats:
            stats = [s for s in stats if s.get("cost") is not None]
            threshold = None
            max_games = None
            limit = None
        else:
            # Min-games qualification: 50% of the most-active player's games, rounded down
            max_games = max((s.get("games_played", 0) or 0) for s in stats) if stats else 0
            threshold = max_games // 2
            stats = [s for s in stats if (s.get("games_played", 0) or 0) >= threshold]
            limit = 10

        # Filter by position if specified
        if pos is not None:
            stats = [s for s in stats if s.get("role_position") == pos]
            week_label += f" (Position {pos})"

        if not stats:
            await interaction.followup.send(f"⚠️ No data found for {week_label.lower()}. If this seems wrong, the request may have timed out — please try again.", ephemeral=True)
            return

        embed = format_leaderboard(stats, sort_by=stat.value, week_label=week_label, threshold=threshold, max_games=max_games, limit=limit)
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
            stats = get_all_time_stats(guild_id, season_start)
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


@tree.command(name="lookup", description="Look up a player's windrun rating and MMR")
@app_commands.describe(
    query="Player name (partial match) or numeric account ID"
)
async def lookup(interaction: discord.Interaction, query: str):
    await interaction.response.defer()

    q = (query or "").strip()
    if not q:
        await interaction.followup.send("⚠️ Empty query.", ephemeral=True)
        return

    account_id: int | None = None
    player_name: str | None = None

    if q.isdigit():
        account_id = int(q)
    else:
        # Partial-name match against this guild's player history first; fall
        # back to global so /lookup works even for opponents we've never
        # tracked locally.
        from db import _conn
        guild_id = interaction.guild_id
        with _conn() as conn:
            rows = conn.execute("""
                SELECT DISTINCT p.account_id, p.name
                FROM players p
                JOIN matches m ON p.match_id = m.match_id
                WHERE m.guild_id = ? AND LOWER(p.name) LIKE LOWER(?)
            """, (guild_id, f"%{q}%")).fetchall()
            if not rows:
                rows = conn.execute("""
                    SELECT DISTINCT account_id, name
                    FROM players
                    WHERE LOWER(name) LIKE LOWER(?)
                """, (f"%{q}%",)).fetchall()
        unique = list({(r["account_id"], r["name"]) for r in rows})
        if not unique:
            await interaction.followup.send(
                f"⚠️ No player matching `{q}`. Try a different spelling or use the numeric account ID.",
                ephemeral=True,
            )
            return
        if len(unique) > 1:
            # Prefer an exact case-insensitive match — `Vanity` shouldn't be
            # ambiguous with `vanity destroyer 999`.
            ql = q.lower()
            exact = [(aid, name) for aid, name in unique if (name or "").lower() == ql]
            if len(exact) == 1:
                unique = exact
            else:
                names = ", ".join(f"`{n}`" for _, n in unique[:10])
                await interaction.followup.send(
                    f"Multiple matches: {names}\nBe more specific.",
                    ephemeral=True,
                )
                return
        account_id, player_name = unique[0]

    # Fetch in parallel: one Windrun call (rate-limited but only one, so fast)
    # plus OpenDota profile + game counts. AD counts come from OpenDota now
    # (game_mode=18 filter) so we don't need windrun's heavy /matches endpoint.
    from windrun import fetch_player as fetch_windrun_player
    from opendota_lookup import fetch_player_profile, fetch_player_game_counts
    import asyncio

    wr_task        = asyncio.create_task(fetch_windrun_player(account_id))
    od_task        = asyncio.create_task(fetch_player_profile(account_id))
    od_counts_task = asyncio.create_task(fetch_player_game_counts(account_id))

    results = await asyncio.gather(
        wr_task, od_task, od_counts_task, return_exceptions=True,
    )
    wr, od, od_counts = (None if isinstance(r, Exception) else r for r in results)
    for label, r in zip(("windrun", "opendota", "opendota-counts"), results):
        if isinstance(r, Exception):
            logger.exception("%s fetch failed for %d", label, account_id, exc_info=r)

    # AD counts from OpenDota (game_mode=18). These drive the internal-rating
    # threshold rules. The display also surfaces them.
    ad_all_time  = (od_counts or {}).get("all_time_ad")
    ad_last_year = (od_counts or {}).get("last_year_ad")

    # Pick up any admin-set skill override for this account.
    from db import get_skill_override
    override = get_skill_override(account_id)

    from formatters import format_lookup
    embed = format_lookup(
        account_id=account_id,
        fallback_name=player_name,
        windrun=wr,
        opendota=od,
        ad_all_time=ad_all_time,
        ad_last_year=ad_last_year,
        od_counts=od_counts,
        override=override,
    )
    await interaction.followup.send(embed=embed)


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
    with _conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT p.account_id, p.name
            FROM players p
            JOIN matches m ON p.match_id = m.match_id
            WHERE m.guild_id = ? AND LOWER(p.name) LIKE LOWER(?)
        """, (guild_id, f"%{q}%")).fetchall()
        if not rows:
            rows = conn.execute("""
                SELECT DISTINCT account_id, name
                FROM players
                WHERE LOWER(name) LIKE LOWER(?)
            """, (f"%{q}%",)).fetchall()
    unique = list({(r["account_id"], r["name"]) for r in rows})
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


@tree.command(name="set_skill", description="[Admin] Manually override a player's skill ratings used by /lookup")
@app_commands.describe(
    name="Player name (partial match) or numeric account ID",
    windrun="Manual windrun rating (omit to keep existing)",
    ranked_mmr="Manual ranked MMR (omit to keep existing)",
    note="Optional note (e.g. 'smurf — real rank is 10k')",
    clear="Set True to remove any existing override for this player",
)
async def set_skill(
    interaction: discord.Interaction,
    name: str,
    windrun: float = None,
    ranked_mmr: int = None,
    note: str = None,
    clear: bool = False,
):
    if not (ADMIN_USER_ID and interaction.user.id == ADMIN_USER_ID):
        await interaction.response.send_message("⚠️ Bot owner only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    account_id, player_name, err = await _resolve_player_query(interaction, name)
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    from db import upsert_skill_override, delete_skill_override, get_skill_override
    label = f"`{player_name}` (`{account_id}`)" if player_name else f"`{account_id}`"

    if clear:
        removed = delete_skill_override(account_id)
        msg = f"✅ Cleared override for {label}." if removed else f"ℹ️ No override existed for {label}."
        await interaction.followup.send(msg, ephemeral=True)
        return

    if windrun is None and ranked_mmr is None and note is None:
        # Show current state
        existing = get_skill_override(account_id)
        if existing:
            await interaction.followup.send(
                f"Current override for {label}:\n"
                f"• windrun: `{existing.get('windrun_rating')}`\n"
                f"• ranked_mmr: `{existing.get('ranked_mmr')}`\n"
                f"• note: `{existing.get('note')}`\n"
                f"• set by `{existing.get('set_by_name')}` at `{existing.get('set_at')}`",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"No override set for {label}.\n"
                "Use `windrun:` and/or `ranked_mmr:` to set one. `clear:True` to remove.",
                ephemeral=True,
            )
        return

    upsert_skill_override(
        account_id=account_id,
        windrun_rating=windrun,
        ranked_mmr=ranked_mmr,
        note=note,
        set_by_user_id=interaction.user.id,
        set_by_name=interaction.user.display_name,
    )

    parts = []
    if windrun is not None:    parts.append(f"windrun=`{windrun}`")
    if ranked_mmr is not None: parts.append(f"ranked_mmr=`{ranked_mmr}`")
    if note is not None:       parts.append(f"note=`{note}`")
    await interaction.followup.send(
        f"✅ Updated override for {label}: {', '.join(parts)}",
        ephemeral=True,
    )


@tree.command(name="playerdiff", description="Compare a player's fantasy points to their same-side teammates")
@app_commands.describe(
    name="Player name (partial match is fine)",
    week="Season week number (1, 2, 3...) or -1 for all-time. Leave blank for all-time."
)
async def playerdiff(interaction: discord.Interaction, name: str, week: int = None):
    await interaction.response.defer()

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    season_start = division["season_start"]

    from db import get_stats_for_season_week, get_all_time_stats, get_player_team_diff

    try:
        if week is None or week == -1:
            stats = get_all_time_stats(guild_id, season_start)
            week_label = "All-Time"
            week_arg = None
        else:
            stats = get_stats_for_season_week(guild_id, week, season_start)
            week_label = f"Week {week}"
            week_arg = week

        if not stats:
            await interaction.followup.send(f"⚠️ No data found for {week_label.lower()}.", ephemeral=True)
            return

        matches = [p for p in stats if name.lower() in p["name"].lower()]
        if not matches:
            await interaction.followup.send(f"⚠️ No player matching \"{name}\" found for {week_label.lower()}.", ephemeral=True)
            return
        if len(matches) > 1:
            names = ", ".join(f"`{p['name']}`" for p in matches[:10])
            await interaction.followup.send(f"Multiple matches found: {names}\nPlease be more specific.", ephemeral=True)
            return

        player = matches[0]
        diff = get_player_team_diff(guild_id, player["account_id"], season_start, week_arg)
        if not diff:
            await interaction.followup.send(f"⚠️ Couldn't compute team diff for {player['name']}.", ephemeral=True)
            return

        from formatters import format_player_diff
        embed = format_player_diff(diff, week_label=week_label)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.exception(f"Error in playerdiff command for week {week}")
        await interaction.followup.send(f"❌ Error loading player diff: {str(e)}", ephemeral=True)


@tree.command(name="suggested_cost", description="Suggest a draft cost for each player based on diff + fp + mmr")
@app_commands.describe(
    name="Player name (partial match). Leave blank to see all players.",
    mmr_weight="How heavily to weight MMR in the prediction. 1.0 = natural fit, 2.0 = double weight (default), 0 = ignore MMR.",
)
async def suggested_cost(interaction: discord.Interaction, name: str = None, mmr_weight: float = 2.0):
    await interaction.response.defer()

    division = _require_division(interaction)
    if not division:
        await interaction.followup.send("⚠️ No division configured. Ask an admin to run `/config` first.", ephemeral=True)
        return

    guild_id = interaction.guild_id
    season_start = division["season_start"]

    from db import get_all_time_stats, fit_cost_from_perf

    try:
        stats = get_all_time_stats(guild_id, season_start)
        if not stats:
            await interaction.followup.send("⚠️ No stats available yet.", ephemeral=True)
            return

        # Silently exclude tiny-sample noise from both the fit and the suggestions.
        MIN_GAMES = 4

        # Forward regression: cost = a + b_d*diff + b_f*fp + b_m*mmr,
        # fit on drafted players with all four fields present and >= MIN_GAMES games.
        eligible = [
            s for s in stats
            if s.get("cost") is not None and s.get("mmr") is not None
               and s.get("diff") is not None and s.get("fantasy_points") is not None
               and (s.get("games_played", 0) or 0) >= MIN_GAMES
        ]
        fit = fit_cost_from_perf(eligible)
        if fit is None:
            await interaction.followup.send("⚠️ Not enough data to fit the cost regression yet.", ephemeral=True)
            return
        a, b_d, b_f, b_m = fit

        # Re-center the intercept so the *weighted* model's average prediction
        # still matches the average historical cost. With weight W:
        #   predicted_cost = a' + b_d*diff + b_f*fp + (W*b_m)*mmr
        #   a' = a + (1 - W) * b_m * mean_mmr
        mean_mmr = sum(s["mmr"] for s in eligible) / len(eligible)
        a_eff = a + (1.0 - mmr_weight) * b_m * mean_mmr
        b_m_eff = mmr_weight * b_m

        suggestions = []
        for s in stats:
            d = s.get("diff")
            fp = s.get("fantasy_points")
            mmr = s.get("mmr")
            if d is None or fp is None or mmr is None:
                continue
            if s.get("cost") is None:
                continue
            if (s.get("games_played", 0) or 0) < MIN_GAMES:
                continue
            raw_suggested = a_eff + b_d * d + b_f * fp + b_m_eff * mmr
            suggestions.append({
                "name":           s.get("name", ""),
                "account_id":     s.get("account_id", 0),
                "diff":           d,
                "fp":             fp,
                "games_played":   s.get("games_played", 0) or 0,
                "actual_cost":    s.get("cost"),
                "mmr":            mmr,
                "suggested_cost": max(1, round(raw_suggested)),  # floor at 1
            })

        if name:
            matches = [p for p in suggestions if name.lower() in p["name"].lower()]
            if not matches:
                await interaction.followup.send(f"⚠️ No player matching \"{name}\".", ephemeral=True)
                return
            if len(matches) > 1:
                names = ", ".join(f"`{p['name']}`" for p in matches[:10])
                await interaction.followup.send(f"Multiple matches: {names}\nBe more specific.", ephemeral=True)
                return
            suggestions = matches

        from formatters import format_suggested_costs
        embed = format_suggested_costs(
            suggestions,
            fit=(a, b_d, b_f, b_m),
            mmr_weight=mmr_weight,
            single=bool(name),
        )
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.exception("Error in suggested_cost command")
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


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
            stats = get_all_time_stats(guild_id, season_start)
            week_label = "All-Time"
        else:
            stats = get_stats_for_season_week(guild_id, week, season_start)
            week_label = f"Week {week}"

        # Min-games qualification: 50% of the most-active player's games, rounded down
        max_games = max((s.get("games_played", 0) or 0) for s in stats) if stats else 0
        threshold = max_games // 2
        stats = [s for s in stats if (s.get("games_played", 0) or 0) >= threshold]

        logger.info(f"Roles command: week={week}, found {len(stats) if stats else 0} qualified players (threshold={threshold} of {max_games})")

        if not stats:
            await interaction.followup.send(f"⚠️ No data found for {week_label.lower()}. If this seems wrong, the request may have timed out — please try again.", ephemeral=True)
            return

        # Group by role, show best player per role per stat
        from formatters import format_roles_summary
        embed = format_roles_summary(stats, week_label=week_label, threshold=threshold, max_games=max_games)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.exception(f"Error in roles command for week {week}")
        await interaction.followup.send(f"❌ Error loading roles: {str(e)}", ephemeral=True)


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

        all_time_stats = get_all_time_stats(guild_id, season_start)

        # Min-games qualification: 50% of the most-active player's games, rounded down
        week_max = max((s.get("games_played", 0) or 0) for s in week_stats) if week_stats else 0
        week_threshold = week_max // 2
        week_stats = [s for s in week_stats if (s.get("games_played", 0) or 0) >= week_threshold]

        alltime_max = max((s.get("games_played", 0) or 0) for s in all_time_stats) if all_time_stats else 0
        alltime_threshold = alltime_max // 2
        all_time_stats = [s for s in all_time_stats if (s.get("games_played", 0) or 0) >= alltime_threshold]

        # Create embeds
        embeds = []

        # Latest week embed
        week_embed = discord.Embed(
            title=f"📊 {week_label} — Fantasy Points by Position",
            description=f"Qualified: ≥ {week_threshold} of {week_max} games",
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
            description=f"Qualified: ≥ {alltime_threshold} of {alltime_max} games",
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


@tree.command(name="tipjar", description="Support the bot creator")
async def tipjar(interaction: discord.Interaction):
    await interaction.response.send_message(
        "If you're enjoying the bot, consider tipping the creator!\n"
        "https://venmo.com/Joe-Dobrow",
        ephemeral=True,
    )


@tree.command(name="refresh", description="[Admin] Manually trigger a data fetch from OpenDota")
async def refresh(interaction: discord.Interaction):
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
