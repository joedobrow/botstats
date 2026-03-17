"""
aiohttp web server for the AD helper page.

Designed to run inside the Discord bot's asyncio event loop.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from botstats.data_builder import build_ad_data

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


async def ad_helper_handler(request: web.Request) -> web.Response:
    """Serve the AD helper page with live data injected."""
    data = await build_ad_data()
    if not data:
        return web.Response(text="Failed to load ability data. Try again later.", status=503)

    template_path = TEMPLATE_DIR / "ad_helper.html"
    html = template_path.read_text(encoding="utf-8")

    html = (
        html
        .replace("<<DATA_JSON>>", data["data_json"])
        .replace("<<HS_JSON>>", data["hs_json"])
        .replace("<<ROLES_JSON>>", data["roles_json"])
    )

    return web.Response(text=html, content_type="text/html")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/ad-helper", ad_helper_handler)
    return app


async def start_web_server(host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    """Start the web server. Call from an already-running event loop."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("AD helper web server running on http://%s:%d/ad-helper", host, port)
    return runner
