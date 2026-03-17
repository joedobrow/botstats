"""
Build the compact data payloads that the AD helper HTML template expects.

Fetches from:
  - Windrun API (ability-high-skill)
  - OpenDota constants (ability_ids, abilities, heroes)
  - Local ability_roles.json

Produces the same JSON shapes the original build_ad_helper_page.py created:
  DATA: {HeroName: {hero_id, hero_img, body_winrate, abilities: [{id, name, img, win_pct, pick_num}]}}
  HS:   {ability_or_hero_name: {win_pct, pick_num}}
  ROLES: {ability_name: "carry"|"support"|"both"}
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

import aiohttp

from botstats.windrun_api import fetch_ability_high_skill

logger = logging.getLogger(__name__)

CDN_BASE = "https://cdn.cloudflare.steamstatic.com"
OPENDOTA_API = "https://api.opendota.com/api/constants"
ROLES_FILE = Path(__file__).parent / "data" / "ability_roles.json"

# In-memory cache
_cache: dict = {}
_cache_time: float = 0.0
CACHE_TTL = 3600  # 1 hour for windrun data

_constants_cache: dict = {}
_constants_time: float = 0.0
CONSTANTS_TTL = 86400  # 24 hours for OpenDota constants


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> Optional[dict | list]:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.warning("GET %s returned %d", url, resp.status)
                return None
            return await resp.json()
    except Exception:
        logger.exception("Failed to fetch %s", url)
        return None


async def _get_opendota_constants() -> dict:
    """Fetch and cache OpenDota constants (ability_ids, abilities, heroes)."""
    global _constants_cache, _constants_time

    if _constants_cache and (time.monotonic() - _constants_time) < CONSTANTS_TTL:
        return _constants_cache

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        ability_ids, abilities, heroes = await asyncio.gather(
            _fetch_json(session, f"{OPENDOTA_API}/ability_ids"),
            _fetch_json(session, f"{OPENDOTA_API}/abilities"),
            _fetch_json(session, f"{OPENDOTA_API}/heroes"),
        )

    if not ability_ids or not abilities or not heroes:
        logger.error("Failed to fetch one or more OpenDota constants")
        return _constants_cache  # return stale cache if available

    _constants_cache = {
        "ability_ids": ability_ids,   # {"5007": "axe_berserkers_call", ...}
        "abilities": abilities,       # {"axe_berserkers_call": {"dname": "...", "img": "...", ...}}
        "heroes": heroes,             # [{"id": 2, "localized_name": "Axe", "img": "...", ...}, ...]
    }
    _constants_time = time.monotonic()
    return _constants_cache


def _load_roles() -> dict:
    if not ROLES_FILE.exists():
        return {}
    try:
        with ROLES_FILE.open("r", encoding="utf-8") as f:
            doc = json.load(f)
        labels = doc.get("labels", {})
        return labels if isinstance(labels, dict) else {}
    except Exception:
        return {}


import asyncio


async def build_ad_data() -> Optional[dict]:
    """
    Build the three JSON payloads the AD helper template needs.

    Returns:
        {"data_json": str, "hs_json": str, "roles_json": str}
        or None on failure.
    """
    global _cache, _cache_time

    if _cache and (time.monotonic() - _cache_time) < CACHE_TTL:
        return _cache

    # Fetch windrun + OpenDota in parallel
    windrun_task = asyncio.create_task(fetch_ability_high_skill())
    constants_task = asyncio.create_task(_get_opendota_constants())

    ability_stats = await windrun_task
    constants = await constants_task

    if not ability_stats or not constants:
        logger.error("Failed to fetch required data")
        return _cache  # return stale cache

    ability_id_map = constants["ability_ids"]    # str(id) -> internal_name
    abilities_info = constants["abilities"]       # internal_name -> {dname, img, ...}
    heroes_list = constants["heroes"]             # [{id, localized_name, img, ...}, ...]

    # Build hero lookup: hero_id -> {name, img}
    # heroes_list may be a list of dicts or a dict keyed by hero internal name
    hero_lookup = {}
    hero_items = heroes_list.values() if isinstance(heroes_list, dict) else heroes_list
    for h in hero_items:
        if not isinstance(h, dict):
            continue
        hid = h.get("id")
        if hid is not None:
            hero_lookup[hid] = {
                "name": h.get("localized_name", ""),
                "img": CDN_BASE + h["img"] if h.get("img") else None,
            }

    # Build ability lookup: ability_id -> {name, img}
    def resolve_ability(aid: int) -> dict:
        internal = ability_id_map.get(str(aid))
        if not internal:
            return {"name": None, "img": None}
        info = abilities_info.get(internal, {})
        return {
            "name": info.get("dname"),
            "img": CDN_BASE + info["img"] if info.get("img") else None,
        }

    # Group windrun stats by ownerHero
    # Each entry: {abilityId, ownerHero, winrate, avgPickPosition, numPicks, ...}
    heroes_abilities: dict[int, list[dict]] = {}
    hero_model_stats: dict[int, dict] = {}

    for stat in ability_stats:
        aid = stat.get("abilityId")
        if aid is None:
            continue

        # Negative ability IDs are hero model rows (abilityId = -heroId, ownerHero is None)
        if aid < 0:
            hero_id = abs(aid)
            hero_model_stats[hero_id] = stat
            continue

        owner = stat.get("ownerHero")
        if owner is None:
            continue

        heroes_abilities.setdefault(owner, []).append(stat)

    # Build DATA (by-hero compact) and HS (ability stats)
    data_compact = {}
    hs_stats = {}

    for hero_id, abil_list in heroes_abilities.items():
        hero_info = hero_lookup.get(hero_id)
        if not hero_info:
            continue

        hero_name = hero_info["name"]
        hero_img = hero_info["img"]
        model = hero_model_stats.get(hero_id)

        body_winrate = None
        if model:
            wr = model.get("winrate")
            if isinstance(wr, (int, float)):
                body_winrate = round(wr * 100, 2)

        abilities_out = []
        for stat in abil_list:
            aid = stat["abilityId"]
            resolved = resolve_ability(aid)
            aname = resolved["name"]
            if not aname:
                continue

            win_pct = None
            wr = stat.get("winrate")
            if isinstance(wr, (int, float)):
                win_pct = round(wr * 100, 2)

            pick_num = None
            pp = stat.get("avgPickPosition")
            if isinstance(pp, (int, float)):
                pick_num = round(pp, 2)

            abilities_out.append({
                "id": aid,
                "name": aname,
                "img": resolved["img"],
                "win_pct": win_pct,
                "pick_num": pick_num,
            })

            # HS entry for this ability
            hs_stats[aname] = {
                "win_pct": win_pct,
                "pick_num": pick_num,
            }

        if not abilities_out:
            continue

        # Append the hero model as a draftable "ability" (same as original)
        model_pick = None
        if model:
            pp = model.get("avgPickPosition")
            if isinstance(pp, (int, float)):
                model_pick = round(pp, 2)

        # Only add if hero name isn't already in the list
        if all(a["name"] != hero_name for a in abilities_out):
            abilities_out.append({
                "id": None,
                "name": hero_name,
                "img": hero_img,
                "win_pct": body_winrate,
                "pick_num": model_pick,
            })

        data_compact[hero_name] = {
            "hero_id": hero_id,
            "hero_img": hero_img,
            "body_winrate": body_winrate,
            "abilities": abilities_out,
        }

        # HS entry for the hero model
        if model:
            hs_stats[hero_name] = {
                "win_pct": body_winrate,
                "pick_num": model_pick,
            }

    if not data_compact:
        logger.error("Built zero heroes from windrun + OpenDota data")
        return _cache

    roles = _load_roles()

    result = {
        "data_json": json.dumps(data_compact, ensure_ascii=False),
        "hs_json": json.dumps(hs_stats, ensure_ascii=False),
        "roles_json": json.dumps(roles, ensure_ascii=False),
    }

    _cache = result
    _cache_time = time.monotonic()
    logger.info("Built AD helper data: %d heroes, %d ability stats", len(data_compact), len(hs_stats))
    return result
