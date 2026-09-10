"""
RD2L.gg scraping helpers for /sync_roles_channels.

We use rd2l.gg's public HTML pages directly — the CSV export variants
(?captains=1 etc.) are known to be unreliable (union in the wrong pool).
The HTML pages are clean, ~1 request each.

Endpoints:
  - PLAYERS: HTML page at .../players — one <a href="/profile/N">Name</a>
    per player, ~one per row of the draft-sheet table.
  - CAPTAINS: HTML page at .../captains — same anchor pattern, just captains.
  - DISCORD HANDLE: HTML page at /profile/<id> — regex-scraped for the
    "Discord Name" field.
"""

from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30
UA_HEADERS = {"User-Agent": "RD2L-EST-WED-Bot (contact: jdobrow@gmail.com)"}


_PROFILE_ANCHOR_RE = re.compile(
    r'<a[^>]+href="/profile/(\d+)"[^>]*>(.*?)</a>', re.DOTALL
)


async def fetch_roster_from_page(url: str, label: str = "roster") -> list[dict]:
    """Fetch a rd2l.gg HTML roster page (/players or /captains) and return
    [{name, account_id}], deduped by account_id in first-seen order.
    """
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout, headers=UA_HEADERS) as s:
        async with s.get(url) as r:
            if r.status != 200:
                raise RuntimeError(f"{label} page: HTTP {r.status} for {url}")
            html = await r.text()
    out: list[dict] = []
    seen: set[int] = set()
    for m in _PROFILE_ANCHOR_RE.finditer(html):
        aid = int(m.group(1))
        if aid in seen:
            continue
        raw_name = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        raw_name = re.sub(r"\s+", " ", raw_name)
        if raw_name:
            seen.add(aid)
            out.append({"account_id": aid, "name": raw_name})
    return out


_DISCORD_RE = re.compile(
    r'<p class="title">Discord Name</p>\s*<p class="subtitle">([^<]*)</p>'
)


async def fetch_discord_handle(session: aiohttp.ClientSession, account_id: int) -> str | None:
    """Scrape a single player's rd2l.gg profile for their Discord handle.
    Returns the raw string (as they entered it), or None on failure."""
    url = f"https://rd2l.gg/profile/{account_id}"
    try:
        async with session.get(url) as r:
            if r.status != 200:
                return None
            html = await r.text()
    except Exception:
        logger.warning("Discord scrape failed for %d", account_id, exc_info=False)
        return None
    m = _DISCORD_RE.search(html)
    return m.group(1).strip() if m and m.group(1).strip() else None


async def fetch_discord_handles(account_ids: list[int], concurrency: int = 6) -> dict[int, str | None]:
    """Fetch Discord handles for many players concurrently.
    Bounded concurrency so we don't hammer rd2l.gg. Returns {account_id: handle_or_None}."""
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    sem = asyncio.Semaphore(concurrency)
    result: dict[int, str | None] = {}
    async with aiohttp.ClientSession(timeout=timeout, headers=UA_HEADERS) as s:
        async def _one(aid: int):
            async with sem:
                result[aid] = await fetch_discord_handle(s, aid)
        await asyncio.gather(*(_one(aid) for aid in account_ids))
    return result


# ---------------------------------------------------------------------------
# Google Sheets: published "Drafted Players" CSV
# ---------------------------------------------------------------------------

def parse_drafted_players_csv(text: str) -> list[dict]:
    """Parse the Drafted Players tab as CSV text.

    Expects the columns that tab uses: Account ID, Name, Winner, Cost.
    Only rows with BOTH Winner and Cost filled in count as drafted picks —
    blank rows are undrafted players, not errors, and are skipped silently.

    Returns [{account_id, name, captain, cost}].
    """
    import csv as _csv, io as _io
    reader = _csv.DictReader(_io.StringIO(text))
    if not reader.fieldnames:
        raise RuntimeError("drafted-players CSV: empty or unreadable")

    # Tolerate header drift (case/spacing) by normalising to a lookup.
    cols = {(h or "").strip().lower(): h for h in reader.fieldnames}
    def col(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None
    c_id   = col("account id", "account_id", "accountid")
    c_name = col("name", "player")
    c_win  = col("winner", "captain", "team")
    c_cost = col("cost", "price")
    # The Players tab has no Account ID column, but carries Dotabuff/Windrun
    # profile links that end in the id — use those as the fallback identity.
    c_url  = col("dotabuff", "windrun", "opendota", "profile")
    missing = [lbl for lbl, c in
               [("Account ID or a Dotabuff/Windrun link", c_id or c_url),
                ("Winner", c_win), ("Cost", c_cost)] if not c]
    if missing:
        raise RuntimeError(
            f"drafted-players CSV missing column(s): {', '.join(missing)}. "
            f"Found: {', '.join(reader.fieldnames)}"
        )

    out: list[dict] = []
    for row in reader:
        captain = (row.get(c_win) or "").strip()
        raw_cost = (row.get(c_cost) or "").strip().replace("$", "").replace(",", "")
        if not captain or not raw_cost:
            continue                      # undrafted, not an error
        aid = None
        if c_id:
            raw_id = str(row.get(c_id) or "").strip()
            if raw_id.isdigit():
                aid = int(raw_id)
        if aid is None and c_url:
            m = re.search(r"/players/(\d+)", str(row.get(c_url) or ""))
            if m:
                aid = int(m.group(1))
        if aid is None:
            logger.warning("skipping row with no resolvable account id: %s", row)
            continue
        try:
            cost = int(float(raw_cost))
        except (TypeError, ValueError):
            logger.warning("skipping row with unparseable cost: %s", row)
            continue
        out.append({
            "account_id": aid,
            "name": (row.get(c_name) or "").strip() if c_name else "",
            "captain": captain,
            "cost": cost,
        })
    return out


async def fetch_drafted_players_csv(url: str) -> list[dict]:
    """Fetch a published-to-web Google Sheets CSV and parse it. Thin wrapper
    around parse_drafted_players_csv so the URL and file-upload paths share
    exactly one parser."""
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout, headers=UA_HEADERS) as s:
        async with s.get(url) as r:
            if r.status != 200:
                raise RuntimeError(f"drafted-players CSV: HTTP {r.status} for {url}")
            text = await r.text()
    return parse_drafted_players_csv(text)


async def fetch_team_identities(teams_page_url: str) -> list[dict]:
    """Scrape the RD2L division teams page for [{captain, team_name, logo_url}].

    Team rows render as `Team Name (Captain Name)` next to a logo <img>, so we
    pull the captain out of the trailing parenthetical and keep the rest as the
    display name. Teams whose name IS the captain's name (an unbranded team)
    come back with both fields equal, which is the correct display.
    """
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout, headers=UA_HEADERS) as s:
        async with s.get(teams_page_url) as r:
            if r.status != 200:
                raise RuntimeError(f"teams page: HTTP {r.status} for {teams_page_url}")
            html = await r.text()

    # Split on the team-link boundary rather than using a lookahead — a
    # lookahead anchored on "next team link or end-of-string" silently drops
    # the final team when its chunk runs past the window.
    out: list[dict] = []
    seen: set[str] = set()
    for chunk in re.split(r'href="[^"]*teams/[A-Za-z0-9_-]+"', html)[1:]:
        chunk = chunk[:600]
        logo = None
        lm = re.search(r'<img[^>]+src="([^"]+)"', chunk)
        if lm:
            logo = lm.group(1)
            if logo.startswith("/"):
                logo = "https://rd2l.gg" + logo
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", chunk)).strip().lstrip(">").strip()
        # Format is `Team Name (Captain)`, and the team name may itself contain
        # parentheses (e.g. "Gym Esports (For Aniki) (Yokozuna Hoshoryu)"), so
        # the captain is the LAST parenthetical, not the first.
        parens = list(re.finditer(r"\(([^)]*)\)", text))
        if not parens:
            continue
        last = parens[-1]
        captain = last.group(1).strip()
        team_name = text[:last.start()].strip().lstrip(">").strip()
        key = captain.lower()
        if not team_name or not captain or key in seen:
            continue
        seen.add(key)
        out.append({"captain": captain, "team_name": team_name, "logo_url": logo})
    return out
