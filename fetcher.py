"""
Fetcher — talks to the OpenDota API.

Flow, per season (a season is one Dota league; several divisions may play
in it):
  1. GET /leagues/{league_id}/matchIds  →  every game played in the league
  2. For each game not stored yet, GET /matches/{match_id}  →  full data
  3. Work out which of the season's divisions it belongs to (pick_division)
  4. Persist it under that division via db.py
"""

import aiohttp
import logging
from datetime import datetime, timezone

from config import OPENDOTA_API_KEY, REGION_CLUSTERS, GAME_MODE_FILTERS
from db import (
    upsert_match, upsert_players, upsert_chat_messages, match_is_complete, get_division,
    get_season_divisions, get_player_costs, get_captain_account_ids,
    save_match_draft, games_missing_drafts, assign_builds_to_heroes,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.opendota.com/api"

# OpenDota item_uses keys for the defensive items we score in fantasy points.
# Internal item names without the "item_" prefix. Linken's Sphere is "sphere".
DEFENSIVE_ITEM_KEYS = (
    "pipe",            # Pipe of Insight
    "crimson_guard",
    "lotus_orb",
    "glimmer_cape",
    "force_staff",
    "pavise",
    "solar_crest",
    "heavens_halberd",
    "sphere",          # Linken's Sphere
)


def _sum_defensive_item_uses(player: dict) -> int:
    """Sum item_uses across the defensive items we score."""
    uses = player.get("item_uses") or {}
    return sum(uses.get(k, 0) or 0 for k in DEFENSIVE_ITEM_KEYS)


def _headers(use_auth: bool = True) -> dict:
    """Return auth header if we have an API key and use_auth is True."""
    if use_auth and OPENDOTA_API_KEY:
        return {"Authorization": f"Bearer {OPENDOTA_API_KEY}"}
    return {}


async def _get(session: aiohttp.ClientSession, path: str, use_auth: bool = True) -> dict | list | None:
    """Make a GET request, return parsed JSON or None on error."""
    url = f"{BASE_URL}{path}"
    async with session.get(url, headers=_headers(use_auth)) as resp:
        if resp.status != 200:
            logger.warning("OpenDota returned %d for %s", resp.status, url)
            return None
        return await resp.json()


def _farm_priority(player: dict) -> float:
    """
    Return a farm-priority score for sorting cores vs supports.

    Uses CS at 10 minutes (lh_t[10]) when available — this is the cleanest
    signal for distinguishing cores from supports in the laning phase.
    Falls back to GPM when parsed replay data isn't available.
    """
    lh_t = player.get("lh_t")
    if lh_t and len(lh_t) > 10:
        return lh_t[10]
    return player.get("gold_per_min", 0)


def _assign_team_roles(team_players: list[dict]) -> dict[int, int]:
    """
    Assign positions 1-5 to a team of 5 players using lane_role + CS@10 heuristic.

    OpenDota's lane_role only gives us lane info (1=safe, 2=mid, 3=off, 4=jungle).
    It does NOT distinguish pos 4 vs 5. We use CS at 10 minutes (lh_t[10]) within
    shared lanes to split cores from supports, falling back to GPM when parsed
    replay data isn't available:
        - lane_role 2 (mid)       → Position 2
        - lane_role 3 (off lane)  → Position 3 (highest CS@10), Position 4 (lower)
        - lane_role 1 (safe lane) → Position 1 (highest CS@10), Position 5 (lower)
        - lane_role 4 (jungle)    → fills remaining slots by CS@10
        - Anyone left over        → fills remaining slots by CS@10

    Returns a dict mapping player_slot → position (1-5).
    """
    if len(team_players) != 5:
        # Can't assign roles if we don't have exactly 5 players
        return {p.get("player_slot", 0): None for p in team_players}

    assigned: dict[int, int] = {}     # player_slot → position
    remaining_players = list(team_players)
    taken_positions: set[int] = set()

    # --- Mid lane (lane_role=2) → Position 2 ---
    mid_players = [p for p in remaining_players if p.get("lane_role") == 2]
    if mid_players:
        mid = max(mid_players, key=_farm_priority)
        assigned[mid.get("player_slot", 0)] = 2
        taken_positions.add(2)
        remaining_players.remove(mid)

    # --- Off lane (lane_role=3) → Position 3 (core), Position 4 (support) ---
    off_players = sorted(
        [p for p in remaining_players if p.get("lane_role") == 3],
        key=_farm_priority, reverse=True,
    )
    for i, p in enumerate(off_players):
        if i == 0 and 3 not in taken_positions:
            assigned[p.get("player_slot", 0)] = 3
            taken_positions.add(3)
        elif 4 not in taken_positions:
            assigned[p.get("player_slot", 0)] = 4
            taken_positions.add(4)
        remaining_players.remove(p)

    # --- Safe lane (lane_role=1) → Position 1 (carry), Position 5 (support) ---
    safe_players = sorted(
        [p for p in remaining_players if p.get("lane_role") == 1],
        key=_farm_priority, reverse=True,
    )
    for i, p in enumerate(safe_players):
        if i == 0 and 1 not in taken_positions:
            assigned[p.get("player_slot", 0)] = 1
            taken_positions.add(1)
        elif 5 not in taken_positions:
            assigned[p.get("player_slot", 0)] = 5
            taken_positions.add(5)
        remaining_players.remove(p)

    # --- Jungle / roaming (lane_role=4) and anyone left → fill remaining slots ---
    remaining_players.sort(key=_farm_priority, reverse=True)
    open_positions = sorted(set(range(1, 6)) - taken_positions)
    for p, pos in zip(remaining_players, open_positions):
        assigned[p.get("player_slot", 0)] = pos

    return assigned


def _division_accepts(division: dict, game_mode: int, cluster: int) -> bool:
    """Whether a game's mode and server fit a division's settings."""
    mode_filter = GAME_MODE_FILTERS.get(division["game_mode"], {"include": None, "exclude": set()})
    include, exclude = mode_filter.get("include"), mode_filter.get("exclude") or set()
    if include is not None and game_mode not in include:
        return False
    if game_mode in exclude:
        return False
    clusters = REGION_CLUSTERS.get(division["region"], set())
    return clusters is None or cluster == 0 or cluster in clusters


def pick_division(season_id: int, divisions: list[dict], full: dict) -> dict | None:
    """Which of a season's divisions a game belongs to.

    Only divisions whose game mode and server region fit the game qualify.
    One left: that one. Several (divisions sharing a league and servers):
    the one whose drafted rosters (players + captains) include the most of
    the game's players. None when nothing fits or that's a tie."""
    candidates = [d for d in divisions
                  if _division_accepts(d, full.get("game_mode", 0), full.get("cluster", 0))]
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    players = {p.get("account_id") for p in full.get("players", []) if p.get("account_id")}
    votes = []
    for d in candidates:
        roster = set(get_player_costs(season_id, d["id"]))
        roster |= {aid for aid in get_captain_account_ids(season_id, d["id"]).values() if aid}
        votes.append((len(roster & players), d))
    votes.sort(key=lambda v: -v[0])
    if votes[0][0] == 0 or (len(votes) > 1 and votes[0][0] == votes[1][0]):
        return None
    return votes[0][1]


def _store_full_match(full: dict, division_id: int | None,
                      league_id: int | None = None) -> int:
    """Persist one OpenDota /matches/{id} response under a division: the
    match row, player rows (updated in place if already there) and typed
    chat. Returns the number of player rows.

    league_id overrides the one OpenDota reports, for a league game that was
    played without a ticket — it has no league of its own, and a season's
    matches are scoped by (division, league), so without this it would be
    stored and then never seen (see fetch_and_store_match)."""
    # --- Persist match row ---
    radiant_win = 1 if full.get("radiant_win") else 0
    match_row = {
        "match_id":     full["match_id"],
        "division_id":  division_id,
        "league_id":    league_id if league_id is not None else full.get("leagueid", 0),
        "start_time":   full.get("start_time", 0),
        "duration":     full.get("duration", 0),
        "game_mode":    full.get("game_mode", 0),
        "cluster":      full.get("cluster", 0),
        "radiant_win":  radiant_win,
        "radiant_score": full.get("radiant_score", 0),
        "dire_score":   full.get("dire_score", 0),
        "fetched_at":   datetime.now(timezone.utc).isoformat(),
        "replay_salt":  full.get("replay_salt", 0) or 0,
    }
    upsert_match(match_row)

    # --- Persist player rows ---
    duration = full.get("duration", 0)
    all_players = full.get("players", [])

    # Assign roles per team (need full team context for GPM heuristic)
    radiant_team = [p for p in all_players if p.get("player_slot", 0) < 128]
    dire_team = [p for p in all_players if p.get("player_slot", 0) >= 128]
    role_map = {}
    role_map.update(_assign_team_roles(radiant_team))
    role_map.update(_assign_team_roles(dire_team))

    # --- Compute team's first tormentor kill time from objectives ---
    # team 2 = radiant, team 3 = dire; -1 = tormentor never taken by that team
    team_first_tormentor: dict[int, int] = {}
    for obj in full.get("objectives", []):
        if obj.get("type") == "CHAT_MESSAGE_MINIBOSS_KILL":
            team = obj.get("team")
            t = obj.get("time", 0)
            if team and (team not in team_first_tormentor or t < team_first_tormentor[team]):
                team_first_tormentor[team] = t

    players = []
    for p in all_players:
        slot = p.get("player_slot", 0)
        is_radiant = slot < 128
        won = 1 if (is_radiant and radiant_win) or (not is_radiant and not radiant_win) else 0
        team_num = 2 if is_radiant else 3
        killed_dict    = p.get("killed") or {}
        ability_uses   = p.get("ability_uses") or {}

        players.append({
            "match_id":       full["match_id"],
            "account_id":     p.get("account_id", 0),
            "name":           p.get("personaname") or p.get("name") or f"Player_{p.get('account_id', '?')}",
            "hero_id":        p.get("hero_id", 0),
            "player_slot":    slot,
            "team_side":      "radiant" if is_radiant else "dire",
            "role_position":  role_map.get(slot),
            "kills":          p.get("kills", 0),
            "deaths":         p.get("deaths", 0),
            "assists":        p.get("assists", 0),
            "gpm":            p.get("gold_per_min", 0),
            "xpm":            p.get("xp_per_min", 0),
            "last_hits":      p.get("last_hits", 0),
            "denies":         p.get("denies", 0),
            "hero_damage":    p.get("hero_damage", 0),
            "hero_healing":   p.get("hero_healing", 0),
            "gold_spent":     p.get("gold_spent", 0),
            "duration":       duration,
            "won":            won,
            "obs_placed":              p.get("obs_placed", 0) or 0,
            "sen_placed":              p.get("sen_placed", 0) or 0,
            "observer_kills":          p.get("observer_kills", 0) or 0,
            "sentry_kills":            p.get("sentry_kills", 0) or 0,
            "tower_kills":             p.get("towers_killed", 0) or 0,
            "roshans_killed":          p.get("roshans_killed", 0) or 0,
            "firstblood_claimed":      1 if p.get("firstblood_claimed") else 0,
            "teamfight_participation": p.get("teamfight_participation", 0) or 0,
            "stuns":                   p.get("stuns", 0) or 0,
            "camps_stacked":           p.get("camps_stacked", 0) or 0,
            "rune_pickups":            p.get("rune_pickups", 0) or 0,
            "tormentor_kills":         killed_dict.get("npc_dota_miniboss", 0),
            "watcher_captures":        ability_uses.get("ability_lamp_use", 0),
            "team_first_tormentor_time": team_first_tormentor.get(team_num, -1),
            "defensive_item_uses":     _sum_defensive_item_uses(p),
            "building_damage":         p.get("tower_damage", 0) or 0,
        })

    upsert_players(players)

    # --- Persist chat messages (typed only, not chatwheel) ---
    chat_data = full.get("chat", [])
    # Build a map of player_slot -> player_name for this match
    slot_to_name = {p.get("player_slot", 0): p.get("personaname") or p.get("name") or "Unknown" for p in all_players}

    chat_messages = []
    for msg in chat_data:
        if msg.get("type") == "chat":  # Only typed messages, not chatwheel
            slot = msg.get("player_slot", msg.get("slot", 0))
            chat_messages.append({
                "match_id": full["match_id"],
                "player_slot": slot,
                "player_name": slot_to_name.get(slot, "Unknown"),
                "time": msg.get("time", 0),
                "message": msg.get("key", ""),
            })

    if chat_messages:
        upsert_chat_messages(chat_messages)
        logger.info("Stored %d chat messages for match %d", len(chat_messages), full["match_id"])

    return len(players)


async def fetch_and_store_match(match_id: int, division_id: int,
                                league_id: int | None = None) -> dict:
    """Fetch one match by id and store it under a division, whatever league
    OpenDota thinks it belongs to. For the game somebody forgot to ticket.

    Returns {match_id, players, game_mode, start_time, already}. Raises
    ValueError with a message meant for whoever asked, since this runs from
    an admin pressing a button rather than from a scheduled job.
    """
    from db import _conn

    with _conn() as conn:
        already = conn.execute("SELECT 1 FROM matches WHERE match_id = ?",
                               (match_id,)).fetchone() is not None
    async with aiohttp.ClientSession() as session:
        full = await _get(session, f"/matches/{match_id}")
    if not full or not full.get("match_id"):
        raise ValueError(f"OpenDota has no match {match_id}. "
                         f"A game can take a few minutes to appear after it ends.")
    if not full.get("players"):
        raise ValueError(f"Match {match_id} came back without its players — "
                         f"OpenDota may still be parsing it. Try again shortly.")
    rows = _store_full_match(full, division_id, league_id=league_id)
    logger.info("Manually added match %d to division %s (%d player rows)",
                match_id, division_id, rows)
    return {"match_id": match_id, "players": rows, "game_mode": full.get("game_mode"),
            "start_time": full.get("start_time"), "already": already}


async def fetch_and_store_season(season_id: int) -> int:
    """Fetch every new game of a season (its Dota league) and store each
    under the division it belongs to. Returns the number of games stored."""
    divisions = get_season_divisions(season_id)
    if not divisions:
        logger.warning("Season %d has no divisions; nothing to fetch", season_id)
        return 0
    league_id = season_id  # a season's id is its Dota league id
    logger.info("Fetching league %d for divisions %s", league_id, [d["name"] for d in divisions])

    stored = 0

    async with aiohttp.ClientSession() as session:
        # --- Step 1: get league match IDs (works for amateur leagues) ---
        match_ids = await _get(session, f"/leagues/{league_id}/matchIds", use_auth=False)
        if not match_ids:
            logger.warning("No match IDs returned for league %d", league_id)
            return 0

        logger.info("Fetched %d match IDs for league %d", len(match_ids), league_id)

        # --- Step 2: fetch full details for each new match ID ---
        for match_id_str in match_ids:
            mid = int(match_id_str)

            # A game is stored once, in one division. One stored without its
            # player rows is fetched again (player rows update in place).
            if match_is_complete(mid):
                continue

            # Don't use API key - causes 400s for amateur league matches
            # Add small delay to avoid hitting rate limits (60 req/min = 1 per second)
            import asyncio
            await asyncio.sleep(1.1)

            full = await _get(session, f"/matches/{mid}", use_auth=False)
            if not full:
                logger.warning("Failed to fetch full data for match %d", mid)
                continue

            game_mode = full.get("game_mode", 0)
            cluster = full.get("cluster", 0)
            division = pick_division(season_id, divisions, full)
            if division is None:
                logger.info("FILTERED OUT: Match %d — game_mode=%d cluster=%d fits no single division",
                            mid, game_mode, cluster)
                continue

            # Skip remakes / bugged matches — no kills on either team means the game didn't really happen
            radiant_score = full.get("radiant_score", 0)
            dire_score = full.get("dire_score", 0)
            if radiant_score == 0 and dire_score == 0:
                logger.info("FILTERED OUT: Match %d — score 0-0 (likely remake or bugged game)", mid)
                continue

            # Log matches that PASS the filter
            logger.info("STORING: Match %d for %s — cluster=%d, game_mode=%d", mid, division["name"], cluster, game_mode)

            full.setdefault("leagueid", league_id)
            n_players = _store_full_match(full, division["id"])
            if game_mode == 18:
                await fetch_draft(mid)
            stored += 1
            logger.info("Stored match %d (%d players)", mid, n_players)

    return stored


async def fetch_and_store_matches_for_division(division_id: int) -> int:
    """Fetch the division's current season. Every division playing in that
    season gets its new games, not just this one."""
    division = get_division(division_id)
    if not division or not division.get("current_season_id"):
        logger.warning("Division %s has no current season", division_id)
        return 0
    return await fetch_and_store_season(division["current_season_id"])


async def refetch_incomplete_games(season_id: int) -> int:
    """Fetch player data for games of a season that are stored without any
    (an interrupted fetch, say), keeping each game in its division. Unlike
    fetch_and_store_season it never adds games. Returns how many it filled."""
    from db import _conn, match_is_complete
    with _conn() as conn:
        games = [tuple(r) for r in conn.execute(
            "SELECT match_id, division_id FROM matches WHERE league_id = ?", (season_id,))]
    missing = [(mid, did) for mid, did in games if not match_is_complete(mid)]
    logger.info("League %d: %d of %d stored games have no player rows", season_id, len(missing), len(games))
    filled = 0
    async with aiohttp.ClientSession() as session:
        import asyncio
        for mid, division_id in missing:
            await asyncio.sleep(1.1)  # OpenDota's free tier: 60 requests a minute
            full = await _get(session, f"/matches/{mid}", use_auth=False)
            if not full:
                logger.warning("Failed to fetch full data for match %d", mid)
                continue
            n = _store_full_match(full, division_id)
            filled += 1 if n else 0
            logger.info("Filled match %d (%d players)", mid, n)
    return filled


async def fetch_draft(match_id: int) -> bool:
    """Store an Ability Draft game's picks from windrun.io (the model hero
    and abilities each player drafted, in pick order). windrun's client
    keeps to its rate limit. Returns whether a draft was stored."""
    from windrun import fetch_match
    data = await fetch_match(match_id)
    if not data or not data.get("picks"):
        logger.info("No windrun draft for match %d", match_id)
        return False
    owner = {aid: p.get("steamId") for side in ("radiant", "dire")
             for p in data.get(side, []) for aid in p.get("abilities", [])}
    builds: dict[int, list[tuple[int, int]]] = {}
    for pk in data["picks"]:
        if owner.get(pk["abilityId"]):
            builds.setdefault(owner[pk["abilityId"]], []).append((pk["pickOrder"], pk["abilityId"]))
    if not builds:
        return False
    # windrun can pin a build on the wrong player (seat swaps); file each
    # build under whoever actually played its hero.
    builds = assign_builds_to_heroes(match_id, builds)
    save_match_draft(match_id, [(po, account, ability) for account, picks in builds.items() for po, ability in picks])
    return True


async def backfill_drafts(season_id: int) -> int:
    """Fetch drafts for a season's Ability Draft games that have none stored."""
    filled = 0
    for match_id in games_missing_drafts(season_id):
        filled += await fetch_draft(match_id)
    logger.info("League %d: stored %d drafts", season_id, filled)
    return filled
