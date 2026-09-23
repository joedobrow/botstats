import os

# ---------------------------------------------------------------------------
# Required — set these in your environment (or in a .env file if using
# python-dotenv locally). On Railway/Render you set them in the service's
# Environment Variables panel.
# ---------------------------------------------------------------------------

DISCORD_TOKEN: str = os.environ["DISCORD_TOKEN"]

# Optional: your OpenDota API key. Free tier works without one but has lower
# rate limits (60 req/min, 50k/month). Get one at https://www.opendota.com/api-keys
OPENDOTA_API_KEY: str | None = os.environ.get("OPENDOTA_API_KEY")

# Bot owner user ID - can configure any division and nuke any data
ADMIN_USER_ID: int | None = int(os.environ["ADMIN_USER_ID"]) if os.environ.get("ADMIN_USER_ID") else None

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")

# Shared secret required (as the X-Api-Key header) to call GET /api/ratings.
# Used by the Google Apps Script that syncs the scout sheet. Generate with
# e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
RATINGS_API_KEY: str | None = os.environ.get("RATINGS_API_KEY")

# Keys handed to other people for GET /api/player only, as "label:key" pairs
# separated by commas — e.g. "sam:abc123,pat:def456". Each person gets their
# own, so one can be revoked (remove it, redeploy) without breaking the rest
# or the sheet's RATINGS_API_KEY. The label shows in the logs, never the key.
PLAYER_API_KEYS: dict[str, str] = {
    key.strip(): label.strip()
    for label, _, key in (pair.partition(":") for pair in os.environ.get("PLAYER_API_KEYS", "").split(","))
    if key.strip()
}

# How many never-before-seen players one /api/player key may have computed
# per hour. Each costs a windrun call, and windrun is rate-limited to one
# account every 5 seconds under the user-agent they whitelisted for us —
# so this is really a budget for that, not for our own capacity. Cached
# players are unlimited.
PLAYER_API_NEW_LOOKUPS_PER_HOUR: int = int(os.environ.get("PLAYER_API_NEW_LOOKUPS_PER_HOUR", "60"))

# The division /api/player uses for the season fantasy adjustment when the
# caller doesn't pass guild_id — the same adjustment /player shows there.
API_DEFAULT_GUILD_ID: int = int(os.environ.get("API_DEFAULT_GUILD_ID", "1481800158826991718"))

# Channels where /lookup is open to anyone (typically admin-only Discord
# channels). Owner can always use /lookup anywhere.
LOOKUP_CHANNEL_IDS: set[int] = {1512177911711662272}

# Timezone used for every user-facing date/time. Match timestamps are stored
# as UTC unix seconds; without this they render in the host's zone, which on
# fly.io is UTC and reads as several hours off for an EST-WED league.
# Override with LEAGUE_TZ=America/Los_Angeles etc. if a division moves.
LEAGUE_TZ_NAME: str = os.environ.get("LEAGUE_TZ", "America/New_York")

try:
    from zoneinfo import ZoneInfo
    LEAGUE_TZ = ZoneInfo(LEAGUE_TZ_NAME)
except Exception:  # pragma: no cover - bad tz name shouldn't take the bot down
    from datetime import timezone as _tz
    LEAGUE_TZ = _tz.utc


# ---------------------------------------------------------------------------
# Dota 2 constants
# ---------------------------------------------------------------------------

# Region cluster mappings (from OpenDota API constants)
# Source: https://api.opendota.com/api/constants/cluster
REGION_CLUSTERS: dict[str, set[int] | None] = {
    "us_west": {111, 112, 113, 114, 117, 118},
    "us_east": {121, 122, 123, 124},
    "any": None,
}

# Game mode configurations
# 'cm' = Captain's Mode (mode 2) - exclude Ability Draft
# 'ad' = Ability Draft (mode 18) - only include Ability Draft
GAME_MODE_FILTERS: dict[str, dict] = {
    "cm": {"include": None, "exclude": {18}},  # All modes except Ability Draft
    "ad": {"include": {18}, "exclude": None},  # Only Ability Draft
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
