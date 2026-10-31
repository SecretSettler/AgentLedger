import contextlib
import gzip
import io
import json
import os
import shutil
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agentledger import comments as cm
from agentledger.ask import ClaudeStream, resolve_backend
from agentledger.build import build
from agentledger.cli import main
from agentledger.markdown import render
from agentledger.modules import split_modules
from agentledger.sanitize import sanitize
from agentledger.server import serve
from agentledger.traces import detect_session
from agentledger.store import LedgerError, Store, init_store

DEMO = Path(__file__).resolve().parent.parent / "agentledger" / "demo"


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


def sh(cwd, *argv):
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True)


class AutomationTests(unittest.TestCase):
    """setup (user-level Claude Code install), sync (git round trip), and recorded provenance."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = {k: os.environ.get(k) for k in ("AGENTLEDGER_STORE", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")}
        os.environ.pop("AGENTLEDGER_STORE", None)
        for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
            os.environ[k] = "t"
        for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
            os.environ[k] = "t@example.com"

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp)

    def test_setup_remembers_store_and_installs_skill(self):
        store = init_store(self.tmp / "store", name="S")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        skills = self.tmp / "skills"
        try:
            for _ in range(2):
                self.assertEqual(run("setup", str(store), "--skills-dir", str(skills))[0], 0)
            cwd = os.getcwd()
            os.chdir(self.tmp)  # outside any store: the remembered one is used
            try:
                code, out = run("root")
            finally:
                os.chdir(cwd)
        finally:
            os.environ.pop("XDG_CONFIG_HOME", None)
        self.assertEqual(out.strip(), str(store))
        for agent in ("claude-code", "codex"):
            skill = (skills / agent / "agentledger" / "SKILL.md").read_text()
            self.assertTrue(skill.startswith("---\nname: agentledger\ndescription: "))

    def test_repo_skill_matches_packaged_skill(self):
        top = Path(__file__).resolve().parent.parent
        self.assertEqual((top / "skills/agentledger/SKILL.md").read_text(), (top / "agentledger/assets/SKILL.md").read_text())
        self.assertEqual((top / "docs/logo.svg").read_text(), (top / "agentledger/assets/logo.svg").read_text())

    def test_publish_records_context_and_sync_pushes(self):
        remote = self.tmp / "remote.git"
        sh(self.tmp, "git", "init", "-q", "--bare", str(remote))
        store = init_store(self.tmp / "store", name="S")
        sh(store, "git", "init", "-q", "-b", "main")
        sh(store, "git", "remote", "add", "origin", str(remote))
        work = self.tmp / "work-repo"
        work.mkdir()
        sh(work, "git", "init", "-q")
        (work / "a.txt").write_text("a")
        sh(work, "git", "add", "-A")
        sh(work, "git", "commit", "-qm", "init")
        os.environ["AGENTLEDGER_STORE"] = str(store)
        cwd = os.getcwd()
        try:
            os.chdir(work)
            code, out = run("publish", "work-repo/findings", str(DEMO / "dna-primer.md"))
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, out)
        meta = json.loads((store / "projects/work-repo/findings/v001/meta.json").read_text())
        self.assertEqual(meta["context"]["repo"], "work-repo")
        self.assertFalse(meta["context"]["dirty"])
        self.assertIn("work-repo@", (store / "projects/work-repo/findings/v001/index.html").read_text())
        code, out = run("sync", "-m", "first report")
        self.assertEqual(code, 0, out)
        log = subprocess.run(["git", "--git-dir", str(remote), "log", "--oneline", "main"], capture_output=True, text=True).stdout
        self.assertIn("first report", log)
        code, out = run("sync")
        self.assertIn("nothing to commit", out)


FAKE_AGENT = """
import sys
prompt = sys.stdin.read()
trace = next((l for l in prompt.splitlines() if l.startswith("- ") and ".jsonl" in l), "no trace")
quote = next((l for l in prompt.splitlines() if l.startswith("> ")), "no quote")
last = [l for l in prompt.splitlines() if l.startswith("- ") and "(human)" in l][-1]
print("FAKE ANSWER | " + quote + " | " + trace.split()[1].rsplit("/", 1)[-1] + " | " + last)
"""


class AskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = init_store(self.tmp / "store", name="Ask")
        self.store = Store(self.root)
        self.trace = self.tmp / "session.jsonl"
        self.trace.write_text('{"type":"user","message":"how many tokens?"}\n' * 200)
        self.store.publish("demo/benchmark", DEMO / "benchmark-v1.md", traces=[self.trace])
        self.store.save_rows(cm.parse((DEMO / "comments-v1.csv").read_text()))
        build(self.store)
        agent = self.tmp / "fake_agent.py"
        agent.write_text(FAKE_AGENT)
        self.cmd = f"{sys.executable} {agent}"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_trace_stored_compressed_and_not_carried(self):
        v1 = self.root / "projects/demo/benchmark/v001"
        self.assertTrue((v1 / "traces/session.jsonl.gz").exists())
        meta = json.loads((v1 / "meta.json").read_text())
        self.assertEqual(meta["traces"][0]["bytes"], self.trace.stat().st_size)
        self.store.publish("demo/benchmark", DEMO / "benchmark-v2.md")
        self.assertFalse((self.root / "projects/demo/benchmark/v002/traces").exists())
        build(self.store)
        self.assertIn("traces/session.jsonl.gz", (self.root / "projects/demo/benchmark/v002/index.html").read_text())

    def test_print_prompt_and_cli_ask(self):
        code, out = run("--store", str(self.root), "ask", "c_demo1", "--print-prompt")
        self.assertEqual(code, 0)
        self.assertIn("> Harness out tok", out)
        self.assertIn(".cache/projects/demo/benchmark/v001/traces/session.jsonl", out)
        self.assertIn("alice (human): look at this column", out)
        code, out = run("--store", str(self.root), "ask", "c_demo1", "--cmd", self.cmd)
        self.assertEqual(code, 0, out)
        self.assertIn("FAKE ANSWER | > Harness out tok | session.jsonl", out)
        reply = [r for r in Store(self.root).comments() if r["reply_to"] == "c_demo1"][-1]
        self.assertEqual((reply["role"], reply["version"]), ("agent", "1"))

    def test_claude_stream_parsing(self):
        st = ClaudeStream()
        notes = [st.feed(json.dumps(e)) for e in (
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Grep", "input": {"pattern": "out tok", "path": "/x/s.jsonl"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "draft"}]}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "final answer"},
        )]
        self.assertEqual(notes[1], "Grep 'out tok' in s.jsonl")
        self.assertEqual(st.final(), "final answer")
        bad = ClaudeStream()
        bad.feed(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}))
        self.assertTrue(bad.error)

    def test_detect_claude_session(self):
        work = self.tmp / "work dir"
        work.mkdir()
        proj = self.tmp / "claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(work.resolve()))
        proj.mkdir(parents=True)
        (proj / "old.jsonl").write_text("{}")
        os.utime(proj / "old.jsonl", (time.time() - 600, time.time() - 600))
        (proj / "current.jsonl").write_text("{}")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "claude")
        os.environ["CODEX_HOME"] = str(self.tmp / "no-codex")
        try:
            self.assertEqual(detect_session(work).name, "current.jsonl")
            self.assertIsNone(detect_session(self.tmp))
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR")
            os.environ.pop("CODEX_HOME")

    def test_server_ask_round_trip(self):
        backend = resolve_backend(cmd=self.cmd)
        httpd = serve(self.store, port=0, quiet=True, backend=backend)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}/__agentledger"
        hdr = {"Content-Type": "application/json", "X-AgentLedger": "1"}
        try:
            with urllib.request.urlopen(f"{base}/ping") as r:
                self.assertEqual(json.loads(r.read())["ask"], "fake_agent")
            q = {"id": "c_q1", "report": "demo/benchmark", "version": "1", "module": "results", "kind": "question",
                 "quote": "Recorded env calls", "body": "Where do these come from?", "author": "carol", "role": "human",
                 "created": "2026-10-27T00:00:00Z"}
            req = urllib.request.Request(f"{base}/ask", data=json.dumps({"rows": [q], "thread": "c_q1"}).encode(), headers=hdr, method="POST")
            with urllib.request.urlopen(req) as r:
                job = json.loads(r.read())["job"]
            for _ in range(100):
                with urllib.request.urlopen(urllib.request.Request(f"{base}/ask/{job}", headers=hdr)) as r:
                    st = json.loads(r.read())
                if st["status"] in ("done", "error"):
                    break
                time.sleep(0.05)
            self.assertEqual(st["status"], "done", st)
            self.assertIn("> Recorded env calls", st["reply"]["body"])
            self.assertIn("carol (human): Where do these come from?", st["reply"]["body"])
            csv_text = (self.root / "projects/demo/benchmark/v001/comments.csv").read_text()
            self.assertIn("c_q1", csv_text)
            self.assertIn(st["reply"]["id"], csv_text)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(f"{base}/ask/{job}")  # no X-AgentLedger header
        finally:
            httpd.shutdown()
            httpd.server_close()


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_ARGV_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\\n")
prompt = sys.stdin.read()
if "--resume" in argv and os.environ.get("FAKE_FAIL_RESUME"):
    print("Error: session is busy", file=sys.stderr)
    sys.exit(1)
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else argv[argv.index("--resume") + 1]
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}))
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x/report.md"}}]}}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
                  "result": "fake claude answer" + (" (resumed)" if "--resume" in argv else "")}))
"""


class SessionAskTests(unittest.TestCase):
    """Ask continues the session that wrote the report: forked once per thread, then continued."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = {k: os.environ.get(k) for k in ("PATH", "CLAUDE_CONFIG_DIR", "FAKE_ARGV_LOG", "FAKE_FAIL_RESUME")}
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "claude").write_text(FAKE_CLAUDE)
        (bin_dir / "claude").chmod(0o755)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "claude")
        os.environ["FAKE_ARGV_LOG"] = str(self.tmp / "argv.log")
        os.environ.pop("FAKE_FAIL_RESUME", None)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.sid = "11111111-2222-3333-4444-555555555555"
        tdir = self.tmp / "claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.repo))
        tdir.mkdir(parents=True)
        (tdir / f"{self.sid}.jsonl").write_text(json.dumps({"sessionId": self.sid, "cwd": str(self.repo)}) + "\n")
        self.root = init_store(self.tmp / "store", name="S")
        self.store = Store(self.root)
        self.store.publish("p/r", DEMO / "dna-primer.md", session={"agent": "claude", "id": self.sid, "cwd": str(self.repo)})
        self.cid = self.store.add_comment("p/r", "why?", module="outcome", author="carol")["id"]

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp)

    def argv_log(self):
        return [json.loads(ln) for ln in (self.tmp / "argv.log").read_text().splitlines()]

    def test_session_info_from_transcript(self):
        from agentledger.traces import session_info
        t = next((self.tmp / "claude" / "projects").rglob("*.jsonl"))
        self.assertEqual(session_info(t), {"agent": "claude", "id": self.sid, "cwd": str(self.repo)})

    def test_fork_then_continue(self):
        from agentledger.ask import answer_thread, thread_session
        backend = resolve_backend("auto")
        row = answer_thread(self.store, self.cid, backend)
        self.assertEqual((row["body"], row["author"]), ("fake claude answer (resumed)", "claude"))
        first = self.argv_log()[-1]
        self.assertEqual(first[first.index("--resume") + 1], self.sid)
        self.assertIn("--fork-session", first)
        fork = first[first.index("--session-id") + 1]
        self.assertNotEqual(fork, self.sid)
        self.assertIn("Read", first)
        self.assertIn("Bash", first[first.index("--disallowedTools"):])
        self.assertEqual(thread_session(self.store, self.cid)["id"], fork)
        self.store.reply(self.cid, "and then?", author="carol", role="human")
        answer_thread(self.store, self.cid, backend)
        second = self.argv_log()[-1]
        self.assertEqual(second[second.index("--resume") + 1], fork)
        self.assertNotIn("--fork-session", second)
        self.assertNotIn("--session-id", second)

    def test_falls_back_to_fresh_session(self):
        from agentledger.ask import answer_thread
        os.environ["FAKE_FAIL_RESUME"] = "1"
        progress = []
        row = answer_thread(self.store, self.cid, resolve_backend("auto"), on_progress=progress.append)
        self.assertEqual(row["body"], "fake claude answer")
        self.assertNotIn("--resume", self.argv_log()[-1])
        self.assertTrue(any("could not resume" in p for p in progress))

    def test_shared_server_keeps_ask_local(self):
        backend = resolve_backend("auto")
        httpd = serve(self.store, port=0, quiet=True, backend=backend, share=True)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def call(path, host, data=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST" if data else "GET",
                                         headers={"Host": host, "X-AgentLedger": "1", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        try:
            status, ping = call("/__agentledger/ping", "192.168.1.20")
            self.assertEqual((status, ping["shared"], ping["ask"]), (200, True, None))
            body = json.dumps({"thread": self.cid}).encode()
            self.assertEqual(call("/__agentledger/ask", "192.168.1.20", body)[0], 403)
            self.assertEqual(call("/__agentledger/ping", "evil.example")[0], 403)  # DNS rebinding
            row = {"id": "c_lan", "report": "p/r", "version": "1", "body": "from LAN", "author": "dan", "created": "2026-10-31T00:00:00Z"}
            self.assertEqual(call("/__agentledger/comments", "192.168.1.20", json.dumps({"rows": [row]}).encode())[0], 200)
            self.assertEqual(call("/__agentledger/ping", "localhost")[1]["ask"], "claude")
        finally:
            httpd.shutdown()
            httpd.server_close()


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.saved
        shutil.rmtree(self.tmp)

    def test_setup_creates_a_store_and_status_reports_it(self):
        target = self.tmp / "ledger"
        code, out = run("setup", str(target), "--skills-dir", str(self.tmp / "skills"))
        self.assertEqual(code, 0, out)
        self.assertTrue((target / "agentledger.json").exists())
        self.assertIn("--private", out)
        code, out = run()
        self.assertIn(str(target), out)
        busy = self.tmp / "busy"
        busy.mkdir()
        (busy / "x.txt").write_text("x")
        self.assertEqual(run("setup", str(busy), "--skills-dir", str(self.tmp / "skills"))[0], 2)

    def test_demo_builds_and_refuses_foreign_dirs(self):
        code, out = run("demo", "--dir", str(self.tmp / "demo"), "--no-serve")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.tmp / "demo/projects/demo/benchmark/v002/index.html").exists())
        self.assertEqual(run("demo", "--dir", str(self.tmp / "demo"), "--no-serve")[0], 0)  # rebuilds its own
        other = self.tmp / "mine"
        other.mkdir()
        (other / "keep.txt").write_text("keep")
        with self.assertRaises(SystemExit):
            run("demo", "--dir", str(other), "--no-serve")
        self.assertTrue((other / "keep.txt").exists())


class PrivacyTests(unittest.TestCase):
    """Nothing machine-specific (checkout paths, home directory) ends up in committed store files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home" / "alice"
        self.home.mkdir(parents=True)
        self.saved = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.root = init_store(self.tmp / "store", name="P")
        self.store = Store(self.root)

    def tearDown(self):
        os.environ["HOME"] = self.saved
        shutil.rmtree(self.tmp)

    def committed_text(self) -> str:
        return "\n".join(f.read_text(errors="replace") for f in (self.root / "projects").rglob("*") if f.is_file() and f.suffix != ".gz")

    def test_local_paths_stay_in_the_ignored_cache(self):
        repo = self.home / "code" / "proj"
        repo.mkdir(parents=True)
        trace = self.home / "session.jsonl"
        trace.write_text(json.dumps({"cwd": str(repo), "note": f"read {self.home}/notes.txt and /home/alicex/y"}) + "\n")
        self.store.publish("p/r", DEMO / "dna-primer.md", traces=[trace],
                           context={"repo": "git@github.com:me/proj.git", "path": str(repo), "branch": "main", "commit": "abc1234"},
                           session={"agent": "claude", "id": "11111111-2222-3333-4444-555555555555", "cwd": str(repo)})
        build(self.store)
        self.assertNotIn(str(self.home), self.committed_text())
        meta = json.loads((self.root / "projects/p/r/v001/meta.json").read_text())
        self.assertEqual(meta["context"], {"repo": "git@github.com:me/proj.git", "branch": "main", "commit": "abc1234"})
        self.assertEqual(meta["session"], {"agent": "claude", "id": "11111111-2222-3333-4444-555555555555"})
        self.assertEqual(self.store.local_info("p/r", 1), {"repo_path": str(repo), "session_cwd": str(repo)})
        self.assertIn(".cache/", (self.root / ".gitignore").read_text())
        stored = gzip.decompress((self.root / "projects/p/r/v001/traces/session.jsonl.gz").read_bytes()).decode()
        self.assertIn('"cwd": "~/code/proj"', stored)
        self.assertIn("read ~/notes.txt", stored)
        self.assertIn("/home/alicex/y", stored)  # only whole path components are rewritten
        self.assertEqual(run("--store", str(self.root), "check")[1].count("home directory"), 0)

    def test_answers_cite_relative_paths(self):
        from agentledger.ask import scrub_paths
        repo = self.home / "code" / "proj"
        text = f"See {self.root}/projects/p/r/v001/report.md:5 and {repo}/src/x.py:40 and {self.home}/notes.txt"
        self.assertEqual(scrub_paths(text, self.root, repo),
                         "See projects/p/r/v001/report.md:5 and src/x.py:40 and ~/notes.txt")

    def test_check_flags_a_leaked_path(self):
        self.store.publish("p/r", DEMO / "dna-primer.md")
        self.store.add_comment("p/r", f"log at {self.home}/run.log", author="me")
        code, out = run("--store", str(self.root), "check")
        self.assertIn("comments.csv: contains a path under your home directory", out)


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
