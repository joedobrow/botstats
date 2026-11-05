"""Messages from the site to the league's owner.

The site runs inside the bot's process, so the bot sets `send` when it
starts: a coroutine that DMs the owner on Discord. Run on its own (a local
preview), the site has no bot, and a message is only logged.
"""
import logging

logger = logging.getLogger(__name__)

send = None   # async (text: str) -> None, set by bot.py


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
