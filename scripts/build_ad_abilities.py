"""Write web/static/ad_abilities.json: every Ability Draft ability's hero,
whether it's an ultimate, and its place in that hero's ability order.

The match page's draft board lays the pool out the way the game does (each
hero's three abilities beside it, the ultimates in two rows on top), and
needs this to put an ability in its cell. Run it after a patch changes the
pool:  python3 scripts/build_ad_abilities.py

Sources: windrun (which hero owns each AD ability), OpenDota (ability ids
to names), and Valve's hero data (slot order, and type 1 for ultimates).
"""
import asyncio
import json
import re
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from botstats.windrun_api import fetch_ability_high_skill  # noqa: E402

OUT = os.path.join(ROOT, "web", "static", "ad_abilities.json")
VALVE = "https://www.dota2.com/datafeed/herodata?language=english&hero_id={}"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "adcl.gg ability table"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    stats = asyncio.run(fetch_ability_high_skill())
    if not stats:
        print("windrun gave nothing")
        return 1
    names = _get("https://api.opendota.com/api/constants/ability_ids")
    owned: dict[int, list[int]] = {}
    for s in stats:
        if s["abilityId"] > 0 and s.get("ownerHero"):
            owned.setdefault(s["ownerHero"], []).append(s["abilityId"])

    out = {}
    for hero_id, ids in sorted(owned.items()):
        hero = _get(VALVE.format(hero_id))["result"]["data"]["heroes"][0]
        order = {a["name"]: (i, a["type"] == 1) for i, a in enumerate(hero["abilities"])}
        for aid in ids:
            name = names.get(str(aid), "")
            # AD's own copies (kez_falcon_rush_ad) go by the original's slot.
            base = name[:-3] if name.endswith("_ad") else name
            # Shadow Fiend drafts one raze; Valve lists razes 1 to 3 together.
            i, ult = order.get(base) or order.get(re.sub(r"\d$", "1", base)) or (99, False)
            if i == 99:
                print(f"  {hero['name_loc']}: {name or aid} not in Valve's list; a basic ability last")
            out[aid] = {"hero": hero_id, "ult": ult, "order": i}
    with open(OUT, "w") as f:
        json.dump({str(k): v for k, v in sorted(out.items())}, f, separators=(",", ":"))
    print(f"wrote {len(out)} abilities of {len(owned)} heroes to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
