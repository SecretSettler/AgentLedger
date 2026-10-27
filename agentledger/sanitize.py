"""Allow-list HTML sanitiser for report content.

Report pages can hold a GitHub token in memory while comments are being saved,
so report bodies written by agents (who may be quoting untrusted material) are
re-serialised through this filter: scripts, event handlers, frames, forms and
``javascript:`` URLs never reach the page.  Publish with ``--allow-scripts`` to
skip it for reports you fully trust.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

ALLOWED_TAGS = set(
    """a abbr address article aside b bdi bdo blockquote br caption center cite code col colgroup data dd del
    details dfn div dl dt em figcaption figure footer h1 h2 h3 h4 h5 h6 header hr i img input ins kbd li main
    mark nav ol p picture pre q rp rt ruby s samp section small source span strong sub summary sup table tbody
    td tfoot th thead time tr track u ul var video audio wbr font
    svg g path rect circle ellipse line polyline polygon text tspan textpath defs lineargradient radialgradient
    stop title desc use marker clippath mask pattern symbol""".split()
)
VOID_TAGS = {"br", "col", "hr", "img", "input", "source", "track", "wbr"}
# Dropped together with everything inside them.
DROP_WITH_CONTENT = {
    "script", "style", "iframe", "object", "embed", "noscript", "template", "textarea", "select",
    "button", "form", "frame", "frameset", "applet", "head", "foreignobject", "math", "dialog",
}
GLOBAL_ATTRS = {
    "id", "class", "title", "lang", "dir", "style", "role", "align", "valign", "width", "height", "hidden",
}
TAG_ATTRS = {
    "a": {"href", "name", "target", "rel"},
    "img": {"src", "alt", "loading"},
    "source": {"src", "type", "media"},
    "video": {"src", "controls", "poster", "loop", "muted", "playsinline", "preload"},
    "audio": {"src", "controls", "loop", "muted", "preload"},
    "track": {"src", "kind", "srclang", "label", "default"},
    "td": {"colspan", "rowspan", "headers"},
    "th": {"colspan", "rowspan", "headers", "scope", "abbr"},
    "col": {"span"},
    "colgroup": {"span"},
    "ol": {"start", "reversed", "type"},
    "li": {"value"},
    "details": {"open"},
    "input": {"type", "checked", "disabled"},
    "time": {"datetime"},
    "data": {"value"},
    "q": {"cite"},
    "blockquote": {"cite"},
    "del": {"cite", "datetime"},
    "ins": {"cite", "datetime"},
    "font": {"color", "size", "face"},
}
SVG_ATTRS = set(
    """viewbox xmlns xmlns:xlink fill stroke stroke-width stroke-linecap stroke-linejoin stroke-dasharray
    stroke-dashoffset stroke-opacity stroke-miterlimit fill-opacity fill-rule clip-rule opacity d x y x1 y1 x2 y2
    cx cy r rx ry fx fy points transform text-anchor dominant-baseline alignment-baseline font-size font-family
    font-weight font-style letter-spacing offset stop-color stop-opacity gradientunits gradienttransform
    spreadmethod preserveaspectratio marker-start marker-mid marker-end markerwidth markerheight markerunits
    refx refy orient clip-path clippathunits mask maskunits patternunits patterncontentunits patterntransform
    dx dy rotate textlength lengthadjust vector-effect href xlink:href pathlength visibility display
    shape-rendering paint-order startoffset""".split()
)
SVG_TAGS = {
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan", "textpath",
    "defs", "lineargradient", "radialgradient", "stop", "title", "desc", "use", "marker", "clippath", "mask",
    "pattern", "symbol",
}
URL_ATTRS = {"href", "src", "xlink:href", "poster", "cite"}
SAFE_SCHEMES = {"http", "https", "mailto", "tel"}
DATA_IMAGE_RE = re.compile(r"^data:image/(png|jpe?g|gif|webp|svg\+xml|avif);", re.IGNORECASE)
STYLE_PROPS = set(
    """color background background-color font-weight font-style font-size font-family font-variant text-align
    text-decoration text-transform vertical-align white-space word-break overflow-wrap line-height
    letter-spacing width min-width max-width height min-height max-height margin margin-top margin-right
    margin-bottom margin-left padding padding-top padding-right padding-bottom padding-left border border-top
    border-right border-bottom border-left border-color border-style border-width border-radius
    border-collapse border-spacing display float clear list-style-type opacity gap row-gap column-gap
    flex flex-direction flex-wrap flex-grow flex-shrink flex-basis align-items align-self justify-content
    grid-template-columns grid-template-rows grid-column grid-row overflow overflow-x overflow-y
    table-layout caption-side fill stroke stroke-width""".split()
)


def safe_url(value: str, attr: str, tag: str) -> str | None:
    v = re.sub(r"[\x00-\x20\x7f]+", "", value)
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):", v)
    if not m:
        return value  # relative URL or fragment
    scheme = m.group(1).lower()
    if scheme in SAFE_SCHEMES and attr != "xlink:href":
        return value
    if scheme == "data" and attr == "src" and tag in ("img", "source") and DATA_IMAGE_RE.match(v):
        return value
    return None


def safe_style(value: str) -> str:
    kept = []
    for decl in value.split(";"):
        if ":" not in decl:
            continue
        prop, val = decl.split(":", 1)
        prop, val = prop.strip().lower(), val.strip()
        if prop not in STYLE_PROPS:
            continue
        if re.search(r"url\s*\(|expression\s*\(|javascript:|@import|\\", val, re.IGNORECASE):
            continue
        kept.append(f"{prop}: {val}")
    return "; ".join(kept)


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.drop: list[str] = []  # stack of dropped container tags

    def _attrs(self, tag: str, attrs) -> str:
        allowed = GLOBAL_ATTRS | TAG_ATTRS.get(tag, set())
        if tag in SVG_TAGS:
            allowed = allowed | SVG_ATTRS
        parts = []
        external = False
        for name, value in attrs:
            name = name.lower()
            value = "" if value is None else value
            if name.startswith("on"):
                continue
            if not (name in allowed or name.startswith("data-") or name.startswith("aria-")):
                continue
            if name in URL_ATTRS:
                if tag == "use" or name == "xlink:href":
                    if not value.startswith("#"):
                        continue
                else:
                    checked = safe_url(value, name, tag)
                    if checked is None:
                        continue
                    value = checked
                    if tag == "a" and name == "href" and re.match(r"^https?:", value, re.IGNORECASE):
                        external = True
            if name == "style":
                value = safe_style(value)
                if not value:
                    continue
            if name == "id" and value.startswith("al-"):
                value = "x-" + value
            if name == "target":
                value = "_blank"
            if name == "rel":
                continue
            if tag == "input":
                if name == "type" and value.lower() != "checkbox":
                    return None
            parts.append(f' {name}="{html.escape(value, quote=True)}"' if value != "" or name not in {"checked", "disabled", "open", "controls", "hidden", "reversed", "muted", "loop", "default", "playsinline"} else f" {name}")
        if tag == "input":
            if not any(p.startswith(" disabled") for p in parts):
                parts.append(" disabled")
        if tag == "a" and external:
            parts.append(' rel="noopener noreferrer"')
        return "".join(parts)

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, closed=True)

    def _start(self, tag, attrs, closed):
        tag = tag.lower()
        if self.drop:
            if tag == self.drop[-1] and not closed:
                self.drop.append(tag)
            return
        if tag in DROP_WITH_CONTENT:
            if not closed and tag not in VOID_TAGS:
                self.drop.append(tag)
            return
        if tag not in ALLOWED_TAGS:
            return
        rendered = self._attrs(tag, attrs)
        if rendered is None:
            return
        if closed and tag not in VOID_TAGS:
            self.out.append(f"<{tag}{rendered}/>" if tag in SVG_TAGS else f"<{tag}{rendered}></{tag}>")
        else:
            self.out.append(f"<{tag}{rendered}>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.drop:
            if tag == self.drop[-1]:
                self.drop.pop()
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.drop:
            self.out.append(html.escape(data, quote=False))


def sanitize(fragment: str) -> str:
    parser = _Sanitizer()
    parser.feed(fragment)
    parser.close()
    return "".join(parser.out)
