"""Traces: the transcripts and logs behind a report.

``publish --trace auto`` attaches the transcript of the agent session that is
publishing (Claude Code or Codex); ``--trace <path>`` attaches any file or
folder.  Traces are stored gzip-compressed in ``vNNN/traces/`` so the store
stays small, and unpacked on demand into ``<store>/.cache/`` when an agent is
asked a question about the report.
"""

from __future__ import annotations

import gzip
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


def _claude_projects() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def _codex_sessions() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


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
        session_cwd = (first.get("payload") or {}).get("cwd") or first.get("cwd")
        if session_cwd and (Path(session_cwd) == cwd or Path(session_cwd) in cwd.parents):
            out.append(f)
    return out


def detect_session(cwd: Optional[Path] = None) -> Optional[Path]:
    """Transcript of the agent session running in ``cwd``: the most recently written one."""
    cwd = (cwd or Path.cwd()).resolve()
    now = time.time()
    found = [f for f in _claude_candidates(cwd) + _codex_candidates(cwd) if now - f.stat().st_mtime < RECENT]
    return max(found, key=lambda f: f.stat().st_mtime) if found else None


def attach(vdir: Path, sources: list[Path]) -> list[dict]:
    """Copy traces into ``vdir/traces`` (gzip-compressed); return their metadata."""
    out = []
    tdir = vdir / TRACES_DIR
    for src in sources:
        src = Path(src)
        files = [src] if src.is_file() else sorted(f for f in src.rglob("*") if f.is_file())
        for f in files:
            rel = f.name if src.is_file() else f.relative_to(src.parent).as_posix()
            dest = tdir / (rel + ("" if rel.endswith(".gz") else ".gz"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            if f.suffix == ".gz":
                shutil.copy2(f, dest)
            else:
                with f.open("rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
                    shutil.copyfileobj(fin, fout)
            out.append({"path": dest.relative_to(vdir).as_posix(), "source": str(f), "bytes": f.stat().st_size})
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
