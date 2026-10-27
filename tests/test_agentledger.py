import contextlib
import io
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agentledger import comments as cm
from agentledger.build import build
from agentledger.cli import main
from agentledger.markdown import render
from agentledger.modules import split_modules
from agentledger.sanitize import sanitize
from agentledger.server import serve
from agentledger.store import LedgerError, Store, init_store

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo"


def run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(list(argv))
    return code, out.getvalue()


class MarkdownTests(unittest.TestCase):
    def test_title_front_matter_and_attrs(self):
        meta, title, html, heads = render("---\nsummary: s\ntags: [a, b]\n---\n# My title\n\nIntro.\n\n## Results {#res status=needs-review}\n\nx\n")
        self.assertEqual(title, "My title")
        self.assertEqual(meta["tags"], ["a", "b"])
        self.assertIn('<h2 id="res" data-status="needs-review">Results</h2>', html)
        self.assertNotIn("My title", html)

    def test_sections_written_with_h1_are_demoted(self):
        _, _, html, _ = render("# Title\n\n# One\n\n## Sub\n")
        self.assertIn('<h2 id="one">One</h2>', html)
        self.assertIn('<h3 id="sub">Sub</h3>', html)

    def test_tables_lists_code(self):
        _, _, html, _ = render("| a | b |\n|:--|--:|\n| `x|y` | 2 |\n\n1. one\n  - nested\n2. two\n\n```py\nif a < b: pass\n```\n")
        self.assertIn('<td style="text-align:left"><code>x|y</code></td>', html)
        self.assertIn("<ol>\n<li>one\n<ul>\n<li>nested</li>\n</ul></li>\n<li>two</li>\n</ol>", html)
        self.assertIn('<pre><code class="language-py">if a &lt; b: pass\n</code></pre>', html)

    def test_generics_in_prose_are_text_not_tags(self):
        _, _, html, _ = render("Returns Option<T> and <kbd>Ctrl</kbd>.")
        self.assertIn("Option&lt;T&gt;", html)
        self.assertIn("<kbd>Ctrl</kbd>", html)


class SanitizeTests(unittest.TestCase):
    def test_strips_scripts_handlers_and_bad_urls(self):
        out = sanitize('<p onclick="x()">a<script>alert(1)</script><a href="javascript:alert(1)">b</a>'
                       '<img src="x" onerror="y"><iframe src="z"></iframe><div id="al-panel" style="position:fixed;color:red"></div></p>')
        self.assertNotIn("script", out)
        self.assertNotIn("onclick", out)
        self.assertNotIn("onerror", out)
        self.assertNotIn("javascript", out)
        self.assertNotIn("iframe", out)
        self.assertNotIn("position", out)
        self.assertIn('id="x-al-panel"', out)
        self.assertIn("color: red", out)

    def test_keeps_svg_and_details(self):
        out = sanitize('<details open><summary>s</summary><svg viewBox="0 0 1 1"><path d="M0 0"/></svg></details>')
        self.assertIn("<details open>", out)
        self.assertIn('<path d="M0 0"/>', out)


class ModuleTests(unittest.TestCase):
    def test_split_on_top_level_h2_only(self):
        _, _, html, _ = render("# T\n\nintro\n\n## A\n\nx\n\n<div>\n<h2>nested</h2>\n</div>\n\n## B {#bee}\n\ny\n")
        mods = split_modules(html)
        self.assertEqual([m.id for m in mods], ["intro", "a", "bee"])
        self.assertIn("nested", mods[1].html)
        self.assertNotIn('id="bee"', mods[2].html)


class CommentTests(unittest.TestCase):
    def test_merge_newest_wins_and_roundtrip(self):
        a = {"id": "c1", "report": "p/r", "version": "v001", "body": "old", "created": "2026-01-01T00:00:00Z"}
        b = dict(a, body="new", updated="2026-01-02T00:00:00Z")
        rows, changed = cm.merge([a], [b])
        self.assertEqual(changed, 1)
        self.assertEqual(rows[0]["body"], "new")
        self.assertEqual(rows[0]["version"], "1")
        self.assertEqual(rows[0]["status"], "open")
        rows2, changed2 = cm.merge(rows, [a])
        self.assertEqual(changed2, 0)
        self.assertEqual(cm.parse(cm.dumps(rows2)), rows2)

    def test_csv_quotes_newlines_and_commas(self):
        row = cm.normalize({"id": "c1", "body": 'line, one\n"two"', "created": "t"})
        self.assertEqual(cm.parse(cm.dumps([row]))[0]["body"], 'line, one\n"two"')


class StoreFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = init_store(self.tmp / "store", name="Test Ledger", repo="o/r")
        self.store = Store(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_publish_comment_revise_resolve(self):
        s = self.store
        v1 = s.publish("demo/benchmark", DEMO / "benchmark-v1.md", author="agent")
        self.assertEqual(v1.path, self.root / "projects/demo/benchmark/v001")
        self.assertTrue((self.root / "projects/demo/project.json").exists())
        self.assertEqual(json.loads((self.root / "projects/demo/benchmark/report.json").read_text())["about"],
                         "Benchmark comparison — harness vs. paper trajectories")
        s.save_rows(cm.parse((DEMO / "comments-v1.csv").read_text()))
        reply = s.reply("c_demo1", "looking", author="agent")
        self.assertEqual(reply["version"], "1")
        v2 = s.publish("demo/benchmark", DEMO / "benchmark-v2.md", resolves=["c_demo1"], changes="explained")
        self.assertEqual(v2.number, 2)
        root = s.find_comment("c_demo1")
        self.assertEqual((root["status"], root["resolved_in"]), ("resolved", "2"))
        s.publish("demo/benchmark/dna", DEMO / "dna-primer.md")
        written = build(s)
        self.assertTrue((self.root / "index.html").exists())
        page = (self.root / "projects/demo/benchmark/v002/index.html").read_text()
        self.assertIn('data-state="changed"', page)
        self.assertIn('data-state="new"', page)
        self.assertIn("What changed in v2", page)
        self.assertIn("../dna/v001/index.html", page)
        self.assertIn('href="../v001/index.html"', page)
        self.assertIn("v002/index.html", (self.root / "projects/demo/benchmark/index.html").read_text())
        manifest = json.loads((self.root / "manifest.json").read_text())
        self.assertEqual(manifest["projects"][0]["reports"][0]["children"][0]["id"], "demo/benchmark/dna")
        self.assertTrue(written)
        self.assertEqual(build(s), [], "rebuilding an unchanged store must not rewrite files")

    def test_bad_ids_are_rejected(self):
        for bad in ("demo", "Demo/X", "demo/v001", "demo/assets", "../x/y"):
            with self.assertRaises(LedgerError, msg=bad):
                self.store.publish(bad, DEMO / "dna-primer.md")

    def test_assets_carry_forward(self):
        src = self.tmp / "src"
        (src / "img").mkdir(parents=True)
        (src / "a.md").write_text("# T\n\n![x](img/p.png)\n")
        (src / "img" / "p.png").write_bytes(b"png")
        self.store.publish("p/r", src)
        self.store.publish("p/r", DEMO / "dna-primer.md")
        self.assertTrue((self.root / "projects/p/r/v002/img/p.png").exists())


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "store"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_cli_round_trip(self):
        s = str(self.root)
        self.assertEqual(run("init", s, "--name", "CLI")[0], 0)
        self.assertEqual(run("--store", s, "publish", "demo/benchmark", str(DEMO / "benchmark-v1.md"), "--author", "a")[0], 0)
        self.assertEqual(run("--store", s, "comments", "import", str(DEMO / "comments-v1.csv"))[0], 0)
        code, out = run("--store", s, "comments", "list")
        self.assertIn("c_demo1", out)
        self.assertIn("report.md:", out)
        self.assertEqual(run("--store", s, "comments", "reply", "c_demo1", "done", "--resolve")[0], 0)
        code, out = run("--store", s, "comments", "list", "--status", "waiting", "--format", "json")
        self.assertEqual({t["root"]["id"] for t in json.loads(out)}, {"c_demo2", "c_demo3"})
        code, out = run("--store", s, "check")
        self.assertEqual(code, 0, out)
        (self.root / "projects/demo/notes.md").write_text("stray")
        code, out = run("--store", s, "check")
        self.assertIn("stray file", out)
        draft = self.tmp / "d.md"
        self.assertEqual(run("--store", s, "draft", "demo/benchmark", "-o", str(draft))[0], 0)
        self.assertTrue(draft.read_text().startswith("---"))
        code, out = run("--store", s, "tree")
        self.assertIn("benchmark", out)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        root = init_store(self.tmp / "store", name="Srv")
        self.store = Store(root)
        self.store.publish("p/r", DEMO / "dna-primer.md")
        build(self.store)
        self.httpd = serve(self.store, port=0, quiet=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp)

    def post(self, rows, headers):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/__agentledger/comments", data=json.dumps({"rows": rows}).encode(),
                                     headers={"Content-Type": "application/json", **headers}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_save_requires_header_and_same_origin(self):
        row = {"id": "c_x", "report": "p/r", "version": "1", "body": "hi", "author": "me", "created": "2026-01-01T00:00:00Z"}
        self.assertEqual(self.post([row], {})[0], 403)
        self.assertEqual(self.post([row], {"X-AgentLedger": "1", "Origin": "http://evil.example"})[0], 403)
        status, body = self.post([row], {"X-AgentLedger": "1", "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["changed"], 1)
        self.assertIn("c_x", (self.store.root / "projects/p/r/v001/comments.csv").read_text())
        self.assertIn("c_x", (self.store.root / "projects/p/r/v001/index.html").read_text())
        status, body = self.post([dict(row, report="p/nope")], {"X-AgentLedger": "1"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
