"""Render every generated file in a store.

Each report version becomes one self-contained ``index.html`` (CSS, JS and
data inlined) so it works from ``file://``, a static host, githack, or after an
``scp``.  Output is deterministic: rebuilding an unchanged store writes nothing.
"""

from __future__ import annotations

import html
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __version__
from . import comments as cm
from .markdown import Markdown
from .markdown import render as render_markdown
from .modules import Module, split_modules
from .sanitize import sanitize
from .store import Project, Report, Store, Version, write_json, write_text

ASSETS = Path(__file__).parent / "assets"


def asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def script_json(data) -> str:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text.replace("</", "<\\/").replace("<!--", "<\\!--")


def fill(template: str, parts: dict) -> str:
    return re.sub(r"<!--AL:(\w+)-->", lambda m: parts[m.group(1)], template)


def href(target: Path, start: Path) -> str:
    return Path(os.path.relpath(target, start)).as_posix()


def esc(text) -> str:
    return html.escape(str(text or ""), quote=True)


def inline_md(text: str) -> str:
    return sanitize(Markdown().inline(text or ""))


@dataclass
class Rendered:
    title: str
    modules: list[Module]


def render_version(v: Version) -> Rendered:
    src = v.source
    text = src.read_text(encoding="utf-8")
    if src.suffix == ".md":
        _, title, body, _ = render_markdown(text)
    else:
        tm = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        bm = re.search(r"<body[^>]*>(.*)</body>", text, re.IGNORECASE | re.DOTALL)
        body = bm.group(1) if bm else re.sub(r"(?is)<head\b.*?</head>|<!doctype[^>]*>|</?html[^>]*>", "", text)
        h1 = re.search(r"^\s*<h1[^>]*>(.*?)</h1>", body, re.IGNORECASE | re.DOTALL)
        title = re.sub(r"<[^>]+>", "", (tm or h1).group(1)).strip() if (tm or h1) else None
        if h1:
            body = body[h1.end():]
    if not v.meta.get("allow_scripts"):
        body = sanitize(body)
    return Rendered(title=v.meta.get("title") or title or v.report.title, modules=split_modules(body))


def module_states(cur: Rendered, prev: Optional[Rendered]) -> tuple[dict, list[dict]]:
    if prev is None:
        return {m.id: "" for m in cur.modules}, []
    before = {m.id: m.hash for m in prev.modules}
    states = {m.id: ("new" if m.id not in before else "changed" if before[m.id] != m.hash else "same") for m in cur.modules}
    ids = {m.id for m in cur.modules}
    removed = [{"id": m.id, "title": m.title} for m in prev.modules if m.id not in ids]
    return states, removed


def open_count(rows: list[dict]) -> int:
    return sum(1 for r in rows if not r["reply_to"] and r["status"] == "open")


class Builder:
    def __init__(self, store: Store):
        self.store = store
        self.written: list[Path] = []
        self.rendered: dict[tuple[str, int], Rendered] = {}
        self.comments: dict[str, list[dict]] = {}

    # ----------------------------------------------------------- helpers
    def write(self, path: Path, text: str) -> None:
        if write_text(path, text):
            self.written.append(path)

    def render(self, v: Version) -> Rendered:
        key = (v.report.id, v.number)
        if key not in self.rendered:
            self.rendered[key] = render_version(v)
        return self.rendered[key]

    def report_comments(self, report: Report) -> list[dict]:
        if report.id not in self.comments:
            self.comments[report.id] = self.store.comments(report)
        return self.comments[report.id]

    def store_info(self, start: Path) -> dict:
        return {
            "name": self.store.name,
            "key": self.store.key,
            "github": self.store.github,
            "root": href(self.store.root, start) + "/",
            "dashboard": href(self.store.root / "index.html", start),
        }

    def version_entry(self, v: Version, start: Path) -> dict:
        return {
            "n": v.number,
            "title": v.meta.get("title") or v.report.title,
            "created": v.created,
            "author": v.meta.get("author", ""),
            "summary": v.meta.get("summary", ""),
            "changes": v.meta.get("changes", ""),
            "href": href(v.view_path, start),
            "comments_href": href(v.comments_path, start),
            "comments_path": self.store.repo_path(v.comments_path),
            "source_path": self.store.repo_path(v.source),
        }

    def report_entry(self, r: Report, start: Path, depth: int = 0, *, children: bool = True) -> dict:
        rows = self.report_comments(r)
        entry = {
            "id": r.id,
            "title": r.title,
            "about": r.info.get("about", ""),
            "tags": r.info.get("tags", []),
            "status": r.info.get("status", ""),
            "created": r.info.get("created", ""),
            "updated": r.latest.created if r.latest else r.info.get("updated", ""),
            "depth": depth,
            "href": href(r.latest.view_path, start) if r.latest else None,
            "versions": [self.version_entry(v, start) for v in r.versions],
            "open": open_count(rows),
            "threads": sum(1 for x in rows if not x["reply_to"] and x["status"] != "deleted"),
        }
        if children:
            entry["children"] = [self.report_entry(c, start, depth + 1) for c in r.children]
        return entry

    # ------------------------------------------------------------- build
    def build(self) -> list[Path]:
        for project in self.store.projects():
            for report in project.walk():
                for idx, v in enumerate(report.versions):
                    prev = report.versions[idx - 1] if idx else None
                    self.write(v.view_path, self.report_page(project, report, v, prev))
                self.write(report.path / "index.html", self.redirect_report(report))
            self.write(project.path / "index.html", self.redirect_project(project))
        self.write(self.store.root / "index.html", self.dashboard())
        write_json_path = self.store.root / "manifest.json"
        if write_json(write_json_path, self.manifest()):
            self.written.append(write_json_path)
        return self.written

    # ------------------------------------------------------- report page
    def report_page(self, project: Project, report: Report, v: Version, prev: Optional[Version]) -> str:
        start = v.path
        cur = self.render(v)
        states, removed = module_states(cur, self.render(prev) if prev else None)
        rows = self.report_comments(report)

        project_flat = []

        def flatten(r: Report, depth: int) -> None:
            e = self.report_entry(r, start, depth, children=False)
            e["current"] = r is report
            project_flat.append(e)
            for c in r.children:
                flatten(c, depth + 1)

        for r in project.reports:
            flatten(r, 0)

        crumbs = [{"title": self.store.name, "href": href(self.store.root / "index.html", start)},
                  {"title": project.title, "href": href(self.store.root / "index.html", start) + "#project=" + project.id}]
        anc, p = [], report.parent
        while p:
            anc.insert(0, p)
            p = p.parent
        for a in anc:
            crumbs.append({"title": a.title, "href": href(a.latest.view_path, start) if a.latest else None})

        versions = []
        for x in report.versions:
            e = self.version_entry(x, start)
            e["modules"] = [m.id for m in self.render(x).modules]
            versions.append(e)

        data = {
            "app": __version__,
            "store": self.store_info(start),
            "project": {"id": project.id, "title": project.title},
            "report": {
                "id": report.id,
                "title": report.title,
                "about": report.info.get("about", ""),
                "tags": report.info.get("tags", []),
                "status": report.info.get("status", ""),
                "parent": self.report_entry(report.parent, start, children=False) if report.parent else None,
                "children": [self.report_entry(c, start, children=False) for c in report.children],
            },
            "version": {
                "n": v.number,
                "title": cur.title,
                "created": v.created,
                "author": v.meta.get("author", ""),
                "summary": v.meta.get("summary", ""),
                "changes": v.meta.get("changes", ""),
                "resolves": v.meta.get("resolves", []),
                "source_path": self.store.repo_path(v.source),
                "comments_path": self.store.repo_path(v.comments_path),
            },
            "latest": report.latest.number,
            "versions": versions,
            "modules": [
                {"id": m.id, "title": m.title, "state": states.get(m.id, ""), "status": m.attrs.get("status", ""),
                 "subs": m.subs, "words": m.words}
                for m in cur.modules
            ],
            "removed": removed,
            "comments": rows,
            "map": project_flat,
            "crumbs": crumbs,
        }
        return fill(asset("report.html"), {
            "TITLE": esc(f"{cur.title} · v{v.number}"),
            "CSS": asset("common.css") + asset("viewer.css"),
            "JS": asset("core.js") + asset("viewer.js"),
            "DATA": script_json(data),
            "BODY": self.article(project, report, v, cur, states, removed, rows, start),
        })

    def article(self, project: Project, report: Report, v: Version, cur: Rendered, states: dict,
                removed: list[dict], rows: list[dict], start: Path) -> str:
        meta = v.meta
        dash = href(self.store.root / "index.html", start)
        kicker = f'<a href="{esc(dash)}#project={esc(project.id)}">{esc(project.title)}</a>'
        if report.info.get("about"):
            kicker += f' <span class="al-dot">·</span> <span>{esc(report.info["about"])}</span>'
        words = sum(m.words for m in cur.modules)
        parts = [
            '<header class="al-doc-head">',
            f'<div class="al-kicker">{kicker}</div>',
            f'<h1 class="al-title">{esc(cur.title)}</h1>',
        ]
        if meta.get("summary"):
            parts.append(f'<p class="al-summary">{inline_md(meta["summary"])}</p>')
        info = [f'<span class="al-vtag">v{v.number}</span>',
                f'<time datetime="{esc(v.created)}">{esc(v.created[:10])}</time>']
        if meta.get("author"):
            info.append(f"by {esc(meta['author'])}")
        ctx = meta.get("context") or {}
        if ctx.get("commit"):
            repo = re.sub(r"\.git$", "", (ctx.get("repo") or "").rstrip("/")).rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            label = f"{repo}@{ctx['commit']}" + ("+dirty" if ctx.get("dirty") else "")
            tip = f"{ctx.get('repo', '')} · branch {ctx.get('branch', '')} · commit {ctx['commit']}"
            info.append(f'<span title="{esc(tip)}">from <code>{esc(label)}</code></span>')
        info.append(f"{len(cur.modules)} module{'s' if len(cur.modules) != 1 else ''}")
        info.append(f"{words:,} words")
        parts.append('<div class="al-docmeta">' + ' <span class="al-dot">·</span> '.join(info) + "</div>")

        changed = [m for m in cur.modules if states.get(m.id) in ("new", "changed")]
        if v.number > 1 or meta.get("changes"):
            box = ['<div class="al-changes">', f'<div class="al-changes-h">What changed in v{v.number}</div>']
            if meta.get("changes"):
                box.append(f'<p>{inline_md(meta["changes"])}</p>')
            if changed or removed:
                chips = "".join(
                    f'<a class="al-chip al-state-{states[m.id]}" href="#{esc(m.id)}">{esc(m.title)}'
                    f'<small>{states[m.id]}</small></a>' for m in changed)
                chips += "".join(f'<span class="al-chip al-state-removed">{esc(m["title"])}<small>removed</small></span>' for m in removed)
                box.append(f'<div class="al-chips">{chips}</div>')
            elif v.number > 1:
                box.append('<p class="al-muted">No module text changed.</p>')
            if meta.get("resolves"):
                n = len(meta["resolves"])
                box.append(f'<p class="al-muted">Resolves {n} review thread{"s" if n != 1 else ""}.</p>')
            box.append("</div>")
            parts.extend(box)
        parts.append("</header>")

        for m in cur.modules:
            attrs = f' id="{esc(m.id)}" data-module="{esc(m.id)}"'
            if states.get(m.id):
                attrs += f' data-state="{states[m.id]}"'
            if m.attrs.get("status"):
                attrs += f' data-status="{esc(m.attrs["status"])}"'
            parts.append(f'<section class="al-module"{attrs}>\n{m.html}\n</section>')

        foot = ['<footer class="al-doc-foot">']
        if report.children:
            foot.append('<div class="al-foot-block"><div class="al-foot-h">Sub-reports</div><div class="al-cards">')
            for c in report.children:
                target = href(c.latest.view_path, start) if c.latest else "#"
                foot.append(
                    f'<a class="al-card" href="{esc(target)}"><strong>{esc(c.title)}</strong>'
                    f'<span>{esc(c.info.get("about", ""))}</span>'
                    f'<small>{len(c.versions)} version{"s" if len(c.versions) != 1 else ""}'
                    f'{" · " + str(open_count(self.report_comments(c))) + " open" if open_count(self.report_comments(c)) else ""}</small></a>')
            foot.append("</div></div>")
        foot.append('<div class="al-foot-block"><div class="al-foot-h">Version history</div><ol class="al-history" reversed>')
        for x in reversed(report.versions):
            cls = ' class="is-current"' if x is v else ""
            label = f"v{x.number}"
            link = label if x is v else f'<a href="{esc(href(x.view_path, start))}">{label}</a>'
            desc = x.meta.get("changes") or x.meta.get("summary") or ""
            foot.append(f'<li{cls}><span class="al-hv">{link}</span> <time datetime="{esc(x.created)}">{esc(x.created[:10])}</time>'
                        f'{" · " + esc(x.meta.get("author")) if x.meta.get("author") else ""}'
                        f'{"<div>" + inline_md(desc) + "</div>" if desc else ""}</li>')
        foot.append("</ol></div>")
        foot.append(f'<div class="al-foot-src">Source: <code>{esc(self.store.repo_path(v.source))}</code> · '
                    f'comments: <code>{esc(self.store.repo_path(v.comments_path))}</code></div>')
        foot.append("</footer>")
        parts.extend(foot)
        return "\n".join(parts)

    # --------------------------------------------------------- redirects
    def redirect(self, title: str, target: str) -> str:
        t = esc(target)
        return (
            "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
            f"<title>{esc(title)}</title>"
            f"<meta http-equiv=\"refresh\" content=\"0; url={t}\">"
            f"<script>location.replace({json.dumps(target)} + location.hash)</script>"
            f"</head><body><a href=\"{t}\">{esc(title)}</a></body></html>\n"
        )

    def redirect_report(self, report: Report) -> str:
        if report.latest:
            return self.redirect(report.title, href(report.latest.view_path, report.path))
        return self.redirect(report.title, href(self.store.root / "index.html", report.path) + "#report=" + report.id)

    def redirect_project(self, project: Project) -> str:
        return self.redirect(project.title, href(self.store.root / "index.html", project.path) + "#project=" + project.id)

    # --------------------------------------------------------- dashboard
    def dashboard(self) -> str:
        root = self.store.root
        projects = [
            {
                "id": p.id,
                "title": p.title,
                "description": p.info.get("description", ""),
                "reports": [self.report_entry(r, root) for r in p.reports],
            }
            for p in self.store.projects()
        ]
        rows = [row for p in self.store.projects() for r in p.walk() for row in self.report_comments(r)]
        data = {"app": __version__, "store": self.store_info(root), "projects": projects, "comments": cm.sort_rows(rows)}
        return fill(asset("dashboard.html"), {
            "TITLE": esc(self.store.name),
            "CSS": asset("common.css") + asset("dashboard.css"),
            "JS": asset("core.js") + asset("dashboard.js"),
            "DATA": script_json(data),
            "BODY": "",
        })

    def manifest(self) -> dict:
        def node(r: Report) -> dict:
            rows = self.report_comments(r)
            return {
                "id": r.id,
                "title": r.title,
                "about": r.info.get("about", ""),
                "tags": r.info.get("tags", []),
                "status": r.info.get("status", ""),
                "path": self.store.rel(r.path),
                "latest": r.latest.number if r.latest else None,
                "versions": [
                    {"n": v.number, "path": self.store.rel(v.path), "source": self.store.rel(v.source),
                     "created": v.created, "author": v.meta.get("author", ""), "summary": v.meta.get("summary", "")}
                    for v in r.versions
                ],
                "open_threads": open_count(rows),
                "children": [node(c) for c in r.children],
            }

        return {
            "name": self.store.name,
            "generator": "agentledger",
            "projects": [
                {"id": p.id, "title": p.title, "description": p.info.get("description", ""),
                 "path": self.store.rel(p.path), "reports": [node(r) for r in p.reports]}
                for p in self.store.projects()
            ],
        }


def build(store: Store) -> list[Path]:
    store.invalidate()
    return Builder(store).build()
