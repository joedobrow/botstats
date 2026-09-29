"""Run just the website, without the Discord bot, for local development.

    DISCORD_TOKEN=dev DB_PATH=~/dota-bot-prod.db python -m web [port]

Use a copy of the database: the site creates its own tables in it.
"""

import logging
import sys

from aiohttp import web

import db
from botstats.server import create_app

logging.basicConfig(level=logging.INFO)
db.init_db()
web.run_app(create_app(), host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8080)
