"""``agentledger trace review``: have an agent review the session copy ``trace prepare`` wrote.

The program's pass (``redact.sanitize_session``) removes what it can recognise: system text, attached files,
reasoning, account ids, known secrets, credential formats.  What is left needs judgment: personal data, credentials
in unusual shapes, other people's data, work unrelated to the report.  The agent that publishes a report reviews the
copy itself; this command does the same from the command line, by running Claude Code or Codex (as Ask does) in its
sandbox, able to change only the copy's folder.  It never approves: ``agentledger trace approve`` stays a separate
step, and any change resets an earlier approval.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

from . import sandbox
from .ask import CODEX_FLAGS, AskError, Backend, run_backend
from .store import Store
from .traces import load_trace_state, prepared_dir, record_review
from .redact import known_secrets, scan_file

REVIEW_NOTES = "review.md"


def _report_context(store: Store, report_id: str) -> list[str]:
    """What the report is about, so the reviewer can tell related work from unrelated: its draft or latest source."""
    name = report_id.replace("/", "__")
    lines = []
    for ext in (".md", ".html"):
        draft = store.root / ".drafts" / f"{name}{ext}"
        if draft.is_file():
            lines.append(f"The report's draft: {draft}")
            break
    try:
        latest = store.report(report_id).latest
    except Exception:  # a report not published yet
        latest = None
    if latest:
        lines.append(f"Its latest published version: {latest.source} (title: {latest.meta.get('title') or ''}; "
                     f"summary: {latest.meta.get('summary') or ''})")
    return lines or ["The report has no draft or published version to compare with: judge from the session itself."]


def review_prompt(copy: Path, report_id: str, findings: dict, context: list[str], can_edit: bool) -> str:
    done = ", ".join(f"{k} ×{n}" for k, n in sorted(findings.items()) if k != "kept records") or "nothing"
    to_review = [k for k in findings if k.startswith("to review:")]
    how = ("Edit only that file. Replace each item with [REDACTED:<kind>] (e.g. [REDACTED:email], [REDACTED:name]); "
           "where a whole exchange is unrelated to the report, delete those lines. The file is JSON Lines: one JSON "
           "object per line, and every line must still parse when you are done."
           if can_edit else
           "You cannot change files in this run. List, for each item, the line number and what to replace it with, "
           "so a person can make the change.")
    return "\n".join([
        f"Review a copy of an agent session before it is shared with everyone who can read the report store "
        f"(report `{report_id}`). The copy: {copy}",
        "",
        "A program already removed system text, attached files, reasoning and account ids, and replaced known secrets "
        f"and credential formats with [REDACTED:<kind>]. It did: {done}."
        + (f" It flagged for you: {', '.join(to_review)}." if to_review else ""),
        "",
        "Find and remove what a program cannot recognise:",
        "- personal data: e-mail addresses (keep only no-reply and example addresses), names of people other than "
        "public projects, usernames and machine or host names, phone numbers, home directories other than `~`;",
        "- credentials it missed: passwords or tokens in prose, in unusual formats, or split across lines;",
        "- work unrelated to the report: exchanges about other tasks.",
        "",
        *context,
        "",
        "The file can be large: search it (Grep, rg) for candidates instead of reading it whole.",
        how,
        "",
        "Then reply with a short list of what you changed or would change: kinds and counts, never the values "
        "themselves, and anything you were unsure about.",
    ]) + "\n"


def _policy(store: Store, kind: str, folder: Path) -> tuple[Optional[sandbox.Policy], bool]:
    """The policy for the reviewer, and whether it can change the copy.  Writes: the copy's folder (and a scratch
    folder for Bash); reads: everything but the secrets."""
    secrets = sandbox.secret_paths([store.root, folder])
    if sandbox.usable(kind, folder):
        key = f"review:{folder.name}"
        scratch = sandbox.scratch_dir(store.root, key)
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / ".key").write_text(f"{key}\n{store.root.resolve()}\n", encoding="utf-8")
        pol = sandbox.Policy(scratch.resolve(), [], secrets, writes=[folder.resolve()])
        if kind == "claude" or sandbox.codex_verified(folder, pol, CODEX_FLAGS):
            return pol, True
    if kind == "claude":  # no sandbox: no Bash, but the Edit tool, limited to the folder, still works
        return sandbox.Policy(None, [], secrets, writes=[folder.resolve()]), True
    return None, False    # Codex in its plain read-only sandbox: it can only say what to change


def _valid_jsonl(path: Path) -> Optional[int]:
    """The first line that does not parse as JSON, or None."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            if line.strip():
                try:
                    json.loads(line)
                except ValueError:
                    return n
    return None


def review_trace(store: Store, report_id: str, backend: Backend, timeout: float = 1800,
                 on_progress: Callable[[str], None] = lambda s: None) -> dict:
    """Run ``backend`` (claude or codex) over the prepared copy of ``report_id``; return what happened."""
    state = load_trace_state(store.root, report_id)
    if not state or not Path(state["path"]).is_file():
        raise AskError(f"no prepared trace for {report_id}: run `agentledger trace prepare {report_id} "
                       f"--link-session auto` first")
    copy, folder = Path(state["path"]), prepared_dir(store.root, report_id)
    pol, can_edit = _policy(store, backend.kind, folder)
    on_progress(pol.summary() if pol else "read-only: cannot change the copy, will only list what to change")
    prompt = review_prompt(copy, report_id, state.get("findings") or {}, _report_context(store, report_id), can_edit)
    before = copy.stat().st_mtime_ns
    answer, _ = run_backend(backend, prompt, folder, folder, on_progress, timeout,
                            name=f"AgentLedger trace review: {report_id}", policy=pol)
    bad = _valid_jsonl(copy)
    if bad:
        raise AskError(f"after the review, line {bad} of {copy} is not valid JSON: fix it, or prepare the copy again")
    left = scan_file(copy, known_secrets([store.root, folder] + ([Path(state["cwd"])] if state.get("cwd") else [])))
    (folder / REVIEW_NOTES).write_text(f"# Review of {copy.name} by {backend.name}\n\n{answer.strip()}\n",
                                       encoding="utf-8")
    changed = copy.stat().st_mtime_ns != before
    record_review(store.root, report_id, {"by": backend.name, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                          "changed": changed, "notes": REVIEW_NOTES})
    return {"copy": str(copy), "notes": str(folder / REVIEW_NOTES), "changed": changed, "can_edit": can_edit,
            "left": dict(left), "answer": answer}
