"""Checks of the site's Discord sync against a pretend server:
python3 tests/test_discord_sync.py

Nothing here talks to Discord. The server, its members, roles and channels
are stand-ins that record what was done to them.
"""
import asyncio
import itertools
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import discord_sync    # noqa: E402

failures: list[str] = []
_ids = itertools.count(1)


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if not ok and detail else ""))
    if not ok:
        failures.append(label)


class Role:
    def __init__(self, name, position=1, managed=False):
        self.name, self.id, self.position, self.managed = name, next(_ids), position, managed

    def is_default(self):
        return self.name == "@everyone"

    def is_assignable(self):
        return not self.is_default() and not self.managed and self.position < 50

    def __repr__(self):
        return self.name


class Member:
    def __init__(self, name, global_name=None, nick=None, roles=()):
        self.id = next(_ids)
        self.name, self.discriminator, self.global_name, self.nick = name, "0", global_name, nick
        self.display_name = nick or global_name or name
        self.roles = list(roles)

    async def add_roles(self, *roles, reason=None):
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class Channel:
    def __init__(self, name, category):
        self.name, self.category = name, category

    async def delete(self, reason=None):
        self.category.channels.remove(self)


class Category:
    def __init__(self, name):
        self.name, self.channels = name, []


class Guild:
    def __init__(self, members, roles=(), categories=()):
        self.name, self.members, self.roles, self.categories = "Test server", members, list(roles), list(categories)
        self.id = 1
        for m in members:
            m.guild = self

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    def get_member(self, member_id):
        return next((m for m in self.members if m.id == member_id), None)

    async def fetch_members(self, limit=None):
        for m in self.members:
            yield m

    async def create_role(self, name, reason=None):
        role = Role(name)
        self.roles.append(role)
        return role

    async def create_category(self, name, reason=None):
        cat = Category(name)
        self.categories.append(cat)
        return cat

    async def create_voice_channel(self, name, category, reason=None):
        ch = Channel(name, category)
        category.channels.append(ch)
        return ch


class Client:
    def __init__(self, guild):
        self.guild = guild

    def get_guild(self, guild_id):
        return self.guild if guild_id == 1 else None


NA = {"name": "NA", "slug": "na"}
EU = {"name": "EU", "slug": "eu"}


def run(coro):
    return asyncio.run(coro)


def main() -> int:
    check("roles are named for the division", discord_sync.role_names(NA) == ("player-na", "captain-na"))

    alice = Member("alice")
    bob = Member("bob_new", global_name="Bob")
    carol = Member("carol", nick="Carol C")
    leftover = Member("dave")                  # had NA's roles last season
    erin = Member("erin", roles=[Role("Mods")])
    guild = Guild([alice, bob, carol, leftover, erin])
    client = Client(guild)

    people = [
        {"account_id": 1, "name": "Alice", "discord": "@Alice", "level": "captain"},
        {"account_id": 2, "name": "Bob", "discord": "bob_new#1234", "level": "player"},
        {"account_id": 3, "name": "Carol", "discord": "Carol C (call me carol)", "level": "player"},
        {"account_id": 4, "name": "Frank", "discord": "frank", "level": "captain"},
        {"account_id": 5, "name": "Gus", "discord": "erin", "level": "standin"},
    ]
    plan = run(discord_sync.plan(client, 1, people, NA))
    found = {r["name"]: r["member"] for r in plan["rows"]}
    check("matches a name typed with an @ and other capitals", found["Alice"] is alice)
    check("matches an old name#1234", found["Bob"] is bob)
    check("matches a server nickname, note in parentheses and all", found["Carol"] is carol)
    check("someone not in the server isn't matched", found["Frank"] is None)
    check("stand-ins are left out", "Gus" not in found)
    check("roles not made yet are None in the plan", plan["player_role"] is None and plan["category"] is None)

    lines = run(discord_sync.apply(plan))
    roles = {r.name: r for r in guild.roles}
    check("the roles and the category are made", {"player-na", "captain-na"} <= set(roles)
          and any(c.name == discord_sync.CAPTAIN_ROOMS for c in guild.categories), str(list(roles)))
    pna, cna = roles["player-na"], roles["captain-na"]
    check("captains get both roles", pna in alice.roles and cna in alice.roles)
    check("players get the player role only", pna in bob.roles and cna not in bob.roles)
    check("stand-ins and others get nothing", [r.name for r in erin.roles] == ["Mods"])
    rooms = guild.categories[0]
    check("a room per captain, named with the division",
          sorted(c.name for c in rooms.channels) == ["Alice (NA)", "Frank (NA)"], str([c.name for c in rooms.channels]))
    check("says what happened", "3 of 4 people" in lines[0] and "Made player-na, captain-na" in lines[0], lines[0])

    # Next season: Bob captains, Alice is gone, Dave somehow had the role.
    leftover.roles.append(pna)
    eu_room = Channel("Zed (EU)", rooms)
    rooms.channels.append(eu_room)
    people2 = [{"account_id": 2, "name": "Bob", "discord": "bob_new", "level": "captain"}]
    run(discord_sync.apply(run(discord_sync.plan(client, 1, people2, NA))))
    check("the roles come off people no longer signed up", pna not in alice.roles and pna not in leftover.roles)
    check("no second copy of a role is made", sum(r.name == "player-na" for r in guild.roles) == 1)
    check("the division's rooms follow its captains; another division's are left",
          sorted(c.name for c in rooms.channels) == ["Bob (NA)", "Zed (EU)"], str([c.name for c in rooms.channels]))

    lines = run(discord_sync.delete_rooms(client, 1))
    check("deleting rooms empties the category", rooms.channels == [] and rooms in guild.categories, lines[0])

    mods, bot_role, top = Role("Mods", 5), Role("Some bot", 3, managed=True), Role("Owner", 60)
    guild.roles += [mods, bot_role, top]
    picks = discord_sync.assignable_roles(guild)
    check("only roles the bot can hand out are offered, highest first",
          picks[0] is mods and bot_role not in picks and top not in picks, str(picks))
    said = run(discord_sync.add_role(client, 1, bob.id, mods.id, "test"))
    check("giving one role", mods in bob.roles and said == "Gave Bob Mods.", said)
    check("giving it again says so", "already has" in run(discord_sync.add_role(client, 1, bob.id, mods.id, "test")))
    try:
        run(discord_sync.add_role(client, 1, bob.id, top.id, "test"))
        check("a role above the bot is refused", False)
    except LookupError:
        check("a role above the bot is refused", True)

    try:
        run(discord_sync.plan(client, 99, people, NA))
        check("a server the bot isn't in is refused", False)
    except LookupError:
        check("a server the bot isn't in is refused", True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
