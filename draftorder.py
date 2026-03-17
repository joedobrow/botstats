"""
Ability Draft order visualization.

Parses windrun.io match data, resolves ability/hero icons via OpenDota
constants + Dota 2 CDN, and generates a composite image showing the
snake-draft pick order for all 10 seats.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from io import BytesIO
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPENDOTA_BASE = "https://api.opendota.com/api/constants"
CDN_BASE = "https://cdn.cloudflare.steamstatic.com"

ICON_SIZE = 48
ICON_PAD = 4
NAME_WIDTH = 120
ROW_HEIGHT = ICON_SIZE + ICON_PAD * 2
DIVIDER_WIDTH = 40
PICKS_PER_PLAYER = 5

# Colours
BG_COLOUR = (30, 30, 36)
RADIANT_ACCENT = (60, 120, 60)
DIRE_ACCENT = (120, 50, 50)
HERO_BORDER_COLOUR = (218, 165, 32)  # gold
TEXT_COLOUR = (220, 220, 220)
DIVIDER_COLOUR = (80, 80, 90)
HEADER_COLOUR = (180, 180, 190)

CACHE_DIR = Path(__file__).parent / ".icon_cache"

# ---------------------------------------------------------------------------
# OpenDota constants cache (populated on first use)
# ---------------------------------------------------------------------------

_ability_ids: dict[str, str] = {}   # "5462" -> "nyx_assassin_impale"
_heroes: dict[str, dict] = {}       # "5" -> {name, img, icon, ...}
_constants_loaded = False
_constants_lock = asyncio.Lock()


async def _load_constants() -> None:
    """Fetch OpenDota ability_ids and heroes constants, cache in memory."""
    global _ability_ids, _heroes, _constants_loaded

    async with _constants_lock:
        if _constants_loaded:
            return

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{OPENDOTA_BASE}/ability_ids") as resp:
                if resp.status == 200:
                    _ability_ids = await resp.json()
                    logger.info("Loaded %d ability ID mappings", len(_ability_ids))
                else:
                    logger.warning("Failed to load ability_ids: %d", resp.status)

            async with session.get(f"{OPENDOTA_BASE}/heroes") as resp:
                if resp.status == 200:
                    _heroes = await resp.json()
                    logger.info("Loaded %d hero mappings", len(_heroes))
                else:
                    logger.warning("Failed to load heroes: %d", resp.status)

        _constants_loaded = True


def _ability_icon_url(ability_id: int) -> str | None:
    """Return CDN URL for an ability icon, or None if unknown."""
    name = _ability_ids.get(str(ability_id))
    if not name:
        return None
    return f"{CDN_BASE}/apps/dota2/images/dota_react/abilities/{name}.png"


def _hero_icon_url(hero_id: int) -> str | None:
    """Return CDN URL for a hero portrait icon, or None if unknown."""
    hero = _heroes.get(str(hero_id))
    if not hero:
        return None
    # Use the icon field (small portrait), fallback to img
    icon_path = hero.get("icon") or hero.get("img")
    if not icon_path:
        return None
    return f"{CDN_BASE}{icon_path}"


def _icon_url_for_pick(ability_id: int) -> tuple[str | None, bool]:
    """
    Return (icon_url, is_hero) for a draft pick.

    Negative ability IDs represent hero model picks.
    """
    if ability_id < 0:
        return _hero_icon_url(abs(ability_id)), True
    return _ability_icon_url(ability_id), False


# ---------------------------------------------------------------------------
# Icon downloading with disk cache
# ---------------------------------------------------------------------------

async def _download_icon(session: aiohttp.ClientSession, url: str) -> Image.Image | None:
    """Download an icon, caching to disk. Returns a PIL Image or None."""
    CACHE_DIR.mkdir(exist_ok=True)

    # Use URL hash as cache filename
    url_hash = hashlib.md5(url.encode()).hexdigest()
    cache_path = CACHE_DIR / f"{url_hash}.png"

    if cache_path.exists():
        try:
            return Image.open(cache_path).convert("RGBA")
        except Exception:
            cache_path.unlink(missing_ok=True)

    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.debug("Icon download failed (%d): %s", resp.status, url)
                return None
            data = await resp.read()

        img = Image.open(BytesIO(data)).convert("RGBA")
        img.save(cache_path, "PNG")
        return img
    except Exception:
        logger.debug("Failed to download icon: %s", url, exc_info=True)
        return None


def _make_placeholder(size: int = ICON_SIZE) -> Image.Image:
    """Create a gray placeholder icon with a '?' mark."""
    img = Image.new("RGBA", (size, size), (60, 60, 70, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size // 2)
    except (OSError, IOError):
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), "?", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) // 2, (size - th) // 2 - 2), "?", fill=(150, 150, 160), font=font)
    return img


# ---------------------------------------------------------------------------
# Draft order parsing
# ---------------------------------------------------------------------------

def _parse_draft_order(match_data: dict) -> list[dict]:
    """
    Parse windrun match data into per-seat draft sequences.

    Returns a list of 10 dicts sorted by seat number:
        {seat, player_name, team, picks: [{ability_id, is_hero, pick_order}]}
    """
    picks = match_data.get("picks", [])
    radiant = match_data.get("radiant", [])
    dire = match_data.get("dire", [])

    # Build lookups: ability_id -> (player_name, team), player_name -> stats
    ability_owner: dict[int, tuple[str, str]] = {}
    player_stats: dict[str, dict] = {}
    for player in radiant:
        for aid in player.get("abilities", []):
            ability_owner[aid] = (player["name"], "radiant")
        player_stats[player["name"]] = {
            "kills": player.get("kills", 0),
            "deaths": player.get("deaths", 0),
            "assists": player.get("assists", 0),
        }
    for player in dire:
        for aid in player.get("abilities", []):
            ability_owner[aid] = (player["name"], "dire")
        player_stats[player["name"]] = {
            "kills": player.get("kills", 0),
            "deaths": player.get("deaths", 0),
            "assists": player.get("assists", 0),
        }

    # Assign picks to players in draft order
    player_picks: dict[str, list[dict]] = {}  # player_name -> list of picks
    player_team: dict[str, str] = {}

    for pick in sorted(picks, key=lambda p: p["pickOrder"]):
        aid = pick["abilityId"]
        owner = ability_owner.get(aid)
        if not owner:
            continue
        name, team = owner
        player_team[name] = team
        if name not in player_picks:
            player_picks[name] = []
        player_picks[name].append({
            "ability_id": aid,
            "is_hero": aid < 0,
            "pick_order": pick["pickOrder"],
        })

    # Determine seat order: seat 1 = whoever picked first, etc.
    # The first 10 picks (round 1) determine seat order
    seat_order: list[str] = []
    seen = set()
    for pick in sorted(picks, key=lambda p: p["pickOrder"]):
        aid = pick["abilityId"]
        owner = ability_owner.get(aid)
        if owner and owner[0] not in seen:
            seat_order.append(owner[0])
            seen.add(owner[0])
        if len(seat_order) == 10:
            break

    result = []
    for seat_num, player_name in enumerate(seat_order, 1):
        stats = player_stats.get(player_name, {})
        result.append({
            "seat": seat_num,
            "player_name": player_name,
            "team": player_team.get(player_name, "radiant"),
            "picks": player_picks.get(player_name, []),
            "kills": stats.get("kills", 0),
            "deaths": stats.get("deaths", 0),
            "assists": stats.get("assists", 0),
        })

    return result


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

def _try_load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try to load a nice font, falling back to default."""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


async def generate_draft_image(match_data: dict) -> BytesIO:
    """
    Generate a draft order visualization image.

    Returns a BytesIO containing a PNG image.
    """
    await _load_constants()

    seats = _parse_draft_order(match_data)
    if len(seats) < 10:
        logger.warning("Only found %d seats (expected 10)", len(seats))

    # Collect all icon URLs we need to download
    all_picks_with_urls: list[tuple[int, str | None, bool]] = []
    for seat in seats:
        for pick in seat["picks"]:
            url, is_hero = _icon_url_for_pick(pick["ability_id"])
            all_picks_with_urls.append((pick["ability_id"], url, is_hero))

    # Download all icons concurrently
    unique_urls = {url for _, url, _ in all_picks_with_urls if url}
    icons: dict[str, Image.Image] = {}

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tasks = {url: asyncio.create_task(_download_icon(session, url)) for url in unique_urls}
        for url, task in tasks.items():
            result = await task
            if result:
                icons[url] = result

    placeholder = _make_placeholder()

    # Calculate image dimensions
    cell_width = NAME_WIDTH + (ICON_SIZE + ICON_PAD) * PICKS_PER_PLAYER + ICON_PAD
    img_width = cell_width * 2 + DIVIDER_WIDTH
    num_rows = 5  # 10 seats in 5 paired rows
    title_height = 40
    img_height = title_height + num_rows * ROW_HEIGHT + ICON_PAD

    img = Image.new("RGBA", (img_width, img_height), BG_COLOUR + (255,))
    draw = ImageDraw.Draw(img)

    font = _try_load_font(14)
    font_small = _try_load_font(13)
    font_title = _try_load_font(18)

    # Title
    match_id = match_data.get("matchId", "?")
    winner = "Radiant" if match_data.get("radiantWin") else "Dire"
    title = f"Match {match_id}, Winner: {winner}"
    draw.text((img_width // 2, 12), title, fill=HEADER_COLOUR, font=font_title, anchor="mt")

    # Draw each row (pair of seats)
    for row_idx in range(num_rows):
        left_seat_idx = row_idx * 2
        right_seat_idx = row_idx * 2 + 1

        y = title_height + row_idx * ROW_HEIGHT

        for side, seat_idx in [("left", left_seat_idx), ("right", right_seat_idx)]:
            if seat_idx >= len(seats):
                continue

            seat = seats[seat_idx]
            team = seat["team"]
            accent = RADIANT_ACCENT if team == "radiant" else DIRE_ACCENT

            if side == "left":
                x_start = 0
            else:
                x_start = cell_width + DIVIDER_WIDTH

            # Team accent bar
            draw.rectangle(
                [x_start, y, x_start + 3, y + ROW_HEIGHT - 1],
                fill=accent,
            )

            # Player name
            name_x = x_start + 8
            name_y = y + (ROW_HEIGHT - 14) // 2
            # Truncate name if too long
            display_name = seat["player_name"]
            if len(display_name) > 12:
                display_name = display_name[:11] + "\u2026"
            draw.text((name_x, name_y), display_name, fill=TEXT_COLOUR, font=font)

            # KDA line
            kda_str = f"{seat['kills']}/{seat['deaths']}/{seat['assists']}"
            draw.text(
                (name_x, name_y - 13),
                kda_str,
                fill=HEADER_COLOUR,
                font=font_small,
            )

            # Draw pick icons
            icon_x = x_start + NAME_WIDTH
            icon_y = y + ICON_PAD

            for pick in seat["picks"]:
                url, is_hero = _icon_url_for_pick(pick["ability_id"])
                icon_img = icons.get(url) if url else None
                if not icon_img:
                    icon_img = placeholder

                # Resize icon
                resized = icon_img.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)

                # Hero model picks get a gold border
                if pick["is_hero"]:
                    border_size = 2
                    bordered = Image.new(
                        "RGBA",
                        (ICON_SIZE + border_size * 2, ICON_SIZE + border_size * 2),
                        HERO_BORDER_COLOUR + (255,),
                    )
                    bordered.paste(resized, (border_size, border_size))
                    # Paste bordered icon (slightly offset to account for border)
                    img.paste(bordered, (icon_x - border_size, icon_y - border_size), bordered)
                else:
                    img.paste(resized, (icon_x, icon_y), resized)

                icon_x += ICON_SIZE + ICON_PAD

        # Divider between left and right seats
        div_x = cell_width + DIVIDER_WIDTH // 2
        draw.line(
            [(div_x, y + 4), (div_x, y + ROW_HEIGHT - 4)],
            fill=DIVIDER_COLOUR,
            width=1,
        )

    # Convert to PNG bytes
    output = BytesIO()
    final = img.convert("RGB")
    final.save(output, "PNG")
    output.seek(0)
    return output
