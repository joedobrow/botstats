# Dota 2 League Stats Discord Bot

A multi-guild Discord bot that pulls match data from the [OpenDota API](https://docs.opendota.com/) for a configured league, filters by region and game mode, parses Ability Draft replays via a custom Go parser, and exposes per-player stats with a tunable fantasy-points leaderboard.

---

## Features

| Feature | Details |
|---|---|
| **Per-server config** | Each Discord server (guild) configures its current season (by Dota league id), region, mode, and season start via `/config`. |
| **Game modes** | Captain's Mode (`cm`) or Ability Draft (`ad`, game_mode 18). |
| **Weekly auto-fetch** | Runs every Thursday at 09:00 UTC (early morning ET, after Wednesday league night). Pulls new matches and parses any new AD drafts. |
| **SQLite caching** | Match data is stored locally on a persistent fly.io volume. |
| **Ability Draft replays** | A bundled Go parser (`parser/`) extracts pick order from Source 2 replays; OpenDota fills in ability/hero names. |
| **Fantasy points** | A tunable per-game scoring formula with duration normalization. See `fantasy.py`. |
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
├── replay.py         # Replay download + Go parser shell-out
├── opendota_lookup.py# Resolve picks → ability/hero names via OpenDota
├── parser/           # Go (manta) replay parser binary source
├── botstats/         # Lightweight web server for AD helper overlay
├── web/              # League website (Steam login, signups, season/player pages)
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

### Divisions

A division is one competition within the league (e.g. NA, EU): its own game
mode, server region, rosters and games. Divisions are records of their own,
not Discord servers: each has an optional Discord server where its commands
run, and several may share one (commands then use the server's default
division). Every game belongs to one division, assigned when it's fetched
(`fetcher.pick_division`: the division whose mode and region fit, and if
several do, whose drafted rosters include most of the game's players).
`/config name:` names or renames a division. `scripts/merge_divisions.py` merges one division
into another.

### Seasons

A season is identified by its **Dota league id** — RD2L Season 39 is `20056`,
the same number OpenDota and Dotabuff use. `seasons.id` is that league id, and
everything filed under a season (draft costs, weeks, team names, the
division's current season, website signups and status) refers to it. The
start date is an ordinary field, so it can change without moving anything.
A division's current season is also the league the fetcher pulls, and a
season's matches are exactly the matches played in its league — every match
records its league id — rather than a date range. Weeks are date ranges
within that.

`/config league:<id>` makes that league the current season (`season_start`
is required the first time a league is seen). Rolling a division over to a new
league — its weeks, drafted rosters, and team names and logos — is **Admin →
Season setup** on the site, which previews everything before it writes
(`season_setup.roll_over`). It was the `/start_new_season` command until that
setup grew past what a slash command with a CSV attachment could show you.
A season's weeks are laid out and adjusted on the season page's Matchups tab,
which is where the week filters they drive are read — they were
`/generate_weeks`, `/set_week`, `/delete_week` and `/list_weeks` until it
became silly to fix in Discord something that only shows up on the site.
Every `season:` option in Discord accepts the
league id, the start date, or the season's name. The stats API endpoints take
`?season_id=<league_id>` (plus `&division=<slug>` when several divisions play
in it), or the older `?guild_id=...&season_start=YYYY-MM-DD`.

### Schema migrations

Structural schema changes are numbered migrations in `db._MIGRATIONS`; the
database records how far it got in `PRAGMA user_version`, and `init_db`
applies the rest in order, each in one transaction with row-count checks.
Append new ones; never edit or reorder existing ones.

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

## League Website

The bot's web server (port 8080) also serves a small league site modeled on rd2l.gg, from the `web/` package:

- `/seasons`, `/season/<league_id>/<division>`: every season of every division, with Teams, Matches, Leaders and Signups tabs (`/season/<league_id>` goes to the division, or offers a choice when several play in it)
- `/players`, `/player/<account_id>`: player search and profiles (league stats per season, heroes, recent matches)
- `/match/<match_id>`: scoreboard with fantasy points
- `/`: tiles for Sign-Up (active only while a season has signups open), Seasons, Players and Info (`/info` → Rules, About)
- `/settings`: Steam log in and out, Discord name, and your signups. Logged-in players can sign up for seasons with open signups
- `/admin`: create seasons, set each season's status (Upcoming, Signups open, Active, Completed; unset seasons default to Active for the newest started one and Completed before it), rename divisions, edit the About and Rules text, download signups as CSV

It reads the bot's own tables (a team is the captain's drafted roster in `player_costs`, and a match side belongs to a team when three or more of its roster played) and keeps its own data in `site_*` tables. Player names follow the bot's rule (`db.get_display_names`: `/set_player` nickname, then the rating-cache name, then the latest match name), so the site and `/player` agree. Public pages show stats and ranked badges only, never internal or windrun ratings.

| Variable | Description |
|---|---|
| `SITE_UBERADMIN_STEAM_IDS` | Comma-separated Steam64 or Dota account IDs of uberadmins: everything admins can do, plus adding and removing admins on `/admin` |
| `SITE_BASE_URL` | Public URL, e.g. `https://dota-bot.fly.dev`. Used for the Steam return URL; defaults to the request's host |
| `STEAM_API_KEY` | Already used by the bot; also fills in Steam names and avatars at login |

Run just the site locally, on a copy of the database:

```bash
DISCORD_TOKEN=dev DB_PATH=/path/to/copy.db SITE_UBERADMIN_STEAM_IDS=<your id> python -m web 8765
```

---

## Slash Commands

| Command | Description |
|---|---|
| `/config [league] [region] [mode] [season_start] [scold_channel] [name]` | [Admin] Point this server's division at a league (divisions are made on the site) |
| `/h2h_card team1 team2 [season]` | Render a head-to-head PNG card for two teams |
| `/h2h_player player1 player2 [season]` | Render a head-to-head PNG card comparing two players |
| `/hall_of_fame [public]` | Show every season that has been won, and who won it |
| `/hi_vs_low players [public]` | Split 10 players into hi-MMR vs lo-MMR teams and set random requirements |
| `/leaderboard stat [week] [pos] [all] [season] [min_games] [debug] [public]` | Show the leaderboard sorted by a stat |
| `/lookup query [force_refresh] [public]` | Diagnostic lookup — full rating breakdown (in the admin/bot channels) |
| `/matches [week] [player] [season] [public]` | Show matches with Dotabuff links |
| `/nuke` | [Admin] Wipe all data for this server and re-fetch |
| `/player name [week] [season] [public]` | Show detailed stats for a specific player |
| `/player_card name [season]` | Render a stream-friendly PNG card for a player |
| `/players [show_all] [debug] [public]` | Show all guild players ordered by rating |
| `/quote [public]` | Display a random chat message from league matches |
| `/refresh_leaderboard` | [Admin] Manually trigger a data fetch from OpenDota |
| `/refresh_ratings` | [Owner] Refresh the internal rating cache for all players in this guild |
| `/set_player name [nickname] [windrun] [badge] [rank] [rating] [add_account] [alt_account_for] [discord_handle] [note] [hide_avatar] [clear] [delete] [restore]` | [Owner] Override a player's nickname, windrun, badge/rank used by /lookup and /players |
| `/standings [season] [public]` | Show the season's table — record and tiebreaker, the way the site ranks them |
| `/summary [season] [public]` | Show 10 fantasy-points leaderboards: each position × latest-week and all-time |
| `/sync_roles_channels [players_url] [captains_url] [players_role] [captains_role] [category] [dry_run]` | [Owner] Sync players/captains roles + rebuild Captain Rooms voice channels from RD2L |
| `/team_card name [season]` | Render a stream-friendly PNG card for a player's team |
| `/team_stats name [season] [public]` | Show strengths/weaknesses of a player's team vs the rest of the league |
| `/tipjar` | Support the bot creator |

Stat, match and quote commands post their result in the channel. Pass `public:False` to any of them (`/leaderboard`, `/player`, `/players`, `/matches`, `/summary`, `/team_stats`, `/hi_vs_low`, `/quote`, `/lookup`) to get the reply as a whisper only you can see. Image cards (`/player_card`, `/team_card`, `/h2h_card`, `/h2h_player`) always post in the channel. Owner-only commands (`/set_player`, `/sync_roles_channels`, `/nuke`) always reply privately.

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
