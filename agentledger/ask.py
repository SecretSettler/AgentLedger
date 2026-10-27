"""Ask an agent about a passage of a report.

The reviewer's selection and question become the prompt; the report source, the
review thread so far, the repository the report describes, and the traces
attached at publish time are the context.  The question is answered by the
agent CLI already installed on this machine (Claude Code or Codex), run
read-only inside that repository, so nothing needs an API key and private data
stays on the host.  The answer is saved as an agent reply in the thread.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import comments as cm
from .build import build
from .store import LedgerError, Store, now_iso, source_line
from .traces import human_size, unpacked

CLAUDE_ARGV = [
    "claude", "-p", "--output-format", "stream-json", "--verbose",
    "--allowedTools", "Read", "Grep", "Glob",
    "--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch",
]
CODEX_ARGV = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check"]
# Nested runs must not look like they are inside the parent agent's session.
SCRUB_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")


class AskError(Exception):
    pass


@dataclass
class Backend:
    name: str
    argv: list[str]
    kind: str  # "claude", "codex" or "plain" (prompt on stdin, answer on stdout)


def resolve_backend(choice: Optional[str] = "auto", cmd: Optional[str] = None) -> Optional[Backend]:
    if cmd:
        argv = shlex.split(cmd)
        name = Path(argv[0]).name
        if name.rstrip("0123456789.") in ("python", "node", "sh", "bash", "uv", "npx") and len(argv) > 1:
            name = Path(argv[-1]).stem  # `python3 my_agent.py` signs its replies as my_agent
        return Backend(name, argv, "plain")
    if choice == "off":
        return None
    order = ["claude", "codex"] if choice in (None, "auto") else [choice]
    for name in order:
        if name not in ("claude", "codex"):
            raise LedgerError(f"unknown ask backend {name!r}: use claude, codex, auto, off, or --ask-cmd")
        if shutil.which(name):
            return Backend(name, list(CLAUDE_ARGV if name == "claude" else CODEX_ARGV), name)
        if choice not in (None, "auto"):
            raise LedgerError(f"`{name}` is not on PATH")
    return None


# ------------------------------------------------------------------ prompt
def thread_of(store: Store, cid: str) -> tuple[dict, list[dict]]:
    root = store.find_comment(cid)
    if root["reply_to"]:
        root = store.find_comment(root["reply_to"])
    rows = [r for r in store.comments(store.report(root["report"])) if r["reply_to"] == root["id"] and r["status"] != "deleted"]
    return root, cm.sort_rows(rows)


def build_prompt(store: Store, cid: str) -> tuple[str, Path]:
    """Return (prompt, working directory) for answering thread ``cid``."""
    root, replies = thread_of(store, cid)
    report = store.report(root["report"])
    v = report.version(int(root["version"]))
    ctx = v.meta.get("context") or {}
    repo_path = Path(ctx["path"]) if ctx.get("path") else None
    cwd = repo_path if repo_path and repo_path.is_dir() else store.root

    lines = [
        "A reviewer is asking about a passage in a report an agent wrote. Answer from evidence.",
        "",
        f"Report: {report.title}  (id `{report.id}`, opened on v{v.number}, latest v{report.latest.number})",
        f"Report source: {store.root / store.rel(v.source)}",
    ]
    where = source_line(store, root)
    if where:
        lines.append(f"Passage location: {store.root / where.split(':')[0]}" + (f", line {where.split(':')[1]}" if ":" in where else ""))
    if report.latest.number != v.number:
        lines.append(f"Latest version source: {report.latest.source}")
    if ctx:
        lines.append(f"Written about: {ctx.get('repo', '')} @ {ctx.get('commit', '')} (branch {ctx.get('branch', '')})"
                     + (f", checked out at {repo_path}" if repo_path and repo_path.is_dir() else ""))
    traces = []
    for x in report.versions:
        for t in x.meta.get("traces", []):
            try:
                traces.append((x.number, unpacked(store.root, x.path, t), t.get("bytes", 0)))
            except OSError:
                continue
    if traces:
        lines += ["", "Traces of the work behind the report (transcripts and logs; search them with Grep rather than reading them whole):"]
        lines += [f"- {p}  (v{n}, {human_size(b)})" for n, p, b in traces]
    if root["module"]:
        lines.append(f"\nModule: `{root['module']}`")
    if root["quote"]:
        lines += ["", "Quoted passage:", *[f"> {ln}" for ln in root["quote"].splitlines()]]
    lines += ["", "Conversation:"]
    for m in [root, *replies]:
        who = f"{m['author'] or 'reviewer'} ({m['role'] or 'human'})"
        lines.append(f"- {who}: {m['body']}")
    lines += [
        "",
        "Reply to the last message of the conversation, as the agent that wrote the report.",
        "- Check the claim against the source, the traces and the repository before answering; cite evidence as path:line.",
        "- If the report is wrong or unsupported, say so plainly and give the corrected statement.",
        "- If the evidence is not there, say what is missing instead of guessing.",
        "- Be concise: a short paragraph or a few bullets. Answer in the language the reviewer used.",
        "- Do not modify any files.",
    ]
    return "\n".join(lines) + "\n", cwd


# ------------------------------------------------------------------ running
def describe_tool(name: str, args: dict) -> str:
    target = args.get("file_path") or args.get("path") or ""
    if name == "Grep":
        return f"Grep {args.get('pattern', '')!r}" + (f" in {Path(target).name}" if target else "")
    if name == "Glob":
        return f"Glob {args.get('pattern', '')}"
    if target:
        return f"{name} {Path(target).name}"
    return name


class ClaudeStream:
    """Parse ``claude -p --output-format stream-json`` lines."""

    def __init__(self):
        self.answer: Optional[str] = None
        self.text: list[str] = []
        self.error: Optional[str] = None

    def feed(self, line: str) -> Optional[str]:
        try:
            ev = json.loads(line)
        except ValueError:
            return None
        if ev.get("type") == "assistant":
            note = None
            for part in (ev.get("message") or {}).get("content") or []:
                if part.get("type") == "tool_use":
                    note = describe_tool(part.get("name", ""), part.get("input") or {})
                elif part.get("type") == "text" and part.get("text", "").strip():
                    self.text.append(part["text"])
            return note
        if ev.get("type") == "result":
            if ev.get("is_error") or ev.get("subtype", "success") != "success":
                self.error = ev.get("result") or ev.get("subtype") or "agent error"
            else:
                self.answer = ev.get("result") or ""
        return None

    def final(self) -> str:
        return (self.answer if self.answer is not None else (self.text[-1] if self.text else "")).strip()


def run_backend(backend: Backend, prompt: str, cwd: Path, store_root: Path,
                on_progress: Callable[[str], None] = lambda s: None, timeout: float = 900) -> str:
    argv = list(backend.argv)
    if backend.kind == "claude":
        argv += ["--add-dir", str(store_root)]
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env)
    except OSError as exc:
        raise AskError(f"could not start {argv[0]}: {exc}") from exc
    err_lines: list[str] = []

    def read_err():
        for ln in proc.stderr:
            ln = ln.rstrip()
            if ln:
                err_lines.append(ln)
                if backend.kind == "codex":
                    on_progress(ln[:200])

    t_err = threading.Thread(target=read_err, daemon=True)
    t_err.start()
    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    try:
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        if backend.kind == "claude":
            stream = ClaudeStream()
            for ln in proc.stdout:
                note = stream.feed(ln)
                if note:
                    on_progress(note)
            proc.wait()
            if stream.error:
                raise AskError(stream.error)
            answer = stream.final()
        else:
            answer = proc.stdout.read().strip()
            proc.wait()
    finally:
        killer.cancel()
        t_err.join(timeout=2)
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
    if proc.returncode != 0 and not answer:
        tail = "\n".join(err_lines[-6:]) or f"exit code {proc.returncode}"
        if proc.returncode in (-9, 137):
            tail = f"timed out after {int(timeout)}s"
        raise AskError(f"{backend.name} failed: {tail}")
    if not answer:
        raise AskError(f"{backend.name} returned an empty answer")
    return answer


def answer_thread(store: Store, cid: str, backend: Backend, *, lock: Optional[threading.Lock] = None,
                  on_progress: Callable[[str], None] = lambda s: None, timeout: float = 900) -> dict:
    """Ask ``backend`` to answer thread ``cid``; save and return the reply row."""
    guard = lock or threading.Lock()
    with guard:
        store.invalidate()
        prompt, cwd = build_prompt(store, cid)
        root, _ = thread_of(store, cid)
    on_progress(f"{backend.name} is reading the report and its traces")
    answer = run_backend(backend, prompt, cwd, store.root, on_progress, timeout)
    ts = now_iso()
    row = cm.normalize({
        "id": cm.new_id(), "reply_to": root["id"], "report": root["report"], "version": root["version"],
        "module": root["module"], "body": answer, "author": backend.name, "role": "agent", "status": "",
        "created": ts, "updated": ts,
    })
    with guard:
        store.invalidate()
        store.save_rows([row])
        build(store)
    return row


# --------------------------------------------------------------------- jobs
@dataclass
class AskJob:
    id: str
    thread: str
    status: str = "queued"
    progress: list[str] = field(default_factory=list)
    reply: Optional[dict] = None
    error: str = ""
    started: float = field(default_factory=time.time)

    def view(self) -> dict:
        return {"id": self.id, "thread": self.thread, "status": self.status, "progress": self.progress[-8:],
                "reply": self.reply, "error": self.error, "elapsed": round(time.time() - self.started)}


class AskService:
    """Runs ask jobs in the background for ``agentledger serve``."""

    def __init__(self, store: Store, backend: Backend, lock: threading.Lock, timeout: float = 900, workers: int = 2):
        self.store, self.backend, self.lock, self.timeout = store, backend, lock, timeout
        self.jobs: dict[str, AskJob] = {}
        self.slots = threading.Semaphore(workers)

    def start(self, cid: str) -> AskJob:
        with self.lock:
            self.store.invalidate()
            thread_of(self.store, cid)  # raises LedgerError for unknown ids
        running = next((j for j in self.jobs.values() if j.thread == cid and j.status in ("queued", "running")), None)
        if running:
            return running
        job = AskJob(uuid.uuid4().hex[:12], cid)
        self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: AskJob) -> None:
        with self.slots:
            job.status = "running"
            try:
                job.reply = answer_thread(self.store, job.thread, self.backend, lock=self.lock,
                                          on_progress=job.progress.append, timeout=self.timeout)
                job.status = "done"
            except (AskError, LedgerError) as exc:
                job.error, job.status = str(exc), "error"
            except Exception as exc:  # keep the server alive whatever the agent does
                job.error, job.status = f"{type(exc).__name__}: {exc}", "error"

    def get(self, job_id: str) -> Optional[dict]:
        job = self.jobs.get(job_id)
        return job.view() if job else None
