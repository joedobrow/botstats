"""The small slice of Markdown a rules page needs.

The rules were plain text split on blank lines, so pasting them out of a
Google Doc lost every heading, list and bold run — which is most of what makes
rules readable. This renders the parts that carry that meaning and nothing
else: headings, bullet and numbered lists, bold, italic, inline code, links,
horizontal rules, paragraphs.

No table, image or raw-HTML support, deliberately. Everything is escaped
before any tag is built, so what goes in is text and the only HTML that comes
out is HTML this module wrote. A rules page is editable by every admin, and
none of them should be able to put a script on it by accident.

Google Docs will export Markdown (File → Download → Markdown), which is the
point: the document people edit together converts into this without a rewrite.
"""

from __future__ import annotations

import html
import re

__all__ = ["render"]

# Only schemes that go somewhere safe. A link is the one place a document can
# smuggle javascript: into a page.
_SAFE_LINK = re.compile(r"^(https?://|mailto:|/)", re.I)

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(\S(?:[^*]*\S)?)\*\*")
_ITALIC = re.compile(r"(?<![\*\w])\*(\S(?:[^*]*\S)?)\*(?!\w)")
_CODE = re.compile(r"`([^`]+)`")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(?:\d+|[a-z]|[ivx]+)[.)]\s+(.*)$", re.I)
_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_RULE = re.compile(r"^\s*([-*_])\s*(?:\1\s*){2,}$")


def _inline(text: str) -> str:
    """Escaped text -> escaped text with inline formatting applied."""
    def link(m: re.Match) -> str:
        label, url = m.group(1), html.unescape(m.group(2))
        if not _SAFE_LINK.match(url):
            return m.group(0)        # leave it as the text it is
        external = url.startswith("http")
        attrs = ' rel="noopener" target="_blank"' if external else ""
        return f'<a href="{html.escape(url, quote=True)}"{attrs}>{label}</a>'

    out = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", text)
    out = _LINK.sub(link, out)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    return out


def _render_items(items: list[tuple[int, str, str]], start: int = 0) -> tuple[str, int]:
    """Render one list and everything nested under it.

    items are (indent, "ul"|"ol", text) in document order. A deeper indent
    belongs inside the item above it, which is what a Google Docs export means
    by indenting — losing that turned a numbered outline with a, b, c beneath
    each heading into one flat list renumbered 1 to 12.
    """
    indent, tag, _ = items[start]
    parts = [f"<{tag}>"]
    i = start
    while i < len(items):
        here, here_tag, text = items[i]
        if here < indent or (here == indent and here_tag != tag):
            break                     # belongs to an outer list, or a new one
        if here > indent:
            nested, i = _render_items(items, i)
            parts[-1] = parts[-1][:-len("</li>")] + nested + "</li>"
            continue
        parts.append(f"<li>{_inline(text)}</li>")
        i += 1
    parts.append(f"</{tag}>")
    return "\n".join(parts), i


def render(text: str) -> str:
    """Markdown -> HTML, for the rules and about pages."""
    lines = html.escape(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    items: list[tuple[int, str, str]] = []

    def close_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        while items:
            rendered, used = _render_items(items)
            out.append(rendered)
            del items[:used]

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close_paragraph(); close_list()
            continue
        heading = _HEADING.match(line)
        if heading:
            close_paragraph(); close_list()
            level = min(len(heading.group(1)) + 1, 5)   # a page's h1 is its title
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            continue
        if _RULE.match(line):
            close_paragraph(); close_list()
            out.append("<hr>")
            continue
        bullet, numbered = _BULLET.match(line), _NUMBERED.match(line)
        if bullet or numbered:
            close_paragraph()
            m = bullet or numbered
            # Tabs are indentation too, and a lettered marker is only a list
            # marker when something above it opened a list — otherwise a line
            # starting "a. " is just a sentence.
            depth = len(m.group(1).expandtabs(4))
            if numbered and not bullet and not m.group(0).lstrip()[0].isdigit() and not items:
                paragraph.append(line.strip())
                continue
            items.append((depth, "ul" if bullet else "ol", m.group(2).strip()))
            continue
        close_list()
        paragraph.append(line.strip())

    close_paragraph(); close_list()
    return "\n".join(out)
