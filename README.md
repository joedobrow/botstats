# Dota 2 League Stats Discord Bot

A multi-guild Discord bot that pulls match data from the [OpenDota API](https://docs.opendota.com/) for a configured league, filters by region and game mode, parses Ability Draft replays via a custom Go parser, and exposes per-player stats with a tunable fantasy-points leaderboard.

---

## Features

| Feature | Details |
|---|---|
| **Per-server config** | Each Discord server (guild) configures its own league, region, mode, and season start via `/config`. |
| **Game modes** | Captain's Mode (`cm`) or Ability Draft (`ad`, game_mode 18). |
| **Weekly auto-fetch** | Runs every Monday at 06:00 UTC. Pulls new matches and parses any new AD drafts. |
| **SQLite caching** | Match data is stored locally on a persistent fly.io volume. |
| **Ability Draft replays** | A bundled Go parser (`parser/`) extracts pick order from Source 2 replays; OpenDota fills in ability/hero names. |
| **Fantasy points** | A tunable per-game scoring formula with duration normalization. See `fantasy.py`. |
| **Draft order images** | `/draftorder <match_id>` renders the full pick sequence with ability icons. |
| **Chat quotes** | `/quote` pulls a random non-boring in-game chat message. |
| **Scold channel** | Optional channel where any user post gets deleted with a snarky reply. |

---

## Project Structure

```
.
├── bot.py            # Discord client, slash commands, weekly scheduler
├── config.py         # Loads env vars and constants (regions, modes, role labels)
├── db.py             # SQLite schema + read/write functions
├── fetcher.py        # OpenDota API calls, filtering, persistence
├── fantasy.py        # Fantasy points formula (tunable weights)
├── formatters.py     # Builds Discord Embed objects
├── draftorder.py     # PIL image rendering for AD pick order
├── replay.py         # Replay download + Go parser shell-out
├── opendota_lookup.py# Resolve picks → ability/hero names via OpenDota
├── parser/           # Go (manta) replay parser binary source
├── botstats/         # Lightweight web server for AD helper overlay
├── scripts/          # One-off analysis/maintenance scripts (run from repo root)
├── data/             # Local CSV exports and analysis data (not shipped in image)
├── Dockerfile        # Two-stage build: Go parser + Python bot
├── fly.toml          # fly.io deployment config
└── requirements.txt
```

---

## Setup

### 1. Prerequisites

- Python 3.12+
- Go 1.23+ (only needed locally if you want to rebuild the AD parser; the Dockerfile builds it for you)
- A Discord bot token from [discord.com/developers](https://discord.com/developers/applications)
- A league ID (visible in OpenDota or in-game)
- *(Optional)* An OpenDota API key from [opendota.com/api-keys](https://www.opendota.com/api-keys)
- *(Optional)* A Steam Web API key for replay-salt fallback

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Your Discord bot token |
| `ADMIN_USER_ID` | ❌ | Bot owner Discord user ID (can configure any guild, run `/nuke`) |
| `OPENDOTA_API_KEY` | ❌ | OpenDota API key. Free tier works without one but has lower rate limits |
| `STEAM_API_KEY` | ❌ | Steam Web API key, used as a fallback to fetch replay salts |
| `DB_PATH` | ❌ | Path to the SQLite file. Defaults to `/data/dota_stats.db` if `/data` exists, else `dota_stats.db` |

### 4. Per-guild config

League/region/mode are configured per Discord server at runtime via `/config`:

```
/config league:<id> region:<us_west|us_east|any> mode:<cm|ad> season_start:<YYYY-MM-DD>
```

You can also bind a `scold_channel` to silently delete posts there. Re-run `/config` to update.

### 5. Run locally

```bash
python bot.py
```

---

## Hosting on fly.io

The bot ships with a `Dockerfile` and `fly.toml` configured for fly.io.

1. `fly launch` (or `fly deploy` if the app already exists).
2. Set secrets: `fly secrets set DISCORD_TOKEN=... OPENDOTA_API_KEY=... STEAM_API_KEY=... ADMIN_USER_ID=...`
3. The `[mounts]` block in `fly.toml` provisions a persistent volume at `/data`, where SQLite lives. Don't delete it.

---

## Slash Commands

| Command | Description |
|---|---|
| `/config` | *(Admin)* Configure this server's league, region, mode, season start, scold channel. |
| `/leaderboard <stat> [week] [pos]` | Top players sorted by the chosen stat. Optional season week and positional filter. |
| `/player <name> [week]` | Full stat breakdown for one player. Partial name matching. |
| `/playerdiff <name> [week]` | Compare a player's fantasy points to the average of their same-side teammates. |
| `/roles [week]` | Best fantasy-points player at each position (1–5). |
| `/matches [week] [player]` | Match list with Dotabuff/OpenDota links; optional player filter. |
| `/summary` | Compact fantasy-pts leaderboard per position for latest week + all-time. |
| `/quote` | Random in-game chat message from a parsed match. |
| `/draftorder <match_id>` | Render the AD pick order as an image. Triggers an on-demand replay parse if needed. |
| `/tipjar` | Venmo link for the bot creator. |
| `/refresh` | *(Admin)* Manually trigger a data fetch right now. |
| `/nuke` | *(Admin)* Wipe all data for this server and re-fetch from scratch. |

---

## How Fantasy Points Work

See `fantasy.py` for the full formula and current weights. Points are calculated as a **per-game average** (so games-played doesn't inflate scores), with a duration-normalization term that subtracts the expected pts gained purely from longer games. Re-run `analyze_duration.py` periodically to refresh the regression coefficients as more matches accumulate.

---

## Filtering Logic

Match fetches apply two filters before storing:

1. **Server region** — Cluster IDs are grouped in `config.py` under `REGION_CLUSTERS`. `any` disables the filter.
2. **Game mode** — `cm` includes everything *except* Ability Draft (mode 18). `ad` includes *only* Ability Draft. Edit `GAME_MODE_FILTERS` in `config.py` to add modes.

---

## Role Assignment

Dota 2 has no explicit role field — positions are inferred from the player's slot in the draft. The labels in `config.py` (`ROLE_LABELS`) are conventional and players routinely play off-position; treat them as organizational, not gospel. In Ability Draft, "position" is largely meaningless and roles are mostly a quirk of how OpenDota reports the slot.
