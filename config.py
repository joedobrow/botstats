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
