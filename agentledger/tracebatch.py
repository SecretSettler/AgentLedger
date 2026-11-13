"""Trace-store batches: raw traces kept outside the ledger, in GitHub releases (TrajFS experiment batches).

A batch release holds ``batch.json`` (its runs: id, source path, parts), one ``<run>.capture.json`` per run (every
file's path, SHA256 and size), each run's TrajFS store split into ``<run>.trace.trajstore.tar.partNNNN``, and
``checksums.json``.

``publish --trace-batch <release>`` (or ``agentledger trace batch add``) records a batch on a version: its runs and
their file lists, downloaded and checked against the release's checksums, never the raw traces.  A citation such as
``trace:runs/r1/sessions/x/session.v3.jsonl#L2559`` then resolves to one file of one run, and
``agentledger trace get`` downloads only that run with the reader's own GitHub access, checks every byte, restores it
with TrajFS and keeps the file under the store's ``.cache`` (never committed, and served only to this machine).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

BATCH_DIR = "trace-batches"
CACHE = ".cache/trace-store"
RELEASE_RE = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/releases/tag/([^/?#\s]+)/?$")
SHORT_RE = re.compile(r"^([\w.-]+/[\w.-]+)@([^\s@/]+)$")


class BatchError(Exception):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(tag: str) -> str:
    return re.sub(r"[^\w.-]", "_", tag)


def relative(value: str) -> PurePosixPath:
    p = PurePosixPath(value)
    if not value or p.is_absolute() or "\\" in value or any(x in ("", ".", "..") for x in value.split("/")):
        raise BatchError(f"unsafe path in the batch: {value!r}")
    return p


# ------------------------------------------------------------------ GitHub
class _SafeRedirect(HTTPRedirectHandler):
    """Follow GitHub's redirect to its download host, and never carry the token anywhere else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse(newurl)
        host = target.hostname or ""
        if target.scheme != "https" or target.username or not (
                host in ("api.github.com", "github.com") or host.endswith(".githubusercontent.com")):
            raise BatchError("refusing an unexpected download redirect")
        nxt = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nxt is not None and target.netloc != urlparse(req.full_url).netloc:
            nxt.remove_header("Authorization")
        return nxt


class GitHub:
    """The reader's own access to the trace store: GH_TOKEN, GITHUB_TOKEN or `gh auth token` (never the author's)."""

    API = "https://api.github.com"

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not self.token and shutil.which("gh"):
            r = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"], capture_output=True, text=True)
            if r.returncode == 0:
                self.token = r.stdout.strip() or None
        self.opener = build_opener(_SafeRedirect())

    def _open(self, path: str, binary: bool = False):
        headers = {"Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                   "User-Agent": "agentledger", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        for attempt in range(3):  # GitHub's download hosts answer 502/503 now and then; a second try usually works
            try:
                return self.opener.open(Request(self.API + path, headers=headers), timeout=120)
            except HTTPError as e:
                if e.code in (502, 503, 504) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                repo = "/".join(path.split("/")[2:4])
                if e.code in (401, 403, 404):
                    raise BatchError(f"GitHub answered {e.code} for {repo}: reading it needs your own access to that "
                                     "repository (gh auth login, or GH_TOKEN); a report link does not grant it") from None
                raise BatchError(f"GitHub answered {e.code}") from None
            except URLError as e:
                raise BatchError(f"cannot reach GitHub ({e.reason})") from None

    def json(self, path: str):
        with self._open(path) as fh:
            return json.load(fh)

    def release(self, repo: str, tag: str, release_id: Optional[int] = None) -> dict:
        if release_id:
            try:
                return self.json(f"/repos/{repo}/releases/{int(release_id)}")
            except BatchError:
                pass
        try:
            return self.json(f"/repos/{repo}/releases/tags/{quote(tag, safe='')}")
        except BatchError:
            pass  # a draft has no tag yet: look for it among the releases
        for page in range(1, 11):
            rows = self.json(f"/repos/{repo}/releases?per_page=100&page={page}")
            for r in rows:
                if r.get("tag_name") == tag or (r.get("html_url") or "").endswith("/tag/" + tag):
                    return r
            if len(rows) < 100:
                break
        raise BatchError(f"no release {tag} in {repo} that you can read")

    def download(self, repo: str, asset: dict, dest: Path, expected: Optional[str] = None) -> Path:
        """Download one release asset to ``dest``, checked against ``expected`` and GitHub's own digest."""
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            if expected and expected != digest[7:]:
                raise BatchError(f"{asset['name']}: the batch's checksum and GitHub's digest disagree")
            expected = expected or digest[7:]
        if dest.exists() and expected and sha256(dest) == expected:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".part-")
        try:
            with os.fdopen(fd, "wb") as out, self._open(f"/repos/{repo}/releases/assets/{asset['id']}", binary=True) as src:
                shutil.copyfileobj(src, out, 1 << 20)
            if expected and sha256(Path(tmp)) != expected:
                raise BatchError(f"{asset['name']} failed its SHA256 check")
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return dest


def parse(value: str, default_repo: Optional[str] = None) -> tuple[str, str]:
    """``(owner/repo, tag)`` from a release URL, ``owner/repo@tag``, or a tag of the store's ``trace_store``."""
    value = value.strip()
    m = RELEASE_RE.match(value) or SHORT_RE.match(value)
    if m:
        return m.group(1), m.group(2)
    if default_repo and re.fullmatch(r"[\w.-]+", value):
        return default_repo, value
    raise BatchError(f"{value}: give the batch's release link (https://github.com/<owner>/<repo>/releases/tag/<tag>) "
                     "or <owner>/<repo>@<tag>")


# ------------------------------------------------------------------ registering
def register(version, value: str, default_repo: Optional[str] = None, gh: Optional[GitHub] = None) -> dict:
    """Record batch ``value`` on ``version``: its runs and their file lists, checked; returns the index written."""
    repo, tag = parse(value, default_repo)
    gh = gh or GitHub()
    rel = gh.release(repo, tag)
    assets = {a["name"]: a for a in rel.get("assets", [])}
    for need in ("batch.json", "checksums.json"):
        if need not in assets:
            raise BatchError(f"{repo}@{tag} is not a TrajFS experiment batch (no {need})")
    with tempfile.TemporaryDirectory(prefix="agentledger-batch-") as tmp:
        tmp = Path(tmp)
        sums = json.loads(gh.download(repo, assets["checksums.json"], tmp / "checksums.json").read_text())
        batch = json.loads(gh.download(repo, assets["batch.json"], tmp / "batch.json", sums.get("batch.json")).read_text())
        runs = []
        for r in batch.get("runs", []):
            cap_name = r["capture_asset"]
            if cap_name not in assets:
                raise BatchError(f"{tag}: the release has no {cap_name}")
            want = r.get("asset_hashes", {}).get(cap_name) or sums.get(cap_name)
            if sums.get(cap_name) and want != sums[cap_name]:
                raise BatchError(f"{tag}: run {r['id']} and the release checksums disagree")
            cap = json.loads(gh.download(repo, assets[cap_name], tmp / safe_name(cap_name), want).read_text())
            if cap.get("run_id") != r["id"] or cap.get("fingerprint") != r.get("fingerprint"):
                raise BatchError(f"{tag}: the capture of run {r['id']} does not match the batch")
            parts = []
            for name in r.get("parts", []):
                if name not in assets:
                    raise BatchError(f"{tag}: the release has no {name}")
                parts.append({"name": name, "sha256": r["asset_hashes"][name], "size": assets[name].get("size", 0)})
            for f in cap.get("files", []):
                relative(f["path"])
            runs.append({"id": r["id"], "source": r.get("source", ""), "fingerprint": r.get("fingerprint"),
                         "parts": parts, "trajfs_commit": cap.get("trajfs_commit"),
                         "files": [[f["path"], f["sha256"], f["size"]] for f in cap.get("files", [])]})
    index = {"schema": 1, "repo": repo, "tag": tag, "release_id": rel.get("id"), "release_url": rel.get("html_url"),
             "title": rel.get("name") or batch.get("title", ""), "draft": bool(rel.get("draft")),
             "batch_sha256": sums.get("batch.json"), "runs": runs,
             "blocked": [b.get("run") if isinstance(b, dict) else b for b in batch.get("blocked", [])]}
    out = Path(version.path) / BATCH_DIR / f"{safe_name(tag)}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    meta_path = Path(version.path) / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    listed = [b for b in meta.get("trace_batches", []) if b.get("tag") != tag]
    meta["trace_batches"] = listed + [{"repo": repo, "tag": tag, "release_url": index["release_url"],
                                       "index": f"{BATCH_DIR}/{out.name}", "runs": len(runs)}]
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return index


def indexes(version) -> list[dict]:
    out = []
    for b in version.meta.get("trace_batches", []):
        f = Path(version.path) / b.get("index", "")
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


# ------------------------------------------------------------------ resolving
@dataclass
class Hit:
    version: object
    batch: dict
    run: dict
    path: str
    sha256: str
    size: int

    def cache_path(self, store_root: Path) -> Path:
        return Path(store_root) / CACHE / safe_name(self.batch["tag"]) / self.run["id"] / self.path

    def describe(self) -> str:
        return f"{self.batch['repo']}@{self.batch['tag']}, run {self.run['id']}, {self.path}"


def resolve(report, v, name: str, version: Optional[int] = None):
    """The batch file a citation names: ``<path in run>``, ``<end of the run's source>/<path>``, ``<run id>/<path>``,
    or any of those after ``<tag>/``.  Returns a Hit, None, or raises BatchError when it is ambiguous."""
    candidates = ([x for x in report.versions if x.number == version] if version
                  else [x for x in reversed(report.versions) if x.number <= v.number])
    parts = name.split("/")
    for x in candidates:
        found = []
        for b in indexes(x):
            segs = parts[1:] if parts and parts[0] in (b["tag"], safe_name(b["tag"])) else parts
            for run in b["runs"]:
                files = {f[0]: f for f in run["files"]}
                src = run.get("source", "").rstrip("/")
                for i in range(len(segs)):
                    prefix, rest = "/".join(segs[:i]), "/".join(segs[i:])
                    if rest in files and (i == 0 or src.endswith("/" + prefix) or src == prefix or prefix == run["id"]):
                        found.append((i, b, run, files[rest]))
                        break
        if found:
            best = max(i for i, *_ in found)
            top = {(b["tag"], run["id"], f[0]): (b, run, f) for i, b, run, f in found if i == best}
            if len({(f[1], f[2]) for _, _, f in top.values()}) > 1:
                where = "; ".join(f"{t} run {r}" for t, r, _ in list(top)[:4])
                raise BatchError(f"{name} matches more than one run ({where}): give more of the run's path")
            # The same bytes in several runs (an audit keeps a copy of a session) are the same evidence: take the copy
            # that is cheapest to download.
            b, run, f = min(top.values(), key=lambda c: (sum(p.get("size", 0) for p in c[1]["parts"]), c[1]["id"]))
            return Hit(x, b, run, f[0], f[1], f[2])
    return None


# ------------------------------------------------------------------ fetching
def _download_root() -> Path:
    return Path(os.environ.get("AGENTLEDGER_TRACE_CACHE") or (Path.home() / ".cache" / "agentledger" / "trace-store"))


def _unpack(parts: list[Path], dest: Path) -> Path:
    """Join a run's parts and extract the TrajFS store, refusing anything but plain files under trace.trajstore/."""
    store = dest / "trace.trajstore"
    if (store / "MANIFEST.json").is_file():
        return store
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".unpack-") as tmp:
        tmp = Path(tmp)
        with tempfile.TemporaryFile() as joined:
            for p in parts:
                with open(p, "rb") as fh:
                    shutil.copyfileobj(fh, joined, 1 << 20)
            joined.seek(0)
            with tarfile.open(fileobj=joined, mode="r|") as tf:
                for m in tf:
                    name = m.name.rstrip("/")
                    rel = relative(name)
                    if rel.parts[0] != "trace.trajstore" or not (m.isfile() or m.isdir()):
                        raise BatchError(f"unsafe member in the TrajFS archive: {m.name!r}")
                    target = tmp.joinpath(*rel.parts)
                    if m.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tf.extractfile(m) as src, open(target, "xb") as out:
                        shutil.copyfileobj(src, out, 1 << 20)
        if not (tmp / "trace.trajstore").is_dir():
            raise BatchError("the archive holds no trace.trajstore")
        if dest.exists():
            shutil.rmtree(dest)
        (tmp / "trace.trajstore").rename(tmp / "ready")
        dest.mkdir(parents=True)
        (tmp / "ready").rename(store)
    return store


def find_traj(explicit: Optional[str] = None) -> Optional[str]:
    for cand in (explicit, os.environ.get("AGENTLEDGER_TRAJ"), shutil.which("traj")):
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def fetch(store_root: Path, hit: Hit, *, raw_root: Optional[Path] = None, traj: Optional[str] = None,
          gh: Optional[GitHub] = None, say: Callable[[str], None] = lambda s: None) -> Path:
    """Put the cited file in the store's cache, byte-for-byte as captured; return its path."""
    dest = hit.cache_path(store_root)
    if dest.is_file() and dest.stat().st_size == hit.size and sha256(dest) == hit.sha256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".get-") as tmp:
        got = Path(tmp) / "file"
        if raw_root:
            src = Path(raw_root).joinpath(*relative(hit.path).parts)
            for node in [src] + list(src.parents):
                if node == Path(raw_root):
                    break
                if node.is_symlink():
                    raise BatchError(f"refusing a symlink in {raw_root}")
            shutil.copyfile(src, got)
        else:
            tool = find_traj(traj)
            if not tool:
                commit = hit.run.get("trajfs_commit") or "the commit the batch was packed with"
                raise BatchError("reading a TrajFS store needs `traj` (https://github.com/jingliu9/TrajFS, built at "
                                 f"{commit}) on PATH or in AGENTLEDGER_TRAJ; or pass --raw-root with the run's "
                                 "original folder if you have it")
            gh = gh or GitHub()
            rel = gh.release(hit.batch["repo"], hit.batch["tag"], hit.batch.get("release_id"))
            assets = {a["name"]: a for a in rel.get("assets", [])}
            root = _download_root() / hit.batch["repo"].replace("/", "__") / safe_name(hit.batch["tag"])
            total = sum(p.get("size", 0) for p in hit.run["parts"])
            say(f"downloading run {hit.run['id']} ({len(hit.run['parts'])} part(s), {total / 1e6:.1f} MB) "
                f"from {hit.batch['repo']}@{hit.batch['tag']}")
            files = []
            for p in hit.run["parts"]:
                if p["name"] not in assets:
                    raise BatchError(f"the release no longer has {p['name']}")
                files.append(gh.download(hit.batch["repo"], assets[p["name"]], root / "parts" / p["name"], p["sha256"]))
            store = _unpack(files, root / "stores" / f"{hit.run['id']}-{(hit.run.get('fingerprint') or '')[:12]}")
            say(f"restoring {hit.path} with TrajFS")
            out = Path(tmp) / "extract"
            r = subprocess.run([tool, "extract", "-S", str(store), hit.path, str(out)], capture_output=True, text=True)
            restored = out if out.is_file() else out.joinpath(*relative(hit.path).parts)
            if r.returncode != 0 or not restored.is_file():
                hits = [p for p in out.rglob("*") if p.is_file()] if out.exists() else []
                if r.returncode == 0 and len(hits) == 1:
                    restored = hits[0]
                else:
                    raise BatchError("TrajFS could not restore the file: " + (r.stderr.strip()[-400:] or "no output"))
            shutil.copyfile(restored, got)
        if got.stat().st_size != hit.size or sha256(got) != hit.sha256:
            raise BatchError(f"{hit.path} does not match the SHA256 and size recorded when the run was captured")
        os.replace(got, dest)
    receipt = {"repo": hit.batch["repo"], "tag": hit.batch["tag"], "release_url": hit.batch.get("release_url"),
               "run": hit.run["id"], "path": hit.path, "sha256": hit.sha256, "size": hit.size,
               "from": "raw-root" if raw_root else "release"}
    dest.with_name(dest.name + ".receipt.json").write_text(json.dumps(receipt, indent=1) + "\n", encoding="utf-8")
    return dest
