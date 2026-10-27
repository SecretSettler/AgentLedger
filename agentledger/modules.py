"""Turn a rendered report body into modules: one per top-level ``<h2>``.

Modules are the unit of review.  Each gets a stable id (the heading slug, or an
explicit ``{#id}``), a content hash used to flag what changed between versions,
and its ``<h3>`` sub-headings for the navigation rail.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .markdown import slugify

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


@dataclass
class Module:
    id: str
    title: str
    html: str
    attrs: dict = field(default_factory=dict)
    subs: list[dict] = field(default_factory=list)
    hash: str = ""
    words: int = 0


class _H2Finder(HTMLParser):
    """Record the source offsets of ``<h2>`` elements that sit at the top level."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.line_starts = [0]
        for m in re.finditer("\n", text):
            self.line_starts.append(m.end())
        self.starts: list[int] = []

    def _offset(self) -> int:
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def handle_starttag(self, tag, attrs):
        if tag == "h2" and self.depth == 0:
            self.starts.append(self._offset())
        if tag not in VOID:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag not in VOID:
            self.depth = max(0, self.depth - 1)


def text_of(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment, flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _attr(tag_html: str, name: str) -> str | None:
    m = re.search(r'\s' + re.escape(name) + r'="([^"]*)"', tag_html)
    return html.unescape(m.group(1)) if m else None


def split_modules(body: str, intro_title: str = "Overview") -> list[Module]:
    finder = _H2Finder(body)
    finder.feed(body)
    finder.close()
    cuts = finder.starts
    chunks: list[str] = []
    if not cuts:
        chunks = [body]
    else:
        if body[: cuts[0]].strip():
            chunks.append(body[: cuts[0]])
        for a, b in zip(cuts, cuts[1:] + [len(body)]):
            chunks.append(body[a:b])

    used: set[str] = set()
    taken = set(re.findall(r'\sid="([^"]*)"', body))
    modules: list[Module] = []
    for chunk in chunks:
        chunk = chunk.strip()
        hm = re.match(r"<h2\b([^>]*)>(.*?)</h2>", chunk, flags=re.DOTALL | re.IGNORECASE)
        if hm:
            open_tag = "<h2" + hm.group(1) + ">"
            title = text_of(hm.group(2))
            mid = _attr(open_tag, "id") or slugify(title)
            attrs = {k[5:]: html.unescape(v) for k, v in re.findall(r'\s(data-[\w-]+)="([^"]*)"', open_tag)}
            # The section carries the id, so the anchor lands on the module, not inside it.
            chunk = "<h2" + re.sub(r'\sid="[^"]*"', "", hm.group(1)) + ">" + hm.group(2) + "</h2>" + chunk[hm.end():]
        else:
            title, mid, attrs = intro_title, "intro", {}
        base, n = mid, 2
        while mid in used or (not hm and mid in taken):
            mid = f"{base}-{n}"
            n += 1
        used.add(mid)
        subs = [
            {"id": html.unescape(sid), "title": text_of(stitle)}
            for sid, stitle in re.findall(r'<h3\b[^>]*\sid="([^"]*)"[^>]*>(.*?)</h3>', chunk, flags=re.DOTALL | re.IGNORECASE)
        ]
        text = text_of(chunk)
        modules.append(
            Module(
                id=mid,
                title=title,
                html=chunk,
                attrs=attrs,
                subs=subs,
                hash=hashlib.sha1(text.encode("utf-8")).hexdigest()[:12],
                words=len(text.split()),
            )
        )
    return modules
