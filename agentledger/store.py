"""The ledger store: a folder (usually its own git repo) with a fixed layout.

    <store>/
      agentledger.json                 store config (name, GitHub repo for saving comments)
      index.html                       dashboard            (generated)
      manifest.json                    machine-readable index (generated)
      projects/
        <project>/
          project.json                 title, description
          index.html                   -> dashboard filtered to the project (generated)
          <report>/                    one topic: "what the report is about"
            report.json                title, about, tags, status
            index.html                 -> latest version (generated)
            v001/
              report.md | report.html  source, as published by the agent
              meta.json                publish record: author, summary, changes, resolves
              comments.csv             review threads
              index.html               the report viewer (generated)
              ...                      assets the report references
            v002/
            <sub-report>/              same shape, nested under its parent report

Nothing else belongs in ``projects/``; ``agentledger check`` reports strays.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from . import comments as cm
from .markdown import slugify, split_front_matter

CONFIG_NAME = "agentledger.json"
PROJECTS = "projects"
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
VERSION_DIR_RE = re.compile(r"^v(\d{3,})$")
SOURCE_NAMES = ("report.md", "report.html")
MD_SUFFIXES = {".md", ".markdown", ".txt"}
HTML_SUFFIXES = {".html", ".htm"}
RESERVED = {"assets", "index", "projects"}
GENERATED_OR_SOURCE = {"report.md", "report.html", "meta.json", "comments.csv", "index.html"}


class LedgerError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise LedgerError(f"{path}: invalid JSON ({exc})") from exc


def write_json(path: Path, data: dict) -> bool:
    return write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def write_text(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def check_slug(seg: str, what: str = "name") -> str:
    if not SLUG_RE.match(seg) or VERSION_DIR_RE.match(seg) or seg in RESERVED:
        raise LedgerError(
            f"invalid {what} {seg!r}: use lowercase letters, digits and hyphens "
            f"(e.g. {slugify(seg)!r}); 'v001'-style names and {sorted(RESERVED)} are reserved"
        )
    return seg


def parse_report_id(report_id: str) -> list[str]:
    segs = [s for s in report_id.strip().strip("/").split("/") if s]
    if segs and segs[0] == PROJECTS:
        segs = segs[1:]
    if len(segs) < 2:
        raise LedgerError(f"report id {report_id!r} must be <project>/<report>[/<sub-report>...]")
    for s in segs:
        check_slug(s, "path segment")
    return segs


@dataclass(eq=False)
class Version:
    report: "Report"
    number: int
    path: Path
    meta: dict

    @property
    def name(self) -> str:
        return f"v{self.number:03d}"

    @property
    def source(self) -> Path:
        named = self.meta.get("source")
        if named and (self.path / named).exists():
            return self.path / named
        for name in SOURCE_NAMES:
            if (self.path / name).exists():
                return self.path / name
        raise LedgerError(f"{self.path}: no report.md or report.html")

    @property
    def comments_path(self) -> Path:
        return self.path / "comments.csv"

    @property
    def view_path(self) -> Path:
        return self.path / "index.html"

    @property
    def created(self) -> str:
        return self.meta.get("created", "")


@dataclass(eq=False)
class Report:
    id: str
    path: Path
    info: dict
    parent: Optional["Report"] = None
    versions: list[Version] = field(default_factory=list)
    children: list["Report"] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return self.id.rsplit("/", 1)[-1]

    @property
    def project_id(self) -> str:
        return self.id.split("/", 1)[0]

    @property
    def title(self) -> str:
        return self.info.get("title") or self.slug.replace("-", " ").capitalize()

    @property
    def latest(self) -> Optional[Version]:
        return self.versions[-1] if self.versions else None

    @property
    def depth(self) -> int:
        return self.id.count("/") - 1

    def version(self, number: int) -> Version:
        for v in self.versions:
            if v.number == number:
                return v
        raise LedgerError(f"{self.id} has no version {number}")

    def walk(self) -> Iterator["Report"]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(eq=False)
class Project:
    id: str
    path: Path
    info: dict
    reports: list[Report] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.info.get("title") or self.id.replace("-", " ").capitalize()

    def walk(self) -> Iterator[Report]:
        for r in self.reports:
            yield from r.walk()


def _is_report_dir(path: Path) -> bool:
    if (path / "report.json").exists():
        return True
    return any(c.is_dir() and VERSION_DIR_RE.match(c.name) for c in path.iterdir())


def find_root(start: Optional[Path] = None) -> Path:
    env = os.environ.get("AGENTLEDGER_STORE")
    if env:
        root = Path(env).expanduser().resolve()
        if not (root / CONFIG_NAME).exists():
            raise LedgerError(f"AGENTLEDGER_STORE={env} has no {CONFIG_NAME}")
        return root
    here = (start or Path.cwd()).resolve()
    for p in [here, *here.parents]:
        if (p / CONFIG_NAME).exists():
            return p
    raise LedgerError(f"no store configured: no {CONFIG_NAME} here or above and AGENTLEDGER_STORE is unset; "
                      "run `agentledger setup <path-to-store>` once (or `agentledger init` to create a store)")


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.config = read_json(self.root / CONFIG_NAME)
        self._projects: Optional[list[Project]] = None

    # ------------------------------------------------------------ config
    @property
    def name(self) -> str:
        return self.config.get("name") or self.root.name

    @property
    def key(self) -> str:
        return self.config.get("key") or slugify(self.name)

    @property
    def github(self) -> Optional[dict]:
        gh = self.config.get("github") or {}
        if not gh.get("repo"):
            return None
        return {"repo": gh["repo"], "branch": gh.get("branch") or "main", "root": (gh.get("root") or "").strip("/")}

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def repo_path(self, path: Path) -> str:
        gh = self.github
        rel = self.rel(path)
        return f"{gh['root']}/{rel}" if gh and gh["root"] else rel

    # ------------------------------------------------------------- tree
    def invalidate(self) -> None:
        self._projects = None

    def projects(self) -> list[Project]:
        if self._projects is None:
            self._projects = self._scan()
        return self._projects

    def _scan(self) -> list[Project]:
        base = self.root / PROJECTS
        projects = []
        if not base.is_dir():
            return projects
        for pdir in sorted(base.iterdir()):
            if not pdir.is_dir() or pdir.name.startswith("."):
                continue
            project = Project(pdir.name, pdir, read_json(pdir / "project.json"))
            for rdir in sorted(pdir.iterdir()):
                if rdir.is_dir() and not rdir.name.startswith(".") and _is_report_dir(rdir):
                    project.reports.append(self._scan_report(rdir, f"{pdir.name}/{rdir.name}", None))
            project.reports.sort(key=_report_order)
            projects.append(project)
        projects.sort(key=lambda p: (p.info.get("order", 1_000_000), p.title.lower()))
        return projects

    def _scan_report(self, path: Path, rid: str, parent: Optional[Report]) -> Report:
        report = Report(rid, path, read_json(path / "report.json"), parent)
        for child in sorted(path.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            m = VERSION_DIR_RE.match(child.name)
            if m:
                report.versions.append(Version(report, int(m.group(1)), child, read_json(child / "meta.json")))
            elif _is_report_dir(child):
                report.children.append(self._scan_report(child, f"{rid}/{child.name}", report))
        report.versions.sort(key=lambda v: v.number)
        report.children.sort(key=_report_order)
        return report

    def project(self, project_id: str) -> Optional[Project]:
        return next((p for p in self.projects() if p.id == project_id), None)

    def reports(self) -> list[Report]:
        return [r for p in self.projects() for r in p.walk()]

    def report(self, report_id: str) -> Report:
        rid = "/".join(parse_report_id(report_id))
        for r in self.reports():
            if r.id == rid:
                return r
        raise LedgerError(f"no report {rid!r} (see `agentledger tree`)")

    def report_dir(self, report_id: str) -> Path:
        return self.root / PROJECTS / Path(*parse_report_id(report_id))

    # ---------------------------------------------------------- writing
    def ensure_report(self, report_id: str, *, title: str | None = None, about: str | None = None) -> Path:
        segs = parse_report_id(report_id)
        pdir = self.root / PROJECTS / segs[0]
        if not (pdir / "project.json").exists():
            write_json(pdir / "project.json", {"title": segs[0].replace("-", " ").capitalize(), "description": "", "created": now_iso()})
        path = pdir
        for depth, seg in enumerate(segs[1:], start=1):
            path = path / seg
            if not (path / "report.json").exists():
                is_target = depth == len(segs) - 1
                info = {
                    "title": (title if is_target and title else seg.replace("-", " ").capitalize()),
                    "about": (about or "") if is_target else "",
                    "tags": [],
                    "status": "",
                    "created": now_iso(),
                }
                write_json(path / "report.json", info)
        self.invalidate()
        return path

    def publish(
        self,
        report_id: str,
        source: Path,
        *,
        title: str | None = None,
        summary: str | None = None,
        about: str | None = None,
        author: str | None = None,
        changes: str | None = None,
        resolves: tuple[str, ...] | list[str] = (),
        assets: tuple[Path, ...] | list[Path] = (),
        tags: list[str] | None = None,
        allow_scripts: bool = False,
        carry_assets: bool = True,
        context: dict | None = None,
    ) -> Version:
        source = Path(source)
        if not source.exists():
            raise LedgerError(f"source {source} does not exist")
        rdir = self.ensure_report(report_id, title=title, about=about)
        existing = [int(m.group(1)) for c in rdir.iterdir() if c.is_dir() and (m := VERSION_DIR_RE.match(c.name))]
        number = max(existing, default=0) + 1
        vdir = rdir / f"v{number:03d}"
        vdir.mkdir()

        try:
            if source.is_dir():
                parts = []
                for f in sorted(source.rglob("*")):
                    rel = f.relative_to(source)
                    if any(p.startswith(".") for p in rel.parts) or f.is_dir():
                        continue
                    if f.suffix.lower() in MD_SUFFIXES and len(rel.parts) == 1:
                        parts.append(f.read_text(encoding="utf-8").strip("\n"))
                    else:
                        (vdir / rel).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, vdir / rel)
                if not parts:
                    raise LedgerError(f"{source}: no .md files at the top level")
                # Only the first part may carry front matter / the title heading.
                text = "\n\n".join(parts) + "\n"
                src_name = "report.md"
            elif source.suffix.lower() in MD_SUFFIXES:
                text, src_name = source.read_text(encoding="utf-8"), "report.md"
            elif source.suffix.lower() in HTML_SUFFIXES:
                text, src_name = source.read_text(encoding="utf-8"), "report.html"
            else:
                raise LedgerError(f"{source}: publish a .md or .html file, or a folder of .md files")
            (vdir / src_name).write_text(text, encoding="utf-8")
            prev = vdir.parent / f"v{number - 1:03d}"
            if carry_assets and prev.is_dir():
                # Images and data the previous version referenced keep working in the new one.
                for f in sorted(prev.rglob("*")):
                    rel = f.relative_to(prev)
                    if f.is_dir() or (len(rel.parts) == 1 and rel.name in GENERATED_OR_SOURCE) or (vdir / rel).exists():
                        continue
                    (vdir / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, vdir / rel)
            for asset in assets:
                asset = Path(asset)
                dest = vdir / asset.name
                if asset.is_dir():
                    shutil.copytree(asset, dest, dirs_exist_ok=True)
                elif asset.exists():
                    shutil.copy2(asset, dest)
                else:
                    raise LedgerError(f"asset {asset} does not exist")
        except Exception:
            shutil.rmtree(vdir, ignore_errors=True)
            raise

        front = split_front_matter(text)[0] if src_name == "report.md" else {}
        doc_title = title or front.get("title") or _doc_title(text, src_name)
        meta = {
            "version": number,
            "title": doc_title or "",
            "created": now_iso(),
            "author": author or front.get("author") or os.environ.get("AGENTLEDGER_AUTHOR", ""),
            "summary": summary if summary is not None else front.get("summary", ""),
            "changes": changes if changes is not None else front.get("changes", ""),
            "resolves": list(resolves),
            "source": src_name,
        }
        if context:
            meta["context"] = context
        if allow_scripts:
            meta["allow_scripts"] = True
        write_json(vdir / "meta.json", meta)
        cm.write(vdir / "comments.csv", [])

        info = read_json(rdir / "report.json")
        if doc_title:
            info["title"] = doc_title
        if about is not None:
            info["about"] = about
        elif front.get("about") and not info.get("about"):
            info["about"] = front["about"]
        if tags is not None or front.get("tags"):
            info["tags"] = tags if tags is not None else front["tags"]
        info["updated"] = meta["created"]
        write_json(rdir / "report.json", info)
        self.invalidate()

        for cid in resolves:
            self.set_status(cid, "resolved", resolved_in=number)
        return self.report(report_id).version(number)

    # --------------------------------------------------------- comments
    def comments(self, report: Optional[Report] = None) -> list[dict]:
        rows = []
        for r in [report] if report else self.reports():
            for v in r.versions:
                for row in cm.read(v.comments_path):
                    row["report"] = r.id
                    row["version"] = str(v.number)
                    rows.append(row)
        return rows

    def find_comment(self, cid: str) -> dict:
        for row in self.comments():
            if row["id"] == cid:
                return row
        raise LedgerError(f"no comment {cid!r}")

    def save_rows(self, rows: list[dict]) -> int:
        """Merge rows into the comments.csv of the version each belongs to."""
        groups: dict[Path, list[dict]] = {}
        for row in rows:
            row = cm.normalize(row)
            if not row["id"]:
                raise LedgerError("comment row without id")
            if row["status"] and row["status"] not in cm.STATUSES:
                raise LedgerError(f"comment {row['id']}: unknown status {row['status']!r}")
            if row["reply_to"]:
                root = next((r for r in rows if r.get("id") == row["reply_to"]), None)
                if root is None:
                    try:
                        root = self.find_comment(row["reply_to"])
                    except LedgerError:
                        root = None
                if root is not None:
                    row["report"], row["version"] = root["report"], cm.normalize(root)["version"]
            if not row["report"] or not row["version"]:
                raise LedgerError(f"comment {row['id']}: missing report/version")
            version = self.report(row["report"]).version(int(row["version"]))
            groups.setdefault(version.comments_path, []).append(row)
        changed = 0
        for path, incoming in groups.items():
            merged, n = cm.merge(cm.read(path), incoming)
            if n:
                cm.write(path, merged)
                changed += n
        return changed

    def add_comment(self, report_id: str, body: str, *, version: int | None = None, author: str = "", role: str = "human",
                    module: str = "", quote: str = "", kind: str = "note") -> dict:
        report = self.report(report_id)
        if not report.versions:
            raise LedgerError(f"{report.id} has no versions yet")
        v = report.version(version) if version else report.latest
        ts = now_iso()
        row = cm.normalize({
            "id": cm.new_id(), "report": report.id, "version": v.number, "module": module, "kind": kind,
            "quote": quote, "body": body, "author": author, "role": role, "status": "open", "created": ts, "updated": ts,
        })
        self.save_rows([row])
        return row

    def reply(self, cid: str, body: str, *, author: str = "", role: str = "agent", status: str | None = None) -> dict:
        root = self.find_comment(cid)
        if root["reply_to"]:
            root = self.find_comment(root["reply_to"])
        ts = now_iso()
        row = cm.normalize({
            "id": cm.new_id(), "reply_to": root["id"], "report": root["report"], "version": root["version"],
            "module": root["module"], "body": body, "author": author, "role": role, "status": "", "created": ts, "updated": ts,
        })
        rows = [row]
        if status:
            rows.append(self._with_status(root, status))
        self.save_rows(rows)
        return row

    def set_status(self, cid: str, status: str, resolved_in: int | None = None) -> dict:
        row = self.find_comment(cid)
        if row["reply_to"]:
            row = self.find_comment(row["reply_to"])
        updated = self._with_status(row, status, resolved_in)
        self.save_rows([updated])
        return updated

    def _with_status(self, row: dict, status: str, resolved_in: int | None = None) -> dict:
        if status not in cm.STATUSES:
            raise LedgerError(f"status must be one of {sorted(cm.STATUSES)}")
        row = dict(row)
        row["status"] = status
        if status == "resolved":
            report = self.report(row["report"])
            row["resolved_in"] = str(resolved_in or (report.latest.number if report.latest else row["version"]))
        elif status == "open":
            row["resolved_in"] = ""
        row["updated"] = now_iso()
        return row


def _report_order(r: Report):
    return (r.info.get("order", 1_000_000), r.title.lower())


def _doc_title(text: str, src_name: str) -> str:
    if src_name == "report.md":
        body = split_front_matter(text)[1]
        for line in body.splitlines():
            if not line.strip():
                continue
            m = re.match(r"^ {0,3}#[ \t]+(.+?)[ \t#]*$", line)
            return re.sub(r"\s*\{[^}]*\}\s*$", "", m.group(1)).strip() if m else ""
        return ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL) or re.search(
        r"<h1[^>]*>(.*?)</h1>", text, re.IGNORECASE | re.DOTALL
    )
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


def init_store(path: Path, *, name: str, repo: str | None = None, branch: str = "main") -> Path:
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    cfg = path / CONFIG_NAME
    if cfg.exists():
        raise LedgerError(f"{cfg} already exists")
    config = {"name": name, "key": slugify(name)}
    if repo:
        config["github"] = {"repo": repo, "branch": branch, "root": ""}
    write_json(cfg, config)
    (path / PROJECTS).mkdir(exist_ok=True)
    gitignore = path / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".drafts/\n__pycache__/\n.DS_Store\n", encoding="utf-8")
    return path
