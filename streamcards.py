"""
Static PNG cards for stream graphics — player and team variants.

Each entry point returns a BytesIO of a PNG, ready to attach to a Discord
message. The rendering avoids any network calls — callers are expected to
have already fetched the player/team data and (optionally) the avatar bytes.
"""

import io
import logging
from typing import Iterable

import aiohttp
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


# --- Palette ---------------------------------------------------------------
BG_COLOR        = (16, 18, 28, 255)        # near-black with a hint of blue
PANEL_COLOR     = (24, 28, 42, 255)
ACCENT_COLOR    = (255, 195, 60, 255)      # gold
ACCENT_WIN      = (96, 200, 120, 255)      # green
ACCENT_LOSS     = (220, 90, 90, 255)       # red
TEXT_PRIMARY    = (235, 235, 240, 255)
TEXT_SECONDARY  = (160, 165, 180, 255)
TEXT_MUTED      = (110, 115, 130, 255)
LINE_COLOR      = (38, 42, 58, 255)


def _try_load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


async def fetch_avatar(url: str | None, *, size: int = 160) -> Image.Image | None:
    """Best-effort image download. Returns an RGBA PIL image (max size×size,
    aspect-preserved) or None. Used for both player avatars and team logos."""
    if not url:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                data = await r.read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        img.thumbnail((size, size), Image.LANCZOS)
        return img
    except Exception:
        logger.info("Avatar fetch failed: %s", url, exc_info=False)
        return None


def _circle_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    return mask


def _draw_rounded_rect(draw: ImageDraw.ImageDraw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _truncate(text: str, font, max_width: int, draw) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


# ---------------------------------------------------------------------------
# Player card
# ---------------------------------------------------------------------------

def render_player_card(
    name: str,
    rating: int | None,
    role: str,
    wins: int,
    losses: int,
    games: int,           # kept for API compatibility; not shown
    strengths: list[str],
    weaknesses: list[str],
    avatar: Image.Image | None = None,
    account_id: int | None = None,  # kept for API compatibility; not shown
) -> io.BytesIO:
    """Render a player card. Strengths/weaknesses are pre-formatted one-liners
    (e.g., "KDA: 4.32 (2/35)"). Pass at most 3 of each."""
    W, H = 900, 520

    img  = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    pad = 28
    avatar_size = 180

    # --- Avatar (circle) ---
    if avatar is not None:
        a = avatar.resize((avatar_size, avatar_size), Image.LANCZOS)
        img.paste(a, (pad, pad), _circle_mask(avatar_size))
    else:
        draw.ellipse(
            (pad, pad, pad + avatar_size, pad + avatar_size),
            fill=PANEL_COLOR, outline=LINE_COLOR, width=2,
        )

    # --- Right-of-avatar header ---
    name_x = pad + avatar_size + pad
    f_name   = _try_load_font(48, bold=True)
    f_rating = _try_load_font(26, bold=True)
    f_meta   = _try_load_font(28)
    f_label  = _try_load_font(24)

    name_text = _truncate(name, f_name, W - name_x - pad, draw)
    draw.text((name_x, pad), name_text, font=f_name, fill=TEXT_PRIMARY)

    rating_y = pad + 70
    if rating is not None:
        draw.text((name_x, rating_y), f"Adjusted Windrun {rating}", font=f_rating, fill=ACCENT_COLOR)

    meta_y = rating_y + 56
    record_text = f"{wins}W–{losses}L · {role}"
    draw.text((name_x, meta_y), record_text, font=f_meta, fill=TEXT_SECONDARY)

    # --- Divider ---
    div_y = pad + avatar_size + pad
    draw.line((pad, div_y, W - pad, div_y), fill=LINE_COLOR, width=2)

    # --- Strengths / Weaknesses columns ---
    col_top = div_y + 24
    col_w   = (W - pad * 3) // 2

    def _draw_column(x, title, lines, color):
        f_col_title = _try_load_font(28, bold=True)
        draw.text((x, col_top), title, font=f_col_title, fill=color)
        y = col_top + 42
        for line in lines[:3]:
            line = _truncate(line, f_label, col_w, draw)
            draw.text((x, y), "• " + line, font=f_label, fill=TEXT_PRIMARY)
            y += 36

    _draw_column(pad,                     "▲ STRENGTHS",  strengths,  ACCENT_WIN)
    _draw_column(pad + col_w + pad,       "▼ WEAKNESSES", weaknesses, ACCENT_LOSS)

    _paste_logo(img, "assets/rd2l_logo.png", corner="br", pad=pad, size=108)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Head-to-head card
# ---------------------------------------------------------------------------

# Stats compared in the head-to-head table. Each entry: (label, key, fmt).
# Higher is always better here — we use Sum / KDA semantics from team_match_aggregates.
H2H_STATS: list[tuple[str, str, callable]] = [
    ("KDA",            "kda",                     lambda v: f"{v:.2f}"),
    ("GPM",            "gpm",                     lambda v: f"{v:,.0f}"),
    ("Hero Dmg/Min",   "hero_damage_per_min",     lambda v: f"{v:,.0f}"),
    ("Hero Heal/Min",  "hero_healing_per_min",    lambda v: f"{v:,.0f}"),
    ("Stuns/Min",      "stuns_per_min",           lambda v: f"{v:.2f}"),
    ("Teamfight %",    "teamfight_participation", lambda v: f"{v*100:.0f}%"),
    ("Rosh / Game",    "roshans_killed",          lambda v: f"{v:.1f}"),
    ("Obs Kills/Min",  "observer_kills_per_min",  lambda v: f"{v:.2f}"),
]


_PER_MIN_KEYS = {
    "hero_damage_per_min":   "hero_damage",
    "hero_healing_per_min":  "hero_healing",
    "observer_kills_per_min": "observer_kills",
}


def _h2h_value(agg: dict, key: str) -> float | None:
    """Derived getter — per-minute rates aren't stored directly; compute from
    the team total + avg_duration on the fly."""
    if key in _PER_MIN_KEYS:
        dur = agg.get("avg_duration") or 0
        if dur <= 0:
            return None
        return (agg.get(_PER_MIN_KEYS[key]) or 0) / (dur / 60.0)
    return agg.get(key)


def render_h2h_card(
    team_a_label: str,
    team_a_record: tuple[int, int],
    team_a_members: list[dict],
    team_a_agg: dict,
    team_b_label: str,
    team_b_record: tuple[int, int],
    team_b_members: list[dict],
    team_b_agg: dict,
    history: list[dict] | None = None,
    team_a_logo: Image.Image | None = None,
    team_b_logo: Image.Image | None = None,
) -> io.BytesIO:
    W, H = 1400, 880
    img  = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)
    pad = 32

    # Fonts (all +2 from prior pass)
    f_team    = _try_load_font(48, bold=True)
    f_record  = _try_load_font(30, bold=True)
    f_name    = _try_load_font(18, bold=True)
    f_cap     = _try_load_font(13, bold=True)
    f_sec_h   = _try_load_font(32, bold=True)
    f_stat_l  = _try_load_font(28, bold=True)
    f_stat_v  = _try_load_font(30, bold=True)
    f_hist    = _try_load_font(24)
    f_score   = _try_load_font(28, bold=True)

    # ------------------------------------------------------------------
    # 1. Top header band (full width): team logos + names + W-L
    # ------------------------------------------------------------------
    header_mid_x = W // 2

    # 3-div header: [W-L | Team A logo] [VS] [Team B logo | W-L]
    # W-L sits beside the logo (inline), so the header doesn't push the
    # main content down past the logo's height.
    vs_text = "VS"
    middle_div_w = 140
    side_div_w = (W - pad * 2 - middle_div_w) // 2
    left_div_cx  = pad + side_div_w // 2
    right_div_cx = W - pad - side_div_w // 2

    LOGO_SIZE = 130
    GAP = 24

    aw, al = team_a_record
    bw, bl = team_b_record
    a_rec = f"{aw}W–{al}L"
    b_rec = f"{bw}W–{bl}L"
    arw = int(draw.textlength(a_rec, font=f_record))
    brw = int(draw.textlength(b_rec, font=f_record))
    rec_h = 36  # approximate text height for f_record

    def _scaled_logo(logo: Image.Image | None) -> Image.Image | None:
        if logo is None:
            return None
        s = logo.copy()
        s.thumbnail((LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)
        return s

    la = _scaled_logo(team_a_logo)
    lb = _scaled_logo(team_b_logo)
    a_logo_w = la.width  if la else 0
    a_logo_h = la.height if la else 0
    b_logo_w = lb.width  if lb else 0
    b_logo_h = lb.height if lb else 0

    # Left side: [W-L][gap][logo], centered together in the left div.
    left_inline_w = arw + (GAP if la else 0) + a_logo_w
    left_x = int(left_div_cx - left_inline_w // 2)
    block_h_a = max(a_logo_h, rec_h)
    a_rec_y = int(pad + (block_h_a - rec_h) // 2)
    draw.text((left_x, a_rec_y), a_rec, font=f_record, fill=ACCENT_COLOR)
    if la:
        a_logo_x = left_x + arw + GAP
        a_logo_y = int(pad + (block_h_a - a_logo_h) // 2)
        img.paste(la, (a_logo_x, a_logo_y), la if la.mode == "RGBA" else None)

    # Right side: [logo][gap][W-L], centered together in the right div.
    right_inline_w = b_logo_w + (GAP if lb else 0) + brw
    right_x = int(right_div_cx - right_inline_w // 2)
    block_h_b = max(b_logo_h, rec_h)
    if lb:
        b_logo_y = int(pad + (block_h_b - b_logo_h) // 2)
        img.paste(lb, (right_x, b_logo_y), lb if lb.mode == "RGBA" else None)
    b_rec_x = right_x + b_logo_w + (GAP if lb else 0)
    b_rec_y = int(pad + (block_h_b - rec_h) // 2)
    draw.text((b_rec_x, b_rec_y), b_rec, font=f_record, fill=ACCENT_COLOR)

    # VS, vertically centered to the taller block.
    vs_w = draw.textlength(vs_text, font=f_team)
    deeper_h = max(block_h_a, block_h_b, LOGO_SIZE)
    vs_y = int(pad + (deeper_h - 48) // 2)
    draw.text((header_mid_x - int(vs_w) // 2, vs_y), vs_text, font=f_team, fill=ACCENT_COLOR)

    header_bottom = pad + deeper_h + 12

    draw.line((pad, header_bottom, W - pad, header_bottom), fill=LINE_COLOR, width=2)

    # ------------------------------------------------------------------
    # 2. Middle section: sidebars (rosters) + central content
    # ------------------------------------------------------------------
    middle_top = header_bottom + 16
    footer_h   = 60
    middle_bot = H - footer_h
    sidebar_w  = 200
    avatar_size = 88
    slot_h     = avatar_size + 44   # avatar + name, with extra vertical breathing room
    n_slots    = 5
    column_total_h = slot_h * n_slots
    slot_y0    = middle_top + (middle_bot - middle_top - column_total_h) // 2

    def _draw_sidebar(members: list[dict], x_center: int):
        roster = sorted(members[:5], key=lambda m: m.get("role_position") or 99)
        for i, m in enumerate(roster):
            cx = x_center
            cy = slot_y0 + i * slot_h + avatar_size // 2
            top = cy - avatar_size // 2
            left = cx - avatar_size // 2
            av = m.get("avatar")
            if av is not None:
                a = av.resize((avatar_size, avatar_size), Image.LANCZOS)
                img.paste(a, (left, top), _circle_mask(avatar_size))
            else:
                draw.ellipse(
                    (left, top, left + avatar_size, top + avatar_size),
                    fill=PANEL_COLOR, outline=LINE_COLOR, width=2,
                )
            if m.get("is_captain"):
                draw.ellipse(
                    (left - 3, top - 3, left + avatar_size + 3, top + avatar_size + 3),
                    outline=ACCENT_COLOR, width=3,
                )
            # Name (role label intentionally omitted per design)
            name = _truncate(m.get("name") or "—", f_name, sidebar_w - 16, draw)
            nw = draw.textlength(name, font=f_name)
            draw.text((cx - nw // 2, top + avatar_size + 4), name,
                      font=f_name, fill=TEXT_PRIMARY)

    _draw_sidebar(team_a_members, pad + sidebar_w // 2)
    _draw_sidebar(team_b_members, W - pad - sidebar_w // 2)

    # Sidebar divider lines
    div_x_a = pad + sidebar_w + 12
    div_x_b = W - pad - sidebar_w - 12
    draw.line((div_x_a, middle_top, div_x_a, middle_bot - 8), fill=LINE_COLOR, width=1)
    draw.line((div_x_b, middle_top, div_x_b, middle_bot - 8), fill=LINE_COLOR, width=1)

    # ------------------------------------------------------------------
    # 3. Central content (between sidebars): H2H stats + history
    # ------------------------------------------------------------------
    inner_left  = div_x_a + 24
    inner_right = div_x_b - 24
    inner_w     = inner_right - inner_left
    inner_mid_x = (inner_left + inner_right) // 2

    cur_y = middle_top + 20

    # HEAD TO HEAD title (extra breathing room below)
    title = "HEAD TO HEAD"
    tw = draw.textlength(title, font=f_sec_h)
    draw.text((inner_mid_x - tw // 2, cur_y), title, font=f_sec_h, fill=ACCENT_COLOR)
    cur_y += 60

    # Value column anchors: spread well wide of the center label.
    value_offset = 220
    a_x_right = inner_mid_x - value_offset
    b_x_left  = inner_mid_x + value_offset

    # Stat rows
    row_h = 42
    a_wins_count = 0
    for label, key, fmt in H2H_STATS:
        av = _h2h_value(team_a_agg, key)
        bv = _h2h_value(team_b_agg, key)
        a_str = fmt(av) if av is not None else "—"
        b_str = fmt(bv) if bv is not None else "—"
        # Treat as tied when the rendered strings match (handles "59%" vs "59%"
        # even if the underlying floats differ in trailing decimals).
        if a_str == b_str:
            a_color = b_color = ACCENT_COLOR
        else:
            a_better = (av is not None and bv is not None and av > bv)
            b_better = (av is not None and bv is not None and bv > av)
            if a_better:
                a_wins_count += 1
            a_color = ACCENT_WIN if a_better else TEXT_PRIMARY
            b_color = ACCENT_WIN if b_better else TEXT_PRIMARY

        # Label centered
        lw = draw.textlength(label, font=f_stat_l)
        draw.text((inner_mid_x - lw // 2, cur_y), label, font=f_stat_l, fill=TEXT_SECONDARY)

        # Value A right-aligned at a_x_right
        avw = draw.textlength(a_str, font=f_stat_v)
        draw.text((a_x_right - avw, cur_y - 2), a_str, font=f_stat_v, fill=a_color)

        # Value B left-aligned at b_x_left
        draw.text((b_x_left, cur_y - 2), b_str, font=f_stat_v, fill=b_color)

        cur_y += row_h

    cur_y += 36
    # Section divider line inside inner column
    draw.line((inner_left, cur_y, inner_right, cur_y), fill=LINE_COLOR, width=1)
    cur_y += 36

    # PREVIOUS MATCHUPS (extra breathing room below)
    h_title = "PREVIOUS MATCHUPS"
    hw = draw.textlength(h_title, font=f_sec_h)
    draw.text((inner_mid_x - hw // 2, cur_y), h_title, font=f_sec_h, fill=ACCENT_COLOR)
    cur_y += 56

    if history:
        from datetime import datetime as _dt, timezone as _tz
        a_h_wins = sum(1 for h in history if h.get("a_won"))
        b_h_wins = len(history) - a_h_wins
        dates: list[str] = []
        for h in history:
            ts = h.get("start_time", 0)
            try:
                d = _dt.fromtimestamp(ts, _tz.utc).strftime("%b %d")
                if d not in dates:
                    dates.append(d)
            except Exception:
                pass
        date_str = ", ".join(dates) if dates else ""

        # Build the line piece-by-piece: [logo_a] [score] [logo_b] [· dates]
        INLINE_LOGO = 44
        la = team_a_logo.copy() if team_a_logo else None
        lb = team_b_logo.copy() if team_b_logo else None
        if la: la.thumbnail((INLINE_LOGO, INLINE_LOGO), Image.LANCZOS)
        if lb: lb.thumbnail((INLINE_LOGO, INLINE_LOGO), Image.LANCZOS)
        la_w = la.width if la else 0
        lb_w = lb.width if lb else 0

        score_str = f" {a_h_wins}–{b_h_wins} "
        dates_str = f" · {date_str}" if date_str else ""

        # textlength returns float on newer Pillow — paste needs int coords.
        score_w = int(draw.textlength(score_str, font=f_score))
        dates_w = int(draw.textlength(dates_str, font=f_score)) if dates_str else 0
        total_w = la_w + score_w + lb_w + dates_w

        x = int(inner_mid_x - total_w // 2)
        # Vertically center logos to the text's mid-height.
        text_h_approx = 30
        max_logo_h = max(la.height if la else 0, lb.height if lb else 0)
        logo_y_offset = -(max_logo_h - text_h_approx) // 2

        if la:
            img.paste(la, (x, int(cur_y + logo_y_offset)), la if la.mode == "RGBA" else None)
        x += la_w
        draw.text((x, cur_y), score_str, font=f_score, fill=TEXT_PRIMARY)
        x += score_w
        if lb:
            img.paste(lb, (x, int(cur_y + logo_y_offset)), lb if lb.mode == "RGBA" else None)
        x += lb_w
        if dates_str:
            draw.text((x, cur_y), dates_str, font=f_score, fill=TEXT_SECONDARY)
    else:
        note = "No previous matchups this season"
        nw = draw.textlength(note, font=f_hist)
        draw.text((inner_mid_x - nw // 2, cur_y), note, font=f_hist, fill=TEXT_MUTED)

    # ------------------------------------------------------------------
    # 4. Footer: logo only
    # ------------------------------------------------------------------
    _paste_logo(img, "assets/rd2l_logo.png", corner="bm", pad=pad, size=135)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Player head-to-head card
# ---------------------------------------------------------------------------

# Candidate stat pool for player-vs-player comparison. We pick the 8 with the
# largest z-diff at render time so the card auto-adapts to whichever pair is
# being compared (carry-vs-carry looks different from support-vs-support).
# Per-min derivations happen in _player_stat_value below.
H2H_PLAYER_CANDIDATE_STATS: list[tuple[str, str, callable]] = [
    ("KDA",              "kda",                     lambda v: f"{v:.2f}"),
    ("GPM",              "gpm",                     lambda v: f"{v:,.0f}"),
    ("XPM",              "xpm",                     lambda v: f"{v:,.0f}"),
    ("Last Hits/Game",   "last_hits",               lambda v: f"{v:,.0f}"),
    ("Denies/Game",      "denies",                  lambda v: f"{v:.1f}"),
    ("Hero Dmg/Min",     "hero_damage_per_min",     lambda v: f"{v:,.0f}"),
    ("Hero Heal/Min",    "hero_healing_per_min",    lambda v: f"{v:,.0f}"),
    ("Tower Dmg/Game",   "building_damage",         lambda v: f"{v:,.0f}"),
    ("Obs Kills/Min",    "observer_kills_per_min",  lambda v: f"{v:.2f}"),
    ("Rosh/Game",        "roshans_killed",          lambda v: f"{v:.2f}"),
    ("Camps/Game",       "camps_stacked",           lambda v: f"{v:.1f}"),
    ("Rune Pickups",     "rune_pickups",            lambda v: f"{v:.1f}"),
    ("Teamfight %",      "teamfight_participation", lambda v: f"{v*100:.0f}%"),
    ("Stuns/Min",        "stuns_per_min",           lambda v: f"{v:.2f}"),
    ("Def Item Uses",    "defensive_item_uses",     lambda v: f"{v:.1f}"),
    ("Tormentors",       "tormentor_kills",         lambda v: f"{v:.2f}"),
    ("Watcher Caps",     "watcher_captures",        lambda v: f"{v:.2f}"),
    ("Fantasy Pts/Game", "fantasy_points",          lambda v: f"{v:.1f}"),
]


_PLAYER_PER_MIN_KEYS = {
    "hero_damage_per_min":  "hero_damage",
    "hero_healing_per_min": "hero_healing",
}


def _player_stat_value(row: dict, key: str) -> float | None:
    """Fetch a stat by key from a get_all_time_stats row, computing per-minute
    derivations from the raw AVG values on the fly."""
    if key in _PLAYER_PER_MIN_KEYS:
        dur = row.get("avg_duration") or 0
        if dur <= 0:
            return None
        raw = row.get(_PLAYER_PER_MIN_KEYS[key])
        if raw is None:
            return None
        return raw / (dur / 60.0)
    v = row.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_h2h_stat_rows(
    player_a: dict,
    player_b: dict,
    pool: list[dict],
    top_n: int = 8,
) -> list[dict]:
    """Rank candidate stats by |z_a - z_b| across the population `pool` and
    return the top N as [{label, a, b, a_str, b_str, a_better, b_better}].
    Stats missing for either player, or with zero variance in the pool, are
    skipped."""
    import math

    ranked: list[tuple[float, dict]] = []
    for label, key, fmt in H2H_PLAYER_CANDIDATE_STATS:
        av = _player_stat_value(player_a, key)
        bv = _player_stat_value(player_b, key)
        if av is None or bv is None:
            continue

        vals = [_player_stat_value(p, key) for p in pool]
        vals = [v for v in vals if v is not None]
        if len(vals) < 2:
            continue
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        std = math.sqrt(var)
        if std <= 0:
            continue

        za = (av - mean) / std
        zb = (bv - mean) / std
        gap = abs(za - zb)

        a_str = fmt(av)
        b_str = fmt(bv)
        if a_str == b_str:
            a_better = b_better = False
        else:
            a_better = av > bv
            b_better = bv > av

        ranked.append((gap, {
            "label":    label,
            "key":      key,
            "a":        av,
            "b":        bv,
            "a_str":    a_str,
            "b_str":    b_str,
            "a_better": a_better,
            "b_better": b_better,
        }))

    ranked.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in ranked[:top_n]]


def render_h2h_player_card(
    name_a: str,
    rating_a: int | None,
    role_a: str,
    wins_a: int,
    losses_a: int,
    avatar_a: Image.Image | None,
    name_b: str,
    rating_b: int | None,
    role_b: str,
    wins_b: int,
    losses_b: int,
    avatar_b: Image.Image | None,
    stat_rows: list[dict],
) -> io.BytesIO:
    """Render a player-vs-player card. `stat_rows` is the output of
    compute_h2h_stat_rows — expected length 8."""
    W, H = 1300, 900
    img  = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)
    pad  = 32

    f_vs      = _try_load_font(56, bold=True)
    f_name    = _try_load_font(36, bold=True)
    f_rating  = _try_load_font(22, bold=True)
    f_meta    = _try_load_font(22)
    f_sec_h   = _try_load_font(32, bold=True)
    f_stat_l  = _try_load_font(28, bold=True)
    f_stat_v  = _try_load_font(30, bold=True)

    # ------------------------------------------------------------------
    # 1. Top header band: [Avatar A | name/rating] [VS] [name/rating | Avatar B]
    # ------------------------------------------------------------------
    avatar_size = 160
    header_y    = pad
    header_h    = avatar_size + 12
    header_bot  = header_y + header_h

    def _draw_player_panel(name, rating, role, wins, losses, avatar,
                           avatar_x, text_x, text_align):
        # Avatar (circle)
        top = header_y
        left = avatar_x
        if avatar is not None:
            a = avatar.resize((avatar_size, avatar_size), Image.LANCZOS)
            img.paste(a, (left, top), _circle_mask(avatar_size))
        else:
            draw.ellipse(
                (left, top, left + avatar_size, top + avatar_size),
                fill=PANEL_COLOR, outline=LINE_COLOR, width=2,
            )

        # Name (truncate to available width)
        max_text_w = W // 2 - pad - avatar_size - 32
        name_str = _truncate(name, f_name, max_text_w, draw)
        nw = draw.textlength(name_str, font=f_name)
        rating_str = f"Adjusted Windrun {rating}" if rating is not None else ""
        rw = draw.textlength(rating_str, font=f_rating) if rating_str else 0
        record_str = f"{wins}W–{losses}L · {role}" if role else f"{wins}W–{losses}L"
        recw = draw.textlength(record_str, font=f_meta)

        # Vertical stack, tight against avatar edge
        line_h_name    = 44
        line_h_rating  = 30
        line_h_record  = 30
        stack_h = line_h_name + (line_h_rating if rating_str else 0) + line_h_record
        y = top + (avatar_size - stack_h) // 2

        if text_align == "left":
            xn = text_x
            xr = text_x
            xrec = text_x
        else:
            xn   = text_x - int(nw)
            xr   = text_x - int(rw)
            xrec = text_x - int(recw)

        draw.text((xn, y), name_str, font=f_name, fill=TEXT_PRIMARY)
        y += line_h_name
        if rating_str:
            draw.text((xr, y), rating_str, font=f_rating, fill=ACCENT_COLOR)
            y += line_h_rating
        draw.text((xrec, y), record_str, font=f_meta, fill=TEXT_SECONDARY)

    # Left player: avatar on the far left, text to its right
    _draw_player_panel(
        name_a, rating_a, role_a, wins_a, losses_a, avatar_a,
        avatar_x=pad,
        text_x=pad + avatar_size + 20,
        text_align="left",
    )
    # Right player: avatar on the far right, text to its left (right-aligned)
    _draw_player_panel(
        name_b, rating_b, role_b, wins_b, losses_b, avatar_b,
        avatar_x=W - pad - avatar_size,
        text_x=W - pad - avatar_size - 20,
        text_align="right",
    )

    # VS in the center of the header band
    vs_text = "VS"
    vw = draw.textlength(vs_text, font=f_vs)
    vs_y = header_y + (avatar_size - 56) // 2
    draw.text((W // 2 - int(vw) // 2, vs_y), vs_text, font=f_vs, fill=ACCENT_COLOR)

    draw.line((pad, header_bot + 4, W - pad, header_bot + 4), fill=LINE_COLOR, width=2)

    # ------------------------------------------------------------------
    # 2. Stat table
    # ------------------------------------------------------------------
    cur_y = header_bot + 40

    title = "HEAD TO HEAD"
    tw = draw.textlength(title, font=f_sec_h)
    draw.text((W // 2 - int(tw) // 2, cur_y), title, font=f_sec_h, fill=ACCENT_COLOR)
    cur_y += 60

    mid_x = W // 2
    value_offset = 260
    a_x_right = mid_x - value_offset  # A values right-aligned to this x
    b_x_left  = mid_x + value_offset  # B values left-aligned from this x
    row_h = 60

    for row in stat_rows:
        label = row["label"]
        a_str = row["a_str"]
        b_str = row["b_str"]

        if row["a_better"]:
            a_color, b_color = ACCENT_WIN, TEXT_PRIMARY
        elif row["b_better"]:
            a_color, b_color = TEXT_PRIMARY, ACCENT_WIN
        else:
            a_color = b_color = ACCENT_COLOR

        lw = draw.textlength(label, font=f_stat_l)
        draw.text((mid_x - int(lw) // 2, cur_y), label, font=f_stat_l, fill=TEXT_SECONDARY)

        avw = draw.textlength(a_str, font=f_stat_v)
        draw.text((a_x_right - int(avw), cur_y - 2), a_str, font=f_stat_v, fill=a_color)
        draw.text((b_x_left, cur_y - 2), b_str, font=f_stat_v, fill=b_color)

        cur_y += row_h

    _paste_logo(img, "assets/rd2l_logo.png", corner="bm", pad=pad, size=110)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


def _paste_logo(img: Image.Image, path: str, *, corner: str = "br", pad: int = 28, size: int = 72) -> None:
    """Drop a logo PNG into a corner. Silently skipped if the file is missing."""
    try:
        import os
        if not os.path.exists(path):
            return
        logo = Image.open(path).convert("RGBA")
        # Fit within size×size, preserve aspect.
        logo.thumbnail((size, size), Image.LANCZOS)
        W, H = img.size
        if corner == "br":
            x = W - pad - logo.width
            y = H - pad - logo.height
        elif corner == "bl":
            x, y = pad, H - pad - logo.height
        elif corner == "tr":
            x, y = W - pad - logo.width, pad
        elif corner == "bm":   # bottom-middle
            x = (W - logo.width) // 2
            y = H - pad - logo.height
        else:
            x, y = pad, pad
        img.paste(logo, (x, y), logo)
    except Exception:
        logger.info("Logo paste failed for %s", path, exc_info=False)


# ---------------------------------------------------------------------------
# Team card
# ---------------------------------------------------------------------------

def render_team_card(
    team_label: str,
    members: list[dict],   # each: {name, avatar (Image|None), is_captain}
    wins: int,
    losses: int,
    games: int,
    strengths: list[str],
    weaknesses: list[str],
    avg_duration: float | None = None,
    team_logo: Image.Image | None = None,
) -> io.BytesIO:
    """Render a team card with 5 player avatars in a row, W-L, and stats.

    Each `members` entry should include `role_position` (1..5 or None) so we
    can sort left-to-right by role. Members with unknown role go last.
    """
    W, H = 1000, 620
    img  = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)
    pad = 28

    # Sort roster left → right by role_position (1..5).
    def _role_key(m):
        rp = m.get("role_position")
        return rp if isinstance(rp, int) and rp > 0 else 99
    members = sorted(members[:5], key=_role_key)

    # --- Title block: [logo] [team name + W-L stacked], centered together ---
    f_record = _try_load_font(22, bold=True)
    f_label  = _try_load_font(22)

    LOGO_SIZE = 90
    GAP = 18
    logo = None
    if team_logo is not None:
        logo = team_logo.copy()
        logo.thumbnail((LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)
    logo_w = logo.width  if logo else 0
    logo_h = logo.height if logo else 0

    available_w = W - pad * 2 - logo_w - (GAP if logo else 0)
    # Try font sizes from 46 down to 28 to find one that fits the team name.
    name_font = None
    name_w = 0
    for fs in (46, 42, 38, 34, 30, 28):
        f = _try_load_font(fs, bold=True)
        w = int(draw.textlength(team_label, font=f))
        if w <= available_w:
            name_font, name_w = f, w
            break
    if name_font is None:
        name_font = _try_load_font(28, bold=True)
        team_label = _truncate(team_label, name_font, available_w, draw)
        name_w = int(draw.textlength(team_label, font=name_font))
    name_h_approx = name_font.size if hasattr(name_font, "size") else 32

    if avg_duration:
        mm = int(avg_duration) // 60
        ss = int(avg_duration) % 60
        record_text = f"{wins}W–{losses}L · {mm}:{ss:02d} avg game length"
    else:
        record_text = f"{wins}W–{losses}L"
    rw = int(draw.textlength(record_text, font=f_record))
    rec_h_approx = 22

    # Stacked text-block height = name + small gap + record line.
    text_block_h = name_h_approx + 6 + rec_h_approx
    block_h = max(logo_h, text_block_h)
    total_w = logo_w + (GAP if logo else 0) + max(name_w, rw)
    block_left = int((W - total_w) // 2)

    if logo:
        logo_y = int(pad + (block_h - logo_h) // 2)
        img.paste(logo, (block_left, logo_y), logo if logo.mode == "RGBA" else None)
    text_left = block_left + logo_w + (GAP if logo else 0)
    text_top = int(pad + (block_h - text_block_h) // 2)
    draw.text((text_left, text_top), team_label, font=name_font, fill=TEXT_PRIMARY)
    draw.text((text_left, text_top + name_h_approx + 6), record_text,
              font=f_record, fill=ACCENT_COLOR)

    # Used below to position the roster row.
    record_y = pad + block_h

    # --- Roster row ---
    avatar_size = 130
    label_band  = 60   # space below avatar for name + role
    slot_w      = avatar_size + 24
    n           = max(1, len(members))
    row_total_w = slot_w * n - 24
    row_x0      = max(pad, (W - row_total_w) // 2)
    row_y       = record_y + 40   # below the record line, dynamic to title-block height

    f_name = _try_load_font(18, bold=True)
    f_role = _try_load_font(15)
    f_cap  = _try_load_font(14, bold=True)

    ROLE_SHORT = {
        1: "Safe Lane",
        2: "Mid Lane",
        3: "Off Lane",
        4: "Roamer",
        5: "Hard Support",
    }

    for i, m in enumerate(members[:5]):
        x = row_x0 + i * slot_w
        av = m.get("avatar")
        if av is not None:
            av_resized = av.resize((avatar_size, avatar_size), Image.LANCZOS)
            img.paste(av_resized, (x, row_y), _circle_mask(avatar_size))
        else:
            draw.ellipse(
                (x, row_y, x + avatar_size, row_y + avatar_size),
                fill=PANEL_COLOR, outline=LINE_COLOR, width=2,
            )
        if m.get("is_captain"):
            draw.ellipse(
                (x - 4, row_y - 4, x + avatar_size + 4, row_y + avatar_size + 4),
                outline=ACCENT_COLOR, width=4,
            )
            cap_text = "CAPTAIN"
            cw = draw.textlength(cap_text, font=f_cap)
            draw.text((x + (avatar_size - cw) // 2, row_y - 22), cap_text,
                      font=f_cap, fill=ACCENT_COLOR)
        # Name + role under the avatar
        name = m.get("name") or "—"
        name = _truncate(name, f_name, avatar_size + 8, draw)
        nw = draw.textlength(name, font=f_name)
        draw.text(
            (x + (avatar_size - nw) // 2, row_y + avatar_size + 8),
            name, font=f_name, fill=TEXT_PRIMARY,
        )
        role_text = ROLE_SHORT.get(m.get("role_position"), "")
        if role_text:
            rrw = draw.textlength(role_text, font=f_role)
            draw.text(
                (x + (avatar_size - rrw) // 2, row_y + avatar_size + 32),
                role_text, font=f_role, fill=TEXT_SECONDARY,
            )

    # --- Divider ---
    div_y = row_y + avatar_size + label_band + 16
    draw.line((pad, div_y, W - pad, div_y), fill=LINE_COLOR, width=2)

    # --- Strengths / Weaknesses columns (bumped fonts) ---
    col_top = div_y + 22
    col_w   = (W - pad * 3) // 2

    def _draw_column(x, title, lines, color):
        f_col_title = _try_load_font(26, bold=True)
        draw.text((x, col_top), title, font=f_col_title, fill=color)
        y = col_top + 40
        for line in lines[:3]:
            line = _truncate(line, f_label, col_w, draw)
            draw.text((x, y), "• " + line, font=f_label, fill=TEXT_PRIMARY)
            y += 34

    _draw_column(pad,                     "▲ STRENGTHS",  strengths,  ACCENT_WIN)
    _draw_column(pad + col_w + pad,       "▼ WEAKNESSES", weaknesses, ACCENT_LOSS)

    _paste_logo(img, "assets/rd2l_logo.png", corner="br", pad=pad, size=120)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
