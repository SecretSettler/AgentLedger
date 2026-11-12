"""agentledger command line."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Optional

from . import __version__
from . import comments as cm
from .build import build
from .gitsync import git
from . import sandbox
from .ask import (CODEX_FLAGS, AskError, answer_thread, build_prompt, plan, repo_checkout, resolve_backend,
                  review_key, thread_of)
from .traces import (approve_trace, approved_trace, detect_session, discard_trace, human_size, link_session,
                     load_trace_state, prepare_trace, session_info)
from .store import (check_slug, CONFIG_NAME, find_root, GENERATED_OR_SOURCE, init_store, LedgerError,
                   parse_report_id, PROJECTS, read_json, SLUG_RE, source_line, Store, user_config_path,
                   VERSION_DIR_RE, write_json)

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
    print("next: agentledger publish <project>/<report> report.md --link-session auto")
    return 0


def git_context(cwd: Path, store: Store) -> dict | None:
    """Which repository and commit a report was written about (the directory publish ran in)."""
    top = git(cwd, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    top_dir = Path(top.stdout.strip()).resolve()
    if top_dir == store.root:  # publishing from inside the store itself
        return None
    commit = git(top_dir, "rev-parse", "--short", "HEAD")
    if commit.returncode != 0:
        return None
    remote = git(top_dir, "config", "--get", "remote.origin.url").stdout.strip()
    remote = re.sub(r"//[^@/]+@", "//", remote)  # never record credentials embedded in a remote URL
    return {
        "repo": remote or top_dir.name,
        "path": str(top_dir),
        "branch": git(top_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
        "commit": commit.stdout.strip(),
        "dirty": bool(git(top_dir, "status", "--porcelain").stdout.strip()),
    }


def linked_session(value: str) -> tuple[Path, dict]:
    """``--link-session``: the session that wrote the report (Ask on this machine continues it)."""
    try:
        transcript, info, how = link_session(value)
    except ValueError as exc:
        raise LedgerError(str(exc)) from exc
    print(f"  linked: {info['agent']} session {info['id']}  ({how})", file=sys.stderr)
    return transcript, info


def secret_roots(store: Store, session: Optional[dict]) -> list[Path]:
    """Where known secrets come from: .env files near the work (here, the session's directory) and the store."""
    roots = [Path.cwd(), store.root]
    if session and session.get("cwd"):
        roots.append(Path(session["cwd"]))
    return roots


def shared_traces(store: Store, report_id: str, values: list[str], session: dict) -> tuple[list[Path], bool]:
    """``--trace``: what this version shares.  ``auto`` = the approved copy of the linked session; a path = as is."""
    from .redact import known_secrets, scan_file

    out, auto = [], False
    for value in values or []:
        if value == "auto":
            try:
                out.append(approved_trace(store.root, report_id, session))
            except ValueError as exc:
                raise LedgerError(str(exc)) from exc
            auto = True
            continue
        path = Path(value).expanduser()
        if not path.exists():
            raise LedgerError(f"--trace {value}: no such file or folder")
        out.append(path)
        files = [path] if path.is_file() else [f for f in path.rglob("*") if f.is_file()]
        secrets = known_secrets(secret_roots(store, session))
        hits = sum((scan_file(f, secrets) for f in files if f.suffix != ".gz"), Counter())
        if hits:
            print(f"warning: {value} is attached as it is, and looks like it holds "
                  + ", ".join(f"{k} ×{n}" for k, n in hits.items())
                  + "; everyone who can read the store will see it", file=sys.stderr)
    return out, auto


def cmd_trace_prepare(args) -> int:
    store = open_store(args)
    report_id = "/".join(parse_report_id(args.report))
    transcript, session = linked_session(args.link_session)
    state = prepare_trace(store.root, report_id, transcript, session, secret_roots(store, session))
    f = state["findings"]
    print(f"prepared {state['path']}")
    print(f"  kept {f.pop('kept records', 0)} records (user and assistant messages, tool calls and results)")
    for k, n in sorted(f.items()):
        print(f"  {k}: {n}")
    print("Review it before sharing (search the file, do not rely on memory): remove or replace personal data, "
          "credentials the program missed, other people's data, and work unrelated to the report. Then run "
          f"`agentledger trace approve {report_id}` and publish with `--trace auto`.")
    return 0


def cmd_trace_review(args) -> int:
    from .review import review_trace

    store = open_store(args)
    report_id = "/".join(parse_report_id(args.report))
    backend = resolve_backend(args.agent)
    if backend is None:
        raise LedgerError("no agent to review with: install Claude Code (`claude`) or Codex (`codex`)")
    try:
        out = review_trace(store, report_id, backend, timeout=args.timeout,
                           on_progress=lambda m: print(f"  … {m}", file=sys.stderr))
    except AskError as exc:
        raise LedgerError(str(exc)) from exc
    print(out["answer"].strip())
    print(f"\n{backend.name} {'changed' if out['changed'] else 'did not change'} {out['copy']}; notes: {out['notes']}")
    if not out["can_edit"]:
        print("It could not change the copy here (no sandbox): make the changes it lists yourself.")
    if out["left"]:
        print("The program still finds: " + ", ".join(f"{k} ×{n}" for k, n in out["left"].items()))
    print(f"Check the copy, then `agentledger trace approve {report_id}` and publish with `--trace auto`.")
    return 0


def cmd_trace_approve(args) -> int:
    store = open_store(args)
    report_id = "/".join(parse_report_id(args.report))
    state = load_trace_state(store.root, report_id) or {}
    try:
        state = approve_trace(store.root, report_id, secret_roots(store, {"cwd": state.get("cwd")}))
    except ValueError as exc:
        raise LedgerError(str(exc)) from exc
    print(f"approved {state['path']} (sha256 {state['approved']['sha256'][:12]}); "
          f"`agentledger publish {report_id} ... --trace auto` shares exactly this file")
    return 0


def cmd_trace_check(args) -> int:
    from .redact import known_secrets, scan_file

    store = None
    try:
        store = open_store(args)
    except LedgerError:
        pass
    secrets = known_secrets([Path.cwd()] + ([store.root] if store else []))
    found = 0
    for name in args.files:
        path = Path(name).expanduser()
        files = [path] if path.is_file() else [f for f in path.rglob("*") if f.is_file()]
        for f in files:
            hits = scan_file(f, secrets)
            if hits:
                found += 1
                print(f"{f}: " + ", ".join(f"{k} ×{n}" for k, n in hits.items()))
    if not found:
        print("no known secrets or credential formats found")
    return 1 if found else 0


def cmd_publish(args) -> int:
    store = open_store(args)
    _, session = linked_session(args.link_session)
    report_id = "/".join(parse_report_id(args.report))
    traces, shared_session = shared_traces(store, report_id, args.trace, session)
    v = store.publish(
        args.report, Path(args.source),
        title=args.title, summary=args.summary, about=args.about,
        author=args.author or author_default(), changes=args.changes,
        resolves=split_ids(args.resolves), assets=[Path(a) for a in args.asset or []],
        tags=split_ids(args.tag) if args.tag else None, allow_scripts=args.allow_scripts,
        carry_assets=not args.no_carry,
        context=None if args.no_context else git_context(Path.cwd(), store),
        traces=traces, session=session,
    )
    if shared_session:
        discard_trace(store.root, report_id)  # used: the next version prepares its own
    if not args.no_build:
        rebuild(store, quiet=True)
    print(f"published {v.report.id} v{v.number}")
    print(f"  source: {store.rel(v.source)}")
    print(f"  page:   {store.rel(v.view_path)}")
    gh = store.github
    if gh:
        print(f"  github: https://github.com/{gh['repo']}/blob/{gh['branch']}/{store.repo_path(v.source)}")
    if v.meta.get("context"):
        c = v.meta["context"]
        print(f"  about:  {c['repo']} @ {c['commit']}{' (uncommitted changes)' if c.get('dirty') else ''}")
    for src in traces:
        print(f"  trace:  {src}")
    for t in v.meta.get("traces", []):
        print(f"          → {t['path']}  ({human_size(t['bytes'])}, home directory shown as ~)")
    if v.meta.get("session"):
        sess = v.meta["session"]
        shared = "shared as a reviewed copy" if shared_session else "not shared"
        print(f"  session: {sess['agent']} {sess['id']}  (linked: Ask on this machine continues it; {shared})")
    if v.meta.get("resolves"):
        print(f"  resolved: {', '.join(v.meta['resolves'])}")
    if (store.root / ".git").exists():
        print('next: agentledger sync -m "..."  (commit and push the store)')
    return 0


def cmd_sync(args) -> int:
    """Pull the store, rebuild, commit everything, push."""
    from .gitsync import sync

    store = open_store(args)
    r = sync(store, args.message, pull=not args.no_pull, push=not args.no_push)
    if not r["repo"]:
        print(f"built {r['rebuilt']} file{'s' if r['rebuilt'] != 1 else ''}", file=sys.stderr)
        print("store is not a git repository; rebuilt only")
        return 0
    if r.get("pulled"):
        print(f"pulled {r['pulled']} commit{'s' if r['pulled'] != 1 else ''}")
    if not r["ok"] and r.get("stage") == "pull":
        print(r.get("output", ""), file=sys.stderr)
        print(f"agentledger: {r['error']}", file=sys.stderr)
        return 1
    if r["committed"]:
        print(f"committed: {r['committed']}")
    elif r["ok"]:
        print("nothing to commit" + (f" (rebuilt {r['rebuilt']} files)" if r["rebuilt"] else ""))
    if not r["ok"]:
        print(r.get("output", ""), file=sys.stderr)
        print(f"agentledger: {r['error']}", file=sys.stderr)
        return 1
    if r.get("up_to_date"):
        print("up to date with the remote")
    elif r["pushed"]:
        print("pushed")
    return 0


SKILL_TARGETS = (("Claude Code", "~/.claude/skills"), ("Codex", "~/.agents/skills"))


DEFAULT_STORE = Path("~/agentledger-store")


def cmd_setup(args) -> int:
    """Remember the store (creating a private one if needed) and install the skill for Claude Code and Codex."""
    remembered = read_json(user_config_path()).get("store")
    root = Path(args.store_path or args.store or remembered or DEFAULT_STORE).expanduser().resolve()
    created = False
    if not (root / CONFIG_NAME).exists():
        if root.exists() and any(p for p in root.iterdir() if p.name != ".git"):
            raise LedgerError(f"{root} has files but no {CONFIG_NAME}; give an empty folder, an existing store, or nothing")
        init_store(root, name=args.name or "Agent Ledger")
        if shutil.which("git") and not (root / ".git").exists():
            subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=False)
        build(Store(root))
        created = True
    cfg = user_config_path()
    data = read_json(cfg)
    data["store"] = str(root)
    write_json(cfg, data)
    print(f"store:  {root}  (remembered in {cfg})")
    skill = (Path(__file__).parent / "assets" / "SKILL.md").read_text(encoding="utf-8")
    for name, base in SKILL_TARGETS:
        if args.skills_dir:
            base = str(Path(args.skills_dir) / name.lower().replace(" ", "-"))
        dest = Path(base).expanduser() / "agentledger" / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(skill, encoding="utf-8")
        print(f"skill:  {dest}  ({name})")
    if created:
        print(f"\nCreated a new store. Keep it private: push it to a private repo, e.g.\n"
              f"  gh repo create my-agent-ledger --private --source {root} --push")
    print("\nRestart Claude Code / Codex, then just ask: \"write this up as a report\", "
          "or \"handle the review comments on <report>\".\nReview in the browser: agentledger open")
    if not shutil.which("agentledger"):
        print("warning: `agentledger` is not on PATH for this shell; the agent needs it there", file=sys.stderr)
    return 0


def cmd_root(args) -> int:
    print(open_store(args).root)
    return 0


def cmd_status(args) -> int:
    """What `agentledger` alone prints: where the store is and what to do next."""
    print(f"AgentLedger {__version__} — review what your agents report")
    try:
        store = open_store(args)
    except LedgerError:
        print("\n  No store yet. One command sets everything up (a private store + the Claude Code / Codex skill):\n"
              "    agentledger setup\n\n  Or look around first:\n    agentledger demo")
        return 0
    reports = store.reports()
    threads = cm.threads(store.comments())
    waiting = [t for t in threads if t["root"]["status"] == "open"
               and not (t["replies"] and t["replies"][-1]["role"] == "agent")]
    print(f"\n  store    {store.root}")
    print(f"  reports  {len(reports)} in {len(store.projects())} project(s), "
          f"{sum(len(r.versions) for r in reports)} version(s)")
    print(f"  threads  {sum(1 for t in threads if t['root']['status'] == 'open')} open, {len(waiting)} waiting on the agent")
    print("\n  agentledger open        review in the browser (comment, Ask)\n"
          "  agentledger tree        list reports\n"
          "  agentledger --help      everything else")
    return 0


def cmd_share(args) -> int:
    from . import share

    store = open_store(args)
    report = store.report(args.report)
    out = Path(args.output).expanduser() if args.output else Path.cwd() / share.default_name(report.id)
    info = share.pack(store, report.id, out, traces=not args.no_traces, comments=not args.no_comments)
    print(f"shared {report.id}: {info['path']}  ({human_size(info['bytes'])})")
    print(f"  {info['versions']} version(s), {info['traces']} trace(s), {info['threads']} review thread(s)"
          + ("" if not args.no_traces else "; traces left out") + ("" if not args.no_comments else "; threads left out"))
    print("  the rest of your store, your local cache and GitHub settings are not included")
    creds = info["redacted"]
    if creds:
        print(f"  credentials: redacted {sum(creds.values())} in {len([f for f, c in info['files'].items() if set(c) - {'home directory paths'}])}"
              f" file(s): " + ", ".join(f"{k} ×{n}" for k, n in sorted(creds.items(), key=lambda kv: -kv[1])))
        for f, c in info["files"].items():
            kinds = {k: n for k, n in c.items() if k != "home directory paths"}
            if kinds:
                print(f"    {f}: " + ", ".join(f"{k} ×{n}" for k, n in kinds.items()))
        print("    (in the bundle only; your store is unchanged)")
    else:
        print(f"  credentials: none found in {info['checked']} file(s) (secrets known on this machine and common key formats)")
    if info["sessions_trimmed"]:
        print(f"  {info['sessions_trimmed']} raw agent transcript(s) cut down to the conversation "
              "(no system text, attached files, reasoning or account ids)")
    if info["home_paths"]:
        print(f"  {info['home_paths']} path(s) under your home directory shown as ~")
    if info["emails"]:
        print(f"  note: {info['emails']} e-mail address(es) left in (not credentials; check them if that matters)")
    if info["traces"]:
        print("  traces are the agent transcripts and logs behind the report: share them only with people you trust")
    print(f"\nThey run:  agentledger open {out.name}")
    print("Their comments come back as a CSV:  agentledger comments import <their-file>.csv")
    return 0


def cmd_link(args) -> int:
    store = open_store(args)
    report = store.report(args.report)
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        raise LedgerError(f"{repo} is not a folder")
    for v in report.versions:
        store.remember_local(report.id, v.number, {"repo_path": str(repo)})
    ctx = (report.latest.meta.get("context") or {}) if report.latest else {}
    print(f"{report.id}: Ask now also reads {repo}  (kept in .cache, never committed)")
    if ctx.get("commit"):
        head = git(repo, "rev-parse", "HEAD")
        head = head.stdout.strip() if head.returncode == 0 else ""
        if head and head.startswith(ctx["commit"]):
            print(f"  it is at {ctx['commit']}, the commit the report describes"
                  + ("; the report was written with uncommitted changes on top" if ctx.get("dirty") else ""))
        elif head:
            print(f"  it is at {head[:12]}, but the report describes {ctx.get('repo', '')} @ {ctx['commit']}: "
                  f"git -C {shlex.quote(str(repo))} checkout {ctx['commit']}  for exact answers")
        else:
            print(f"  the report describes {ctx.get('repo', '')} @ {ctx['commit']}: check that commit out there for exact answers")
    return 0


def reproduce_lines(store: Store, report) -> list[str]:
    """How a reader gets the code a shared report describes, so their agent can check and re-run it."""
    ctx = (report.latest.meta.get("context") or {}) if report.latest else {}
    if not ctx.get("commit"):
        return ["  to reproduce: no repository was recorded for this report; work from the report and its traces"]
    repo, commit = ctx.get("repo") or "", ctx["commit"]
    remote = "/" in repo or ":" in repo
    name = re.sub(r"\.git$", "", repo.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]) or "repo"
    store_arg = shlex.quote(str(store.root))
    lines = [f"  to reproduce: the report describes {repo} @ {commit}"
             + (f" (branch {ctx['branch']})" if ctx.get("branch") else "")]
    if remote:
        lines.append(f"    git clone {shlex.quote(repo)} {name} && git -C {name} checkout {commit}")
    else:
        lines.append(f"    (no remote was recorded: ask the sender for the {name} repository, then check out {commit})")
    lines += [f"    agentledger --store {store_arg} link {report.id} {name}",
              "    then Ask can read that checkout and re-run the analysis there, in your agent's sandbox"]
    if ctx.get("dirty"):
        lines.append("    (it was written with uncommitted changes, so the commit alone may not match exactly)")
    return lines


def cmd_open(args) -> int:
    """Serve the store on this machine and open it (a report's latest version, or the dashboard)."""
    target = args.report or ""
    if target.endswith(".zip") and Path(target).expanduser().is_file():
        from . import share

        store, info = share.unpack(Path(target), Path(args.into) if args.into else None)
        print(f"opened {info['report']} shared from {info.get('from', 'another store')}  →  {store.root}")
        for line in reproduce_lines(store, store.report(info["report"])):
            print(line)
        print(f"  send your comments back:  agentledger --store {shlex.quote(str(store.root))} comments export "
              f"{info['report']} -o feedback.csv   (or Export CSV in the page)")
        args.store, args.report = str(store.root), info["report"]
    store = open_store(args)
    if args.report:
        report = store.report(args.report)
        if not report.latest:
            raise LedgerError(f"{report.id} has no versions yet")
        args.path = store.rel(report.latest.view_path)
    args.open = True
    return cmd_serve(args)


def cmd_demo(args) -> int:
    from . import demo

    target = Path(args.dir).expanduser() if args.dir else demo.default_dir()
    store = demo.create(target)
    print(f"demo store: {store.root}  (sample data; the numbers are made up)")
    if args.no_serve:
        return 0
    args.store = str(store.root)
    if args.ask == "demo":
        args.ask, args.ask_cmd = "auto", f"{shlex.quote(sys.executable)} -m agentledger.demo"
    else:
        args.ask_cmd = None
    args.path = "projects/demo/benchmark/v002/index.html"
    args.open = not args.no_open
    return cmd_serve(args)


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
    home = str(Path.home()).rstrip("/")
    if len(home) > 1 and base.is_dir():
        pattern = re.compile(re.escape(home) + r"(?=[/\\\"'\s:,;)\]}]|$)")
        for f in sorted(base.rglob("*")):
            if f.is_file() and f.suffix in (".json", ".csv", ".md", ".html"):
                text = f.read_text(encoding="utf-8", errors="replace")
                if pattern.search(text):
                    warnings.append(f"{store.rel(f)}: contains a path under your home directory ({home}); "
                                    "edit it out before sharing the store")
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


def lan_address() -> str:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname()


def cmd_serve(args) -> int:
    from .server import serve

    store = open_store(args)
    rebuild(store, quiet=True)
    if getattr(args, "ask_read_only", False):
        os.environ[sandbox.SANDBOX_ENV] = "off"
    backend = resolve_backend(args.ask, args.ask_cmd)
    host = args.host or ("0.0.0.0" if args.share else "127.0.0.1")
    httpd = serve(store, host=host, port=args.port, extra_hosts=tuple(args.allow_host or ()), quiet=args.quiet,
                  backend=backend, ask_timeout=args.ask_timeout, share=args.share,
                  ask_choice=args.ask if not args.ask_cmd else "auto")
    port = httpd.server_address[1]
    url = f"http://localhost:{port}/" + (args.path or "")
    print(f"AgentLedger · {store.name}")
    print(f"  open    {url}")
    if args.share:
        print(f"  shared  http://{lan_address()}:{port}/   (anyone who can reach this can read and comment)")
    if backend and backend.kind == "plain":
        how = f"{backend.name} (only from this machine)"
    elif backend:
        how = f"the agent session that wrote each report, else {backend.name} (only from this machine)"
    else:
        how = "off: no claude/codex on PATH (--ask, --ask-cmd)"
    print(f"  ask     {how}")
    if backend and backend.kind != "plain":
        boxed = [k for k in ("claude", "codex") if sandbox.available(k)]
        print("          " + (f"reads first; runs programs in the sandbox of {' and '.join(boxed)} when it must compute"
                              " (no network, writes only a scratch folder)" if boxed else
                              "read-only (no agent sandbox here, or --ask-read-only)"))
    print("  Ctrl+C to stop")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def cmd_ask(args) -> int:
    store = open_store(args)
    if args.read_only:
        os.environ[sandbox.SANDBOX_ENV] = "off"
    if args.print_prompt:
        root, _ = thread_of(store, args.id)
        try:
            how = plan(store, root, resolve_backend(args.backend, args.cmd), args.backend)
        except AskError:
            how = None  # no agent installed: show what a fresh run would get
        kind = how.backend.kind if how else "claude"
        repo = repo_checkout(store, root["report"], int(root["version"]))
        pol = sandbox.choose(kind, store.root, review_key(root), (how and how.cwd) or repo or store.root, repo, CODEX_FLAGS)
        scratch = pol.scratch if pol else None
        prompt, cwd = build_prompt(store, args.id, resumed=bool(how and how.resume), again=bool(how and how.again),
                                   kind=kind, scratch=scratch)
        who = (how.backend.name + (f", {how.note} ({how.resume})" if how.note else "")) if how else "no agent installed"
        print(f"# {who}; working directory {(how and how.cwd) or cwd}", file=sys.stderr)
        sys.stdout.write(prompt)
        return 0
    backend = resolve_backend(args.backend, args.cmd)
    if backend is None:
        raise LedgerError("no agent to ask: install Claude Code (`claude`) or Codex (`codex`), or pass --cmd")
    try:
        row = answer_thread(store, args.id, backend, choice=args.backend, timeout=args.timeout,
                            on_progress=lambda s: print(f"  … {s}", file=sys.stderr))
    except AskError as exc:
        raise LedgerError(str(exc)) from exc
    print(row["body"])
    print(f"\n(saved as reply {row['id']} on {row['reply_to']})", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- comments
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
              "`agentledger draft <report>` then "
              "`agentledger publish <report> <file> --link-session auto --resolves <ids>`.")
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
    p.add_argument("--store", help="store root (default: $AGENTLEDGER_STORE, the nearest agentledger.json, or the one setup remembered)")
    p.set_defaults(func=cmd_status)
    sub = p.add_subparsers(dest="cmd", metavar="command")

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
    s.add_argument("--link-session", required=True, metavar="auto|PATH|ID",
                   help="the Claude Code / Codex session that wrote the report, so Ask continues it (required): "
                        "'auto' = the session running this command, else the directory's most recent one; or its "
                        "transcript or id. Only the id is recorded; nothing is shared")
    s.add_argument("--trace", action="append", metavar="auto|PATH",
                   help="share the work behind the report with whoever reads the store: 'auto' = the linked session's "
                        "reviewed copy (agentledger trace prepare / approve); PATH = a log file or folder, attached as "
                        "it is (it may hold private data) (repeatable)")
    s.add_argument("--no-context", action="store_true", help="do not record the git repo/commit publish ran in")
    s.add_argument("--no-build", action="store_true")
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("sync", help="pull the store, rebuild, commit everything and push")
    s.add_argument("-m", "--message", help="commit message (default: the reports whose threads changed, else 'agentledger: update')")
    s.add_argument("--no-pull", action="store_true")
    s.add_argument("--no-push", action="store_true")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("setup", help="one-time setup: a private store + the skill for Claude Code and Codex")
    s.add_argument("store_path", nargs="?",
                   help="your store's clone, or an empty folder for a new one (default: the remembered store, else ~/agentledger-store)")
    s.add_argument("--name", help="display name when creating a new store")
    s.add_argument("--skills-dir", help=argparse.SUPPRESS)  # tests: install under one folder instead of ~/.claude and ~/.agents
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("root", help="print the store folder")
    s.set_defaults(func=cmd_root)

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

    def serve_options(s, ask_choices=("auto", "claude", "codex", "off"), ask_default="auto"):
        s.add_argument("--port", type=int, default=8765)
        s.add_argument("--share", action="store_true",
                       help="let others on your network read and comment; Ask stays available only on this machine")
        s.add_argument("--host", help="interface to bind (default 127.0.0.1, or 0.0.0.0 with --share)")
        s.add_argument("--allow-host", action="append", help="extra Host header to accept (e.g. a tunnel hostname)")
        s.add_argument("--ask", default=ask_default, choices=list(ask_choices),
                       help="agent that answers Ask (default: the one that wrote the report, else claude, else codex)")
        s.add_argument("--ask-cmd", help="any command that reads a prompt on stdin and prints an answer, instead of claude/codex")
        s.add_argument("--ask-timeout", type=float, default=900, help="seconds before an answer is abandoned")
        s.add_argument("--ask-read-only", action="store_true",
                       help="never let Ask run programs, even where the agent's sandbox is available")
        s.add_argument("--quiet", action="store_true")

    s = sub.add_parser("open", help="review in the browser: serve on this machine and open a report, a shared bundle, or the dashboard")
    s.add_argument("report", nargs="?", help="report id, or a .agentledger.zip someone shared (default: the dashboard)")
    s.add_argument("--into", help="where to unpack a shared bundle (default ~/.cache/agentledger/shared/<report>)")
    serve_options(s)
    s.set_defaults(func=cmd_open, path=None)

    s = sub.add_parser("share", help="pack one report (versions, traces, threads) into a file someone else can open")
    s.add_argument("report")
    s.add_argument("-o", "--output", help="file to write (default ./<report>.agentledger.zip)")
    s.add_argument("--no-traces", action="store_true", help="leave the agent transcripts out")
    s.add_argument("--no-comments", action="store_true", help="leave the review threads out")
    s.set_defaults(func=cmd_share)

    s = sub.add_parser("link", help="tell Ask where the repository a report describes is checked out on this machine")
    s.add_argument("report")
    s.add_argument("repo", help="path to your checkout")
    s.set_defaults(func=cmd_link)

    s = sub.add_parser("demo", help="try it: build a sample store and open it in the browser")
    s.add_argument("--dir", help="where to build it (default ~/.cache/agentledger/demo-store)")
    s.add_argument("--no-open", action="store_true", help="don't open a browser")
    s.add_argument("--no-serve", action="store_true", help="only build the store")
    serve_options(s, ask_choices=("demo", "auto", "claude", "codex", "off"), ask_default="demo")
    s.set_defaults(func=cmd_demo)

    s = sub.add_parser("serve", help="serve the store; the browser saves comments into it and can Ask the agent")
    s.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    serve_options(s)
    s.set_defaults(func=cmd_serve, path=None)

    t = sub.add_parser("trace", help="prepare, approve or check the session copy a report shares (publish --trace auto)")
    tsub = t.add_subparsers(dest="tcmd", required=True, metavar="action")
    s = tsub.add_parser("prepare", help="write a shareable copy of the session: the conversation only, secrets redacted")
    s.add_argument("report", help="<project>/<report> it is for")
    s.add_argument("--link-session", required=True, metavar="auto|PATH|ID", help="the session to share (as for publish)")
    s.set_defaults(func=cmd_trace_prepare)
    s = tsub.add_parser("review", help="have Claude Code or Codex review the prepared copy in its sandbox, as an agent "
                                       "publishing a report does (it can change only the copy; approve stays yours)")
    s.add_argument("report")
    s.add_argument("--agent", default="auto", choices=["auto", "claude", "codex"])
    s.add_argument("--timeout", type=float, default=1800)
    s.set_defaults(func=cmd_trace_review)
    s = tsub.add_parser("approve", help="mark the prepared copy reviewed (checked again for secrets first)")
    s.add_argument("report")
    s.set_defaults(func=cmd_trace_approve)
    s = tsub.add_parser("check", help="look for secrets and credentials in files (exit 1 if any)")
    s.add_argument("files", nargs="+")
    s.set_defaults(func=cmd_trace_check)

    s = sub.add_parser("ask", help="have the agent answer a review thread (the same as Ask in the page)")
    s.add_argument("id", help="comment id")
    s.add_argument("--backend", default="auto", choices=["auto", "claude", "codex"])
    s.add_argument("--cmd", help="any command that reads a prompt on stdin and prints an answer")
    s.add_argument("--timeout", type=float, default=900)
    s.add_argument("--print-prompt", action="store_true", help="print the prompt and context instead of asking")
    s.add_argument("--read-only", action="store_true", help="never run programs, even where the agent's sandbox is available")
    s.set_defaults(func=cmd_ask)

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
