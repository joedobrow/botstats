"""Discord's message formatting, as HTML for the Blog.

The text is escaped first and only then given its markup, so nothing a
message says can become HTML of its own: the only tags in the output are
the ones written here, and the only links are http(s) ones.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from markupsafe import Markup

_URL = r"https?://[^\s<>\"\x00]+"
_TRAILING = ".,!?:;'*_~|"


def _a(url: str, text: str | None = None) -> str:
    # url and text are already escaped; the URL pattern stops at a quote.
    return f'<a href="{url}" rel="noopener nofollow" target="_blank">{text or url}</a>'


def render(text: str, names: dict[str, str] | None = None, tz=timezone.utc) -> Markup:
    names = names or {}
    held: list[str] = []

    def hold(fragment: str) -> str:
        held.append(fragment)
        return f"\x00{len(held) - 1}\x00"

    text = (text or "").replace("\x00", "")

    # Code first: nothing inside it is formatting.
    text = re.sub(r"```(?:[\w+-]*\n)?(.*?)```",
                  lambda m: hold(f"<pre><code>{html.escape(m.group(1).strip(chr(10)))}</code></pre>"),
                  text, flags=re.S)
    text = re.sub(r"(`+)(.+?)\1", lambda m: hold(f"<code>{html.escape(m.group(2))}</code>"), text)

    text = html.escape(text, quote=False)

    # Mentions, custom emoji and timestamps, by what the bot looked up.
    text = re.sub(r"&lt;@!?(\d+)&gt;",
                  lambda m: hold(f'<span class="mention">@{html.escape(names.get("u:" + m.group(1), "someone"))}</span>'), text)
    text = re.sub(r"&lt;@&amp;(\d+)&gt;",
                  lambda m: hold(f'<span class="mention">@{html.escape(names.get("r:" + m.group(1), "role"))}</span>'), text)
    text = re.sub(r"&lt;#(\d+)&gt;",
                  lambda m: hold(f'<span class="mention">#{html.escape(names.get("c:" + m.group(1), "channel"))}</span>'), text)
    text = re.sub(r"&lt;(a?):(\w+):(\d+)&gt;",
                  lambda m: hold(f'<img class="emoji" alt=":{m.group(2)}:" title=":{m.group(2)}:" '
                                 f'src="https://cdn.discordapp.com/emojis/{m.group(3)}.'
                                 f'{"gif" if m.group(1) else "webp"}?size=48">'), text)

    def stamp(m):
        when = datetime.fromtimestamp(int(m.group(1)), timezone.utc).astimezone(tz)
        style = m.group(2) or "f"
        fmt = {"t": "%-I:%M %p", "T": "%-I:%M:%S %p", "d": "%-m/%-d/%Y", "D": "%B %-d, %Y",
               "F": "%A, %B %-d, %Y %-I:%M %p"}.get(style, "%B %-d, %Y %-I:%M %p")
        return hold(f'<time datetime="{when.isoformat()}">{when.strftime(fmt)}</time>')
    text = re.sub(r"&lt;t:(-?\d{1,12})(?::([tTdDfFR]))?&gt;", stamp, text)

    # Links: [text](url), <url> (no preview) and bare URLs.
    text = re.sub(r"\[([^\]\n]+)\]\(&lt;(" + _URL + r")&gt;\)|\[([^\]\n]+)\]\((" + _URL + r")\)",
                  lambda m: hold(_a((m.group(2) or m.group(4)).rstrip(")"), _inline(m.group(1) or m.group(3)))),
                  text)
    text = re.sub(r"&lt;(" + _URL + r")&gt;", lambda m: hold(_a(m.group(1))), text)

    def bare(m):
        url = m.group(0)
        tail = ""
        while url and url[-1] in _TRAILING + ")":
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break
            tail, url = url[-1] + tail, url[:-1]
        return hold(_a(url)) + tail
    text = re.sub(_URL, bare, text)

    out = _blocks(text)
    while "\x00" in out:
        out = re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], out)
    return Markup(out)


def _inline(s: str) -> str:
    s = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"__(.+?)__", r"<u>\1</u>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<em>\1</em>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    s = re.sub(r"\|\|(.+?)\|\|", r'<span class="spoiler" tabindex="0">\1</span>', s)
    return s


def _blocks(text: str) -> str:
    """Line by line: headings, subtext, quotes and lists; plain lines are
    joined by line breaks, as Discord shows them."""
    out: list[str] = []
    para: list[str] = []
    lines = text.split("\n")

    def flush():
        if para:
            out.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("&gt;&gt;&gt; "):
            flush()
            rest = [line[13:]] + lines[i + 1:]
            out.append("<blockquote>" + "<br>".join(_inline(x) for x in rest) + "</blockquote>")
            break
        if line.startswith("&gt; ") or line == "&gt;":
            flush()
            quote = []
            while i < len(lines) and (lines[i].startswith("&gt; ") or lines[i] == "&gt;"):
                quote.append(_inline(lines[i][5:]))
                i += 1
            out.append("<blockquote>" + "<br>".join(quote) + "</blockquote>")
            continue
        m = re.match(r"(#{1,3}) (.+)", line)
        if m:
            flush()
            level = len(m.group(1)) + 2           # h3–h5: the page has its own h1 and h2
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if line.startswith("-# "):
            flush()
            out.append(f'<p class="subtext">{_inline(line[3:])}</p>')
            i += 1
            continue
        if re.match(r"\s*[-*] \S", line) or re.match(r"\s*\d+\. \S", line):
            flush()
            ordered = bool(re.match(r"\s*\d+\.", line))
            tag = "ol" if ordered else "ul"
            items = []
            pattern = r"\s*\d+\. (.*)" if ordered else r"\s*[-*] (.*)"
            while i < len(lines) and re.match(pattern, lines[i]):
                items.append("<li>" + _inline(re.match(pattern, lines[i]).group(1)) + "</li>")
                i += 1
            out.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
            continue
        para.append(_inline(line))
        i += 1
    flush()
    return "".join(out)
