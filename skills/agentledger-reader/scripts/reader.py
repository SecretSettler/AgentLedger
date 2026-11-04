#!/usr/bin/env python3
"""Prepare a pinned AgentLedger report and retrieve individual private trace files."""
import argparse
import base64
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener

HERE = Path(__file__).resolve().parents[1]
RELEASE_RE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/releases/tag/([\w.-]+)")
HEX64 = re.compile(r"[a-f0-9]{64}")
class ReaderError(Exception):
    pass

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def relative(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or "\\" in value or any(x in ("", ".", "..") for x in value.split("/")):
        raise ReaderError("Unsafe relative path")
    return p

def inside(root, value):
    p = root.joinpath(*relative(value).parts)
    for node in [p] + list(p.parents):
        if node == root:
            break
        if node.is_symlink():
            raise ReaderError("Refusing a symlink in the evidence workspace")
    return p

def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        f.write(data)
    temp.replace(path)

def save(path, data):
    write(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode())

def load(path):
    return json.loads(path.read_text())

def emit(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))

class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse(newurl)
        if target.scheme != "https" or target.username or not (
            target.hostname in ("api.github.com", "github.com")
            or (target.hostname or "").endswith(".githubusercontent.com")
        ):
            raise ReaderError("Refusing an unexpected download redirect")
        nxt = super().redirect_request(req, fp, code, msg, headers, newurl)
        if nxt is not None and target.netloc != urlparse(req.full_url).netloc:
            nxt.remove_header("Authorization")
        return nxt

class GitHub:
    def __init__(self):
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not self.token and shutil.which("gh"):
            r = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                self.token = r.stdout.strip()
        self.opener = build_opener(SafeRedirect())

    def open(self, endpoint, binary=False):
        if not endpoint.startswith("/repos/"):
            raise ReaderError("Expected a GitHub repository API endpoint")
        headers = {"Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                   "User-Agent": "AgentLedger-reader", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            return self.opener.open(Request("https://api.github.com" + endpoint, headers=headers), timeout=60)
        except HTTPError as e:
            repo = "/".join(endpoint.split("/")[2:4])
            if e.code in (401, 403, 404):
                raise ReaderError("GitHub HTTP %s for %s. Use YOUR gh login or GH_TOKEN/GITHUB_TOKEN "
                                  "with read access; a report link does not grant private trace access."
                                  % (e.code, repo)) from None
            raise ReaderError("GitHub HTTP %s" % e.code) from None
        except URLError:
            raise ReaderError("Cannot reach GitHub. Check this machine's network/proxy settings.") from None

    def json(self, endpoint):
        with self.open(endpoint) as f:
            return json.load(f)

    def asset(self, repo, asset, dest, expected=None, asset_cache=None):
        digest = asset.get("digest", "")
        if digest and digest.startswith("sha256:"):
            actual_expected = digest[7:]
            if expected and expected != actual_expected:
                raise ReaderError("Release checksum disagrees with GitHub's asset digest")
            expected = actual_expected
        if not expected or not HEX64.fullmatch(expected):
            raise ReaderError("Asset has no verifiable SHA256: " + asset["name"])
        relative(asset["name"])
        dest = inside(dest, expected + "-" + asset["name"])
        if dest.exists():
            if sha(dest) != expected:
                raise ReaderError("Corrupt cached asset: " + str(dest))
            return dest
        candidate = inside(asset_cache, asset["name"]) if asset_cache else None
        if candidate and candidate.is_file():
            if sha(candidate) != expected:
                raise ReaderError("Local asset cache failed SHA256 verification")
            with open(candidate, "rb") as src:
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as out:
                    temp = Path(out.name)
                    shutil.copyfileobj(src, out)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.open("/repos/%s/releases/assets/%s" % (repo, asset["id"]), binary=True) as src:
                with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as out:
                    temp = Path(out.name)
                    shutil.copyfileobj(src, out)
        if sha(temp) != expected:
            temp.unlink()
            raise ReaderError("Downloaded asset failed SHA256 verification")
        temp.replace(dest)
        return dest

def resolve(url):
    p = urlparse(url)
    if p.scheme != "https" or p.username or p.password:
        raise ReaderError("Use an HTTPS report URL")
    aliases = load(HERE / "references/catalog.json")["reports"]
    for item in aliases:
        if p.netloc == urlparse(item["url"]).netloc:
            public_path = p.path.rstrip("/")
            paths = [urlparse(u).path.rstrip("/") for u in item["aliases"]]
            if public_path in paths:
                return dict(item)
    m = re.fullmatch(r"/([\w.-]+/[\w.-]+)/(?:blob|tree)/(.+?)/projects/(.+)/v(\d+)(?:/report\.md|/index\.html)?/?", p.path)
    if p.netloc != "github.com" or not m:
        raise ReaderError("Unknown report URL. Use the canonical GitHub report link, or install its reader kit.")
    repo, ref, report, version = (unquote(x) for x in m.groups())
    relative(report)
    return {"url": url, "repo": repo, "ref": ref, "report": report, "version": int(version)}

def descriptor_path(d):
    return "projects/%s/v%03d" % (d["report"], d["version"])

def prepare(args):
    d = resolve(args.url)
    root = args.workspace or Path.home() / ".cache/agentledger-reader" / hashlib.sha256(args.url.encode()).hexdigest()[:16]
    root = root.expanduser().absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_path = inside(root, "reader.json")
    if receipt_path.exists():
        old = load(receipt_path)
        if any(old.get(k) != d.get(k) for k in ("repo", "report", "version")):
            raise ReaderError("Workspace already belongs to another report")
    out = inside(root, "report")
    snapshot = args.snapshot or HERE / "assets/reports" / d["report"] / ("v%03d" % d["version"])
    files = {}
    if snapshot.is_dir():
        manifest = load(snapshot / "reader-source.json")
        for key in ("repo", "report", "version", "commit"):
            if d.get(key) and d[key] != manifest.get(key):
                raise ReaderError("Snapshot provenance does not match the requested report")
        d["commit"] = manifest["commit"]
        for name, expected in manifest["files"].items():
            if name in ("comments.csv", "traces") or name.startswith("traces/"):
                continue
            src = inside(snapshot, name)
            if sha(src) != expected:
                raise ReaderError("Report snapshot failed SHA256: " + name)
            write(inside(out, name), src.read_bytes())
            files[name] = expected
    else:
        gh = GitHub()
        ref = d.get("commit", d.get("ref", "main"))
        d["commit"] = gh.json("/repos/%s/commits/%s" % (d["repo"], quote(ref, safe="")))["sha"]
        def copy_tree(path, local=""):
            entries = gh.json("/repos/%s/contents/%s?ref=%s" % (d["repo"], quote(path), d["commit"]))
            for entry in entries:
                name = (local + "/" if local else "") + entry["name"]
                relative(name)
                if entry["name"].startswith(".") or name in ("comments.csv", "traces") or name.startswith("traces/"):
                    continue
                if entry["type"] == "dir":
                    copy_tree(entry["path"], name)
                elif entry["type"] == "file":
                    b = gh.json("/repos/%s/git/blobs/%s" % (d["repo"], entry["sha"]))
                    if b.get("encoding") != "base64":
                        raise ReaderError("Unsupported report blob encoding")
                    data = base64.b64decode(b["content"])
                    blob_sha = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                    if blob_sha != entry["sha"]:
                        raise ReaderError("Git blob integrity mismatch")
                    write(inside(out, name), data)
                    files[name] = hashlib.sha256(data).hexdigest()
                else:
                    raise ReaderError("Report contains a symlink or submodule")
        copy_tree(descriptor_path(d))
    md = inside(out, "report.md")
    actual = sha(md)
    if d.get("sha256") and actual != d["sha256"]:
        raise ReaderError("Report content does not match its pinned version")
    text = md.read_text()
    releases = sorted({m.group(0) for m in RELEASE_RE.finditer(text)})
    receipt = dict(d, schema=1, files=files, releases=releases, report_path=str(md),
                   report_sha256=actual)
    save(receipt_path, receipt)
    emit({"workspace": str(root), "report": str(md), "commit": d["commit"],
          "sha256": actual, "files": sorted(files), "private_releases": releases,
          "next": "Read report/report.md, then use index/files/get only for relevant evidence."})

def workspace(args):
    root = args.workspace.expanduser().absolute()
    if not (root / "reader.json").is_file():
        raise ReaderError("Run prepare first")
    d = load(root / "reader.json")
    for name, expected in d["files"].items():
        if sha(inside(root / "report", name)) != expected:
            raise ReaderError("Prepared report changed: " + name + "; prepare it again before analysis.")
    return root, d

def batch(gh, root, url, asset_cache=None):
    match = RELEASE_RE.fullmatch(url)
    if not match:
        raise ReaderError("Invalid GitHub release URL")
    repo, tag = match.groups()
    cache = inside(root, "cache/" + hashlib.sha256(url.encode()).hexdigest()[:24])
    release_file = inside(cache, "release.json")
    if release_file.exists():
        r = load(release_file)
    else:
        r = gh.json("/repos/%s/releases/tags/%s" % (repo, quote(tag, safe="")))
        if r.get("draft"):
            raise ReaderError("This experiment batch is still a draft")
        save(release_file, r)
    assets = {a["name"]: a for a in r["assets"]}
    for needed in ("checksums.json", "batch.json"):
        if needed not in assets:
            raise ReaderError("Release is not a supported TrajFS experiment batch")
    checksum_path = gh.asset(repo, assets["checksums.json"], cache, asset_cache=asset_cache)
    sums = load(checksum_path)
    bp = gh.asset(repo, assets["batch.json"], cache, sums["batch.json"], asset_cache)
    data = load(bp)
    return {"url": url, "repo": repo, "cache": cache, "assets": assets, "checksums": sums, "batch": data}

def batches(args, gh, root, d):
    if args.release and args.release not in d["releases"]:
        raise ReaderError("The selected release is not cited by this report")
    return [batch(gh, root, u, args.asset_cache) for u in ([args.release] if args.release else d["releases"])]

def get_asset(gh, b, run, name, cache):
    if run["asset_hashes"].get(name) != b["checksums"].get(name):
        raise ReaderError("Run manifest and release checksums disagree")
    if name not in run["asset_hashes"]:
        raise ReaderError("Asset is missing from run manifest")
    return gh.asset(b["repo"], b["assets"][name], b["cache"], run["asset_hashes"][name], cache)

def select(all_batches, run_id=None, source=None):
    candidates = [(b, r) for b in all_batches for r in b["batch"]["runs"]
                  if (not run_id or r["id"] == run_id)
                  and (not source or source.startswith(r["source"].rstrip("/") + "/"))]
    if len(candidates) != 1:
        raise ReaderError("Expected one matching run, found %s. Select --release and --run explicitly." % len(candidates))
    return candidates[0]

def capture(gh, b, run, cache):
    c = load(get_asset(gh, b, run, run["capture_asset"], cache))
    if c["fingerprint"] != run["fingerprint"] or c["source"] != run["source"] or c["run_id"] != run["id"]:
        raise ReaderError("Capture identity does not match the batch")
    return c

def unpack(parts, dest):
    if dest.exists():
        if not (dest / "trace.trajstore/MANIFEST.json").is_file():
            raise ReaderError("Incomplete cached TrajFS store")
        return dest / "trace.trajstore"
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        tmp = Path(tmp)
        with tempfile.TemporaryFile() as joined:
            for part in parts:
                with open(part, "rb") as f:
                    shutil.copyfileobj(f, joined)
            joined.seek(0)
            seen = set()
            with tarfile.open(fileobj=joined, mode="r|") as tf:
                for member in tf:
                    name = member.name.rstrip("/")
                    path = relative(name)
                    if path.parts[0] != "trace.trajstore" or name in seen or not (member.isfile() or member.isdir()):
                        raise ReaderError("Unsafe or duplicate TrajFS tar member")
                    seen.add(name)
                    target = inside(tmp, name)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with tf.extractfile(member) as src, target.open("xb") as out:
                            shutil.copyfileobj(src, out)
                        target.chmod(0o600)
        if not (tmp / "trace.trajstore/MANIFEST.json").is_file():
            raise ReaderError("Archive has no TrajFS manifest")
        ready = tmp / "ready"
        ready.mkdir()
        (tmp / "trace.trajstore").rename(ready / "trace.trajstore")
        ready.rename(dest)
    return dest / "trace.trajstore"

def query(args):
    root, d = workspace(args)
    gh = GitHub()
    bs = batches(args, gh, root, d)
    if args.command == "index":
        emit({"runs": [{"release": b["url"], "id": r["id"], "source": r["source"],
                        "files": r["files"], "raw_bytes": r["raw_bytes"]}
                       for b in bs for r in b["batch"]["runs"]]})
        return
    source, line = args.source, None
    if source:
        m = re.fullmatch(r"(.+):(\d+)", source)
        if m:
            source, line = m.group(1), int(m.group(2))
    b, run = select(bs, args.run, source)
    c = capture(gh, b, run, args.asset_cache)
    if args.command == "files":
        emit({"run": run["id"], "release": b["url"], "files":
              [f for f in c["files"] if fnmatch.fnmatchcase(f["path"], args.glob)]})
        return
    path = source[len(run["source"].rstrip("/")) + 1:] if source else args.path
    if not path:
        raise ReaderError("Provide --source ORIGINAL_PATH[:LINE], or --run with --path")
    relative(path)
    entries = [f for f in c["files"] if f["path"] == path]
    if len(entries) != 1:
        raise ReaderError("File is not in this run's captured evidence")
    entry = entries[0]
    dest = inside(root, "evidence/" + run["id"] + "/" + path)
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not dest.exists():
        with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as out:
            temp = Path(out.name)
            if args.raw_root:
                with inside(args.raw_root, path).open("rb") as src:
                    shutil.copyfileobj(src, out)
            else:
                traj = shutil.which("traj")
                if not traj:
                    temp.unlink()
                    raise ReaderError("Reading packed traces needs TrajFS (traj) on Linux. "
                                      "Use an authorized Linux workspace, or pass --raw-root for an already restored run.")
                parts = [get_asset(gh, b, run, name, args.asset_cache) for name in run["parts"]]
                key = hashlib.sha256((b["url"] + run["fingerprint"]).encode()).hexdigest()[:32]
                store = unpack(parts, inside(root, "stores/" + key))
                r = subprocess.run([traj, "-S", str(store), "cat", path], stdout=out, stderr=subprocess.PIPE)
                if r.returncode:
                    temp.unlink()
                    raise ReaderError("TrajFS could not read the requested file; no verified evidence was returned.")
        if sha(temp) != entry["sha256"] or temp.stat().st_size != entry["size"]:
            temp.unlink()
            raise ReaderError("Restored trace failed the original file SHA256/size check")
        temp.replace(dest)
    if sha(dest) != entry["sha256"] or dest.stat().st_size != entry["size"]:
        raise ReaderError("Cached trace failed SHA256/size verification")
    receipt = {"file": str(dest), "original_source": run["source"].rstrip("/") + "/" + path,
               "line": line, "sha256": entry["sha256"], "size": entry["size"],
               "release": b["url"], "run": run["id"], "capture_fingerprint": c["fingerprint"],
               "verified": "SHA256 and size match the captured original; archive assets verified with release checksums."}
    save(inside(root, "receipts/" + hashlib.sha256(receipt["original_source"].encode()).hexdigest() + ".json"), receipt)
    emit(receipt)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="Pin and fetch a report, without fetching raw traces")
    p.add_argument("url")
    p.add_argument("--workspace", type=Path)
    p.add_argument("--snapshot", type=Path, help="Use an exported report folder with reader-source.json")
    for name in ("index", "files", "get"):
        p = sub.add_parser(name)
        p.add_argument("--workspace", required=True, type=Path)
        p.add_argument("--release")
        p.add_argument("--asset-cache", type=Path, help="Optional directory of already downloaded release assets")
        p.add_argument("--run")
        p.add_argument("--source")
        if name == "files":
            p.add_argument("--glob", default="*")
        if name == "get":
            p.add_argument("--path")
            p.add_argument("--raw-root", type=Path, help="Original/restored root of this run; bytes are still verified")
    args = parser.parse_args()
    try:
        prepare(args) if args.command == "prepare" else query(args)
    except (ReaderError, OSError, ValueError, KeyError, tarfile.TarError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
