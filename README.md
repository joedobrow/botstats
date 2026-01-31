# Dota 2 League Stats Discord Bot

A Discord bot that automatically fetches match data from the [OpenDota API](https://docs.opendota.com/) for a specific league, filters to US West non-Ability-Draft games, and tracks per-player stats with a fantasy points leaderboard.

---

## Features

| Feature | Details |
|---|---|
| **Weekly auto-fetch** | Runs every Monday at 06:00 UTC. Pulls new matches, filters, and stores them. |
| **SQLite caching** | Match data is stored locally — no repeated API calls. |
| **Slash commands** | `/leaderboard`, `/player`, `/roles`, `/refresh` |
| **Fantasy points** | A tunable scoring formula that rewards kills, economy, utility, and wins. |
| **Role tracking** | Players are mapped to positional roles (Pos 1–5) automatically. |

---

## Project Structure

```
.
├── bot.py            # Discord client, slash commands, weekly scheduler
├── config.py         # Loads env vars and constants
├── db.py             # SQLite schema + read/write functions
├── fetcher.py        # OpenDota API calls, filtering, persistence
├── fantasy.py        # Fantasy points formula (tunable weights)
├── formatters.py     # Builds Discord Embed objects
├── requirements.txt
└── README.md
```

---

## Setup

### 1. Prerequisites

- Python 3.11+
- A Discord bot token (create one at [discord.com/developers](https://discord.com/developers/applications))
- Your league ID (visible in OpenDota or in-game)
- *(Optional)* An OpenDota API key from [opendota.com/api-keys](https://www.opendota.com/api-keys) — free tier works without one but has lower rate limits

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Your Discord bot token |
| `LEAGUE_ID` | ✅ | The OpenDota league ID (integer) |
| `STATS_CHANNEL_ID` | ❌ | Discord channel ID for auto-posted weekly summaries. Omit to disable. |
| `OPENDOTA_API_KEY` | ❌ | OpenDota API key. Omit to use the free unauthenticated tier. |
| `DB_PATH` | ❌ | Path to the SQLite file. Defaults to `dota_stats.db` in the working directory. |

#### Running locally with a `.env` file

Create a `.env` file and use [python-dotenv](https://pypi.org/project/python-dotenv/):

```
DISCORD_TOKEN=your_token_here
LEAGUE_ID=12345
STATS_CHANNEL_ID=1234567890123456789
OPENDOTA_API_KEY=your_key_here
```

Then add these two lines at the very top of `bot.py`:

```python
from dotenv import load_dotenv
load_dotenv()
```

And install the extra package: `pip install python-dotenv`

### 4. Run

```bash
python bot.py
```

---

## Hosting on Railway (Free Tier)

1. Push this repo to GitHub.
2. Go to [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo**.
3. In the service **Settings → Environment**, add your environment variables (see table above).
4. Railway will auto-detect `requirements.txt` and build. Set the **Start command** to `python bot.py`.
5. The bot will stay running and execute the Monday morning fetch automatically.

> **Note on SQLite + Railway:** Railway free tier uses ephemeral disks by default. Your `dota_stats.db` file will persist as long as the service isn't redeployed. If persistence across deploys is critical, consider adding a Railway PostgreSQL add-on and swapping `db.py` for a PostgreSQL connection — the interface is the same, just the connection string changes.

---

## Slash Commands

| Command | Description |
|---|---|
| `/leaderboard <stat>` | Shows top 15 players sorted by the chosen stat (Fantasy Pts, GPM, KDA, etc.). Optional `week` param to look back. |
| `/player <name>` | Full stat breakdown for a single player. Supports partial name matching. |
| `/roles` | Best player at each positional role (1–5), ranked by fantasy points. |
| `/refresh` | *(Admin only)* Manually trigger a data fetch right now. |

---

## How Fantasy Points Work

See `fantasy.py` for the full formula. In short, points are calculated **per game played** and then multiplied by games played, so everyone is on fair footing regardless of how many matches they appeared in.

### Default Weights

| Stat | Points |
|---|---|
| Kill | +3.0 |
| Death | −2.5 |
| Assist | +1.5 |
| Last Hit | +0.02 |
| Deny | +0.5 |
| GPM (above 300 baseline) | +0.5 per 100 |
| XPM (above 400 baseline) | +0.3 per 100 |
| Hero Damage | +0.01 per 100 |
| Hero Healing | +0.02 per 100 |
| Win | +5.0 |

To change these, just edit the `WEIGHTS` dict and the baseline constants at the top of `fantasy.py`.

---

## Filtering Logic

When the bot fetches matches for your league, it applies two filters before storing:

1. **Server region** — Only matches played on US West clusters are kept. The known cluster IDs are in `config.py` under `US_WEST_CLUSTERS`. If your league ever uses a different cluster, add it there.
2. **Game mode** — Ability Draft (mode 19) is excluded. Add more mode IDs to `EXCLUDED_GAME_MODES` in `config.py` if needed.

---

## Role Assignment

Dota 2 doesn't have an explicit "role" field — roles are inferred from the positional slot each player occupies in the draft. The bot maps these to:

| Position | Label |
|---|---|
| 1 | Safe Lane |
| 2 | Mid Lane |
| 3 | Off Lane |
| 4 | Roaming |
| 5 | Hard Support |

This is a convention, not gospel — players can and do play off-position. The labels are just for organisation.
