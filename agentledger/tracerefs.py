"""Trace citations: a claim in a report points at the trace lines behind it.

In Markdown ``[4721 us](trace:run.log#L12-L14)``, in an HTML report ``<a href="trace:run.log#L12-L14">``.  The name
is the trace's path under the version's ``traces/`` folder without ``.gz``: ``run.log`` for ``--trace path/run.log``,
``logs/a.txt`` for a file in a folder passed to ``--trace``, the prepared copy's name for ``--trace auto``.  Lines are
1-based: ``#L12`` or ``#L12-L14``; none points at the whole file.  ``trace:v2/run.log`` names another version's
trace; without it, the citation resolves to this version's trace, else the nearest earlier version's by that name.

At build time each citation becomes a link to an excerpt kept in the page (the cited lines and a few around them),
so a reader sees the evidence without the trace, on any machine, from a shared bundle too.  ``check`` lists the
citations that do not resolve, for ``publish`` and ``agentledger check`` to warn about.
"""

from __future__ import annotations

import gzip
import html as htmlmod
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CONTEXT = 3            # lines shown around the cited ones
MAX_LINES = 60         # lines kept per citation
MAX_CHARS = 1200       # characters kept per line
PAGE_BUDGET = 1 << 20  # excerpt text kept per page; later citations link to the trace only

REF_RE = re.compile(r"^(?:trace:)?(?:v(?P<v>\d+)/)?(?P<name>[^#\s]+?)(?:#L(?P<a>\d+)(?:-L?(?P<b>\d+))?)?$")
TEXT_RE = re.compile(r"trace:(?:v\d+/)?[\w.\-/]+(?:#L\d+(?:-L?\d+)?)?")
HREF_RE = re.compile(r"""href=(["'])trace:(.*?)\1""")


@dataclass(frozen=True)
class Ref:
    name: str
    start: Optional[int] = None
    end: Optional[int] = None
    version: Optional[int] = None

    def text(self) -> str:
        out = "trace:" + (f"v{self.version}/" if self.version else "") + self.name
        if self.start:
            out += f"#L{self.start}" + (f"-L{self.end}" if self.end and self.end != self.start else "")
        return out

    def lines(self) -> str:
        if not self.start:
            return ""
        return f"line {self.start}" if self.end == self.start else f"lines {self.start}-{self.end}"


def parse(value: str) -> Optional[Ref]:
    m = REF_RE.match(value.strip())
    if not m or ".." in m.group("name").split("/"):
        return None
    a = int(m.group("a")) if m.group("a") else None
    b = int(m.group("b")) if m.group("b") else a
    if a is not None and (a < 1 or b < a):
        return None
    return Ref(m.group("name"), a, b, int(m.group("v")) if m.group("v") else None)


def find(text: str) -> list[Ref]:
    """Citations written as plain text (in a comment or an answer): ``trace:run.log#L12-L14``."""
    out = []
    for m in TEXT_RE.finditer(text or ""):
        raw = m.group(0)
        if "#L" not in raw:
            raw = raw.rstrip(".,;:")
        ref = parse(raw)
        if ref and ref not in out:
            out.append(ref)
    return out


def trace_name(trace: dict) -> str:
    """The name a citation uses for a stored trace: its path under traces/, without .gz."""
    rel = trace["path"].split("/", 1)[1] if trace["path"].startswith("traces/") else trace["path"]
    return rel[:-3] if rel.endswith(".gz") else rel


def resolve(report, v, ref: Ref):
    """``(version, trace entry, stored file)`` for ``ref`` cited from version ``v``, or None."""
    if ref.version:
        candidates = [x for x in report.versions if x.number == ref.version]
    else:
        candidates = [x for x in reversed(report.versions) if x.number <= v.number]
    for x in candidates:
        traces = x.meta.get("traces", [])
        for t in traces:
            if trace_name(t) == ref.name:
                return x, t, x.path / t["path"]
        if "/" not in ref.name:  # `run.log` also finds logs/run.log when only one trace has that file name
            hits = [t for t in traces if trace_name(t).rsplit("/", 1)[-1] == ref.name]
            if len(hits) == 1:
                return x, hits[0], x.path / hits[0]["path"]
    return None


def _open(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open(encoding="utf-8", errors="replace")


def excerpt(path: Path, ref: Ref, context: int = CONTEXT, max_lines: int = MAX_LINES) -> dict:
    """The cited lines and ``context`` lines around them: ``{"lines": [[n, text], ...]}``, or ``{"error": ...}``."""
    start, end = (ref.start, ref.end) if ref.start else (1, min(20, max_lines))
    lo, hi = max(1, start - context), end + context
    lines, last = [], 0
    try:
        with _open(path) as fh:
            for last, line in enumerate(fh, 1):
                if "\x00" in line:
                    return {"error": "a binary file: it cannot be shown as lines"}
                if last < lo:
                    continue
                if last > hi or len(lines) >= max_lines:
                    break
                text = line.rstrip("\r\n")
                lines.append([last, text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + " …"])
    except (OSError, EOFError) as exc:
        return {"error": f"cannot read the trace ({exc.__class__.__name__})"}
    if ref.start and (not lines or lines[-1][0] < start):
        return {"error": f"the trace has {last} line{'s' if last != 1 else ''}, the citation starts at line {start}"}
    return {"lines": lines}


class Linker:
    """Rewrite one version's ``trace:`` links into links to excerpts kept in the page."""

    def __init__(self, store, report, v, href_for):
        self.store, self.report, self.v, self.href_for = store, report, v, href_for
        self.refs: dict[str, dict] = {}
        self._ids: dict[str, str] = {}
        self.budget = PAGE_BUDGET
        self.cited: dict[str, int] = {}  # stored trace path -> how many citations point at it

    def entry(self, raw: str) -> dict:
        ref = parse(raw)
        if ref is None:
            return {"ref": "trace:" + raw, "error": "not a trace citation: write trace:<file>#L<first>-L<last>"}
        found = resolve(self.report, self.v, ref)
        if not found:
            where = f"in v{ref.version}" if ref.version else f"in v{self.v.number} or an earlier version"
            return {"ref": ref.text(), "name": ref.name, "error": f"no trace named {ref.name} {where}"}
        x, t, path = found
        out = {"ref": ref.text(), "name": trace_name(t), "version": x.number, "start": ref.start, "end": ref.end,
               "href": self.href_for(path), "path": self.store.repo_path(path), "bytes": t.get("bytes", 0)}
        got = excerpt(path, ref)
        if "error" in got:
            out["error"] = got["error"]
        else:
            size = sum(len(s) for _, s in got["lines"])
            if size <= self.budget:
                out["lines"] = got["lines"]
                self.budget -= size
        key = str(path)
        self.cited[key] = self.cited.get(key, 0) + 1
        return out

    def rewrite(self, html: str) -> str:
        def sub(m: re.Match) -> str:
            raw = htmlmod.unescape(m.group(2))
            rid = self._ids.get(raw)
            if rid is None:
                rid = self._ids[raw] = f"tr{len(self._ids) + 1}"
                self.refs[rid] = self.entry(raw)
            bad = ' data-tref-error="1"' if "error" in self.refs[rid] else ""
            return f'href="#{rid}" data-tref="{rid}"{bad}'
        return HREF_RE.sub(sub, html)

    def problems(self) -> list[str]:
        return [f"{e['ref']}: {e['error']}" for e in self.refs.values() if "error" in e]


def check(store, report, v) -> list[str]:
    """The citations in version ``v`` that do not resolve to trace lines."""
    from .build import render_version

    linker = Linker(store, report, v, lambda p: "")
    for m in render_version(v).modules:
        linker.rewrite(m.html)
    return linker.problems()
