"""The site's blog: a copy of the Discord #content channel.

Every message in the channel is kept as one post, as Discord shows it: the
text, pictures and videos, link previews, polls, replies and reactions. The
bot saves a message when it's posted, edited, reacted to or voted on, and
drops it when it's deleted. reconcile() walks the whole channel and puts the
copy right, which catches anything that happened while the bot was offline;
bot.py runs it on connect and every few minutes.

Discord's links to uploaded files stop working after a day or so, so
pictures and videos are copied to the uploads volume. Other files aren't:
an uploaded .html or .svg served from adcl.gg would be a page on our domain.
"""
from __future__ import annotations

import asyncio
import logging
import os

import discord

from config import CONTENT_CHANNEL_ID
from web import store, uploads

logger = logging.getLogger(__name__)

CONTENT_DIR = uploads.UPLOAD_DIR / "content"
MAX_FILE_BYTES = 25 * 1024 * 1024
# Copied, and shown inline. Anything else links back to the message.
MEDIA_TYPES = {".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
               ".mp4": "video", ".webm": "video", ".mov": "video"}

_lock = asyncio.Lock()


def is_content(channel_id: int | None) -> bool:
    return bool(CONTENT_CHANNEL_ID) and channel_id == CONTENT_CHANNEL_ID


def _https(url: str | None) -> str | None:
    return url if url and url.startswith("https://") else None


def _emoji(e) -> dict:
    """A reaction's or poll answer's emoji: unicode text, or a custom one's picture."""
    if e is None:
        return {}
    if isinstance(e, str):
        return {"text": e}
    if getattr(e, "id", None):
        ext = "gif" if getattr(e, "animated", False) else "webp"
        return {"name": e.name, "url": f"https://cdn.discordapp.com/emojis/{e.id}.{ext}?size=48"}
    return {"text": str(e)}


def _author(user) -> dict:
    return {"id": user.id, "name": getattr(user, "display_name", None) or user.name,
            "username": user.name, "global_name": getattr(user, "global_name", None),
            "avatar": str(user.display_avatar.replace(size=64).url), "bot": user.bot}


def _names(message: discord.Message) -> dict[str, str]:
    """Names for the mentions in the text, looked up now: a <@id> means
    nothing to a reader of the site."""
    out = {f"u:{u.id}": getattr(u, "display_name", None) or u.name for u in message.mentions}
    guild = message.guild
    for rid in message.raw_role_mentions:
        role = guild.get_role(rid) if guild else None
        if role:
            out[f"r:{rid}"] = role.name
    for cid in message.raw_channel_mentions:
        ch = guild.get_channel_or_thread(cid) if guild else None
        if ch:
            out[f"c:{cid}"] = ch.name
    return out


def _embed(e: discord.Embed) -> dict:
    return {"type": e.type, "url": _https(e.url), "title": e.title, "description": e.description,
            "provider": e.provider.name if e.provider else None,
            "author": e.author.name if e.author else None,
            "thumbnail": _https(e.thumbnail.url) if e.thumbnail else None,
            "image": _https(e.image.url) if e.image else None,
            "video": _https(e.video.url) if e.video else None,
            "color": e.colour.value if e.colour else None,
            "fields": [{"name": f.name, "value": f.value} for f in e.fields]}


def _poll(p: discord.Poll | None) -> dict | None:
    if p is None:
        return None
    answers = [{"text": a.text, "emoji": _emoji(a.emoji), "votes": a.vote_count or 0} for a in p.answers]
    return {"question": p.question if isinstance(p.question, str) else getattr(p.question, "text", ""),
            "answers": answers, "total": sum(a["votes"] for a in answers),
            "final": p.is_finalised(), "expires": p.expires_at.isoformat() if p.expires_at else None}


def _poll_result(message: discord.Message) -> dict | None:
    """The 'poll has closed' notice: Discord sends its numbers as an embed's fields."""
    e = next((e for e in message.embeds if e.type == "poll_result"), None)
    if e is None:
        return None
    f = {x.name: x.value for x in e.fields}
    return {"question": f.get("poll_question_text"), "winner": f.get("victor_answer_text"),
            "winner_votes": int(f.get("victor_answer_votes") or 0), "total": int(f.get("total_votes") or 0),
            "poll_id": message.reference.message_id if message.reference else None}


async def _attachment(a: discord.Attachment) -> dict:
    ext = os.path.splitext(a.filename)[1].lower()
    kind = MEDIA_TYPES.get(ext, "file")
    out = {"kind": kind, "filename": a.filename, "width": a.width, "height": a.height,
           "spoiler": a.is_spoiler(), "url": None}
    if kind == "file" or (a.size or 0) > MAX_FILE_BYTES:
        out["kind"] = "file"
        return out
    path = CONTENT_DIR / f"{a.id}{ext}"
    if not path.exists():
        CONTENT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        await a.save(tmp)
        tmp.replace(path)
    out["url"] = f"{uploads.URL_PREFIX}content/{path.name}"
    return out


async def to_post(message: discord.Message) -> dict:
    kind = {discord.MessageType.default: "default", discord.MessageType.reply: "reply"}.get(message.type)
    result = _poll_result(message)
    if result:
        kind = "poll_result"
    reply = None
    if message.type == discord.MessageType.reply and message.reference:
        r = message.reference.resolved
        reply = {"id": message.reference.message_id,
                 "author": (getattr(r.author, "display_name", None) or r.author.name)
                 if isinstance(r, discord.Message) else None,
                 "snippet": (r.content[:120] if isinstance(r, discord.Message) else None)}
    return {
        "id": message.id, "channel_id": message.channel.id, "type": kind or "system",
        "created_at": message.created_at.isoformat(),
        "edited_at": message.edited_at.isoformat() if message.edited_at else None,
        "author": _author(message.author),
        "content": message.content,
        "system_text": None if kind else message.system_content,
        "names": _names(message),
        "reply": reply,
        "attachments": [await _attachment(a) for a in message.attachments],
        "embeds": [_embed(e) for e in message.embeds if e.type != "poll_result"],
        "poll": _poll(message.poll),
        "poll_result": result,
        "stickers": [{"name": s.name, "url": _https(str(s.url))} for s in message.stickers
                     if s.format != discord.StickerFormatType.lottie],
        "reactions": [{**_emoji(r.emoji), "count": r.count} for r in message.reactions],
        "thread": {"name": message.thread.name, "count": message.thread.message_count}
        if message.thread else None,
        "jump_url": message.jump_url,
    }


async def save(message: discord.Message) -> None:
    """Keep this message's post up to date."""
    if not is_content(message.channel.id):
        return
    async with _lock:
        try:
            store.save_content_post(await to_post(message))
        except Exception:
            logger.exception("content: couldn't save message %s", message.id)


async def refetch(client: discord.Client, channel_id: int, message_id: int) -> None:
    """Fetch a message again and save it: after an edit, a reaction or a vote."""
    if not is_content(channel_id):
        return
    try:
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        remove([message_id])
        return
    except discord.HTTPException:
        logger.warning("content: couldn't fetch message %s", message_id)
        return
    await save(message)


def remove(message_ids) -> None:
    """Take deleted messages off the site, files and all."""
    for post in store.delete_content_posts(list(message_ids)):
        for a in post.get("attachments", []):
            if a.get("url"):
                try:
                    (CONTENT_DIR / a["url"].rsplit("/", 1)[-1]).unlink(missing_ok=True)
                except OSError:
                    logger.warning("content: couldn't delete %s", a["url"])


async def reconcile(client: discord.Client) -> None:
    """Make the site's copy match the channel: every message saved, and
    every post whose message is gone removed."""
    if not CONTENT_CHANNEL_ID:
        return
    channel = client.get_channel(CONTENT_CHANNEL_ID) or await client.fetch_channel(CONTENT_CHANNEL_ID)
    seen = set()
    async for message in channel.history(limit=None):
        seen.add(message.id)
        await save(message)
    gone = store.content_post_ids(CONTENT_CHANNEL_ID) - seen
    if gone and not seen:
        # A channel that reads as empty is more likely a lost permission
        # than everything deleted at once. Keep the posts; say so.
        logger.warning("content: the channel came back empty; keeping %d posts", len(gone))
        return
    if gone:
        remove(gone)
        logger.info("content: removed %d deleted message(s)", len(gone))
