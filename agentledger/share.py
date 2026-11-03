"""Share one report as a single file, and open such a file on another machine.

``agentledger share <report>`` packs the report (its versions, sub-reports, traces and review threads) into a
small self-contained store and zips it. The rest of your store, your local cache and your GitHub settings stay
behind. ``agentledger open <file>.agentledger.zip`` unpacks it on the reader's machine, where they read it,
comment, and Ask *their own* agent, which answers from the report and its traces. Their comments come back to you
as a CSV for ``agentledger comments import``. Opening a newer bundle keeps the reader's comments.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

from . import __version__
from . import comments as cm
from .build import build
from .markdown import slugify
from .store import CONFIG_NAME, PROJECTS, LedgerError, Store, init_store, now_iso, parse_report_id, read_json, write_json

SUFFIX = ".agentledger.zip"
MARKER = "agentledger-share.json"
MAX_UNPACKED = 4 << 30  # refuse bundles that would unpack to more than 4 GiB


def default_dir(report_id: str) -> Path:
    return Path.home() / ".cache" / "agentledger" / "shared" / report_id.replace("/", "__")


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
        build(Store(root))
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(root).as_posix())
    return {"path": out, "bytes": out.stat().st_size, "versions": n_versions, "traces": n_traces, "threads": n_threads}


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
