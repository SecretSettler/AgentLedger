"""Traces: the sessions and logs behind a report.

Two things, kept apart:

* **Linking** (``publish --link-session auto|PATH|ID``, required): the version records which Claude Code, Codex, Pi
  or Antigravity session wrote it, so Ask on this machine continues that session.  Only the session id is recorded;
  the session itself stays where the agent keeps it.
* **Sharing** (``publish --trace ...``): files copied into ``vNNN/traces/`` (gzip-compressed, home directory shown as
  ``~``) for reviewers and their agents.  ``--trace auto`` shares the linked session only as a reviewed copy:
  ``agentledger trace prepare`` writes it (``redact.sanitize_session``: the conversation only, secrets redacted),
  the agent reviews and edits it, ``agentledger trace approve`` records it, and publish attaches exactly that file.
  ``--trace <path>`` attaches any file or folder as it is.

Traces are unpacked on demand into ``<store>/.cache/`` when an agent is asked about the report.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

TRACES_DIR = "traces"
CACHE_DIR = ".cache"
RECENT = 24 * 3600
ASK_ENTRYPOINT = "agentledger-ask"   # the sessions Ask itself runs (see ask.py): never the author's
AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex", "pi": "Pi", "antigravity": "Antigravity"}

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PI_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._\-]{0,126}[A-Za-z0-9])?")
CONV_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/\-]{0,200}[A-Za-z0-9])?")


def _claude_projects() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "projects"


def _codex_sessions() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser() / "sessions"


def _pi_sessions() -> Path:
    if os.environ.get("PI_CODING_AGENT_SESSION_DIR"):
        return Path(os.environ["PI_CODING_AGENT_SESSION_DIR"]).expanduser()
    base = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent").expanduser()
    return base / "sessions"


def _antigravity_home() -> Path:
    return Path(os.environ.get("ANTIGRAVITY_HOME") or os.environ.get("GEMINI_CLI_HOME") or Path.home() / ".gemini").expanduser()


def _antigravity_brains() -> list[Path]:
    home = _antigravity_home()
    return [home / sub / "brain" for sub in ("antigravity-cli", "antigravity", "jetski")]


def _same_or_inside(session_cwd: str, cwd: Path) -> bool:
    if not session_cwd:
        return False
    try:
        scwd = Path(session_cwd).resolve()
    except OSError:
        scwd = Path(session_cwd)
    return scwd == cwd or scwd in cwd.parents


def _claude_candidates(cwd: Path) -> list[Path]:
    base = _claude_projects()
    for p in [cwd, *cwd.parents]:
        d = base / re.sub(r"[^A-Za-z0-9]", "-", str(p))
        if d.is_dir():
            return list(d.glob("*.jsonl"))
    return []


def _codex_candidates(cwd: Path) -> list[Path]:
    base = _codex_sessions()
    if not base.is_dir():
        return []
    now = time.time()
    recent = [f for f in base.rglob("rollout-*.jsonl") if now - f.stat().st_mtime < RECENT]
    out = []
    for f in sorted(recent, key=lambda f: f.stat().st_mtime, reverse=True)[:50]:
        try:
            with f.open(encoding="utf-8") as fh:
                first = json.loads(fh.readline() or "{}")
        except (OSError, ValueError):
            continue
        session_cwd = (first.get("payload") or {}).get("cwd") or first.get("cwd") or ""
        if _same_or_inside(session_cwd, cwd):
            out.append(f)
    return out


def _pi_candidates(cwd: Path) -> list[Path]:
    base = _pi_sessions()
    if not base.is_dir():
        return []
    now = time.time()
    recent = [f for f in base.rglob("*.jsonl") if now - f.stat().st_mtime < RECENT]
    out = []
    for f in sorted(recent, key=lambda f: f.stat().st_mtime, reverse=True)[:50]:
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                first = json.loads(fh.readline() or "{}")
        except (OSError, ValueError):
            continue
        if isinstance(first, dict) and first.get("type") == "session" and _same_or_inside(first.get("cwd") or "", cwd):
            out.append(f)
    return out


def _antigravity_log_file(logs_dir: Path) -> Optional[Path]:
    for name in ("transcript_full.jsonl", "transcript.jsonl"):
        f = logs_dir / name
        if f.is_file():
            return f
    return None


def _antigravity_candidates(cwd: Path) -> list[Path]:
    now = time.time()
    recent = [f for b in _antigravity_brains() if b.is_dir()
              for logs in b.rglob("logs") if logs.is_dir() and logs.parent.name == ".system_generated"
              for f in [_antigravity_log_file(logs)] if f and now - f.stat().st_mtime < RECENT]
    return [f for f in sorted(recent, key=lambda f: f.stat().st_mtime, reverse=True)[:50]
            if _same_or_inside((session_info(f) or {}).get("cwd") or "", cwd)]


def _env_session_transcript() -> Optional[tuple[Path, dict, str]]:
    """Transcript named by the running agent harness in environment variables, if any."""
    pf = os.environ.get("PI_SESSION_FILE", "")
    if pf:
        p = Path(pf).expanduser()
        info = session_info(p) if p.is_file() else None
        if info:
            return p.resolve(), info, "this Pi session (PI_SESSION_FILE)"
    for env, agent, pattern in (
        ("CLAUDE_CODE_SESSION_ID", "claude", UUID_RE),
        ("CODEX_THREAD_ID", "codex", UUID_RE),
        ("PI_SESSION_ID", "pi", PI_ID_RE),
        ("ANTIGRAVITY_CONVERSATION_ID", "antigravity", CONV_ID_RE),
    ):
        sid = os.environ.get(env, "")
        if not sid or ".." in Path(sid).parts or not pattern.fullmatch(sid):
            continue
        t = session_transcript({"agent": agent, "id": sid})
        info = session_info(t) if t else None
        if info:
            return t, info, f"this {AGENT_NAMES[agent]} session ({env})"
    return None


def detect_session(cwd: Optional[Path] = None) -> Optional[Path]:
    """Transcript of the agent session running in ``cwd``: the one the agent names, else the newest."""
    named = _env_session_transcript()
    if named:
        return named[0]
    cwd = (cwd or Path.cwd()).resolve()
    now = time.time()
    all_candidates = _claude_candidates(cwd) + _codex_candidates(cwd) + _pi_candidates(cwd) + _antigravity_candidates(cwd)
    found = [f for f in all_candidates if now - f.stat().st_mtime < RECENT]
    return max(found, key=lambda f: f.stat().st_mtime) if found else None


def _antigravity_cwd_and_valid(path: Path) -> tuple[bool, str]:
    """Verify ``path`` has Antigravity step records (``step_index`` + ``type``) and extract its working directory."""
    valid, cwd, fallback, gem_home = False, "", "", _antigravity_home().resolve()
    with path.open(encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i > 500 or (valid and cwd):
                break
            try:
                d = json.loads(line) if line.strip() else None
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            valid = valid or ("step_index" in d and "type" in d)
            if isinstance(d.get("cwd"), str) and d["cwd"]:
                cwd = d["cwd"]
                continue
            for tc in d.get("tool_calls") or []:
                args = tc.get("args", tc.get("parameters") or tc.get("input") or {}) if isinstance(tc, dict) else {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if not isinstance(args, dict):
                    continue
                if args.get("Cwd") or args.get("cwd"):
                    cwd = str(args.get("Cwd") or args.get("cwd"))
                    break
                for key in ("DirectoryPath", "SearchPath") if not fallback else ():
                    val = args.get(key)
                    if isinstance(val, str) and val.startswith("/") and gem_home not in (Path(val), *Path(val).parents):
                        fallback = val
                        break
            if not fallback and d.get("type") == "USER_INPUT" and isinstance(d.get("content"), str):
                m = re.search(r"Command Working Directory:\s*(/\S+)", d["content"])
                fallback = m.group(1) if m else ""
    return valid, cwd or fallback


def session_info(transcript: Path) -> Optional[dict]:
    """``{"agent", "id", "cwd"}`` of the Claude Code, Codex, Pi or Antigravity session a transcript belongs to."""
    path = Path(transcript).resolve()
    try:
        if _claude_projects().resolve() in path.parents:
            sid, cwd = path.stem, ""
            with path.open(encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i > 500:
                        break
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and d.get("cwd"):
                        sid, cwd = d.get("sessionId") or sid, d["cwd"]
                        break
            return {"agent": "claude", "id": sid, "cwd": cwd}
        if _codex_sessions().resolve() in path.parents:
            with path.open(encoding="utf-8", errors="replace") as fh:
                first = json.loads(fh.readline() or "{}")
            if not isinstance(first, dict):
                return None
            payload = first.get("payload") or {}
            found = UUID_RE.findall(path.stem)
            sid = payload.get("id") or (found[-1] if found else "")
            return {"agent": "codex", "id": sid, "cwd": payload.get("cwd") or first.get("cwd") or ""} if sid else None
        if _pi_sessions().resolve() in path.parents:
            with path.open(encoding="utf-8", errors="replace") as fh:
                first = json.loads(fh.readline() or "{}")
            if not (isinstance(first, dict) and first.get("type") == "session"):
                return None
            sid = first.get("id") or path.stem.rsplit("_", 1)[-1]
            return {"agent": "pi", "id": str(sid), "cwd": first.get("cwd") or ""} if sid else None
        if path.name in ("transcript.jsonl", "transcript_full.jsonl") and path.parent.name == "logs" \
                and path.parent.parent.name == ".system_generated":
            for brain in _antigravity_brains():
                b = brain.resolve()
                if b in path.parents:
                    cid = path.parent.parent.parent.relative_to(b).as_posix()
                    valid, cwd = _antigravity_cwd_and_valid(path)
                    return {"agent": "antigravity", "id": cid, "cwd": cwd} if (cid and valid) else None
    except (OSError, ValueError):
        return None
    return None


def session_transcript(session: dict) -> Optional[Path]:
    """Where the session's transcript lives on this machine, if it does."""
    sid = session.get("id") or ""
    if not sid or ".." in Path(sid).parts:
        return None
    agent = session.get("agent")
    if agent == "claude":
        hits = list(_claude_projects().glob(f"*/{sid}.jsonl"))
        return hits[0] if hits else None
    if agent == "codex" and _codex_sessions().is_dir():
        hits = list(_codex_sessions().rglob(f"rollout-*{sid}.jsonl"))
        return hits[0] if hits else None
    if agent == "pi" and _pi_sessions().is_dir():
        base = _pi_sessions()
        for h in (*base.rglob(f"*_{sid}.jsonl"), *base.rglob(f"{sid}.jsonl")):
            if (session_info(h) or {}).get("id") == sid:
                return h
        return next((h for h in sorted(base.rglob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:100]
                     if (session_info(h) or {}).get("id") == sid), None)
    if agent == "antigravity":
        for brain in _antigravity_brains():
            logs = brain / sid / ".system_generated" / "logs"
            chosen = _antigravity_log_file(logs) if logs.is_dir() else None
            if chosen and session_info(chosen):
                return chosen
        return None
    return None


def _redactions() -> list[tuple["re.Pattern[bytes]", bytes]]:
    home = str(Path.home()).rstrip("/")
    if len(home) <= 1:
        return []
    # Only whole path components: /home/al must not touch /home/alice.
    return [(re.compile(re.escape(home.encode()) + rb"(?=[/\\\"'\s:,;)\]}]|$)"), b"~")]


def _copy_redacted(fin, fout, pairs: list) -> None:
    """Copy a text trace line by line with the home directory replaced by ``~``; binary files untouched."""
    head = fin.read(8192)
    if not pairs or b"\x00" in head:
        fout.write(head)
        shutil.copyfileobj(fin, fout)
        return
    for line in _lines(head, fin):
        for pattern, new in pairs:
            line = pattern.sub(new, line)
        fout.write(line)


def _lines(head: bytes, fin):
    """Whole lines, read in 1 MB chunks, so a path is never split across a replacement boundary."""
    buf = head
    while True:
        cut = buf.rfind(b"\n")
        if cut >= 0:
            yield buf[: cut + 1]
            buf = buf[cut + 1:]
        chunk = fin.read(1 << 20)
        if not chunk:
            break
        buf += chunk
    if buf:
        yield buf


def attach(vdir: Path, sources: list[Path]) -> list[dict]:
    """Copy traces into ``vdir/traces`` (gzip-compressed, home directory shown as ``~``); return their metadata."""
    out = []
    pairs = _redactions()
    tdir = vdir / TRACES_DIR
    for src in sources:
        src = Path(src)
        files = [src] if src.is_file() else sorted(f for f in src.rglob("*") if f.is_file())
        for f in files:
            rel = f.name if src.is_file() else f.relative_to(src.parent).as_posix()
            dest = tdir / (rel + ("" if rel.endswith(".gz") else ".gz"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            if f.suffix == ".gz":
                with gzip.open(f, "rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
                    _copy_redacted(fin, fout, pairs)
            else:
                with f.open("rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
                    _copy_redacted(fin, fout, pairs)
            out.append({"path": dest.relative_to(vdir).as_posix(), "bytes": f.stat().st_size})
    return out


def unpacked(store_root: Path, vdir: Path, trace: dict) -> Path:
    """Plain-text copy of a stored trace under ``<store>/.cache`` (what an agent can Read and Grep)."""
    stored = vdir / trace["path"]
    rel = stored.relative_to(store_root)
    dest = store_root / CACHE_DIR / (rel.as_posix()[:-3] if rel.name.endswith(".gz") else rel.as_posix())
    if not dest.exists() or dest.stat().st_mtime < stored.stat().st_mtime:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(stored, "rb") as fin, dest.open("wb") as fout:
            shutil.copyfileobj(fin, fout)
    return dest


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


# ------------------------------------------------------------------ linking
def _is_ask_session(path: Path) -> bool:
    """Whether a transcript is one of Ask's (or ``trace review``'s) own sessions rather than an author session."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 60:
                    break
                if '"entrypoint"' in line and f'"{ASK_ENTRYPOINT}"' in line:
                    return True
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                if d.get("type") == "session_info" and str(d.get("name") or "").startswith("AgentLedger"):
                    return True
                if d.get("step_index") in (0, 1) and d.get("type") == "USER_INPUT":
                    txt = str(d.get("content") or "")
                    if ("A reviewer is asking about " in txt
                            or "A reviewer has another question about the report" in txt
                            or "Reply to the last message of the conversation, as the agent that wrote the report." in txt
                            or "Review a copy of an agent session before it is shared" in txt):
                        return True
    except OSError:
        pass
    return False


def link_session(value: str, cwd: Optional[Path] = None) -> tuple[Path, dict, str]:
    """Resolve ``--link-session``: ``auto``, a transcript path, or a session id.  Return the transcript, its session
    (``{"agent", "id", "cwd"}``) and how it was found.  Raises ``ValueError`` when there is nothing to link.

    ``auto`` takes the session the agent names (``CLAUDE_CODE_SESSION_ID``, ``CODEX_THREAD_ID``, ``PI_SESSION_FILE``,
    ``PI_SESSION_ID``, ``ANTIGRAVITY_CONVERSATION_ID``: exact whatever else runs in the directory), else the most
    recently active session for the directory, leaving out Ask's own."""
    cwd = (cwd or Path.cwd()).resolve()
    if value == "auto":
        named = _env_session_transcript()
        if named:
            return named
        now = time.time()
        all_candidates = (_claude_candidates(cwd) + _codex_candidates(cwd)
                          + _pi_candidates(cwd) + _antigravity_candidates(cwd))
        found = [f for f in all_candidates if now - f.stat().st_mtime < RECENT and not _is_ask_session(f)]
        found = [(f, session_info(f)) for f in sorted(found, key=lambda f: f.stat().st_mtime, reverse=True)]
        found = [(f, i) for f, i in found if i]
        if not found:
            raise ValueError(
                f"--link-session auto found no Claude Code, Codex, Pi or Antigravity session for {cwd} in the last "
                f"{RECENT // 3600} hours; pass the session's transcript or id"
            )
        t, info = found[0]
        age = int(time.time() - t.stat().st_mtime)
        others = f"; {len(found) - 1} other recent session(s) there" if len(found) > 1 else ""
        return t, info, f"the most recently active session for {cwd}, {age // 60} min ago{others}"
    path = Path(value).expanduser()
    if path.is_file():
        info = session_info(path)
        if not info:
            raise ValueError(
                f"{path} is not a Claude Code, Codex, Pi or Antigravity session transcript; link the original (under "
                f"{_claude_projects()}, {_codex_sessions()}, {_pi_sessions()} or {_antigravity_home()}), not a copy"
            )
        return path.resolve(), info, "the transcript given"
    for agent in ("claude", "codex", "pi", "antigravity"):
        t = session_transcript({"agent": agent, "id": value})
        info = session_info(t) if t else None
        if info:
            return t, info, "the session id given"
    raise ValueError(f"--link-session {value!r}: no such transcript or session id on this machine")


# ------------------------------------------------------------------ sharing the linked session
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def prepared_dir(store_root: Path, report_id: str) -> Path:
    """Where the copy of a report's session waits for review: ``<store>/.drafts/<project>__<report>.trace/``."""
    return Path(store_root) / ".drafts" / (report_id.strip("/").replace("/", "__") + ".trace")


def _state_file(store_root: Path, report_id: str) -> Path:
    return prepared_dir(store_root, report_id) / "state.json"


def prepare_trace(store_root: Path, report_id: str, transcript: Path, session: dict, roots: list[Path]) -> dict:
    """Write the shareable copy of ``transcript`` for ``report_id``, ready for the agent's review; return its state."""
    from .redact import known_secrets, sanitize_session

    d = prepared_dir(store_root, report_id)
    if d.exists():
        shutil.rmtree(d)
    src = Path(transcript)
    if session["agent"] == "antigravity" and src.name == "transcript.jsonl":
        full = src.with_name("transcript_full.jsonl")
        if full.is_file():
            src = full
    short_id = re.sub(r"[^A-Za-z0-9._\-]", "-", Path(session["id"]).name)[:8]
    copy = d / f"session-{session['agent']}-{short_id}.jsonl"
    counts = sanitize_session(src, copy, session["agent"], known_secrets(roots))
    state = {"report": report_id, "session": {"agent": session["agent"], "id": session["id"]}, "cwd": session.get("cwd"),
             "copy": copy.name, "prepared": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "sha256": _sha256(copy), "findings": dict(counts), "approved": None}
    _state_file(store_root, report_id).write_text(json.dumps(state, indent=2), encoding="utf-8")
    return {**state, "path": str(copy)}


def load_trace_state(store_root: Path, report_id: str) -> Optional[dict]:
    f = _state_file(store_root, report_id)
    if not f.is_file():
        return None
    state = json.loads(f.read_text(encoding="utf-8"))
    return {**state, "path": str(prepared_dir(store_root, report_id) / state["copy"])}


def approve_trace(store_root: Path, report_id: str, roots: list[Path]) -> dict:
    """Record the reviewed copy as approved, after checking no known secret or credential format is left in it."""
    from .redact import known_secrets, scan_file

    state = load_trace_state(store_root, report_id)
    if not state or not Path(state["path"]).is_file():
        raise ValueError(f"no prepared trace for {report_id}: run `agentledger trace prepare {report_id} "
                         f"--link-session auto` first")
    left = scan_file(Path(state["path"]), known_secrets(roots))
    if left:
        raise ValueError(f"{state['path']} still holds: " + ", ".join(f"{k} ×{n}" for k, n in left.items()) +
                         "; edit it or prepare it again")
    state["approved"] = {"sha256": _sha256(Path(state["path"])),
                         "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    path = state.pop("path")
    _state_file(store_root, report_id).write_text(json.dumps(state, indent=2), encoding="utf-8")
    return {**state, "path": path}


def approved_trace(store_root: Path, report_id: str, session: dict) -> Path:
    """The approved copy of ``session`` for ``report_id`` (what ``publish --trace auto`` shares), or ValueError."""
    state = load_trace_state(store_root, report_id)
    how = (f"run `agentledger trace prepare {report_id} --link-session ...`, review the copy, then "
           f"`agentledger trace approve {report_id}`")
    if not state or not Path(state["path"]).is_file():
        raise ValueError(f"--trace auto shares only a reviewed copy of the session, and {report_id} has none: {how}")
    if state["session"].get("id") != session.get("id"):
        raise ValueError(f"the prepared trace is of session {state['session'].get('id')}, but this version links "
                         f"{session.get('id')}: prepare it again for the linked session")
    if not state.get("approved"):
        raise ValueError(f"the prepared trace for {report_id} has not been reviewed and approved: {how}")
    if state["approved"]["sha256"] != _sha256(Path(state["path"])):
        raise ValueError(f"{state['path']} changed after it was approved: "
                         f"run `agentledger trace approve {report_id}` again")
    return Path(state["path"])


def record_review(store_root: Path, report_id: str, review: dict) -> None:
    """Note an agent review of the prepared copy (``agentledger trace review``); an earlier approval no longer holds."""
    f = _state_file(store_root, report_id)
    state = json.loads(f.read_text(encoding="utf-8"))
    state["reviewed"] = review
    state["approved"] = None
    f.write_text(json.dumps(state, indent=2), encoding="utf-8")


def discard_trace(store_root: Path, report_id: str) -> None:
    d = prepared_dir(store_root, report_id)
    if d.exists():
        shutil.rmtree(d)
