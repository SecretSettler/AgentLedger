"""agentledger command line."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import __version__
from . import comments as cm
from .build import build
from .markdown import ATTRS_RE, HEADING_RE, parse_attrs, slugify
from .store import (
    CONFIG_NAME, GENERATED_OR_SOURCE, PROJECTS, SLUG_RE, VERSION_DIR_RE, LedgerError, Store, check_slug,
    find_root, init_store, read_json, write_json,
)

TEMPLATE = """---
title: {title}
summary: One or two sentences a reviewer reads first: what was asked and what you found.
about: {about}
---

# {title}

Start with the outcome. Lead with the answer, the key numbers, and how confident you are.

## Question {{#question}}

What was asked, the scope, and what "done" means.

## Method {{#method}}

How you did it: data, commands, environment, versions. Enough for someone to reproduce it.

## Findings {{#findings status=needs-review}}

One claim per paragraph. Put evidence (numbers, tables, logs) next to each claim.

| Metric | Value | Source |
|---|---:|---|
| example | 0 | `path/to/log` |

## Caveats {{#caveats}}

What could make this wrong, what you did not check.

## Next steps {{#next-steps}}

- [ ] Concrete follow-ups.
"""


def author_default(fallback: str = "") -> str:
    env = os.environ.get("AGENTLEDGER_AUTHOR")
    if env:
        return env
    if fallback:
        return fallback
    try:
        out = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, timeout=3)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def open_store(args) -> Store:
    root = Path(args.store).resolve() if args.store else find_root()
    if not (root / CONFIG_NAME).exists():
        raise LedgerError(f"{root} is not a store (no {CONFIG_NAME}); run `agentledger init {root}`")
    return Store(root)


def rebuild(store: Store, quiet: bool = False) -> None:
    written = build(store)
    if not quiet:
        print(f"built {len(written)} file{'s' if len(written) != 1 else ''}", file=sys.stderr)


def split_ids(values) -> list[str]:
    out = []
    for v in values or []:
        out += [x.strip() for x in v.split(",") if x.strip()]
    return out


# ------------------------------------------------------------------ commands
def cmd_init(args) -> int:
    root = init_store(Path(args.dir), name=args.name or Path(args.dir).resolve().name, repo=args.repo, branch=args.branch)
    store = Store(root)
    build(store)
    print(f"initialised store at {root}")
    print("next: agentledger publish <project>/<report> report.md")
    return 0


def cmd_publish(args) -> int:
    store = open_store(args)
    v = store.publish(
        args.report, Path(args.source),
        title=args.title, summary=args.summary, about=args.about,
        author=args.author or author_default(), changes=args.changes,
        resolves=split_ids(args.resolves), assets=[Path(a) for a in args.asset or []],
        tags=split_ids(args.tag) if args.tag else None, allow_scripts=args.allow_scripts,
        carry_assets=not args.no_carry,
    )
    if not args.no_build:
        rebuild(store, quiet=True)
    print(f"published {v.report.id} v{v.number}")
    print(f"  source: {store.rel(v.source)}")
    print(f"  view:   {store.rel(v.view_path)}")
    if v.meta.get("resolves"):
        print(f"  resolved: {', '.join(v.meta['resolves'])}")
    return 0


def cmd_build(args) -> int:
    rebuild(open_store(args))
    return 0


def cmd_tree(args) -> int:
    store = open_store(args)
    projects = store.projects()
    if not projects:
        print("(empty store)")
        return 0
    for p in projects:
        print(f"{p.id}  —  {p.title}")
        reports = p.reports

        def show(r, prefix: str, last: bool):
            rows = store.comments(r)
            open_n = sum(1 for x in rows if not x["reply_to"] and x["status"] == "open")
            branch = "└─ " if last else "├─ "
            versions = f"v{r.latest.number}" if r.latest else "no versions"
            extra = f"  ({open_n} open)" if open_n else ""
            print(f"{prefix}{branch}{r.slug:<28} {versions:<6} {r.title}{extra}")
            for i, c in enumerate(r.children):
                show(c, prefix + ("   " if last else "│  "), i == len(r.children) - 1)

        for i, r in enumerate(reports):
            show(r, "", i == len(reports) - 1)
    return 0


def cmd_check(args) -> int:
    store = open_store(args)
    errors, warnings = [], []
    base = store.root / PROJECTS
    if not base.is_dir():
        errors.append("missing projects/ folder")
    known_top = {CONFIG_NAME, "index.html", "manifest.json", PROJECTS}
    for f in sorted(store.root.iterdir()):
        if f.name.startswith(".") or f.name in known_top:
            continue
        if f.is_file() and f.suffix.lower() in (".md", ".txt", ".csv", ".html") and f.name.upper() not in ("README.MD", "CLAUDE.MD", "AGENTS.MD", "LICENSE"):
            warnings.append(f"stray file at store root: {f.name} (reports belong under projects/)")

    def walk_report(path: Path, rid: str):
        if not (path / "report.json").exists():
            warnings.append(f"{rid}: missing report.json (run `agentledger build` after publishing)")
        for child in sorted(path.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_file():
                if child.name not in ("report.json", "index.html"):
                    warnings.append(f"{store.rel(child)}: stray file (put files inside a version folder)")
                continue
            if VERSION_DIR_RE.match(child.name):
                if not any((child / n).exists() for n in ("report.md", "report.html")):
                    errors.append(f"{store.rel(child)}: no report.md or report.html")
                if not (child / "meta.json").exists():
                    errors.append(f"{store.rel(child)}: no meta.json")
                if not (child / "index.html").exists():
                    warnings.append(f"{store.rel(child)}: not built (run `agentledger build`)")
                if (child / "comments.csv").exists():
                    try:
                        text = (child / "comments.csv").read_text(encoding="utf-8")
                        header = text.splitlines()[0] if text else ""
                        if header and header.split(",") != cm.FIELDS:
                            warnings.append(f"{store.rel(child / 'comments.csv')}: unexpected header")
                    except UnicodeDecodeError:
                        errors.append(f"{store.rel(child / 'comments.csv')}: not UTF-8")
            elif SLUG_RE.match(child.name):
                walk_report(child, f"{rid}/{child.name}")
            else:
                warnings.append(f"{store.rel(child)}: folder name is not a valid slug or version")

    if base.is_dir():
        for pdir in sorted(base.iterdir()):
            if pdir.name.startswith("."):
                continue
            if pdir.is_file():
                warnings.append(f"{store.rel(pdir)}: stray file in projects/")
                continue
            try:
                check_slug(pdir.name, "project name")
            except LedgerError as exc:
                errors.append(str(exc))
            if not (pdir / "project.json").exists():
                warnings.append(f"{pdir.name}: missing project.json")
            for child in sorted(pdir.iterdir()):
                if child.name.startswith("."):
                    continue
                if child.is_file():
                    if child.name not in ("project.json", "index.html"):
                        warnings.append(f"{store.rel(child)}: stray file (reports go in their own folder)")
                elif VERSION_DIR_RE.match(child.name):
                    errors.append(f"{store.rel(child)}: version folder directly under a project; it belongs in a report folder")
                else:
                    walk_report(child, f"{pdir.name}/{child.name}")
    try:
        ids = {}
        for row in store.comments():
            if row["id"] in ids:
                errors.append(f"comment id {row['id']} appears in two files")
            ids[row["id"]] = row
        for row in ids.values():
            if row["reply_to"] and row["reply_to"] not in ids:
                warnings.append(f"comment {row['id']} replies to unknown {row['reply_to']}")
    except LedgerError as exc:
        errors.append(str(exc))
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    if not errors and not warnings:
        print("store layout OK")
    return 1 if errors else 0


def cmd_draft(args) -> int:
    store = open_store(args)
    report = store.report(args.report)
    if not report.versions:
        raise LedgerError(f"{report.id} has no versions")
    v = report.version(args.version) if args.version else report.latest
    src = v.source
    out = Path(args.output) if args.output else store.root / ".drafts" / f"{report.id.replace('/', '__')}{src.suffix}"
    if out.exists() and not args.force:
        raise LedgerError(f"{out} exists (use --force to overwrite)")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, out)
    print(out)
    assets = [f for f in v.path.iterdir() if f.name not in GENERATED_OR_SOURCE]
    if assets:
        print(f"note: {len(assets)} asset(s) in {store.rel(v.path)} carry over to the next version automatically", file=sys.stderr)
    return 0


def cmd_template(args) -> int:
    print(TEMPLATE.format(title=args.title or "Report title", about=args.about or "What this report is about"), end="")
    return 0


def cmd_set(args) -> int:
    store = open_store(args)
    report = store.report(args.report)
    info = read_json(report.path / "report.json")
    for key in ("title", "about", "status"):
        val = getattr(args, key)
        if val is not None:
            info[key] = val
    if args.tag is not None:
        info["tags"] = split_ids(args.tag)
    if args.order is not None:
        info["order"] = args.order
    write_json(report.path / "report.json", info)
    rebuild(store, quiet=True)
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0


def cmd_project(args) -> int:
    store = open_store(args)
    check_slug(args.project, "project name")
    pdir = store.root / PROJECTS / args.project
    info = read_json(pdir / "project.json") or {"title": args.project.replace("-", " ").capitalize(), "description": ""}
    if args.title is not None:
        info["title"] = args.title
    if args.description is not None:
        info["description"] = args.description
    if args.order is not None:
        info["order"] = args.order
    write_json(pdir / "project.json", info)
    rebuild(store, quiet=True)
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0


def cmd_path(args) -> int:
    store = open_store(args)
    report = store.report(args.report)
    if not report.versions:
        print(report.path)
        return 0
    v = report.version(args.version) if args.version else report.latest
    target = v.source if args.source else v.comments_path if args.comments else v.view_path if args.view else v.path
    print(target)
    return 0


def cmd_serve(args) -> int:
    from .server import serve

    store = open_store(args)
    rebuild(store, quiet=True)
    httpd = serve(store, host=args.host, port=args.port, extra_hosts=tuple(args.allow_host or ()), quiet=args.quiet)
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0", "::") else args.host
    url = f"http://{shown}:{httpd.server_address[1]}/"
    print(f"serving {store.root} at {url}  (Ctrl+C to stop)")
    print("comments saved in the browser are written to the store and rebuilt")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


# ---------------------------------------------------------------- comments
def source_line(store: Store, row: dict) -> str:
    """Best-effort ``path:line`` pointing an agent at the commented text."""
    try:
        v = store.report(row["report"]).version(int(row["version"]))
        src = v.source
        lines = src.read_text(encoding="utf-8").splitlines()
    except (LedgerError, ValueError, OSError):
        return ""
    rel = store.rel(src)
    quote = re.sub(r"\s+", " ", row.get("quote") or "").strip()
    if quote:
        for probe in (quote[:60], quote[:30], max(quote.split(" "), key=len) if " " in quote else ""):
            if len(probe) < 8:
                continue
            for i, ln in enumerate(lines, 1):
                plain = re.sub(r"[*_`~]|\[|\]\([^)]*\)", "", ln)
                if probe in re.sub(r"\s+", " ", plain):
                    return f"{rel}:{i}"
    mod = row.get("module")
    if mod:
        for i, ln in enumerate(lines, 1):
            m = HEADING_RE.match(ln)
            if not m or not m.group(2):
                continue
            text = m.group(2)
            am = ATTRS_RE.search(text)
            hid = parse_attrs(am.group(1)).get("id") if am else None
            if hid == mod or slugify(ATTRS_RE.sub("", text)) == mod:
                return f"{rel}:{i}"
    return rel


def filter_threads(store: Store, args) -> list[dict]:
    reports = [store.report(args.report)] if getattr(args, "report", None) else store.reports()
    if getattr(args, "report", None) and getattr(args, "recursive", False):
        reports = list(reports[0].walk())
    out = []
    for r in reports:
        for t in cm.threads(store.comments(r)):
            root = t["root"]
            st = args.status
            if st == "waiting":
                last = (t["replies"] or [root])[-1]
                if root["status"] != "open" or (t["replies"] and last["role"] == "agent"):
                    continue
            elif st == "closed":
                if root["status"] == "open":
                    continue
            elif st != "all" and root["status"] != st:
                continue
            if getattr(args, "version", None) and root["version"] != str(args.version):
                continue
            t["report_title"] = r.title
            t["latest_source"] = store.rel(r.latest.source) if r.latest else ""
            t["where"] = source_line(store, root)
            out.append(t)
    return out


def cmd_comments_list(args) -> int:
    store = open_store(args)
    threads = filter_threads(store, args)
    if args.format == "json":
        print(json.dumps(threads, indent=2, ensure_ascii=False))
        return 0
    if args.format == "csv":
        rows = [x for t in threads for x in [t["root"], *t["replies"]]]
        sys.stdout.write(cm.dumps(rows))
        return 0
    label = {"open": "Open", "waiting": "Waiting on the agent", "closed": "Closed", "resolved": "Resolved",
             "wontfix": "Won't fix", "all": "All", "deleted": "Deleted"}[args.status]
    print(f"# {label} review threads ({len(threads)})\n")
    current = None
    for t in threads:
        root = t["root"]
        if root["report"] != current:
            current = root["report"]
            print(f"## {t['report_title']} — `{current}`\n")
            print(f"latest source: `{t['latest_source']}`\n")
        bits = [root["status"]] + ([root["kind"]] if root["kind"] else []) + [f"v{root['version']}"]
        if root["module"]:
            bits.append(f"module `{root['module']}`")
        print(f"### {root['id']} · {' · '.join(bits)}")
        if t["where"]:
            print(f"at `{t['where']}`")
        if root["quote"]:
            print()
            for ln in root["quote"].splitlines():
                print(f"> {ln}")
        print()
        for m in [root, *t["replies"]]:
            body = m["body"].replace("\n", "\n  ")
            print(f"- **{m['author'] or 'anonymous'}** ({m['role'] or 'human'}, {m['created']}): {body}")
        print()
    if threads and args.status in ("open", "waiting"):
        print("Reply with `agentledger comments reply <id> \"...\" [--resolve]`; revise with "
              "`agentledger draft <report>` then `agentledger publish <report> <file> --resolves <ids>`.")
    return 0


def cmd_comments_add(args) -> int:
    store = open_store(args)
    row = store.add_comment(args.report, args.body, version=args.version, author=args.author or author_default(),
                            role=args.role, module=args.module or "", quote=args.quote or "", kind=args.kind)
    rebuild(store, quiet=True)
    print(row["id"])
    return 0


def cmd_comments_reply(args) -> int:
    store = open_store(args)
    status = "resolved" if args.resolve else "wontfix" if args.wontfix else None
    row = store.reply(args.id, args.body, author=args.author or author_default("agent"), role=args.role, status=status)
    rebuild(store, quiet=True)
    print(row["id"])
    return 0


def cmd_comments_status(args, status: str) -> int:
    store = open_store(args)
    for cid in split_ids(args.ids):
        row = store.set_status(cid, status, resolved_in=getattr(args, "in_version", None))
        extra = f" in v{row['resolved_in']}" if status == "resolved" and row["resolved_in"] else ""
        print(f"{cid}: {status}{extra}")
    rebuild(store, quiet=True)
    return 0


def cmd_comments_import(args) -> int:
    store = open_store(args)
    text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    rows = cm.parse(text)
    if args.report:
        rows = [dict(r, report=r["report"] or args.report) for r in rows]
    changed = store.save_rows(rows)
    rebuild(store, quiet=True)
    print(f"imported {len(rows)} row{'s' if len(rows) != 1 else ''}; {changed} new or updated")
    return 0


def cmd_comments_export(args) -> int:
    store = open_store(args)
    reports = [store.report(args.report)] if args.report else None
    rows = store.comments(reports[0]) if reports else store.comments()
    if args.status != "all":
        keep = {t["root"]["id"] for t in filter_threads(store, args)}
        rows = [r for r in rows if r["id"] in keep or r["reply_to"] in keep]
    text = cm.dumps(rows)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {len(rows)} rows to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


# ------------------------------------------------------------------- parser
def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentledger", description="Modular, reviewable, versioned reports for AI agents.")
    p.add_argument("--version", action="version", version=f"agentledger {__version__}")
    p.add_argument("--store", help="store root (default: nearest folder with agentledger.json, or $AGENTLEDGER_STORE)")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sub.add_parser("init", help="create a store")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--name", help="display name of the store")
    s.add_argument("--repo", help="GitHub OWNER/REPO the browser saves comments to")
    s.add_argument("--branch", default="main")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("publish", help="publish a report (or a new version of one)")
    s.add_argument("report", help="<project>/<report>[/<sub-report>...]")
    s.add_argument("source", help=".md or .html file, or a folder of .md files (one module per file, plus assets)")
    s.add_argument("--title")
    s.add_argument("--summary", help="one or two sentences shown under the title and on the dashboard")
    s.add_argument("--about", help="what the report is about (shown on the dashboard)")
    s.add_argument("--author", help="who wrote it (default: $AGENTLEDGER_AUTHOR or git user.name)")
    s.add_argument("--changes", help="what changed since the previous version")
    s.add_argument("--resolves", action="append", help="comment ids this version resolves (comma separated, repeatable)")
    s.add_argument("--asset", action="append", help="file or folder to copy next to the report (repeatable)")
    s.add_argument("--tag", action="append", help="tags (comma separated, repeatable)")
    s.add_argument("--allow-scripts", action="store_true", help="skip HTML sanitising (only for fully trusted reports)")
    s.add_argument("--no-carry", action="store_true", help="do not copy the previous version's assets forward")
    s.add_argument("--no-build", action="store_true")
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("build", help="regenerate every page, the dashboard and manifest.json")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("tree", help="print projects, reports and versions")
    s.set_defaults(func=cmd_tree)

    s = sub.add_parser("check", help="validate the store layout")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("draft", help="copy a version's source to a working file for revision")
    s.add_argument("report")
    s.add_argument("-o", "--output", help="destination (default: .drafts/<report>.md in the store)")
    s.add_argument("--version", type=int)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_draft)

    s = sub.add_parser("template", help="print a report skeleton to start from")
    s.add_argument("--title")
    s.add_argument("--about")
    s.set_defaults(func=cmd_template)

    s = sub.add_parser("set", help="edit report metadata")
    s.add_argument("report")
    s.add_argument("--title")
    s.add_argument("--about")
    s.add_argument("--status", help="free-form, e.g. draft, in-review, final")
    s.add_argument("--tag", action="append")
    s.add_argument("--order", type=int)
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("project", help="create or edit a project's metadata")
    s.add_argument("project")
    s.add_argument("--title")
    s.add_argument("--description")
    s.add_argument("--order", type=int)
    s.set_defaults(func=cmd_project)

    s = sub.add_parser("path", help="print the path of a report version (folder, --source, --comments or --view)")
    s.add_argument("report")
    s.add_argument("--version", type=int)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--source", action="store_true")
    g.add_argument("--comments", action="store_true")
    g.add_argument("--view", action="store_true")
    s.set_defaults(func=cmd_path)

    s = sub.add_parser("serve", help="serve the store locally; the browser can save comments straight into it")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--allow-host", action="append", help="extra Host header to accept (e.g. a tunnel hostname)")
    s.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_serve)

    c = sub.add_parser("comments", help="list, reply to, resolve, import or export review threads")
    csub = c.add_subparsers(dest="ccmd", required=True, metavar="action")

    s = csub.add_parser("list", help="list threads (Markdown for agents by default)")
    s.add_argument("report", nargs="?")
    s.add_argument("--status", default="open", choices=["open", "waiting", "closed", "resolved", "wontfix", "deleted", "all"],
                   help="'waiting' = open threads whose last message is not from an agent")
    s.add_argument("--version", type=int)
    s.add_argument("-r", "--recursive", action="store_true", help="include sub-reports")
    s.add_argument("--format", default="md", choices=["md", "json", "csv"])
    s.set_defaults(func=cmd_comments_list)

    s = csub.add_parser("add", help="open a new thread")
    s.add_argument("report")
    s.add_argument("body")
    s.add_argument("--version", type=int)
    s.add_argument("--module")
    s.add_argument("--quote")
    s.add_argument("--kind", default="note", choices=sorted(cm.KINDS))
    s.add_argument("--author")
    s.add_argument("--role", default="human", choices=["human", "agent"])
    s.set_defaults(func=cmd_comments_add)

    s = csub.add_parser("reply", help="reply to a thread")
    s.add_argument("id")
    s.add_argument("body")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--resolve", action="store_true")
    g.add_argument("--wontfix", action="store_true")
    s.add_argument("--author", help="default: $AGENTLEDGER_AUTHOR or 'agent'")
    s.add_argument("--role", default="agent", choices=["human", "agent"])
    s.set_defaults(func=cmd_comments_reply)

    for name, status in (("resolve", "resolved"), ("reopen", "open"), ("wontfix", "wontfix")):
        s = csub.add_parser(name, help=f"mark threads {status}")
        s.add_argument("ids", nargs="+")
        if name == "resolve":
            s.add_argument("--in", dest="in_version", type=int, help="version that resolves it (default: latest)")
        s.set_defaults(func=lambda a, st=status: cmd_comments_status(a, st))

    s = csub.add_parser("import", help="merge a CSV exported from the browser into the store ('-' for stdin)")
    s.add_argument("file")
    s.add_argument("--report", help="report id for rows that lack one")
    s.set_defaults(func=cmd_comments_import)

    s = csub.add_parser("export", help="write threads as CSV")
    s.add_argument("report", nargs="?")
    s.add_argument("--status", default="all", choices=["open", "waiting", "closed", "resolved", "wontfix", "all"])
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_comments_export, version=None, recursive=False)
    return p


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except LedgerError as exc:
        print(f"agentledger: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
