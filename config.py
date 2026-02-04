import os

# ---------------------------------------------------------------------------
# Required — set these in your environment (or in a .env file if using
# python-dotenv locally). On Railway/Render you set them in the service's
# Environment Variables panel.
# ---------------------------------------------------------------------------

DISCORD_TOKEN: str = os.environ["DISCORD_TOKEN"]
# Your OpenDota league ID (integer). Replace with your actual league ID.
LEAGUE_ID: int = int(os.environ.get("LEAGUE_ID", "0"))
# Optional: a channel ID (integer) where the bot auto-posts weekly summaries.
# Leave unset or set to 0 to disable auto-posting.
STATS_CHANNEL_ID: int | None = int(os.environ["STATS_CHANNEL_ID"]) if os.environ.get("STATS_CHANNEL_ID") else None
# Optional: your OpenDota API key. Free tier works without one but has lower
# rate limits (60 req/min, 50k/month). Get one at https://www.opendota.com/api-keys
OPENDOTA_API_KEY: str | None = os.environ.get("OPENDOTA_API_KEY")

# ---------------------------------------------------------------------------
# Season configuration
# ---------------------------------------------------------------------------

# Season start date (used for week numbering). Format: "YYYY-MM-DD"
SEASON_START_DATE: str = os.environ.get("SEASON_START_DATE", "2026-01-26")

# Admin user ID - only this user can run /refresh command
ADMIN_USER_ID: int | None = int(os.environ["ADMIN_USER_ID"]) if os.environ.get("ADMIN_USER_ID") else None

# ---------------------------------------------------------------------------
# Dota 2 constants
# ---------------------------------------------------------------------------

# OpenDota game_mode values we want to EXCLUDE.
# 18 = Ability Draft. Add more here if needed.
EXCLUDED_GAME_MODES: set[int] = {18}

# OpenDota cluster IDs that correspond to US West servers.
# Source: https://api.opendota.com/api/constants/cluster
# Region 1 = US West, Region 2 = US East
US_WEST_CLUSTERS: set[int] = {
    111,  # US West
    112,  # US West
    113,  # US West
    114,  # US West
    117,  # US West
    118,  # US West
}

# Dota 2 role labels mapped to the player_slot positions OpenDota returns.
# In a 5-player team the roles are positional; we label them by convention.
ROLE_LABELS: dict[int, str] = {
    1: "Safe Lane (Pos 1)",
    2: "Mid Lane (Pos 2)",
    3: "Off Lane (Pos 3)",
    4: "Roaming (Pos 4)",
    5: "Hard Support (Pos 5)",
}
