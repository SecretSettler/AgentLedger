"""Ask an agent about a passage of a report.

The reviewer's selection and question become the prompt; the report source, the
review thread so far, the repository the report describes, and the traces
attached at publish time are the context.  The answer comes from the agent CLI
already installed on this machine, run read-only, and is saved as an agent
reply in the thread.

When the report was published with ``--trace auto``, the question goes to the
very session that wrote it, so the agent answers with its full memory of the
work: Claude Code resumes it as a fork (``--resume --fork-session``, the original
stays untouched), Codex resumes it in place (``codex exec resume``).  Each review
thread keeps its own continuation, so follow-up questions pick up where the last
answer left off.  If the session cannot be resumed, a fresh read-only run answers
from the traces instead.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import comments as cm
from .build import build
from .store import LedgerError, Store, now_iso, read_json, source_line, write_json
from .traces import CACHE_DIR, human_size, session_info, session_transcript, unpacked

CLAUDE_ARGV = [
    "claude", "-p", "--output-format", "stream-json", "--verbose",
    "--allowedTools", "Read", "Grep", "Glob",
    "--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch",
]
CODEX_ARGV = ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--json"]
# When serve itself runs inside an agent session, the child agent must not attach to that parent session.
# Auth and provider settings (API keys, Bedrock/Vertex switches) are left alone.
SCRUB_ENV = (
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_AFTER_LAST_COMPACT",
    "CODEX_THREAD_ID", "CODEX_SESSION_ID",
)
THREAD_SESSIONS = "ask-sessions.json"


class AskError(Exception):
    pass


@dataclass
class Backend:
    name: str
    argv: list[str]
    kind: str  # "claude", "codex" or "plain" (prompt on stdin, answer on stdout)


def agent_backend(name: str) -> Backend:
    return Backend(name, list(CLAUDE_ARGV if name == "claude" else CODEX_ARGV), name)


def resolve_backend(choice: Optional[str] = "auto", cmd: Optional[str] = None) -> Optional[Backend]:
    """The default answerer. Per question, the agent that wrote the report is preferred (see ``plan``)."""
    if cmd:
        argv = shlex.split(cmd)
        name = Path(argv[0]).name
        if name.rstrip("0123456789.") in ("python", "node", "sh", "bash", "uv", "npx") and len(argv) > 1:
            name = Path(argv[-1]).stem  # `python3 my_agent.py` signs its replies as my_agent
            if argv[-2:-1] == ["-m"]:
                name = argv[-1].rsplit(".", 1)[-1] + "-agent"
        return Backend(name, argv, "plain")
    if choice == "off":
        return None
    order = ["claude", "codex"] if choice in (None, "auto") else [choice]
    for name in order:
        if name not in ("claude", "codex"):
            raise LedgerError(f"unknown ask backend {name!r}: use claude, codex, auto, off, or --ask-cmd")
        if shutil.which(name):
            return agent_backend(name)
        if choice not in (None, "auto"):
            raise LedgerError(f"`{name}` is not on PATH")
    return None


# ------------------------------------------------------------------ threads
def thread_of(store: Store, cid: str) -> tuple[dict, list[dict]]:
    root = store.find_comment(cid)
    if root["reply_to"]:
        root = store.find_comment(root["reply_to"])
    rows = [r for r in store.comments(store.report(root["report"])) if r["reply_to"] == root["id"] and r["status"] != "deleted"]
    return root, cm.sort_rows(rows)


def _sessions_file(store: Store) -> Path:
    return store.root / CACHE_DIR / THREAD_SESSIONS


def thread_session(store: Store, thread: str) -> Optional[dict]:
    return read_json(_sessions_file(store)).get(thread)


def remember_thread_session(store: Store, thread: str, session: dict) -> None:
    data = read_json(_sessions_file(store))
    data[thread] = session
    write_json(_sessions_file(store), data)


@dataclass
class Plan:
    backend: Backend
    resume: Optional[str] = None  # session id to continue
    fork: bool = False            # Claude: branch off instead of appending to the original
    cwd: Optional[Path] = None
    note: str = ""


def plan(store: Store, root: dict, default: Optional[Backend], choice: str = "auto") -> Plan:
    """Pick who answers: this thread's continuation, else the session that wrote the report, else ``default``."""
    if default is not None and default.kind == "plain":
        return Plan(default)
    report = store.report(root["report"])
    mine = thread_session(store, root["id"])
    if mine and shutil.which(mine["agent"]) and choice in ("auto", mine["agent"]):
        return Plan(agent_backend(mine["agent"]), mine["id"], False, _dir(mine.get("cwd")),
                    "continuing this thread's session")
    versions = [report.version(int(root["version"]))] + [v for v in reversed(report.versions) if v.number != int(root["version"])]
    for v in versions:
        s = v.meta.get("session")
        if not s or not shutil.which(s.get("agent", "")) or choice not in ("auto", s["agent"]):
            continue
        if session_transcript(s) is None:
            continue  # written on another machine
        return Plan(agent_backend(s["agent"]), s["id"], s["agent"] == "claude", session_cwd(store, report.id, v.number, s),
                    f"resuming the {s['agent']} session that wrote v{v.number}")
    if default is None:
        raise AskError("no agent to ask: install Claude Code or Codex, or start serve with --ask-cmd")
    return Plan(default)


def _dir(path: Optional[str]) -> Optional[Path]:
    return Path(path) if path and Path(path).is_dir() else None


def repo_checkout(store: Store, report_id: str, number: int) -> Optional[Path]:
    """Where the repository a version describes is checked out on this machine (local cache only)."""
    local = store.local_info(report_id, number).get("repo_path")
    legacy = (store.report(report_id).version(number).meta.get("context") or {}).get("path")  # stores from 0.2.0
    return _dir(local) or _dir(legacy)


def session_cwd(store: Store, report_id: str, number: int, session: dict) -> Optional[Path]:
    local = store.local_info(report_id, number).get("session_cwd") or session.get("cwd")
    if not local:
        t = session_transcript(session)
        local = (session_info(t) or {}).get("cwd") if t else None
    return _dir(local)


def scrub_paths(text: str, store_root: Path, repo_path: Optional[Path]) -> str:
    """Make paths in an answer relative (to the store or the repo) and show the home directory as ``~``."""
    roots = [(str(store_root), "")] + ([(str(repo_path), "")] if repo_path else []) + [(str(Path.home()), "~/")]
    for root, short in sorted(roots, key=lambda r: -len(r[0])):
        if len(root) > 1:
            text = re.sub(re.escape(root.rstrip("/")) + r"/", lambda _: short, text)
    return text


# ------------------------------------------------------------------ prompt
def build_prompt(store: Store, cid: str, resumed: bool = False) -> tuple[str, Path]:
    """Return (prompt, working directory) for answering thread ``cid``."""
    root, replies = thread_of(store, cid)
    report = store.report(root["report"])
    v = report.version(int(root["version"]))
    ctx = v.meta.get("context") or {}
    repo_path = repo_checkout(store, report.id, v.number)
    cwd = repo_path or store.root

    lines = [
        ("A reviewer is asking about the report you published earlier in this session. Answer from evidence."
         if resumed else "A reviewer is asking about a passage in a report an agent wrote. Answer from evidence."),
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
        "- Cite files relative to the store or the repository (projects/…/report.md:12, src/x.py:40), never as absolute paths:"
        " the answer is saved into a shared repo.",
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
        self.session_id: Optional[str] = None

    def feed(self, line: str) -> Optional[str]:
        try:
            ev = json.loads(line)
        except ValueError:
            return None
        if ev.get("session_id"):
            self.session_id = ev["session_id"]
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


class CodexStream:
    """Parse ``codex exec --json`` events: the thread id, progress, and the last agent message."""

    def __init__(self):
        self.session_id: Optional[str] = None
        self.text: list[str] = []
        self.error: Optional[str] = None

    def feed(self, line: str) -> Optional[str]:
        try:
            ev = json.loads(line)
        except ValueError:
            return line.strip()[:200] or None
        kind = ev.get("type", "")
        if kind == "thread.started":
            self.session_id = ev.get("thread_id") or self.session_id
            return None
        if kind in ("error", "turn.failed"):
            self.error = (ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else ev.get("message")
            return None
        item = ev.get("item") or {}
        if item.get("type") == "agent_message" and kind == "item.completed" and item.get("text"):
            self.text.append(item["text"])
        elif kind == "item.started" and item.get("type"):
            what = item.get("command") or item.get("query") or item.get("path") or ""
            return f"{item['type'].replace('_', ' ')} {what}".strip()[:200]
        return None


def run_backend(backend: Backend, prompt: str, cwd: Path, store_root: Path,
                on_progress: Callable[[str], None] = lambda s: None, timeout: float = 900,
                resume: Optional[str] = None, fork: bool = False) -> tuple[str, Optional[str]]:
    """Run one question; return ``(answer, session id to continue)``."""
    argv = list(backend.argv)
    last_msg = None
    new_id = None
    if backend.kind == "claude":
        # Name new sessions ourselves, so the id to continue is known whatever the environment does.
        if not resume or fork:
            new_id = str(uuid.uuid4())
        if resume:
            argv += ["--resume", resume] + (["--fork-session"] if fork else [])
        if new_id:
            argv += ["--session-id", new_id]
        argv += ["--add-dir", str(store_root)]
    elif backend.kind == "codex":
        fd, last_msg = tempfile.mkstemp(prefix="agentledger-", suffix=".txt")
        os.close(fd)
        argv += ["-o", last_msg] + (["resume", resume, "-"] if resume else ["-"])
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

    t_err = threading.Thread(target=read_err, daemon=True)
    t_err.start()
    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    session = None
    try:
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        if backend.kind in ("claude", "codex"):
            stream = ClaudeStream() if backend.kind == "claude" else CodexStream()
            for ln in proc.stdout:
                note = stream.feed(ln)
                if note:
                    on_progress(note)
            proc.wait()
            if stream.error:
                raise AskError(f"{backend.name}: {stream.error}")
            if backend.kind == "claude":
                answer = stream.final()
            else:
                answer = Path(last_msg).read_text(encoding="utf-8").strip() if last_msg and Path(last_msg).exists() else ""
                answer = answer or (stream.text[-1].strip() if stream.text else "")
            session = new_id or stream.session_id or resume
        else:
            answer = proc.stdout.read().strip()
            proc.wait()
    finally:
        killer.cancel()
        t_err.join(timeout=2)
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
        if last_msg:
            Path(last_msg).unlink(missing_ok=True)
    if proc.returncode != 0 and not answer:
        tail = "\n".join(err_lines[-6:]) or f"exit code {proc.returncode}"
        if proc.returncode in (-9, 137):
            tail = f"timed out after {int(timeout)}s"
        raise AskError(f"{backend.name} failed: {tail}")
    if not answer:
        raise AskError(f"{backend.name} returned an empty answer")
    return answer, session


def answer_thread(store: Store, cid: str, backend: Optional[Backend], *, choice: str = "auto",
                  lock: Optional[threading.Lock] = None, on_progress: Callable[[str], None] = lambda s: None,
                  timeout: float = 900, on_plan: Callable[[Plan], None] = lambda p: None) -> dict:
    """Answer thread ``cid``; save and return the reply row."""
    guard = lock or threading.Lock()
    with guard:
        store.invalidate()
        root, _ = thread_of(store, cid)
        how = plan(store, root, backend, choice)
        prompt, cwd = build_prompt(store, cid, resumed=bool(how.resume))
    on_plan(how)
    on_progress(f"{how.backend.name}: {how.note}" if how.note else f"{how.backend.name} is reading the report and its traces")
    run_dir = how.cwd or cwd
    try:
        answer, session = run_backend(how.backend, prompt, run_dir, store.root, on_progress, timeout, how.resume, how.fork)
    except AskError as exc:
        if not how.resume:
            raise
        # The session may be busy (still open in another terminal) or gone: answer from the traces instead.
        on_progress(f"could not resume the session ({str(exc)[:120]}); answering in a fresh session")
        fresh = backend if backend is not None else agent_backend(how.backend.kind)
        how = Plan(fresh)
        prompt, cwd = build_prompt(store, cid)
        answer, session = run_backend(fresh, prompt, cwd, store.root, on_progress, timeout)
        run_dir = cwd
    answer = scrub_paths(answer, store.root, repo_checkout(store, root["report"], int(root["version"])))
    ts = now_iso()
    row = cm.normalize({
        "id": cm.new_id(), "reply_to": root["id"], "report": root["report"], "version": root["version"],
        "module": root["module"], "body": answer, "author": how.backend.name, "role": "agent", "status": "",
        "created": ts, "updated": ts,
    })
    with guard:
        store.invalidate()
        store.save_rows([row])
        if session and how.backend.kind in ("claude", "codex"):
            remember_thread_session(store, root["id"], {"agent": how.backend.kind, "id": session, "cwd": str(run_dir)})
        build(store)
    return row


# --------------------------------------------------------------------- jobs
@dataclass
class AskJob:
    id: str
    thread: str
    status: str = "queued"
    backend: str = ""
    progress: list[str] = field(default_factory=list)
    reply: Optional[dict] = None
    error: str = ""
    started: float = field(default_factory=time.time)

    def view(self) -> dict:
        return {"id": self.id, "thread": self.thread, "status": self.status, "backend": self.backend,
                "progress": self.progress[-8:], "reply": self.reply, "error": self.error,
                "elapsed": round(time.time() - self.started)}


class AskService:
    """Runs ask jobs in the background for ``agentledger serve``."""

    def __init__(self, store: Store, backend: Optional[Backend], lock: threading.Lock, timeout: float = 900,
                 workers: int = 2, choice: str = "auto"):
        self.store, self.backend, self.lock, self.timeout, self.choice = store, backend, lock, timeout, choice
        self.jobs: dict[str, AskJob] = {}
        self.slots = threading.Semaphore(workers)

    @property
    def name(self) -> str:
        """What the page shows before a question picks its agent."""
        return self.backend.name if self.backend else "agent"

    def start(self, cid: str) -> AskJob:
        with self.lock:
            self.store.invalidate()
            thread_of(self.store, cid)  # raises LedgerError for unknown ids
        running = next((j for j in self.jobs.values() if j.thread == cid and j.status in ("queued", "running")), None)
        if running:
            return running
        job = AskJob(uuid.uuid4().hex[:12], cid, backend=self.name)
        self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: AskJob) -> None:
        with self.slots:
            job.status = "running"

            def on_plan(p: Plan) -> None:
                job.backend = p.backend.name

            try:
                job.reply = answer_thread(self.store, job.thread, self.backend, choice=self.choice, lock=self.lock,
                                          on_progress=job.progress.append, timeout=self.timeout, on_plan=on_plan)
                job.status = "done"
            except (AskError, LedgerError) as exc:
                job.error, job.status = str(exc), "error"
            except Exception as exc:  # keep the server alive whatever the agent does
                job.error, job.status = f"{type(exc).__name__}: {exc}", "error"

    def get(self, job_id: str) -> Optional[dict]:
        job = self.jobs.get(job_id)
        return job.view() if job else None
