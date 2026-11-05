"""Checks of the site's Discord sync against a pretend server:
python3 tests/test_discord_sync.py

Nothing here talks to Discord. The server, its members, roles and channels
are stand-ins that record what was done to them.
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import discord_sync    # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if not ok and detail else ""))
    if not ok:
        failures.append(label)


class Named:
    def __init__(self, name):
        self.name = name


class Member:
    def __init__(self, name, global_name=None, nick=None, roles=()):
        self.name, self.discriminator, self.global_name, self.nick = name, "0", global_name, nick
        self.display_name = nick or global_name or name
        self.roles = list(roles)

    async def add_roles(self, *roles, reason=None):
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class Channel(Named):
    def __init__(self, name, category):
        super().__init__(name)
        self.category = category

    async def delete(self, reason=None):
        self.category.channels.remove(self)


class Category(Named):
    def __init__(self, name):
        super().__init__(name)
        self.channels = []


class Guild:
    def __init__(self, members, roles, categories):
        self.name, self.members, self.roles, self.categories = "Test server", members, roles, categories

    async def fetch_members(self, limit=None):
        for m in self.members:
            yield m

    async def create_voice_channel(self, name, category, reason=None):
        category.channels.append(Channel(name, category))


class Client:
    def __init__(self, guild):
        self.guild = guild

    def get_guild(self, guild_id):
        return self.guild if guild_id == 1 else None


def main() -> int:
    players, captains = Named("Players"), Named("Captains")
    rooms = Category("captain rooms")
    rooms.channels.append(Channel("Last season's captain", rooms))
    alice = Member("alice")
    bob = Member("bob_new", global_name="Bob")
    carol = Member("carol", nick="Carol C")
    leftover = Member("dave", roles=[players, captains])     # had roles last season
    bystander = Member("erin", roles=[Named("Mods")])
    guild = Guild([alice, bob, carol, leftover, bystander], [players, captains], [rooms])

    people = [
        {"account_id": 1, "name": "Alice", "discord": "@Alice", "captain": True, "division": "NA"},
        {"account_id": 2, "name": "Bob", "discord": "bob_new#1234", "captain": False, "division": "NA"},
        {"account_id": 3, "name": "Carol", "discord": "Carol C (call me carol)", "captain": False, "division": "EU"},
        {"account_id": 4, "name": "Frank", "discord": "frank", "captain": True, "division": "EU"},
        {"account_id": 5, "name": "Gus", "discord": None, "captain": False, "division": "NA"},
    ]
    plan = asyncio.run(discord_sync.plan(Client(guild), 1, people))
    found = {r["name"]: r["member"] for r in plan["rows"]}
    check("matches a name typed with an @ and other capitals", found["Alice"] is alice)
    check("matches an old name#1234", found["Bob"] is bob)
    check("matches a server nickname, note in parentheses and all", found["Carol"] is carol)
    check("someone not in the server isn't matched", found["Frank"] is None and found["Gus"] is None)
    check("the role and category names ignore capitals", not plan["missing"], str(plan["missing"]))

    lines = asyncio.run(discord_sync.apply(plan))
    check("captains get both roles", players in alice.roles and captains in alice.roles)
    check("players get the players role only", players in bob.roles and captains not in bob.roles)
    check("last season's roles come off people not signed up", leftover.roles == [])
    check("other roles are left alone", [r.name for r in bystander.roles] == ["Mods"])
    names = sorted(c.name for c in rooms.channels)
    check("one room per captain, named with the division when there are several",
          names == ["Alice (NA)", "Frank (EU)"], str(names))
    check("says what happened", lines[0].startswith("Roles given to 3 of 5 people; 2 not found"), lines[0])

    try:
        asyncio.run(discord_sync.plan(Client(guild), 99, people))
        check("a server the bot isn't in is refused", False)
    except LookupError:
        check("a server the bot isn't in is refused", True)
    bare = Guild([alice], [], [])
    plan = asyncio.run(discord_sync.plan(Client(bare), 1, people))
    lines = asyncio.run(discord_sync.apply(plan))
    check("without the roles and category nothing changes",
          len(plan["missing"]) == 3 and alice.roles == [players, captains] and lines[0].startswith("Nothing changed"))

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
