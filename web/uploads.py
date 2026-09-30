"""Files people upload to the website (team logos), kept on the data volume
next to the database and served at /uploads/."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

from PIL import Image

UPLOAD_DIR = Path(os.environ.get("SITE_UPLOAD_DIR")
                  or ("/data/uploads" if os.path.isdir("/data") else Path(__file__).resolve().parent.parent / "uploads"))
URL_PREFIX = "/uploads/"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
LOGO_SIZE = 512                      # longest side, pixels
_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}
Image.MAX_IMAGE_PIXELS = 40_000_000  # refuse decompression bombs


class BadImage(ValueError):
    """The upload isn't an image we accept; the message says why."""


def save_team_logo(data: bytes, season_id: int, division_id: int, captain: str) -> str:
    """Check, shrink and store a team logo; returns its URL path. The image
    is re-encoded as PNG, so nothing but pixels survives (no metadata)."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise BadImage("That file is over 5 MB.")
    try:
        probe = Image.open(io.BytesIO(data))
        fmt = probe.format
        probe.verify()
        img = Image.open(io.BytesIO(data))
        img.seek(0)                     # first frame of an animation
        img = img.convert("RGBA")
    except Exception:
        raise BadImage("That doesn't look like an image.")
    if fmt not in _FORMATS:
        raise BadImage("Use a PNG, JPEG, WebP or GIF image.")
    img.thumbnail((LOGO_SIZE, LOGO_SIZE))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    png = out.getvalue()
    team_key = hashlib.sha1(captain.strip().lower().encode()).hexdigest()[:10]
    name = f"team-{season_id}-{division_id}-{team_key}-{hashlib.sha1(png).hexdigest()[:10]}.png"
    folder = UPLOAD_DIR / "team-logos"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(png)
    return f"{URL_PREFIX}team-logos/{name}"


def delete_upload(url: str | None) -> None:
    """Remove a file this module stored, given its URL path. Anything else
    (an external logo URL, say) is left alone."""
    if not url or not url.startswith(URL_PREFIX):
        return
    path = (UPLOAD_DIR / url[len(URL_PREFIX):]).resolve()
    if UPLOAD_DIR.resolve() in path.parents and path.is_file():
        path.unlink()
