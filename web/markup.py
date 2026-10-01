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
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
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


def render(text: str) -> str:
    """Markdown -> HTML, for the rules and about pages."""
    lines = html.escape(text or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    list_tag: str | None = None
    paragraph: list[str] = []

    def close_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

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
            want = "ul" if bullet else "ol"
            if list_tag != want:
                close_list()
                out.append(f"<{want}>")
                list_tag = want
            out.append(f"<li>{_inline((bullet or numbered).group(1).strip())}</li>")
            continue
        close_list()
        paragraph.append(line.strip())

    close_paragraph(); close_list()
    return "\n".join(out)
