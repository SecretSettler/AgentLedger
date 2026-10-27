"""A small, dependency-free Markdown renderer tuned for agent-written reports.

Covers the GitHub-flavoured subset agents actually emit: ATX headings with
optional ``{#id key=value}`` attributes, paragraphs, emphasis, links, images,
inline and fenced code, nested lists and task lists, block quotes, tables,
horizontal rules and raw HTML blocks.  Output is sanitised separately (see
``sanitize.py``), so this module only has to be correct, not defensive.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})\s*([^\s`]*)[^`]*$")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
HR_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
LIST_RE = re.compile(r"^( *)([-*+]|\d{1,9}[.)])( +|$)(.*)$")
QUOTE_RE = re.compile(r"^ {0,3}> ?(.*)$")
TABLE_SEP_RE = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
HTML_BLOCK_RE = re.compile(
    r"^ {0,3}(?:<!--|<\?|<![A-Z]|</?(?:address|article|aside|blockquote|body|caption|center|col|colgroup|dd|"
    r"details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|h[1-6]|head|header|hr|html|"
    r"iframe|legend|li|link|main|menu|nav|ol|p|picture|pre|script|section|source|style|summary|svg|"
    r"table|tbody|td|tfoot|th|thead|title|tr|ul|video|audio|canvas|math|noscript|textarea)(?:\s|/?>|$))",
    re.IGNORECASE,
)
RAW_CONTAINER_RE = re.compile(r"^ {0,3}<(pre|script|style|textarea)(?:\s|>|$)", re.IGNORECASE)
ATTRS_RE = re.compile(r"[ \t]*\{([^{}]*)\}[ \t]*$")
ESCAPABLE = "\\`*_{}[]()#+-.!|~<>\"'&$:"
INLINE_TAGS = (
    "a|abbr|b|bdi|bdo|br|cite|code|data|del|dfn|em|i|img|ins|kbd|mark|q|s|samp|small|span|strong|"
    "sub|summary|sup|time|u|var|wbr|details|font"
)
INLINE_TAG_RE = re.compile(
    r"<(?:/?(?:" + INLINE_TAGS + r")(?:\s+[A-Za-z_:][-\w:.]*(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?)*\s*/?"
    r"|!--.*?--)>",
    re.DOTALL | re.IGNORECASE,
)
AUTOLINK_RE = re.compile(r"<((?:https?|mailto):[^\s<>]+)>")
BARE_URL_RE = re.compile(r"(?<![\w/\"'=(])(https?://[^\s<>\"]*[^\s<>\".,;:!?)\]'*_])")
LINK_RE = re.compile(
    r"(!?)\[((?:[^\[\]\\]|\\.|\[[^\[\]]*\])*)\]"
    r"\(\s*<?((?:[^\s()<>\\]|\\.|\([^\s()]*\))*)>?(?:\s+(\"[^\"]*\"|'[^']*'))?\s*\)"
)
PH = "\x00"


def slugify(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text or "section"


def parse_attrs(spec: str) -> dict:
    """Parse ``#id .class key=value key="spaced value"`` into a dict."""
    attrs: dict = {}
    for m in re.finditer(r"""([#.])([\w-]+)|([\w-]+)=("[^"]*"|'[^']*'|\S+)""", spec):
        if m.group(1) == "#":
            attrs["id"] = m.group(2)
        elif m.group(1) == ".":
            attrs.setdefault("class", []).append(m.group(2))
        else:
            attrs[m.group(3)] = m.group(4).strip("\"'")
    return attrs


def split_front_matter(text: str) -> tuple[dict, str]:
    """Strip a leading ``---`` block of ``key: value`` lines."""
    m = re.match(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.DOTALL)
    if not m:
        return {}, text
    meta: dict = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not km:
            continue
        key, value = km.group(1).lower(), km.group(2).strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key] = value.strip("\"'") if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else value
    return meta, text[m.end():]


@dataclass
class Heading:
    level: int
    text: str
    id: str
    attrs: dict = field(default_factory=dict)


class Markdown:
    def __init__(self, heading_shift: int = 0):
        self.heading_shift = heading_shift
        self.headings: list[Heading] = []
        self._ids: set[str] = set()

    # ------------------------------------------------------------------ ids
    def unique_id(self, base: str) -> str:
        if base.startswith("al-"):
            base = "h-" + base
        candidate, n = base, 2
        while candidate in self._ids:
            candidate = f"{base}-{n}"
            n += 1
        self._ids.add(candidate)
        return candidate

    # --------------------------------------------------------------- blocks
    def convert(self, text: str) -> str:
        lines = [re.sub(r"^[ \t]+", lambda m: m.group(0).expandtabs(4), ln) for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        return "\n".join(self._blocks(lines))

    def _starts_block(self, line: str) -> bool:
        return bool(
            FENCE_RE.match(line) or HEADING_RE.match(line) or HR_RE.match(line)
            or QUOTE_RE.match(line) or HTML_BLOCK_RE.match(line)
        )

    def _blocks(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue

            m = FENCE_RE.match(line)
            if m:
                indent, fence, info = len(m.group(1)), m.group(2), m.group(3)
                close = re.compile(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*$")
                body, i = [], i + 1
                while i < n and not close.match(lines[i]):
                    ln = lines[i]
                    strip = min(indent, len(ln) - len(ln.lstrip(" ")))
                    body.append(ln[strip:])
                    i += 1
                i += 1
                cls = f' class="language-{html.escape(info)}"' if info else ""
                code = html.escape("\n".join(body) + ("\n" if body else ""), quote=False)
                out.append(f"<pre><code{cls}>{code}</code></pre>")
                continue

            m = HEADING_RE.match(line)
            if m:
                out.append(self._heading(len(m.group(1)), m.group(2) or ""))
                i += 1
                continue

            if HR_RE.match(line):
                out.append("<hr>")
                i += 1
                continue

            if "|" in line and i + 1 < n and "|" in lines[i + 1] and "-" in lines[i + 1] and TABLE_SEP_RE.match(lines[i + 1]):
                html_table, i = self._table(lines, i)
                out.append(html_table)
                continue

            if QUOTE_RE.match(line):
                inner = []
                while i < n and lines[i].strip():
                    qm = QUOTE_RE.match(lines[i])
                    if qm:
                        inner.append(qm.group(1))
                    elif inner and not self._starts_block(lines[i]) and not LIST_RE.match(lines[i]):
                        inner.append(lines[i])  # lazy continuation
                    else:
                        break
                    i += 1
                    if i < n and not lines[i].strip() and i + 1 < n and QUOTE_RE.match(lines[i + 1]):
                        inner.append("")
                        i += 1
                out.append("<blockquote>\n" + "\n".join(self._blocks(inner)) + "\n</blockquote>")
                continue

            if LIST_RE.match(line) and not HR_RE.match(line):
                html_list, i = self._list(lines, i)
                out.append(html_list)
                continue

            if HTML_BLOCK_RE.match(line):
                raw, i = self._html_block(lines, i)
                out.append(raw)
                continue

            para = [line.lstrip()]
            i += 1
            while i < n and lines[i].strip():
                nxt = lines[i]
                if self._starts_block(nxt) or (LIST_RE.match(nxt) and LIST_RE.match(nxt).group(4).strip()):
                    break
                if "|" in nxt and i + 1 < n and TABLE_SEP_RE.match(lines[i + 1]) and "|" in lines[i + 1]:
                    break
                para.append(nxt)
                i += 1
            out.append("<p>" + self._inline_lines(para) + "</p>")
        return out

    def _heading(self, level: int, raw: str) -> str:
        raw = re.sub(r"[ \t]+#+[ \t]*$", "", raw).strip()
        attrs: dict = {}
        am = ATTRS_RE.search(raw)
        if am and ("#" in am.group(1) or "=" in am.group(1) or "." in am.group(1)):
            attrs = parse_attrs(am.group(1))
            raw = raw[: am.start()].rstrip()
        level = max(1, min(6, level + self.heading_shift))
        inner = self.inline(raw)
        hid = self.unique_id(attrs.pop("id", None) or slugify(inner))
        self.headings.append(Heading(level, html.unescape(re.sub(r"<[^>]+>", "", inner)), hid, dict(attrs)))
        extra = ""
        classes = attrs.pop("class", [])
        if classes:
            extra += f' class="{html.escape(" ".join(classes))}"'
        for k, v in attrs.items():
            extra += f' data-{html.escape(k)}="{html.escape(v)}"'
        return f'<h{level} id="{hid}"{extra}>{inner}</h{level}>'

    def _table(self, lines: list[str], i: int) -> tuple[str, int]:
        header = split_row(lines[i])
        aligns = []
        for cell in split_row(lines[i + 1]):
            c = cell.strip()
            aligns.append("center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":") else "left" if c.startswith(":") else "")
        cols = len(header)
        aligns = (aligns + [""] * cols)[:cols]

        def cell_html(tag: str, text: str, align: str) -> str:
            style = f' style="text-align:{align}"' if align else ""
            return f"<{tag}{style}>{self.inline(text.strip())}</{tag}>"

        parts = ["<table>", "<thead>", "<tr>" + "".join(cell_html("th", c, a) for c, a in zip(header, aligns)) + "</tr>", "</thead>"]
        i += 2
        body = []
        while i < len(lines) and lines[i].strip() and "|" in lines[i] and not self._starts_block(lines[i]):
            cells = (split_row(lines[i]) + [""] * cols)[:cols]
            body.append("<tr>" + "".join(cell_html("td", c, a) for c, a in zip(cells, aligns)) + "</tr>")
            i += 1
        if body:
            parts += ["<tbody>"] + body + ["</tbody>"]
        parts.append("</table>")
        return "\n".join(parts), i

    def _list(self, lines: list[str], i: int) -> tuple[str, int]:
        first = LIST_RE.match(lines[i])
        ordered = first.group(2)[0].isdigit()
        base_indent = len(first.group(1))
        start = int(first.group(2)[:-1]) if ordered else 1
        items: list[list[str]] = []
        loose = False
        n = len(lines)
        while i < n:
            m = LIST_RE.match(lines[i])
            if not m or HR_RE.match(lines[i]) or m.group(2)[0].isdigit() != ordered or len(m.group(1)) > base_indent + 3:
                break
            indent, marker, spaces, rest = len(m.group(1)), m.group(2), m.group(3), m.group(4)
            content_indent = indent + len(marker) + (1 if len(spaces) > 4 or not rest else len(spaces))
            item = [rest if len(spaces) <= 4 else " " * (len(spaces) - 1) + rest]
            i += 1
            blank = False
            while i < n:
                ln = lines[i]
                if not ln.strip():
                    item.append("")
                    blank = True
                    i += 1
                    continue
                lead = len(ln) - len(ln.lstrip(" "))
                if lead >= content_indent:
                    if blank and not LIST_RE.match(ln[content_indent:]):
                        loose = True
                    item.append(ln[content_indent:])
                elif lead > indent and LIST_RE.match(ln):
                    item.append(ln[min(lead, content_indent):])
                elif not blank and not self._starts_block(ln) and not LIST_RE.match(ln):
                    item.append(ln.strip())  # lazy continuation
                else:
                    break
                blank = False
                i += 1
            while item and not item[-1].strip():
                item.pop()
            items.append(item)
            if blank and i < n and LIST_RE.match(lines[i]) and len(LIST_RE.match(lines[i]).group(1)) <= base_indent + 3:
                nm = LIST_RE.match(lines[i])
                if nm.group(2)[0].isdigit() == ordered:
                    loose = True
            if blank and (i >= n or not LIST_RE.match(lines[i])):
                break

        tag = "ol" if ordered else "ul"
        start_attr = f' start="{start}"' if ordered and start != 1 else ""
        task_list = any(re.match(r"^\[[ xX]\](?:\s|$)", it[0]) for it in items if it)
        cls = ' class="task-list"' if task_list else ""
        out = [f"<{tag}{start_attr}{cls}>"]
        for item in items:
            prefix = ""
            if item:
                tm = re.match(r"^\[([ xX])\](?:\s+|$)", item[0])
                if tm:
                    checked = " checked" if tm.group(1).lower() == "x" else ""
                    prefix = f'<input type="checkbox" disabled{checked}> '
                    item = [item[0][tm.end():]] + item[1:]
            blocks = self._blocks(item)
            if not loose:
                blocks = [b[3:-4] if b.startswith("<p>") and b.endswith("</p>") else b for b in blocks]
            body = "\n".join(blocks)
            if prefix:
                if body.startswith("<p>"):
                    body = "<p>" + prefix + body[3:]
                else:
                    body = prefix + body
            li_cls = ' class="task"' if prefix else ""
            out.append(f"<li{li_cls}>{body}</li>")
        out.append(f"</{tag}>")
        return "\n".join(out), i

    def _html_block(self, lines: list[str], i: int) -> tuple[str, int]:
        n = len(lines)
        first = lines[i]
        buf = [first]
        if first.lstrip().startswith("<!--"):
            j = i
            while j < n and "-->" not in lines[j]:
                j += 1
                if j < n:
                    buf.append(lines[j])
            return "\n".join(buf), j + 1
        cm = RAW_CONTAINER_RE.match(first)
        if cm:
            close = f"</{cm.group(1).lower()}>"
            j = i
            while j < n and close not in lines[j].lower():
                j += 1
                if j < n:
                    buf.append(lines[j])
            return "\n".join(buf), j + 1
        i += 1
        while i < n and lines[i].strip():
            buf.append(lines[i])
            i += 1
        return "\n".join(buf), i

    # --------------------------------------------------------------- inline
    def _inline_lines(self, para: list[str]) -> str:
        parts = []
        for idx, ln in enumerate(para):
            last = idx == len(para) - 1
            hard = not last and (ln.endswith("  ") or ln.rstrip(" ").endswith("\\"))
            ln = ln.strip()
            if hard and ln.endswith("\\"):
                ln = ln[:-1]
            parts.append(ln + ("<br>" if hard else ""))
        return self.inline("\n".join(parts))

    def inline(self, text: str) -> str:
        stash: list[str] = []

        def keep(fragment: str) -> str:
            stash.append(fragment)
            return f"{PH}{len(stash) - 1}{PH}"

        # code spans first: their content is literal
        def code(m: re.Match) -> str:
            body = m.group(2)
            if body.startswith(" ") and body.endswith(" ") and body.strip():
                body = body[1:-1]
            return keep("<code>" + html.escape(body.replace("\n", " "), quote=False) + "</code>")

        text = re.sub(r"(`+)(.+?)(?<!`)\1(?!`)", code, text, flags=re.DOTALL)
        text = re.sub(r"\\([" + re.escape(ESCAPABLE) + "])", lambda m: keep(html.escape(m.group(1))), text)
        text = AUTOLINK_RE.sub(lambda m: keep(f'<a href="{html.escape(m.group(1))}">{html.escape(m.group(1))}</a>'), text)
        text = INLINE_TAG_RE.sub(lambda m: keep(m.group(0)), text)

        def link(m: re.Match) -> str:
            is_img, label, url, title = m.group(1), m.group(2), m.group(3), m.group(4)
            url = re.sub(r"\\(.)", r"\1", url)
            t = f' title="{html.escape(title[1:-1])}"' if title else ""
            if is_img:
                return keep(f'<img src="{html.escape(url)}" alt="{html.escape(html.unescape(label))}"{t}>')
            label = BARE_URL_RE.sub(lambda u: keep(html.escape(u.group(1))), label)
            return keep(f'<a href="{html.escape(url)}"{t}>') + label + keep("</a>")

        text = LINK_RE.sub(link, text)
        text = BARE_URL_RE.sub(lambda m: keep(f'<a href="{html.escape(m.group(1))}">{html.escape(m.group(1))}</a>'), text)

        # escape everything else, keeping valid entities
        text = re.sub(r"&(?!#\d+;|#[xX][0-9a-fA-F]+;|\w+;)", "&amp;", text)
        text = text.replace("<", "&lt;").replace(">", "&gt;")

        text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text, flags=re.DOTALL)
        text = re.sub(r"(?<![\w])__(?=\S)(.+?)(?<=\S)__(?![\w])", r"<strong>\1</strong>", text, flags=re.DOTALL)
        text = re.sub(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])", r"<em>\1</em>", text, flags=re.DOTALL)
        text = re.sub(r"(?<![\w])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w])", r"<em>\1</em>", text, flags=re.DOTALL)
        text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", text, flags=re.DOTALL)

        while PH in text:
            text = re.sub(PH + r"(\d+)" + PH, lambda m: stash[int(m.group(1))], text)
        return text


def split_row(line: str) -> list[str]:
    """Split a table row on pipes that are neither escaped nor inside code spans."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells, buf, i, ticks = [], [], 0, 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            buf.append("\\|")
            i += 2
            continue
        if ch == "`":
            run = len(s[i:]) - len(s[i:].lstrip("`"))
            if ticks == 0:
                ticks = run
            elif ticks == run:
                ticks = 0
            buf.append("`" * run)
            i += run
            continue
        if ch == "|" and ticks == 0:
            cells.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    cells.append("".join(buf))
    return cells


def render(text: str) -> tuple[dict, str | None, str, list[Heading]]:
    """Render a Markdown report.

    Returns ``(front_matter, title, html, headings)``.  A leading ``# Title`` is
    lifted out as the report title.  When the remaining document still uses
    ``#`` for its sections, every heading is demoted one level so sections are
    always ``<h2>`` (the module boundary).
    """
    meta, body = split_front_matter(text)
    title = meta.get("title")
    lines = body.lstrip("\n").split("\n")
    for idx, ln in enumerate(lines):
        if not ln.strip():
            continue
        hm = re.match(r"^ {0,3}#[ \t]+(.+?)[ \t#]*$", ln)
        if hm:
            heading_title = ATTRS_RE.sub("", hm.group(1)).strip()
            title = title or re.sub(r"<[^>]+>", "", Markdown().inline(heading_title))
            title = html.unescape(title)
            lines = lines[:idx] + lines[idx + 1:]
        break
    body = "\n".join(lines)

    shift = 0
    in_fence = False
    for ln in body.split("\n"):
        if FENCE_RE.match(ln):
            in_fence = not in_fence
        elif not in_fence and re.match(r"^ {0,3}#[ \t]", ln):
            shift = 1
            break
    md = Markdown(heading_shift=shift)
    out = md.convert(body)
    return meta, title, out, md.headings
