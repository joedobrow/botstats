"""
replay.py — Dota 2 replay download + Ability Draft pick parsing.

Pipeline for a single match:
  1. Call Steam Web API to get replay_salt (needed to construct the download URL)
  2. Download the .dem.bz2 replay from Valve's CDN to a temp directory
  3. Decompress bz2 → .dem
  4. Subprocess the compiled Go binary (ad-parser) to parse the draft events
  5. Return a list of pick dicts: {order, player_id, ability_id}
  6. Temp directory is deleted automatically when done

The caller is responsible for persisting picks to the DB (see fetcher.py).
"""

import asyncio
import bz2
import json
import logging
import os
import tempfile

import aiohttp

from config import STEAM_API_KEY

logger = logging.getLogger(__name__)

STEAM_API_BASE = "https://api.steampowered.com"
# Valve's replay CDN — cluster number goes in the subdomain
REPLAY_URL_TEMPLATE = "http://replay{cluster}.valve.net/570/{match_id}_{salt}.dem.bz2"

# Where the Go binary lives (set by Dockerfile; override with env var for local testing)
PARSER_BIN = os.environ.get("AD_PARSER_BIN", "/usr/local/bin/ad-parser")


# ---------------------------------------------------------------------------
# Step 1 — get replay salt from Steam
# ---------------------------------------------------------------------------

OPENDOTA_BASE = "https://api.opendota.com/api"


async def _opendota_match_salt(session: aiohttp.ClientSession, match_id: int) -> tuple[int, int] | None:
    """Read replay_salt + cluster from OpenDota's /matches/{id} if populated."""
    try:
        async with session.get(f"{OPENDOTA_BASE}/matches/{match_id}") as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None
    salt = data.get("replay_salt")
    cluster = data.get("cluster")
    if salt and cluster:
        return int(salt), int(cluster)
    return None


async def _opendota_request_parse(session: aiohttp.ClientSession, match_id: int) -> bool:
    """
    POST to /api/request/{match_id} to trigger OpenDota's parser. Free, no
    auth. After this returns, /matches/{id} will have replay_salt populated
    within a few seconds.
    """
    try:
        async with session.post(f"{OPENDOTA_BASE}/request/{match_id}") as resp:
            if resp.status == 200:
                logger.info("OpenDota parse triggered for match %d", match_id)
                return True
            logger.warning("OpenDota /request/%d returned %d", match_id, resp.status)
            return False
    except Exception:
        logger.exception("OpenDota /request/%d failed", match_id)
        return False


async def get_replay_salt(match_id: int) -> tuple[int, int] | None:
    """
    Resolve (replay_salt, cluster) for a match, falling back through
    multiple sources:

      1. Steam Web API GetMatchDetails (fastest when it works, but Valve
         returns 500 for many league/amateur matches).
      2. OpenDota /matches/{id} (may already be populated).
      3. Trigger an OpenDota parse via /request/{match_id}, then re-query
         /matches/{id} — works reliably for matches Steam refuses.

    Returns (salt, cluster) or None if no source has it.
    """
    # --- Try Steam Web API first ---
    if STEAM_API_KEY:
        url = f"{STEAM_API_BASE}/IDOTA2Match_570/GetMatchDetails/v1"
        params = {"key": STEAM_API_KEY, "match_id": match_id}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("result", {})
                        salt = result.get("replay_salt")
                        cluster = result.get("cluster")
                        if salt and cluster:
                            logger.info("Steam API: match %d → salt=%d cluster=%d",
                                        match_id, salt, cluster)
                            return int(salt), int(cluster)
                    else:
                        logger.info("Steam API returned %d for match %d — trying OpenDota",
                                    resp.status, match_id)
        except Exception:
            logger.exception("Steam API request failed for match %d", match_id)

    # --- Fall back to OpenDota ---
    async with aiohttp.ClientSession() as session:
        info = await _opendota_match_salt(session, match_id)
        if info:
            logger.info("OpenDota: match %d → salt=%d cluster=%d (already parsed)",
                        match_id, info[0], info[1])
            return info

        # Trigger a parse and wait briefly for the salt to populate. OpenDota
        # typically backfills replay_salt within ~5s of a parse request.
        if not await _opendota_request_parse(session, match_id):
            return None
        # Poll for salt — usually appears within seconds.
        for delay in (3, 5, 7, 10):
            await asyncio.sleep(delay)
            info = await _opendota_match_salt(session, match_id)
            if info:
                logger.info("OpenDota: match %d → salt=%d cluster=%d (after parse trigger)",
                            match_id, info[0], info[1])
                return info
        logger.warning("OpenDota /request triggered but salt didn't appear for match %d",
                       match_id)
    return None


# ---------------------------------------------------------------------------
# Step 2+3 — download and decompress the replay
# ---------------------------------------------------------------------------

async def _download_replay(match_id: int, cluster: int, salt: int, dest_path: str) -> bool:
    """
    Stream-download {match_id}_{salt}.dem.bz2 from Valve CDN, decompress on the
    fly, and write the raw .dem to dest_path.

    Uses streaming + incremental bz2 decompression so the whole file is never
    held in RAM at once — important for 50-400 MB replays on memory-constrained
    servers.
    """
    url = REPLAY_URL_TEMPLATE.format(cluster=cluster, match_id=match_id, salt=salt)
    logger.info("Downloading replay for match %d: %s", match_id, url)

    CHUNK = 256 * 1024  # 256 KB read chunks

    try:
        timeout = aiohttp.ClientTimeout(total=600)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("Replay CDN returned %d for match %d", resp.status, match_id)
                    return False

                decompressor = bz2.BZ2Decompressor()
                bytes_in = 0
                bytes_out = 0

                with open(dest_path, "wb") as out:
                    async for chunk in resp.content.iter_chunked(CHUNK):
                        bytes_in += len(chunk)
                        decompressed = decompressor.decompress(chunk)
                        if decompressed:
                            out.write(decompressed)
                            bytes_out += len(decompressed)

    except asyncio.TimeoutError:
        logger.error("Replay download timed out for match %d", match_id)
        return False
    except Exception:
        logger.exception("Replay download/decompress failed for match %d", match_id)
        return False

    logger.info(
        "Replay for match %d: %d KB compressed → %d KB decompressed",
        match_id, bytes_in // 1024, bytes_out // 1024,
    )
    return True


# ---------------------------------------------------------------------------
# Step 4 — run the Go parser
# ---------------------------------------------------------------------------

async def _run_parser(dem_path: str) -> list[dict] | None:
    """
    Invoke the ad-parser binary on a decompressed .dem file.

    Async so it doesn't block the Discord heartbeat — replay parsing can take
    30-120 seconds for a large file.  Uses asyncio.create_subprocess_exec so
    the event loop stays alive during parsing.

    Returns a list of pick dicts or None on failure.
    Each dict: {"order": int, "player_id": int, "ability_id": int}
    """
    if not os.path.exists(PARSER_BIN):
        logger.error(
            "ad-parser binary not found at %s — set AD_PARSER_BIN or rebuild the Docker image",
            PARSER_BIN,
        )
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            PARSER_BIN, dem_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=240,  # 4-minute ceiling; typical parse is 30-90 s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()  # drain so process is reaped
            logger.error("ad-parser timed out on %s", dem_path)
            return None
    except Exception:
        logger.exception("ad-parser subprocess failed on %s", dem_path)
        return None

    returncode = proc.returncode
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    if returncode != 0:
        logger.error("ad-parser exited %d:\n%s", returncode, stderr[:2000])
        return None

    if stderr:
        # Parser writes diagnostics to stderr; log at INFO so we see pick events
        # and event-discovery dumps in production without needing DEBUG level.
        for line in stderr.strip().splitlines():
            logger.info("ad-parser: %s", line)

    try:
        picks = json.loads(stdout)
    except json.JSONDecodeError:
        logger.error("ad-parser output is not valid JSON: %r", stdout[:500])
        return None

    if not isinstance(picks, list):
        logger.error("ad-parser returned unexpected type: %r", type(picks))
        return None

    return picks


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def fetch_draft_picks(match_id: int, cluster: int, replay_salt: int = 0) -> list[dict] | None:
    """
    Full pipeline: resolve salt → download replay → parse → return picks.

    Args:
        match_id:     Dota 2 match ID.
        cluster:      Cluster stored in the matches table (used in the CDN URL).
        replay_salt:  Salt stored from OpenDota at match-fetch time.  If 0,
                      we fall back to asking the Steam Web API (only works for
                      some match types).

    Returns a list of pick dicts on success, None if any step fails.
    The .dem file is cleaned up automatically (written to a temp dir).
    """
    # Prefer the salt we already have from OpenDota
    if replay_salt:
        salt = replay_salt
        logger.info("Using stored replay_salt=%d for match %d", salt, match_id)
    else:
        # Fall back to Steam API (may not work for amateur league matches)
        logger.info("No stored salt for match %d — trying Steam API", match_id)
        info = await get_replay_salt(match_id)
        if info is None:
            logger.warning("Cannot get replay salt for match %d — skipping", match_id)
            return None
        salt, cluster = info  # Steam API also returns the authoritative cluster

    with tempfile.TemporaryDirectory(prefix=f"dota_replay_{match_id}_") as tmpdir:
        dem_path = os.path.join(tmpdir, f"{match_id}.dem")

        ok = await _download_replay(match_id, cluster, salt, dem_path)
        if not ok:
            return None

        picks = await _run_parser(dem_path)
        # tmpdir and its contents are deleted when the `with` block exits

    if picks is None:
        return None

    logger.info("fetch_draft_picks: match %d → %d picks", match_id, len(picks))
    return picks
