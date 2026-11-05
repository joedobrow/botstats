"""Discord roles and captain voice rooms for a server, from the site's signups.

Everyone signed up for a season gets the players role, its captains get the
captains role too, and the Captain Rooms category is rebuilt with one voice
channel per captain. It works on a whole Discord server at once, because NA
and EU share one: the roles are reset for everyone before they're handed
out, so syncing one division alone would take them away from the other.

People are matched to server members by the Discord name the site has for
them (store.discord_handles). Admin → Discord runs it: plan() is the preview,
apply() makes the changes.
"""
from __future__ import annotations

import logging
import re

import discord

logger = logging.getLogger(__name__)

PLAYERS_ROLE = "players"
CAPTAINS_ROLE = "captains"
CAPTAIN_ROOMS = "Captain Rooms"


def _norm(s: str) -> str:
    return (s or "").strip().lower().lstrip("@")


def _candidates(handle: str) -> list[str]:
    """Every reasonable reading of a Discord name as people type it: old-style
    user#1234, a note in parentheses, stray punctuation, a space before #."""
    raw = (handle or "").strip()
    if not raw:
        return []
    cands = [raw, re.sub(r"\s*#\s*", "#", raw), re.sub(r"\s*[^\w]*#\s*\d+.*$", "", raw)]
    # Someone who moved off the #1234 names but typed the old one.
    cands += [c.split("#", 1)[0] for c in list(cands) if "#" in c]
    cands += [m.group(1) for m in re.finditer(r"\(([^)]+)\)", raw)]
    no_paren = re.sub(r"\s*\([^)]*\)\s*", " ", raw).strip()
    if no_paren:
        cands.append(no_paren)
    cands = [re.sub(r"[?!.,\s]+$", "", c).strip() for c in cands]
    return [c for c in dict.fromkeys(cands) if c]


def _find(items, name: str):
    return next((x for x in items if x.name.lower() == name.lower()), None)


async def plan(client: discord.Client, guild_id: int, people: list[dict]) -> dict:
    """Match people to the server's members, without changing anything.

    people: [{account_id, name, discord, captain, division}], one per person.
    Returns {guild, members, rows: [{...person, member}], players_role,
    captains_role, category, missing: [what the server lacks]}.
    """
    guild = client.get_guild(guild_id)
    if guild is None:
        raise LookupError("The bot isn't in that Discord server.")
    try:
        members = [m async for m in guild.fetch_members(limit=None)]
    except discord.ClientException as e:
        raise LookupError(f"Can't list the server's members ({e}). Turn on the Server Members "
                          "Intent for the bot in the Discord developer portal.") from e
    by_name: dict[str, discord.Member] = {}
    for m in members:
        for key in filter(None, [m.name, f"{m.name}#{m.discriminator}", m.global_name, m.nick]):
            by_name.setdefault(_norm(key), m)

    rows = []
    for p in people:
        member = next((by_name[_norm(c)] for c in _candidates(p.get("discord") or "")
                       if _norm(c) in by_name), None)
        rows.append({**p, "member": member})
    players_role, captains_role = _find(guild.roles, PLAYERS_ROLE), _find(guild.roles, CAPTAINS_ROLE)
    category = _find(guild.categories, CAPTAIN_ROOMS)
    missing = [what for what, found in ((f"the {PLAYERS_ROLE} role", players_role),
                                        (f"the {CAPTAINS_ROLE} role", captains_role),
                                        (f"the {CAPTAIN_ROOMS} category", category)) if not found]
    return {"guild": guild, "members": members, "rows": rows, "players_role": players_role,
            "captains_role": captains_role, "category": category, "missing": missing}


async def apply(p: dict) -> list[str]:
    """Make the changes a plan describes. Returns lines saying what happened."""
    if p["missing"]:
        return ["Nothing changed: the server has no " + ", ".join(p["missing"]) + "."]
    players_role, captains_role, category = p["players_role"], p["captains_role"], p["category"]
    errors: list[str] = []

    async def attempt(what: str, step) -> bool:
        try:
            await step
            return True
        except Exception as e:
            logger.exception("Discord sync: %s failed", what)
            errors.append(f"{what}: {type(e).__name__}: {e}")
            return False

    # Reset, then hand out: only these two roles change on anyone.
    for m in p["members"]:
        held = [r for r in (players_role, captains_role) if r in m.roles]
        if held:
            await attempt(f"removing roles from {m}", m.remove_roles(*held, reason="Season role sync"))
    matched = [r for r in p["rows"] if r["member"]]
    given = 0
    for r in matched:
        roles = [players_role] + ([captains_role] if r["captain"] else [])
        given += await attempt(f"roles for {r['name']}", r["member"].add_roles(*roles, reason="Season role sync"))

    for ch in list(category.channels):
        await attempt(f"deleting {ch.name}", ch.delete(reason="Season role sync"))
    captains = sorted((r for r in p["rows"] if r["captain"]), key=lambda r: r["name"].lower())
    several = len({r["division"] for r in captains}) > 1
    rooms = 0
    for r in captains:
        name = f"{r['name']} ({r['division']})" if several else r["name"]
        rooms += await attempt(f"room for {r['name']}", p["guild"].create_voice_channel(
            name=name, category=category, reason="Season role sync"))

    unmatched = len(p["rows"]) - len(matched)
    lines = [f"Roles given to {given} of {len(p['rows'])} people"
             + (f"; {unmatched} not found in the server" if unmatched else "") + ".",
             f"{rooms} captain rooms rebuilt."]
    if errors:
        lines.append(f"{len(errors)} step(s) failed. First: {errors[0]}")
        if any("Forbidden" in e or "Missing Permissions" in e for e in errors):
            lines.append("The bot needs Manage Roles and Manage Channels, and its role must sit "
                         "above the players and captains roles.")
    return lines
