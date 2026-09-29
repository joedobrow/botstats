"""Steam sign-in (OpenID 2.0) and profile lookup.

Steam's OpenID only proves which Steam account signed in; the persona name
and avatar come from the Web API, which needs STEAM_API_KEY and is optional.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode

import aiohttp

from player_api import STEAM64_OFFSET

logger = logging.getLogger(__name__)

OPENID_URL = "https://steamcommunity.com/openid/login"
_CLAIMED_ID_RE = re.compile(r"^https://steamcommunity\.com/openid/id/(\d{17})$")
TIMEOUT = aiohttp.ClientTimeout(total=15)


def login_url(return_to: str, realm: str) -> str:
    return OPENID_URL + "?" + urlencode({
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": return_to,
        "openid.realm": realm,
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    })


async def verify(params: dict[str, str], expected_return_to: str) -> int | None:
    """Check a callback's assertion with Steam. Returns the 32-bit account id,
    or None if Steam doesn't vouch for it."""
    if params.get("openid.mode") != "id_res":
        return None
    if params.get("openid.op_endpoint") != OPENID_URL:
        return None
    if not (params.get("openid.return_to") or "").startswith(expected_return_to):
        return None
    m = _CLAIMED_ID_RE.match(params.get("openid.claimed_id") or "")
    if not m:
        return None
    body = {k: v for k, v in params.items() if k.startswith("openid.")}
    body["openid.mode"] = "check_authentication"
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.post(OPENID_URL, data=body) as r:
                text = await r.text()
    except Exception:
        logger.warning("Steam OpenID verification request failed", exc_info=True)
        return None
    if "is_valid:true" not in text.split("\n"):
        return None
    return int(m.group(1)) - STEAM64_OFFSET


async def player_summary(account_id: int, api_key: str) -> tuple[str | None, str | None]:
    """(persona name, avatar url) from the Steam Web API, or Nones."""
    if not api_key:
        return None, None
    url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
            async with s.get(url, params={"key": api_key, "steamids": str(account_id + STEAM64_OFFSET)}) as r:
                data = await r.json(content_type=None)
        p = (data.get("response", {}).get("players") or [{}])[0]
        return p.get("personaname"), p.get("avatarfull")
    except Exception:
        logger.warning("Steam player summary failed for %d", account_id, exc_info=True)
        return None, None


async def player_summaries(account_ids: list[int], api_key: str) -> dict[int, tuple[str | None, str | None]]:
    """{account_id: (persona name, avatar url)} for many players, 100 per
    request (the Web API's limit). Accounts Steam doesn't return are left out."""
    out: dict[int, tuple[str | None, str | None]] = {}
    if not api_key:
        return out
    url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
    async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
        for i in range(0, len(account_ids), 100):
            ids = ",".join(str(a + STEAM64_OFFSET) for a in account_ids[i:i + 100])
            async with s.get(url, params={"key": api_key, "steamids": ids}) as r:
                r.raise_for_status()
                data = await r.json(content_type=None)
            for p in data.get("response", {}).get("players", []):
                out[int(p["steamid"]) - STEAM64_OFFSET] = (p.get("personaname"), p.get("avatarfull"))
    return out
