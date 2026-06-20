"""
opendota_lookup.py — resolve ability/model names for an AD match using
OpenDota's per-match parsed data.

This replaces the prior windrun-based static lookup approach. That approach
was broken because `m_n_ability_id` (the opaque opaque ID we get from the
demo) is NOT stable across matches — the same numeric ID maps to different
abilities in different matches. So we resolve per-match.

PIPELINE:
  1. Trigger an OpenDota parse for the match (via /api/request/{match_id}).
     This is free, no auth. Required for ability_upgrades_arr to populate.
  2. Fetch /matches/{match_id} and extract each player's ability set in
     skill-up order (filtering talents).
  3. Map our parser's pid → OpenDota's player_slot:
       pid 0-4  → player_slot 0-4   (radiant)
       pid 5-9  → player_slot 128-132 (dire)
  4. For each parser pick (already in true chronological order with correct
     pid), assign the next-in-skill-up-order ability name from that player's
     OpenDota list.

CAVEATS:
  - Skill-up order is a *proxy* for draft pick order within a player. It's
    usually correct (players upgrade their first-picked basic first) but
    occasionally a player will defer leveling a draft pick.
  - Hero swaps: both our parser and OpenDota appear to label players by their
    ORIGINAL model pick (not the post-swap final). This makes them consistent
    — they always agree on which OpenDota player_slot corresponds to which
    of our pids.
"""

import asyncio
import logging
import math
import os
import aiohttp

logger = logging.getLogger(__name__)

OPENDOTA_BASE = "https://api.opendota.com/api"

# Used by /lookup's profile + game-count fetches. Free tier (no key) is
# 60 req/min — we burn 6 per /lookup, so unauthenticated calls fail under
# even light usage. With key: 1200/min.
OPENDOTA_API_KEY = os.environ.get("OPENDOTA_API_KEY")


def _with_key(url: str) -> str:
    if not OPENDOTA_API_KEY:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}api_key={OPENDOTA_API_KEY}"
WINDRUN_ABILITIES_URL = "https://api.windrun.io/api/v2/static/abilities"
# Windrun's static abilities endpoint is just a snapshot of Valve's ability
# definitions (valveId → snake_case shortName) — it doesn't change per-match
# and isn't sensitive to windrun's parsing pipeline. We use it as a stable
# valveId → name dictionary. (If windrun ever goes down we could replace this
# with a hard-coded mapping shipped in the repo.)

# In-memory caches.
_valve_id_to_name: dict[int, str] | None = None
_hero_id_to_name: dict[int, str] | None = None


async def _load_valve_id_to_name(session: aiohttp.ClientSession) -> dict[int, str]:
    global _valve_id_to_name
    if _valve_id_to_name is not None:
        return _valve_id_to_name
    try:
        async with session.get(WINDRUN_ABILITIES_URL) as resp:
            if resp.status != 200:
                logger.warning("windrun abilities API returned %d", resp.status)
                return {}
            data = (await resp.json()).get("data", [])
    except Exception:
        logger.exception("Failed to fetch windrun static abilities table")
        return {}
    _valve_id_to_name = {
        a["valveId"]: a["shortName"]
        for a in data if a.get("valveId") and a.get("shortName")
    }
    logger.info("Loaded %d valveId→shortName entries", len(_valve_id_to_name))
    return _valve_id_to_name


async def _load_hero_id_to_name(session: aiohttp.ClientSession) -> dict[int, str]:
    global _hero_id_to_name
    if _hero_id_to_name is not None:
        return _hero_id_to_name
    try:
        async with session.get(f"{OPENDOTA_BASE}/constants/heroes") as resp:
            if resp.status != 200:
                logger.warning("OpenDota heroes returned %d", resp.status)
                return {}
            data = await resp.json()
    except Exception:
        logger.exception("Failed to fetch OpenDota heroes constants")
        return {}
    out = {}
    for hid_str, h in data.items():
        full = h.get("name", "")
        if full.startswith("npc_dota_hero_"):
            out[int(hid_str)] = full[len("npc_dota_hero_"):]
    _hero_id_to_name = out
    logger.info("Loaded %d hero_id→name entries", len(out))
    return _hero_id_to_name


async def _request_opendota_parse(session: aiohttp.ClientSession, match_id: int) -> None:
    """POST to /api/request/{match_id} to trigger OpenDota's parser. Silently
    no-ops if the call fails; that's fine if the match is already parsed."""
    try:
        async with session.post(f"{OPENDOTA_BASE}/request/{match_id}") as resp:
            if resp.status != 200:
                logger.info("OpenDota /request/%d returned %d", match_id, resp.status)
    except Exception:
        logger.exception("OpenDota /request/%d failed", match_id)


async def _fetch_match(session: aiohttp.ClientSession, match_id: int) -> dict | None:
    try:
        async with session.get(f"{OPENDOTA_BASE}/matches/{match_id}") as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except Exception:
        logger.exception("OpenDota /matches/%d failed", match_id)
        return None


def _pid_to_player_slot(pid: int) -> int | None:
    """Our parser's pid (m_vecPlayerTeamData array index) → OpenDota player_slot."""
    if 0 <= pid <= 4:
        return pid
    if 5 <= pid <= 9:
        return 128 + (pid - 5)
    return None


async def resolve_picks_with_opendota(match_id: int, parser_picks: list[dict]) -> list[dict]:
    """
    Fill in ability_name for every pick in parser_picks using OpenDota's
    per-match data. Returns a new list — does not mutate inputs.

    For model picks: looks up hero_id → snake_case name.
    For ability picks: assigns from the player's ability_upgrades_arr in
    first-skill-up order (a proxy for chronological pick order within player).

    Picks that can't be resolved get ability_name="unknown".
    """
    if not parser_picks:
        return parser_picks

    async with aiohttp.ClientSession() as session:
        # Make sure OpenDota has parsed this match. If it has, this is a
        # cheap no-op; if not, the parse begins async and we poll briefly.
        await _request_opendota_parse(session, match_id)
        data = await _fetch_match(session, match_id)
        # If ability_upgrades_arr is missing (parse not yet done), retry a
        # few times. OpenDota usually parses within seconds.
        if data and not any(
            p.get("ability_upgrades_arr") for p in data.get("players", [])
        ):
            for delay in (3, 5, 7):
                await asyncio.sleep(delay)
                data = await _fetch_match(session, match_id)
                if data and any(
                    p.get("ability_upgrades_arr") for p in data.get("players", [])
                ):
                    break

        valve_id_to_name = await _load_valve_id_to_name(session)
        hero_id_to_name = await _load_hero_id_to_name(session)

    # Build pid → ordered ability names list AND pid → hero_id from OpenDota.
    # Primary source is `ability_upgrades_arr` (chronological skill-up order).
    # If that list runs short (e.g. player didn't level a drafted ability),
    # append any extras from `ability_uses` to backfill — those won't be in
    # chronological order but ensure we don't show "unknown".
    pid_to_abilities: dict[int, list[str]] = {}
    pid_to_hero_id: dict[int, int] = {}
    if data:
        # Build valveId set we already know about.
        known_valve_ids = set(valve_id_to_name.keys())
        # Reverse map: shortName → True (so we can check if an ability_uses
        # name is a real ability).
        known_names = set(valve_id_to_name.values())
        for op in data.get("players", []):
            slot = op.get("player_slot")
            if slot is None:
                continue
            pid = None
            if 0 <= slot <= 4:
                pid = slot
            elif 128 <= slot <= 132:
                pid = 5 + (slot - 128)
            if pid is None:
                continue
            hero_id = op.get("hero_id") or 0
            if hero_id:
                pid_to_hero_id[pid] = hero_id

            ordered: list[str] = []
            seen: set[str] = set()

            # Primary: ability_upgrades_arr (chronological).
            for vid in (op.get("ability_upgrades_arr") or []):
                name = valve_id_to_name.get(vid)
                if not name:
                    continue
                if name.startswith("special_bonus_") or name.startswith("ad_special_bonus_"):
                    continue
                if name in seen:
                    continue
                seen.add(name)
                ordered.append(name)

            # Backfill: ability_uses keys (set of abilities the player USED in
            # the game, regardless of leveling). Skip non-ability keys like
            # ability_lamp_use, ability_capture, twin_gate_portal_warp etc.
            uses = op.get("ability_uses") or {}
            for name in uses.keys():
                if name in seen:
                    continue
                if name not in known_names:
                    continue  # filters generic_lamp_use etc.
                if name.startswith("special_bonus_") or name.startswith("ad_special_bonus_"):
                    continue
                seen.add(name)
                ordered.append(name)

            pid_to_abilities[pid] = ordered

    # Assign names to picks.
    pid_consumed: dict[int, int] = {}
    out = []
    for p in parser_picks:
        np = dict(p)
        if p.get("type") == "model":
            pid = int(p.get("player_id", -1))
            # Prefer OpenDota's hero_id for this player_slot (works in the
            # retry path where draft_picks rows don't carry hero_id).
            hid = pid_to_hero_id.get(pid) or int(p.get("hero_id") or 0)
            if hid:
                np["hero_id"] = hid
                np["ability_name"] = hero_id_to_name.get(hid, "")
            if not np.get("ability_name"):
                np["ability_name"] = "unknown"
        elif p.get("type") == "ability":
            pid = int(p.get("player_id", -1))
            order = pid_to_abilities.get(pid, [])
            idx = pid_consumed.get(pid, 0)
            if idx < len(order):
                np["ability_name"] = order[idx]
                pid_consumed[pid] = idx + 1
            else:
                np["ability_name"] = "unknown"
        else:
            np["ability_name"] = np.get("ability_name") or "unknown"
        out.append(np)

    n_unknown = sum(1 for p in out if p.get("ability_name") == "unknown")
    if n_unknown:
        logger.warning("Match %d: %d picks remain unknown after OpenDota resolve",
                       match_id, n_unknown)
    else:
        logger.info("Match %d: all %d picks resolved via OpenDota",
                    match_id, len(out))
    return out


# ---------------------------------------------------------------------------
# Player profile lookup (used by /lookup for ranked MMR estimate)
# ---------------------------------------------------------------------------

MEDAL_NAMES = {
    1: "Herald",
    2: "Guardian",
    3: "Crusader",
    4: "Archon",
    5: "Legend",
    6: "Ancient",
    7: "Divine",
    8: "Immortal",
}

# Approximate MMR boundaries per medal tier (lo inclusive, hi exclusive).
# Each non-Immortal tier has 5 stars; each star covers 1/5 of the band.
# Immortal has no upper bound — we use a sensible floor + leaderboard-rank
# context when shown.
MMR_BRACKETS = {
    1: (0,    770),    # Herald
    2: (770,  1540),   # Guardian
    3: (1540, 2310),   # Crusader
    4: (2310, 3080),   # Archon
    5: (3080, 3850),   # Legend
    6: (3850, 4620),   # Ancient
    7: (4620, 5420),   # Divine
    8: (5420, None),   # Immortal
}


async def fetch_player_profile(account_id: int) -> dict | None:
    """Fetch /players/{account_id} from OpenDota. Returns the JSON dict or
    None on error. Uses OPENDOTA_API_KEY when available (1200 req/min)."""
    url = _with_key(f"{OPENDOTA_BASE}/players/{account_id}")
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("OpenDota returned %d for %s", resp.status, url)
                    return None
                return await resp.json()
    except Exception:
        logger.exception("OpenDota player fetch failed for %d", account_id)
        return None


def decode_rank_tier(
    rank_tier: int | None,
    leaderboard_rank: int | None = None,
) -> str | None:
    """Turn a numeric rank_tier into a display string like 'Divine 2' or
    'Immortal (Top 1701)'. Returns None if rank_tier is missing/unparseable."""
    if not rank_tier:
        return None
    tier = rank_tier // 10
    stars = rank_tier % 10
    medal = MEDAL_NAMES.get(tier)
    if not medal:
        return None
    if tier == 8:  # Immortal — no stars, optional leaderboard rank
        return f"Immortal (Top {leaderboard_rank})" if leaderboard_rank else "Immortal"
    if stars > 0:
        return f"{medal} {stars}"
    return medal


def estimate_mmr_from_rank_tier(
    rank_tier: int | None,
    leaderboard_rank: int | None = None,
) -> int | None:
    """Midpoint MMR estimate for the given rank_tier. ±154 MMR within a
    star-band. For Immortal players with a leaderboard_rank, fits a rough
    log curve (mmr ≈ 13000 - 880·ln(rank)) — calibrated so top 1 ≈ 13k,
    top 100 ≈ 8.9k, top 1000 ≈ 6.9k, top 5000 ≈ 5.5k.
    """
    if not rank_tier:
        return None
    tier = rank_tier // 10
    stars = rank_tier % 10
    bracket = MMR_BRACKETS.get(tier)
    if not bracket:
        return None
    lo, hi = bracket
    if hi is None:
        # Immortal: log-linear fit on 8 user-supplied NA anchors (R² = 0.99):
        #   (rank, mmr) = (11, 11000), (550, 8400), (1026, 7980), (1063, 8000),
        #                 (1750, 7500), (2000, 7200), (2207, 7349), (4294, 6800)
        #   → mmr = 12745 − 702.1 · ln(rank)
        # Trained on NA leaderboard. EU has a separate (steeper) curve and
        # will be slightly underestimated, but most use is NA-anchored.
        # Clamped to Immortal floor (5420).
        if leaderboard_rank and leaderboard_rank >= 1:
            mmr = 12745 - 702.1 * math.log(leaderboard_rank)
            return max(lo, int(mmr))
        # Non-leaderboard Immortal: NA leaderboard tops out around rank 5000
        # (~6765 MMR), so a generic Immortal sits between the floor (5420)
        # and that cutoff. Midpoint ≈ 6090; round to 6000.
        return 6000
    width = hi - lo
    if stars > 0:
        # Star n covers 1/5 of the band; pick its midpoint.
        return lo + round(width * (2 * stars - 1) / 10)
    return lo + width // 2


async def fetch_player_game_counts(account_id: int) -> dict | None:
    """Fetch all-time + last-year breakdowns from OpenDota's /wl endpoints
    in parallel. Five queries: all-time total, last-year total, all-time
    ranked (lobby_type=7), all-time AD (game_mode=18), last-year AD.

    Note: OpenDota's lobby_type + date filter returns 0/0 (apparent bug),
    so last_year_ranked is ratio-estimated from the other counts.

    Returns:
        {
            'all_time_total':   int,
            'last_year_total':  int,
            'all_time_ranked':  int,
            'last_year_ranked': int,  # ratio-estimated
            'all_time_ad':      int,
            'last_year_ad':     int,
        }
    or None if any request fails.
    """
    base = f"{OPENDOTA_BASE}/players/{account_id}/wl"
    urls = [
        _with_key(f"{base}?significant=0"),                           # total all-time
        _with_key(f"{base}?significant=0&date=365"),                  # total last-year
        _with_key(f"{base}?lobby_type=7&significant=0"),              # ranked all-time
        _with_key(f"{base}?game_mode=18&significant=0"),              # AD all-time
        _with_key(f"{base}?game_mode=18&date=365&significant=0"),     # AD last-year
    ]
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async def fetch_wl(url: str) -> int | None:
                # One retry on transient failure — OpenDota's /wl endpoints
                # occasionally 500 or time out under load.
                for attempt in range(2):
                    try:
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                j = await resp.json()
                                return (j.get("win", 0) or 0) + (j.get("lose", 0) or 0)
                    except Exception:
                        pass
                    if attempt == 0:
                        await asyncio.sleep(0.5)
                return None

            counts = await asyncio.gather(*(fetch_wl(u) for u in urls))
        if any(c is None for c in counts):
            logger.warning("OpenDota wl fetch failed for %d", account_id)
            return None
        all_total, last_total, all_ranked, all_ad, last_ad = counts
        # OpenDota's lobby_type+date filter is broken (returns 0/0), so we
        # estimate last-year ranked from the *non-AD* slice:
        #   non_ad_last  = last_total - last_ad
        #   ranked_share = all_ranked / (all_total - all_ad)
        #   last_ranked  = round(non_ad_last * ranked_share)
        # The earlier shortcut (last_total * all_ranked / all_total) over-
        # estimated for AD specialists whose recent play has shifted entirely
        # to AD — it treated their few non-AD last-year games as having the
        # same ranked share as their all-time mix.
        non_ad_last = max(0, last_total - last_ad)
        non_ad_all  = max(0, all_total - all_ad)
        ranked_share_non_ad = (all_ranked / non_ad_all) if non_ad_all > 0 else 0
        return {
            "all_time_total":   all_total,
            "last_year_total":  last_total,
            "all_time_ranked":  all_ranked,
            "last_year_ranked": int(round(non_ad_last * ranked_share_non_ad)),
            "all_time_ad":      all_ad,
            "last_year_ad":     last_ad,
        }
    except Exception:
        logger.exception("OpenDota counts fetch failed for %d", account_id)
        return None
