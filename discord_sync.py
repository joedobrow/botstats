"""Discord roles and captain voice rooms for a season, from the site's signups.

A season's players get its division's player role (player-na), its
captains that and the captain role (captain-na), and each captain gets a
voice room in the Captain Rooms category, named with the division
("redground (NA)"). The roles and the category are made when the server
doesn't have them yet. Syncing one division touches only its own roles and
rooms, so NA and EU can share a server. Stand-ins get nothing.

People are matched to server members by the Discord name the site has for
them (store.discord_handles). Admin → Discord runs it: plan() is the
preview, apply() makes the changes, delete_rooms() empties Captain Rooms.
"""
from __future__ import annotations

import logging
import re

import discord

logger = logging.getLogger(__name__)

CAPTAIN_ROOMS = "Captain Rooms"
REASON = "ADCL season sync"


def role_names(division: dict) -> tuple[str, str]:
    """The player and captain roles of a division: player-na, captain-na."""
    slug = (division.get("slug") or division.get("name") or "").lower()
    return f"player-{slug}", f"captain-{slug}"


def room_suffix(division: dict) -> str:
    return f" ({division['name']})"


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


def _keys(m) -> list[str]:
    return [k for k in (m.name, f"{m.name}#{m.discriminator}", m.global_name, m.nick) if k]


async def _members(client: discord.Client, guild_id: int):
    guild = client.get_guild(guild_id)
    if guild is None:
        raise LookupError("The bot isn't in that Discord server.")
    try:
        return guild, [m async for m in guild.fetch_members(limit=None)]
    except discord.ClientException as e:
        raise LookupError(f"Can't list the server's members ({e}). Turn on the Server Members "
                          "Intent for the bot in the Discord developer portal.") from e


def match(members, people: list[dict]) -> list[dict]:
    """Each person with the server member their Discord name points at, or None."""
    by_name: dict[str, discord.Member] = {}
    for m in members:
        for key in _keys(m):
            by_name.setdefault(_norm(key), m)
    return [{**p, "member": next((by_name[_norm(c)] for c in _candidates(p.get("discord") or "")
                                  if _norm(c) in by_name), None)} for p in people]


async def plan(client: discord.Client, guild_id: int, people: list[dict], division: dict) -> dict:
    """Match a season's people to the server's members, without changing
    anything. people: [{account_id, name, discord, level}], one per person.
    Returns {guild, members, rows, division, player_role, captain_role,
    category}: a role or the category is None when it's still to be made."""
    guild, members = await _members(client, guild_id)
    player_name, captain_name = role_names(division)
    return {"guild": guild, "members": members, "division": division,
            "rows": match(members, [p for p in people if p.get("level") != "standin"]),
            "player_role": _find(guild.roles, player_name), "captain_role": _find(guild.roles, captain_name),
            "category": _find(guild.categories, CAPTAIN_ROOMS)}


class _Steps:
    """Runs Discord calls one at a time, keeping going past a failure."""

    def __init__(self):
        self.errors: list[str] = []

    async def __call__(self, what: str, step):
        """The call's result (True for one that returns nothing), or None if it failed."""
        try:
            result = await step
            return True if result is None else result
        except Exception as e:
            logger.exception("Discord sync: %s failed", what)
            self.errors.append(f"{what}: {type(e).__name__}: {e}")
            return None

    def report(self, lines: list[str]) -> list[str]:
        if self.errors:
            lines.append(f"{len(self.errors)} step(s) failed. First: {self.errors[0]}")
            if any("Forbidden" in e or "Missing Permissions" in e for e in self.errors):
                lines.append("The bot needs Manage Roles and Manage Channels, and its own role "
                             "must sit above the roles it hands out.")
        return lines


async def apply(p: dict) -> list[str]:
    """Make the changes a plan describes. Returns lines saying what happened."""
    guild, division = p["guild"], p["division"]
    player_name, captain_name = role_names(division)
    step = _Steps()

    made = []
    for key, name in (("player_role", player_name), ("captain_role", captain_name)):
        if p[key] is None:
            p[key] = await step(f"making the {name} role", guild.create_role(name=name, reason=REASON))
            if p[key]:
                made.append(name)
    if p["category"] is None:
        p["category"] = await step(f"making {CAPTAIN_ROOMS}", guild.create_category(CAPTAIN_ROOMS, reason=REASON))
    player_role, captain_role, category = p["player_role"], p["captain_role"], p["category"]
    if not player_role or not captain_role:
        return step.report(["Nothing changed: the roles couldn't be made."])

    # Each role on exactly the people who should have it, and no one else.
    matched = [r for r in p["rows"] if r["member"]]
    want = {player_role: {r["member"].id for r in matched},
            captain_role: {r["member"].id for r in matched if r["level"] == "captain"}}
    added = removed = 0
    for m in p["members"]:
        give = [role for role, ids in want.items() if m.id in ids and role not in m.roles]
        take = [role for role, ids in want.items() if m.id not in ids and role in m.roles]
        if take and await step(f"taking roles from {m}", m.remove_roles(*take, reason=REASON)):
            removed += 1
        if give and await step(f"giving roles to {m}", m.add_roles(*give, reason=REASON)):
            added += 1

    rooms = 0
    if category:
        suffix = room_suffix(division)
        wanted = [r["name"] + suffix for r in sorted(p["rows"], key=lambda r: r["name"].lower())
                  if r["level"] == "captain"]
        have = {ch.name: ch for ch in category.channels if ch.name.endswith(suffix)}
        for name, ch in have.items():
            if name not in wanted:
                await step(f"deleting {name}", ch.delete(reason=REASON))
        for name in wanted:
            if name not in have:
                if await step(f"room {name}", guild.create_voice_channel(name=name, category=category,
                                                                          reason=REASON)):
                    rooms += 1
        rooms_line = f"{len(wanted)} captain rooms ({rooms} new)."
    else:
        rooms_line = "No captain rooms: the category couldn't be made."

    unmatched = len(p["rows"]) - len(matched)
    lines = [(f"Made {', '.join(made)}. " if made else "")
             + f"{player_name} and {captain_name}: {len(matched)} of {len(p['rows'])} people"
             + (f" ({unmatched} not in the server)" if unmatched else "")
             + f"; {added} given, {removed} taken off.", rooms_line]
    return step.report(lines)


async def delete_rooms(client: discord.Client, guild_id: int) -> list[str]:
    """Delete every channel in the Captain Rooms category; the category stays."""
    guild = client.get_guild(guild_id)
    if guild is None:
        raise LookupError("The bot isn't in that Discord server.")
    category = _find(guild.categories, CAPTAIN_ROOMS)
    if category is None:
        return [f"There's no {CAPTAIN_ROOMS} category."]
    step = _Steps()
    gone = 0
    for ch in list(category.channels):
        if await step(f"deleting {ch.name}", ch.delete(reason=REASON)):
            gone += 1
    return step.report([f"Deleted {gone} captain room{'' if gone == 1 else 's'}."])


async def search(client: discord.Client, guild_id: int, query: str, handles: dict[int, str]) -> list[dict]:
    """Server members whose name has the query in it, each with the site
    accounts whose Discord name points at them: [{member, roles, accounts}]."""
    guild, members = await _members(client, guild_id)
    q = _norm(query)
    hits = [m for m in members if any(q in _norm(k) for k in _keys(m))]
    found = match(hits, [{"account_id": a, "discord": h} for a, h in handles.items()])
    accounts: dict[int, list[int]] = {}
    for f in found:
        if f["member"]:
            accounts.setdefault(f["member"].id, []).append(f["account_id"])
    return [{"member": m, "roles": [r.name for r in m.roles if not r.is_default()],
             "accounts": accounts.get(m.id, [])}
            for m in sorted(hits, key=lambda m: m.display_name.lower())]
