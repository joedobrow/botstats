# ADCL bot and site

The Discord bot and website ([adcl.gg](https://adcl.gg)) for the Ability Draft
Community League. One Python app on one SQLite database: the bot fetches league
games from OpenDota (and Ability Draft picks from Windrun), works out stats,
fantasy points and player ratings, and answers slash commands; the same process
serves the site, where players log in with Steam, sign up for seasons and draft.

How the code is meant to be worked on (where each number comes from, page and
template patterns, the look) is in [CLAUDE.md](CLAUDE.md). Read it before
changing anything.

## Layout

```
bot.py              Discord client, slash commands, weekly fetch
db.py               Schema, migrations, and every stat the bot and site share
fetcher.py          OpenDota fetches; Windrun drafts (windrun.py)
fantasy.py          The fantasy points formula
formatters.py       Discord embeds
rating_compute.py   Player ratings (rating_group.py pools a player's accounts)
season_setup.py     Rolling a division over to a new season
config.py           Environment variables and constants
web/                The website: routes.py, store.py, draft.py, templates/, static/
botstats/           The web server the site runs in, stream cards and the APIs
scripts/            One-off maintenance and analysis scripts
tests/              test_*.py, run as plain scripts
```

`rating_model.py` holds the rating weights and is deliberately not in the repo.
Without it the bot runs with internal ratings switched off.

## Running it

Python 3.12. Install with `pip install -r requirements.txt`, then:

```bash
python bot.py                      # the bot and the site, on port 8080
DISCORD_TOKEN=dev DB_PATH=copy.db python -m web 8765   # just the site
```

Tests (each prints `all checks passed`):

```bash
for t in tests/test_*.py; do python3 $t; done
```

## Configuration

| Variable | |
|---|---|
| `DISCORD_TOKEN` | Required |
| `ADMIN_USER_ID` | The bot owner's Discord id: owner-only commands, and where site feedback is DMed |
| `OPENDOTA_API_KEY` | Higher OpenDota rate limits |
| `STEAM_API_KEY` | Steam names and avatars at login |
| `SITE_BASE_URL` | The site's public URL, for the Steam login return address |
| `SITE_UBERADMIN_STEAM_IDS` | Comma-separated ids of uberadmins (can add and remove site admins) |
| `RATINGS_API_KEY` | Key for `/api/ratings`, `/api/costs` and `/api/season_stats` |
| `PLAYER_API_KEYS` | `label:key` pairs for `/api/player` |
| `RD2L_PLAYERS_URL`, `RD2L_CAPTAINS_URL` | Defaults for `/sync_roles_channels` |
| `LOOKUP_CHANNEL_IDS` | Channels where anyone may use `/lookup` |
| `LEAGUE_TZ` | Default `America/New_York` |
| `TIP_URL` | The site's Tip jar and `/tipjar` |
| `DB_PATH` | Default `/data/dota_stats.db` when `/data` exists |

## Deploying

It runs on fly.io as the app `dota-bot`; the database lives on the `/data`
volume. Set secrets with `fly secrets set`, then:

```bash
fly deploy -a dota-bot
fly logs -a dota-bot --no-tail    # look for "Logged in as"
```

To copy the production database, take a SQLite backup on the machine rather
than copying the file, which misses recent writes still in the WAL:

```bash
fly ssh console -a dota-bot -C "python -c \"import sqlite3;s=sqlite3.connect('/data/dota_stats.db');d=sqlite3.connect('/tmp/snap.db');s.backup(d);d.close()\""
fly ssh sftp get /tmp/snap.db copy.db -a dota-bot
```

## How the league is modeled

- A **division** (NA, EU) is one competition: its own region, game mode,
  rosters and games. Divisions are created on the site; each may be tied to a
  Discord server.
- A **season** is a Dota league id (RD2L Season 39 is `20056`). Divisions can
  share one: NA and EU both play ADCL Season 1. A season's games are the games
  played in its league; weeks are date ranges inside it.
- A division's **current season** is the one marked Active on the site, or else
  its newest season that has started. A season set up ahead of time takes over
  on its start date.
- New seasons are set up under **Admin → Season setup** on the site; weeks,
  playoffs, champions and awards on the season's own page.
- The weekly fetch runs Thursdays at 09:00 UTC, after Wednesday league night.
  `/refresh_leaderboard` runs it on demand.

## Slash commands

Stats: `/leaderboard`, `/player`, `/players`, `/matches`, `/summary`,
`/standings`, `/team_stats`, `/hall_of_fame`, `/lookup`, `/quote`, `/hi_vs_low`.
Pass `public:False` to get the reply privately.

Image cards: `/player_card`, `/team_card`, `/h2h_card`, `/h2h_player`.

Admin: `/config` (point the server's division at a league),
`/refresh_leaderboard`, `/nuke`. Owner: `/set_player` (nicknames, rating
overrides, linking a player's accounts), `/refresh_ratings`,
`/sync_roles_channels`.

`/tipjar` links the tip jar.
