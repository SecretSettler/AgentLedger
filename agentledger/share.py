"""Share one report as a single file, and open such a file on another machine.

``agentledger share <report>`` packs the report (its versions, sub-reports, traces and review threads) into a
small self-contained store and zips it. The rest of your store, your local cache and your GitHub settings stay
behind. ``agentledger open <file>.agentledger.zip`` unpacks it on the reader's machine, where they read it,
comment, and Ask *their own* agent, which answers from the report and its traces. Their comments come back to you
as a CSV for ``agentledger comments import``. Opening a newer bundle keeps the reader's comments.

No credential leaves with a bundle.  Before zipping, every file in it goes through the same redaction as
``agentledger trace prepare`` (``redact.Scrubber``): the values of secrets known on this machine (``.env`` files near
the store, the repositories and the sessions behind the report, and credential-named environment variables) and
common credential formats become ``[REDACTED:<name>]``, and the home directory becomes ``~``.  A trace that is a raw
Claude Code or Codex session (attached before 0.4, or by path) is cut down to the conversation as ``trace prepare``
does.  Then the whole bundle, built pages included, is scanned again, and nothing is written if anything is left.
Only the bundle is changed; the store stays as it is.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
import tempfile
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Optional

from . import __version__
from . import comments as cm
from . import redact
from .build import build
from .markdown import slugify
from .store import CONFIG_NAME, PROJECTS, LedgerError, Store, init_store, now_iso, parse_report_id, read_json, write_json

SUFFIX = ".agentledger.zip"
MARKER = "agentledger-share.json"
MAX_UNPACKED = 4 << 30  # refuse bundles that would unpack to more than 4 GiB


def default_dir(report_id: str) -> Path:
    return Path.home() / ".cache" / "agentledger" / "shared" / report_id.replace("/", "__")


# ------------------------------------------------------------------ credentials
SNIFF = 8192


def secret_roots(store: Store, report_id: str) -> list[Path]:
    """Where the secrets to look for live: here, the store, and every checkout and session folder behind the report."""
    roots = [Path.cwd(), store.root]
    for key, info in read_json(store._local_file()).items():
        if key.startswith((report_id + "@", report_id + "/")) and isinstance(info, dict):
            roots += [Path(info[k]) for k in ("repo_path", "session_cwd") if info.get(k)]
    return list(dict.fromkeys(roots))


def _home_pattern() -> Optional[re.Pattern]:
    home = str(Path.home()).rstrip("/")
    # whole path components only: /home/al must not touch /home/alice
    return re.compile(re.escape(home) + r"(?=[/\\\"'\s:,;)\]}]|$)") if len(home) > 1 else None


def _session_kind(path: Path) -> Optional[str]:
    """``claude``, ``codex``, ``pi`` or ``antigravity`` when a (plain) trace is an agent session transcript, else None."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for _, line in zip(range(50), fh):
            try:
                ev = json.loads(line)
            except ValueError:
                return None
            if not isinstance(ev, dict):
                return None
            if ev.get("type") in ("session_meta", "response_item", "turn_context") and "payload" in ev:
                return "codex"
            if "sessionId" in ev and (ev.get("type") in ("user", "assistant", "summary", "system", "session-owner")
                                      or isinstance(ev.get("message"), dict)):
                return "claude"
            if ev.get("type") == "session" and "id" in ev and ("version" in ev or "cwd" in ev):
                return "pi"
            if "step_index" in ev and (ev.get("type") in redact.ANTIGRAVITY_DROP_STEPS
                                       or ev.get("type") in redact.ANTIGRAVITY_TOOL_STEPS
                                       or ev.get("type") in ("USER_INPUT", "PLANNER_RESPONSE")):
                return "antigravity"
    return None


class Cleaner:
    """Redact one bundle in place and say what changed, as counts (never the values)."""

    def __init__(self, secrets: dict[str, str]):
        self.secrets = secrets
        self.home = _home_pattern()
        self.redacted: Counter = Counter()      # what was replaced, by kind
        self.files: dict[str, Counter] = {}     # per bundle path
        self.sessions = 0                       # raw transcripts cut down to the conversation
        self.dropped: Counter = Counter()       # what those lost (system text, reasoning, ...)
        self.emails = 0                         # left in: personal data, not credentials

    def _lines(self, fin, fout, scrub: redact.Scrubber) -> None:
        for line in fin:
            if self.home:
                line, n = self.home.subn("~", line)
                if n:
                    scrub.counts["home directory paths"] += n
            fout.write(scrub(line))

    def _plain(self, src: Path, dest: Path) -> tuple[Counter, bool]:
        """Clean text file ``src`` into ``dest``; return what was replaced, and whether ``dest`` differs from it."""
        kind = _session_kind(src)
        if kind:
            counts = redact.sanitize_session(src, dest, kind, self.secrets)
            dropped = {k: n for k, n in counts.items() if k.startswith("dropped") and n}
            if dropped:  # a raw transcript; a copy `trace prepare` made already holds only the conversation
                self.sessions += 1
                self.dropped.update(dropped)
            self.emails += counts.get("to review: e-mail addresses", 0)
            tmp = dest.with_name(dest.name + ".home")
            scrub = redact.Scrubber(self.secrets)
            with dest.open(encoding="utf-8") as fin, tmp.open("w", encoding="utf-8") as fout:
                self._lines(fin, fout, scrub)  # sanitize_session already scrubbed; this adds the ~ paths
            tmp.replace(dest)
            return Counter({k: n for k, n in counts.items()
                            if not k.startswith(("dropped", "kept", "to review"))}) + scrub.counts, True
        scrub = redact.Scrubber(self.secrets)
        with src.open(encoding="utf-8", errors="replace", newline="") as fin, \
                dest.open("w", encoding="utf-8", newline="") as fout:
            self._lines(fin, fout, scrub)
        self.emails += scrub.hints.get("e-mail addresses", 0)
        return scrub.counts, bool(scrub.counts)  # nothing replaced: keep the original bytes (encoding and all)

    def file(self, f: Path, name: str) -> None:
        gz = f.name.endswith(".gz")
        with (gzip.open(f, "rb") if gz else f.open("rb")) as fh:
            head = fh.read(SNIFF)
        if b"\x00" in head:
            left = scan(f, self.secrets)
            if left:
                raise LedgerError(f"not shared: {name} is a binary file and looks like it holds {_fmt(left)}; "
                                  "it cannot be redacted, so take it out of the report (or share --no-traces if it is a trace)")
            return
        with tempfile.TemporaryDirectory(prefix="agentledger-clean-") as tmp:
            src, dest = Path(tmp) / "in", Path(tmp) / "out"
            if gz:
                with gzip.open(f, "rb") as fin, src.open("wb") as fout:
                    shutil.copyfileobj(fin, fout)
            else:
                shutil.copyfile(f, src)
            counts, rewrite = self._plain(src, dest)
            if rewrite:
                if gz:
                    with dest.open("rb") as fin, gzip.open(f, "wb", compresslevel=6) as fout:
                        shutil.copyfileobj(fin, fout)
                else:
                    shutil.copyfile(dest, f)
        if counts:
            self.files[name] = counts
            self.redacted.update(counts)

    def summary(self) -> dict:
        creds = Counter({k: n for k, n in self.redacted.items() if k != "home directory paths"})
        return {"redacted": dict(creds), "home_paths": self.redacted.get("home directory paths", 0),
                "files": {k: dict(v) for k, v in self.files.items()}, "sessions_trimmed": self.sessions,
                "dropped": dict(self.dropped), "emails": self.emails}


def scan(f: Path, secrets: Optional[dict] = None) -> Counter:
    """What is still credential-like in ``f`` (gzip read through; a binary file read as text)."""
    scrub = redact.Scrubber(secrets or {})
    with (gzip.open(f, "rt", encoding="utf-8", errors="replace") if f.name.endswith(".gz")
          else f.open(encoding="utf-8", errors="replace")) as fh:
        for line in fh:
            scrub(line)
    return scrub.counts


def _fmt(counts: Counter) -> str:
    return ", ".join(f"{k} ×{n}" for k, n in counts.most_common())


def pack(store: Store, report_id: str, out: Path, *, traces: bool = True, comments: bool = True) -> dict:
    report = store.report(report_id)
    if not report.versions:
        raise LedgerError(f"{report.id} has no versions to share")
    segs = parse_report_id(report.id)
    with tempfile.TemporaryDirectory(prefix="agentledger-share-") as tmp:
        root = init_store(Path(tmp) / "store", name=f"{report.title} (shared from {store.name})")
        (root / ".gitignore").unlink()
        write_json(root / MARKER, {"format": 1, "report": report.id, "from": store.name, "created": now_iso(),
                                   "agentledger": __version__, "traces": traces, "comments": comments})
        project = store.root / PROJECTS / segs[0]
        write_json(root / PROJECTS / segs[0] / "project.json", read_json(project / "project.json"))
        for i in range(2, len(segs)):  # parents of a sub-report: their titles, not their content
            rel = Path(PROJECTS, *segs[:i], "report.json")
            write_json(root / rel, read_json(store.root / rel))

        def skip(folder: str, names: list[str]) -> set[str]:
            out_names = {n for n in names if n.startswith(".") or n == "index.html"}
            if not traces:
                out_names |= {n for n in names if n == "traces"}
            return out_names

        dest = root / PROJECTS / Path(*segs)
        shutil.copytree(report.path, dest, ignore=skip)
        n_versions = n_traces = n_threads = 0
        for meta_path in sorted(dest.rglob("meta.json")):
            meta = read_json(meta_path)
            n_versions += 1
            meta.pop("allow_scripts", None)  # a shared report is always rendered sanitised
            if isinstance(meta.get("context"), dict):
                meta["context"].pop("path", None)  # stores from 0.2.0 kept the checkout path here
            if not traces:
                meta.pop("traces", None)
                meta.pop("session", None)
            n_traces += len(meta.get("traces", []))
            write_json(meta_path, meta)
            csv_path = meta_path.parent / "comments.csv"
            if comments:
                n_threads += len(cm.threads(cm.read(csv_path)))
            else:
                cm.write(csv_path, [])
        secrets = redact.known_secrets(secret_roots(store, report.id))
        cleaner = Cleaner(secrets)
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.name != MARKER:
                cleaner.file(f, f.relative_to(root).as_posix())
        n_checked = sum(1 for f in root.rglob("*") if f.is_file())
        build(Store(root))
        left = {f.relative_to(root).as_posix(): c for f in sorted(root.rglob("*")) if f.is_file()
                for c in [scan(f, secrets)] if c}
        if left:  # the built pages and anything the pass missed: never write a bundle with a credential in it
            raise LedgerError("not shared: still credential-like after redaction: "
                              + "; ".join(f"{k} ({_fmt(c)})" for k, c in left.items()))
        marker = read_json(root / MARKER)
        marker["redaction"] = {k: v for k, v in cleaner.summary().items() if k in ("redacted", "sessions_trimmed")}
        write_json(root / MARKER, marker)
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(root).as_posix())
    return {"path": out, "bytes": out.stat().st_size, "versions": n_versions, "traces": n_traces, "threads": n_threads,
            "checked": n_checked, **cleaner.summary()}


def _safe_members(z: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members, total = [], 0
    for info in z.infolist():
        p = PurePosixPath(info.filename)
        if info.filename.startswith("/") or "\\" in info.filename or ".." in p.parts or (p.parts and ":" in p.parts[0]):
            raise LedgerError(f"refusing bundle entry {info.filename!r}")
        total += info.file_size
        members.append(info)
    if total > MAX_UNPACKED:
        raise LedgerError("refusing a bundle that unpacks to more than 4 GiB")
    names = {m.filename for m in members}
    if MARKER not in names or CONFIG_NAME not in names:
        raise LedgerError("not an AgentLedger share bundle (made with `agentledger share`)")
    return members


def unpack(bundle: Path, target: Optional[Path] = None) -> tuple[Store, dict]:
    """Unpack (or update) a shared report; the reader's own comments survive a newer bundle."""
    bundle = Path(bundle).expanduser()
    with zipfile.ZipFile(bundle) as z:
        members = _safe_members(z)
        info = json.loads(z.read(MARKER))
        report_id = "/".join(parse_report_id(info.get("report", "")))
        target = Path(target).expanduser() if target else default_dir(report_id)
        if target.exists() and not (target / MARKER).exists():
            raise LedgerError(f"{target} exists and is not a shared report; pick another --into")
        if target.exists() and read_json(target / MARKER).get("report") != report_id:
            raise LedgerError(f"{target} holds a different shared report; pick another --into")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="agentledger-open-", dir=target.parent) as tmp:
            fresh = Path(tmp) / "bundle"
            z.extractall(fresh, members=members)
            if not target.exists():
                shutil.move(str(fresh), str(target))
            else:
                for f in sorted(fresh.rglob("*")):
                    if not f.is_file():
                        continue
                    dest = target / f.relative_to(fresh)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if f.name == "comments.csv" and dest.exists():
                        merged, _ = cm.merge(cm.read(dest), cm.read(f))  # keep the reader's own threads
                        cm.write(dest, merged)
                    else:
                        shutil.copy2(f, dest)
    for meta_path in (target / PROJECTS).rglob("meta.json"):
        meta = read_json(meta_path)
        if meta.pop("allow_scripts", None):
            write_json(meta_path, meta)  # never run a sender's scripts on the reader's machine
    gitignore = target / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".drafts/\n.cache/\n", encoding="utf-8")
    store = Store(target)
    build(store)
    return store, info


def default_name(report_id: str) -> str:
    return slugify(report_id.replace("/", "-")) + SUFFIX
