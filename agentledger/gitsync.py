"""Commit and push the store, and pull what others pushed: ``agentledger sync`` and the page's Sync button.

``sync`` pulls (rebase, autostash), rebuilds, commits everything with a message that says what changed (which
reports' review threads, by whom) unless one is given, and pushes.  It returns what happened instead of printing, so
the CLI and ``agentledger serve`` report it each in their own way.  From the server, git never prompts: a push that
needs credentials fails with git's message instead of hanging the page.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from .build import build
from .store import Store

COMMENTS_RE = re.compile(r"^projects/(.+)/v(\d+)/comments\.csv$")


def git(cwd: Path, *argv: str, prompt: bool = True) -> subprocess.CompletedProcess:
    env = None if prompt else {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
                               "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")}
    try:
        return subprocess.run(["git", "-C", str(cwd), *argv], capture_output=True, text=True, timeout=120, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(["git", *argv], 1, "", str(exc))


def remote_label(url: str) -> str:
    """``owner/repo`` for a GitHub remote, else the URL without credentials."""
    url = re.sub(r"//[^@/]+@", "//", url.strip())
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    if m:
        return m.group(1)
    if url.startswith(("/", "~", "file://")) or re.match(r"^[A-Za-z]:[\\/]", url):  # a folder on this machine
        return "/".join(Path(url.replace("file://", "")).parts[-2:])
    return url


def status(root: Path) -> dict:
    """Whether the store can sync, and what is waiting: changed files, commits not pushed."""
    if git(root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return {"repo": False}
    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    remote = git(root, "config", "--get", "remote.origin.url").stdout.strip()
    changed = [ln for ln in git(root, "status", "--porcelain").stdout.splitlines() if ln.strip()]
    ahead = git(root, "rev-list", "--count", "@{u}..HEAD") if upstream.returncode == 0 else None
    return {"repo": True, "remote": remote_label(remote) if remote else None,
            "branch": git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            "upstream": upstream.stdout.strip() if upstream.returncode == 0 else None,
            "changed": len(changed),
            "ahead": int(ahead.stdout.strip()) if ahead is not None and ahead.returncode == 0 else None}


def review_message(staged: list[str], author: Optional[str] = None) -> str:
    """A commit message from what is staged: the reports whose threads changed (and by whom), else a generic one."""
    threads = sorted({f"{m.group(1)} v{int(m.group(2))}" for f in staged for m in [COMMENTS_RE.match(f)] if m})
    others = [f for f in staged if not COMMENTS_RE.match(f) and not f.endswith("index.html") and f != "manifest.json"]
    if not threads:
        return "agentledger: update"
    names = ", ".join(threads[:3]) + (f" and {len(threads) - 3} more" if len(threads) > 3 else "")
    by = f" by {author}" if author else ""
    return f"review: {names}{by}" + ("" if not others else f" (+{len(others)} other file{'s' if len(others) > 1 else ''})")


def _short(text: str, limit: int = 1200) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def sync(store: Store, message: Optional[str] = None, *, author: Optional[str] = None, pull: bool = True,
         push: bool = True, prompt: bool = True) -> dict:
    """Pull, rebuild, commit everything, push.  Returns ``ok`` and what happened; ``error`` and git's output if not."""
    root = store.root
    out = {"ok": True, "repo": True, "pulled": 0, "committed": None, "commit": None, "pushed": False, "rebuilt": 0}
    if git(root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        out.update(repo=False, rebuilt=len(build(store)))
        return out
    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").returncode == 0
    has_origin = bool(git(root, "remote").stdout.split())
    remote = git(root, "config", "--get", "remote.origin.url").stdout.strip()
    out["remote"] = remote_label(remote) if remote else None
    if upstream and pull:
        before = git(root, "rev-parse", "HEAD").stdout.strip()
        r = git(root, "pull", "--rebase", "--autostash", prompt=prompt)
        if r.returncode != 0:
            return {**out, "ok": False, "stage": "pull", "output": _short(r.stdout + r.stderr),
                    "error": "pull failed; resolve it in the store (comments.csv conflicts: keep both sides' rows), "
                             "then sync again"}
        after = git(root, "rev-parse", "HEAD").stdout.strip()
        if before and after != before:
            n = git(root, "rev-list", "--count", f"{before}..{after}")
            out["pulled"] = int(n.stdout.strip() or 0) if n.returncode == 0 else 0
        store.invalidate()
    out["rebuilt"] = len(build(store))
    git(root, "add", "-A")
    if git(root, "diff", "--cached", "--quiet").returncode != 0:
        staged = git(root, "diff", "--cached", "--name-only").stdout.splitlines()
        msg = message or review_message(staged, author)
        r = git(root, "commit", "-m", msg)
        if r.returncode != 0:
            return {**out, "ok": False, "stage": "commit", "output": _short(r.stdout + r.stderr), "error": "commit failed"}
        out["committed"] = msg
        out["commit"] = git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    if not push or not has_origin:
        out["push_skipped"] = "no remote" if not has_origin else "not asked"
        return out
    ahead = git(root, "rev-list", "--count", "@{u}..HEAD") if upstream else None
    if ahead is not None and ahead.returncode == 0 and ahead.stdout.strip() == "0":
        out["up_to_date"] = True
        return out
    r = git(root, "push", prompt=prompt) if upstream else git(root, "push", "-u", "origin", "HEAD", prompt=prompt)
    if r.returncode != 0:
        return {**out, "ok": False, "stage": "push", "output": _short(r.stdout + r.stderr),
                "error": "push failed" + ("" if prompt else " (git could not ask for credentials here: "
                                                             "run `agentledger sync` once in a terminal)")}
    out["pushed"] = True
    return out
