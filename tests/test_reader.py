import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

MODULE = Path(__file__).resolve().parents[1] / "skills/agentledger-reader/scripts/reader.py"
spec = importlib.util.spec_from_file_location("ledger_reader", MODULE)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)

class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        folder = self.root / "snapshot"
        folder.mkdir()
        data = b"# Example report\nA fixed cohort.\n"
        (folder / "report.md").write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        descriptor = {"url": "https://example.test/report", "repo": "reader/reports",
                      "report": "project/topic", "version": 3, "commit": "a" * 40, "sha256": digest}
        manifest = dict(descriptor, files={"report.md": digest})
        (folder / "reader-source.json").write_text(json.dumps(manifest))
        return folder, descriptor

    def test_snapshot_preparation_needs_no_publisher_credentials_or_network(self):
        folder, d = self.snapshot()
        args = SimpleNamespace(url=d["url"], workspace=self.root / "analysis", snapshot=folder)
        with patch.object(reader, "resolve", return_value=d), patch.object(reader, "GitHub") as gh, patch.object(reader, "emit"):
            reader.prepare(args)
        gh.assert_not_called()
        receipt = reader.load(args.workspace / "reader.json")
        self.assertEqual(receipt["commit"], "a" * 40)
        self.assertEqual(reader.sha(args.workspace / "report/report.md"), d["sha256"])

    def test_changed_report_is_rejected_before_trace_analysis(self):
        folder, d = self.snapshot()
        args = SimpleNamespace(url=d["url"], workspace=self.root / "analysis", snapshot=folder)
        with patch.object(reader, "resolve", return_value=d), patch.object(reader, "emit"):
            reader.prepare(args)
        (args.workspace / "report/report.md").write_text("# Changed denominator")
        with self.assertRaises(reader.ReaderError):
            reader.workspace(args)

    def test_corrupt_snapshot_does_not_pass_verification(self):
        folder, d = self.snapshot()
        (folder / "report.md").write_text("Changed content")
        args = SimpleNamespace(url=d["url"], workspace=self.root / "analysis", snapshot=folder)
        with patch.object(reader, "resolve", return_value=d), self.assertRaises(reader.ReaderError):
            reader.prepare(args)

    def test_github_url_preserves_ref_report_and_version(self):
        d = reader.resolve("https://github.com/a/b/blob/feature/reader/projects/project/nested/topic/v007/report.md#results")
        self.assertEqual((d["repo"], d["ref"], d["report"], d["version"]),
                         ("a/b", "feature/reader", "project/nested/topic", 7))

    def test_hosted_alias_is_pinned(self):
        d = reader.resolve("https://ma-infra-report-20261103.seanjiang2001.chatgpt.site/resource-tools/#cases")
        self.assertEqual(d["version"], 3)
        self.assertEqual(len(d["commit"]), 40)
        self.assertEqual(len(d["sha256"]), 64)

    def test_unknown_url_does_not_fetch_untrusted_server(self):
        with self.assertRaises(reader.ReaderError):
            reader.resolve("https://attacker.example/report")
        with self.assertRaises(reader.ReaderError):
            reader.resolve("https://user:secret@github.com/a/b/blob/main/projects/p/t/v001/report.md")

    def test_private_auth_error_is_actionable_without_echoing_token(self):
        gh = reader.GitHub()
        gh.token = "READER-SECRET"
        with patch.object(gh.opener, "open", side_effect=HTTPError("https://api.github.com/", 404, "No", {}, None)):
            with self.assertRaises(reader.ReaderError) as ctx:
                gh.json("/repos/private/traces/releases")
        self.assertIn("private/traces", str(ctx.exception))
        self.assertIn("YOUR", str(ctx.exception))
        self.assertNotIn("READER-SECRET", str(ctx.exception))

    def test_redirect_never_forwards_github_token_to_cdn(self):
        req = Request("https://api.github.com/repos/a/b/releases/assets/1",
                      headers={"Authorization": "Bearer SECRET"})
        nxt = reader.SafeRedirect().redirect_request(req, None, 302, "", {},
                                                     "https://release-assets.githubusercontent.com/file?signature=abc")
        self.assertIsNone(nxt.get_header("Authorization"))
        same = reader.SafeRedirect().redirect_request(req, None, 302, "", {},
                                                      "https://api.github.com/another")
        self.assertEqual(same.get_header("Authorization"), "Bearer SECRET")
        for target in ["http://release-assets.githubusercontent.com/file", "https://attacker.example/file"]:
            with self.assertRaises(reader.ReaderError):
                reader.SafeRedirect().redirect_request(req, None, 302, "", {}, target)

    def test_corrupt_asset_is_not_saved(self):
        gh = reader.GitHub()
        asset = {"name": "batch.json", "id": 1, "digest": "sha256:" + "0" * 64}
        with patch.object(gh, "open", return_value=io.BytesIO(b"wrong")):
            with self.assertRaises(reader.ReaderError):
                gh.asset("a/b", asset, self.root / "assets")
        self.assertFalse(any(p.name.endswith("batch.json") for p in self.root.rglob("*")))

    def test_paths_cannot_escape_or_follow_symlinks(self):
        for name in ["../secret", "/secret", "a/../../secret", "a\\secret", "a//b"]:
            with self.assertRaises(reader.ReaderError):
                reader.inside(self.root, name)
        (self.root / "link").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(reader.ReaderError):
            reader.inside(self.root, "link/secret")

    def archive(self, members):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as tf:
            for member, content in members:
                tf.addfile(member, io.BytesIO(content) if content is not None else None)
        raw = data.getvalue()
        parts = []
        for i, chunk in enumerate([raw[:137], raw[137:]]):
            part = self.root / ("part%d" % i)
            part.write_bytes(chunk)
            parts.append(part)
        return parts

    def test_split_run_archive_restores_without_other_runs(self):
        m = tarfile.TarInfo("trace.trajstore/MANIFEST.json")
        m.size = 2
        result = reader.unpack(self.archive([(m, b"{}")]), self.root / "store")
        self.assertEqual((result / "MANIFEST.json").read_bytes(), b"{}")

    def test_tar_links_traversal_and_duplicate_members_are_rejected(self):
        for name, kind in [("../escape", tarfile.REGTYPE),
                           ("trace.trajstore/link", tarfile.SYMTYPE),
                           ("trace.trajstore/link", tarfile.LNKTYPE)]:
            with self.subTest(name=name, kind=kind):
                m = tarfile.TarInfo(name)
                m.type = kind
                m.linkname = "/etc/passwd"
                with self.assertRaises(reader.ReaderError):
                    reader.unpack(self.archive([(m, b"" if kind == tarfile.REGTYPE else None)]),
                                  self.root / "unsafe")
        m = tarfile.TarInfo("trace.trajstore/MANIFEST.json")
        m.size = 2
        with self.assertRaises(reader.ReaderError):
            reader.unpack(self.archive([(m, b"{}"), (m, b"{}")]), self.root / "duplicate")
        self.assertFalse((self.root / "escape").exists())

    def test_ambiguous_run_selection_needs_explicit_release(self):
        r = {"id": "run", "source": "/original/run"}
        b = {"batch": {"runs": [r]}}
        with self.assertRaises(reader.ReaderError):
            reader.select([b, b], source="/original/run/session.jsonl")

    def test_existing_raw_file_is_verified_and_keeps_original_line_numbers(self):
        root = self.root / "analysis"
        root.mkdir()
        raw = self.root / "raw"
        (raw / "sessions").mkdir(parents=True)
        data = b'{"step":1}\n{"step":2}\n'
        (raw / "sessions/log.jsonl").write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        b = {"url": "https://github.com/private/traces/releases/tag/batch"}
        run = {"id": "example-run", "source": "/original/run"}
        cap = {"fingerprint": "f" * 64, "files": [
            {"path": "sessions/log.jsonl", "sha256": digest, "size": len(data)}]}
        args = SimpleNamespace(command="get", workspace=root, run=None,
                               source="/original/run/sessions/log.jsonl:2", release=b["url"],
                               asset_cache=None, raw_root=raw, path=None)
        with patch.object(reader, "workspace", return_value=(root, {})), \
             patch.object(reader, "GitHub"), patch.object(reader, "batches", return_value=[]), \
             patch.object(reader, "select", return_value=(b, run)), \
             patch.object(reader, "capture", return_value=cap), patch.object(reader, "emit") as emit:
            reader.query(args)
        receipt = emit.call_args[0][0]
        self.assertEqual(receipt["line"], 2)
        self.assertEqual(Path(receipt["file"]).read_bytes(), data)
        self.assertEqual(receipt["sha256"], digest)

if __name__ == "__main__":
    unittest.main()
