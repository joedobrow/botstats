"""The site's line to the bot.

The site runs inside the bot's process, so the bot hands it what it needs
when it starts: `send`, a coroutine that DMs the owner on Discord, and
`client`, the bot itself, for the Discord sync on Admin → Discord. Run on
its own (a local preview) the site has neither: messages are only logged
and the sync says the bot isn't here.
"""
import logging

logger = logging.getLogger(__name__)

send = None     # async (text: str) -> None, set by bot.py
client = None   # the discord.Client, set by bot.py


async def owner(text: str) -> bool:
    """Tell the owner something. True if it reached them."""
    if send is None:
        logger.info("no bot to notify the owner: %s", text[:200])
        return False
    try:
        await send(text)
        return True
    except Exception:
        logger.exception("couldn't notify the owner")
        return False
