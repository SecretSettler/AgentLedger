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
from unittest import mock
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from agentledger import comments as cm
from agentledger.ask import ClaudeStream, resolve_backend
from agentledger.build import build, render_version
from agentledger.cli import main
from agentledger.markdown import render
from agentledger.modules import split_modules
from agentledger.sanitize import sanitize
from agentledger.server import serve
from agentledger.traces import detect_session, load_trace_state, prepared_dir
from agentledger.store import LedgerError, Store, init_store

DEMO = Path(__file__).resolve().parent.parent / "agentledger" / "demo"


def run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(list(argv))
    return code, out.getvalue()


def run_err(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


SID = "aaaaaaaa-1111-2222-3333-444444444444"


def fake_claude_session(config_dir: Path, cwd: Path, sid: str = SID, lines=(), entrypoint: str = "cli",
                        age: float = 0) -> Path:
    """A Claude Code transcript for ``cwd`` under ``config_dir/projects`` (as Claude Code lays them out)."""
    proj = config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
    proj.mkdir(parents=True, exist_ok=True)
    t = proj / f"{sid}.jsonl"
    head = {"type": "user", "sessionId": sid, "cwd": str(cwd), "entrypoint": entrypoint,
            "message": {"role": "user", "content": "start"}}
    t.write_text("\n".join(json.dumps(x) for x in [head, *lines]) + "\n", encoding="utf-8")
    if age:
        os.utime(t, (time.time() - age, time.time() - age))
    return t


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


class HtmlReportTests(unittest.TestCase):
    """HTML reports keep their own CSS and title; the viewer's reading typography is for Markdown only."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = Store(init_store(self.tmp / "store", name="H"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def page(self, rid, source):
        build(self.store)
        v = self.store.report(rid).latest
        return (v.path / "index.html").read_text(encoding="utf-8")

    def test_html_keeps_its_head_css_title_and_width(self):
        src = self.tmp / "r.html"
        src.write_text("""<!doctype html><html><head><title>Pipeline</title>
<meta name="agentledger:width" content="1100">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC">
<style>.al-module .node{flex:1 1 150px}</style></head>
<body><header><div class="eyebrow">task x</div><h1>Pipeline</h1><p>Lede.</p></header>
<h2 id="overview">Overview</h2><div class="node">a</div>
<h2 id="carve">Carve</h2><p>b</p></body></html>""", encoding="utf-8")
        self.store.publish("p/html", src, summary="One line.", allow_scripts=True)
        page = self.page("p/html", src)
        self.assertIn(".al-module .node{flex:1 1 150px}", page)              # <head> CSS survives
        self.assertIn("fonts.googleapis.com", page)
        self.assertIn('<article class="al-article al-html" id="al-article" style="--al-html-width:1100px">', page)
        self.assertNotIn('class="al-title"', page)                           # its own <h1> stands alone
        self.assertNotIn('class="al-summary"', page)
        self.assertIn("<h1>Pipeline</h1>", page)
        self.assertEqual([m.id for m in render_version(self.store.report("p/html").latest).modules],
                         ["intro", "overview", "carve"])

    def test_a_bare_leading_h1_is_still_the_page_title(self):
        src = self.tmp / "s.html"
        src.write_text("<style>.x{color:red}</style><h1>Short</h1><h2>One</h2><p>a</p>", encoding="utf-8")
        self.store.publish("p/short", src, allow_scripts=True)
        page = self.page("p/short", src)
        self.assertIn('<h1 class="al-title">Short</h1>', page)
        self.assertNotIn("<h1>Short</h1>", page)
        self.assertIn('class="al-article al-html"', page)
        self.assertIn(".x{color:red}", page)

    def test_markdown_keeps_the_reading_typography(self):
        self.store.publish("p/md", DEMO / "dna-primer.md")
        page = self.page("p/md", None)
        self.assertIn('<article class="al-article" id="al-article">', page)
        self.assertIn('class="al-title"', page)
        css = (Path(__file__).resolve().parent.parent / "agentledger/assets/viewer.css").read_text()
        self.assertIn(".al-article:not(.al-html) code {", css)                # the code chips stay Markdown-only


class TraceLinkTests(unittest.TestCase):
    """publish links the session that wrote a report (required) and shares only what --trace says: a reviewed,
    redacted copy of that session (auto), or files as they are."""

    SECRET = "s3cr3t-value-from-dotenv-XYZ"
    ENV_SECRET = "envtoken-0123456789abcdef"
    ANTHROPIC = "sk-ant-api03-" + "A" * 40
    GITHUB = "ghp_" + "b" * 36

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.claude = self.tmp / "claude"
        self.work = self.tmp / "work"
        self.work.mkdir()
        (self.work / ".env").write_text(f"MY_API_KEY={self.SECRET}\nDEBUG=1\n")
        self.store = Store(init_store(self.tmp / "store", name="T"))
        self.draft = self.tmp / "r.md"
        self.draft.write_text("# R\n\n## One {#one}\n\nx\n")
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.tmp / "codex"),
                                                "DEMO_TOKEN": self.ENV_SECRET})
        self.env.start()
        for k in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            os.environ.pop(k, None)
        self.cwd = os.getcwd()
        os.chdir(self.work)

    def tearDown(self):
        os.chdir(self.cwd)
        self.env.stop()
        shutil.rmtree(self.tmp)

    def publish(self, *extra):
        return run_err("--store", str(self.store.root), "publish", "p/r", str(self.draft), *extra)

    def trace(self, *argv):
        return run_err("--store", str(self.store.root), "trace", *argv)

    def session_lines(self):
        return [
            {"type": "session-owner", "accountUuid": "acct-123", "organizationUuid": "org-456", "sessionId": SID},
            {"type": "api-request-blob", "message": {"content": [{"type": "text", "text": "SYSTEM PROMPT TEXT"}]}},
            {"type": "attachment", "attachment": {"type": "file", "content": f"MY_API_KEY={self.SECRET}"}},
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "MEMORY INDEX TEXT"}},
            {"type": "user", "sessionId": SID, "message": {"role": "user", "content": [
                {"type": "text", "text": "please check <system-reminder>REMINDER TEXT</system-reminder> the logs"}]}},
            {"type": "assistant", "sessionId": SID, "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "PRIVATE REASONING"},
                {"type": "text", "text": "Reading the config."},
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": f"curl -H 'Authorization: Bearer {self.ENV_SECRET}'"}}]}},
            {"type": "user", "sessionId": SID, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": f"key={self.SECRET} anthropic={self.ANTHROPIC} gh={self.GITHUB} tokens: 123456789012"}]}},
        ]

    # ---------------------------------------------------------------- linking
    def test_publish_requires_a_linked_session(self):
        with self.assertRaises(SystemExit):
            self.publish()

    def test_auto_takes_the_session_the_agent_names(self):
        fake_claude_session(self.claude, self.work, sid=SID, age=600)
        fake_claude_session(self.claude, self.work, sid="bbbbbbbb-1111-2222-3333-444444444444")   # newer
        os.environ["CLAUDE_CODE_SESSION_ID"] = SID
        code, out, err = self.publish("--link-session", "auto")
        self.assertEqual(code, 0, err)
        self.assertIn("CLAUDE_CODE_SESSION_ID", err)
        meta = self.store.report("p/r").latest.meta
        self.assertEqual(meta["session"], {"agent": "claude", "id": SID})
        self.assertNotIn("traces", meta)                                    # linked, not shared

    def test_auto_without_an_id_takes_the_newest_author_session(self):
        fake_claude_session(self.claude, self.work, sid=SID, age=600)
        fake_claude_session(self.claude, self.work, sid="cccccccc-1111-2222-3333-444444444444",
                            entrypoint="agentledger-ask")
        code, out, err = self.publish("--link-session", "auto")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.store.report("p/r").latest.meta["session"]["id"], SID)   # Ask's own session left out
        self.assertIn("most recently active", err)

    def test_auto_takes_the_codex_thread_id(self):
        tid = "01a1c2af-bf61-7ec3-94ba-b54013298128"
        d = self.tmp / "codex" / "sessions" / "2026" / "11" / "09"
        d.mkdir(parents=True)
        (d / f"rollout-2026-11-09T23-41-00-{tid}.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"id": tid, "cwd": str(self.work)}}) + "\n")
        os.environ["CODEX_THREAD_ID"] = tid
        code, out, err = self.publish("--link-session", "auto")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.store.report("p/r").latest.meta["session"], {"agent": "codex", "id": tid})

    def test_link_by_path_or_id_and_the_errors(self):
        t = fake_claude_session(self.claude, self.work)
        self.assertEqual(self.publish("--link-session", SID)[0], 0)
        self.assertEqual(self.publish("--link-session", str(t))[0], 0)
        copy = self.tmp / "copy.jsonl"
        shutil.copy(t, copy)
        for bad in (str(copy), "dddddddd-1111-2222-3333-444444444444"):
            self.assertEqual(self.publish("--link-session", bad)[0], 2, bad)
        shutil.rmtree(self.claude)
        code, out, err = self.publish("--link-session", "auto")
        self.assertEqual(code, 2)
        self.assertIn("found no Claude Code or Codex session", err)

    # ---------------------------------------------------------------- the shared copy
    def test_prepare_keeps_the_conversation_and_redacts(self):
        fake_claude_session(self.claude, self.work, lines=self.session_lines())
        code, out, err = self.trace("prepare", "p/r", "--link-session", SID)
        self.assertEqual(code, 0, err)
        state = load_trace_state(self.store.root, "p/r")
        text = Path(state["path"]).read_text()
        for gone in (self.SECRET, self.ENV_SECRET, self.ANTHROPIC, self.GITHUB, "acct-123", "org-456", "SYSTEM PROMPT",
                     "MEMORY INDEX", "REMINDER TEXT", "PRIVATE REASONING"):
            self.assertNotIn(gone, text, gone)
        for kept in ("please check", "the logs", "Reading the config.", '"name": "Bash"', "[REDACTED:MY_API_KEY]",
                     "[REDACTED:DEMO_TOKEN]", "[REDACTED:anthropic-key]", "[REDACTED:github-token]",
                     "tokens: 123456789012"):
            self.assertIn(kept, text, kept)
        self.assertIn("known secret MY_API_KEY", state["findings"])
        self.assertIn("dropped assistant thinking blocks", out)

    def test_trace_auto_shares_only_an_approved_copy(self):
        fake_claude_session(self.claude, self.work, lines=self.session_lines())
        code, out, err = self.publish("--link-session", SID, "--trace", "auto")
        self.assertEqual(code, 2)
        self.assertIn("reviewed copy", err)                                 # nothing prepared
        self.trace("prepare", "p/r", "--link-session", SID)
        code, out, err = self.publish("--link-session", SID, "--trace", "auto")
        self.assertEqual(code, 2)
        self.assertIn("not been reviewed and approved", err)
        self.assertEqual(self.trace("approve", "p/r")[0], 0)
        path = Path(load_trace_state(self.store.root, "p/r")["path"])
        with path.open("a") as fh:                                          # edited after approval
            fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "late edit"}}) + "\n")
        code, out, err = self.publish("--link-session", SID, "--trace", "auto")
        self.assertEqual(code, 2)
        self.assertIn("changed after it was approved", err)
        self.assertEqual(self.trace("approve", "p/r")[0], 0)
        other = fake_claude_session(self.claude, self.work, sid="eeeeeeee-1111-2222-3333-444444444444")
        code, out, err = self.publish("--link-session", str(other), "--trace", "auto")
        self.assertEqual(code, 2)
        self.assertIn("prepare it again for the linked session", err)
        code, out, err = self.publish("--link-session", SID, "--trace", "auto")
        self.assertEqual(code, 0, err)
        v = self.store.report("p/r").latest
        shared = gzip.open(v.path / v.meta["traces"][0]["path"], "rt").read()
        self.assertIn("late edit", shared)
        self.assertNotIn(self.SECRET, shared)
        self.assertEqual(v.meta["session"]["id"], SID)
        self.assertFalse(prepared_dir(self.store.root, "p/r").exists())    # used up: the next version prepares its own

    def test_approve_refuses_a_copy_with_secrets_left(self):
        fake_claude_session(self.claude, self.work, lines=self.session_lines())
        self.trace("prepare", "p/r", "--link-session", SID)
        path = Path(load_trace_state(self.store.root, "p/r")["path"])
        with path.open("a") as fh:
            fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": f"oops {self.SECRET}"}}) + "\n")
        code, out, err = self.trace("approve", "p/r")
        self.assertEqual(code, 2)
        self.assertIn("known secret MY_API_KEY", err)

    def test_trace_path_is_shared_as_it_is_with_a_warning(self):
        fake_claude_session(self.claude, self.work)
        log = self.tmp / "run.log"
        log.write_text(f"token={self.ENV_SECRET}\n")
        code, out, err = self.publish("--link-session", SID, "--trace", str(log))
        self.assertEqual(code, 0, err)
        self.assertIn("attached as it is", err)
        v = self.store.report("p/r").latest
        self.assertIn(self.ENV_SECRET, gzip.open(v.path / v.meta["traces"][0]["path"], "rt").read())
        self.assertEqual(run_err("trace", "check", str(log))[0], 1)
        clean = self.tmp / "clean.log"
        clean.write_text("all good\n")
        self.assertEqual(run_err("trace", "check", str(clean))[0], 0)

    def test_redaction_rules_catch_secrets_not_code_or_settings(self):
        from agentledger.redact import Scrubber, known_secrets
        env = self.tmp / "deploy"
        env.mkdir()
        (env / ".env").write_text("BIND_HOST=127.0.0.1\nTZ=Asia/Shanghai\nRUN_MODE=standard\n"
                                  "IMAGE=ghcr.io/acme/app:latest\nDB_PASSWORD=hunter2hunter2\n"
                                  "SESSION_SALT=Zq8vR2kLp9Xw4Tn7Bm3Yc6Hd\n")
        secrets = known_secrets([self.tmp], environ={"HOME": "/home/x", "SSH_AUTH_SOCK": "/run/user/1/ssh",
                                                     "GH_TOKEN": "ghs_" + "c" * 36})
        self.assertEqual(set(secrets.values()), {"DB_PASSWORD", "SESSION_SALT", "GH_TOKEN", "MY_API_KEY"})  # no settings
        scrub = Scrubber(secrets)
        cases = {
            "secrets = known_secrets(roots)": "secrets = known_secrets(roots)",               # code, not a secret
            '"input_tokens": 123456789012': '"input_tokens": 123456789012',                 # a count
            "listen 127.0.0.1 in Asia/Shanghai": "listen 127.0.0.1 in Asia/Shanghai",       # plain settings
            "db hunter2hunter2": "db [REDACTED:DB_PASSWORD]",
            "api_key=abc123def456ghi789": "api_key=[REDACTED:assigned-secret]",
            "postgres://app:pa55word@db:5432/x": "postgres://[REDACTED:url-credentials]@db:5432/x",
            '{"accountUuid":"dba7e5e2-1936-4b70-be2d-97a00e9bb7f8"}': '{"accountUuid":"[REDACTED:account-id]"}',
            "creator_account_id: 01a1c2af-bf61-7ec3-94ba-b54013298128": "creator_account_id: [REDACTED:account-id]",
        }
        for given, want in cases.items():
            self.assertEqual(scrub(given), want, given)
        once = scrub("postgres://app:pa55word@db/x")
        self.assertEqual(scrub(once), once)                                                   # idempotent
        scrub("mail ada@example.org")
        self.assertEqual(scrub.hints["e-mail addresses"], 1)                                # flagged, not redacted

    def test_codex_sessions_keep_the_conversation_only(self):
        from agentledger.redact import sanitize_session
        src, dest = self.tmp / "rollout.jsonl", self.tmp / "out.jsonl"
        rows = [
            {"type": "session_meta", "payload": {"id": "t1", "cwd": "/w", "base_instructions": "BASE PROMPT",
                                                  "creator_account_id": "acct-9", "creator_user_id": "user-9"}},
            {"type": "turn_context", "payload": {"cwd": "/w", "sandbox_policy": {}}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                                   "content": [{"type": "input_text", "text": "DEVELOPER TEXT"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "<environment_context><cwd>/w</cwd></environment_context>"},
                {"type": "input_text", "text": "count the lines"}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": "PRIVATE REASONING"}]}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "c1",
                                                   "arguments": json.dumps({"cmd": f"echo {self.ANTHROPIC}"})}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "403"}},
            {"type": "event_msg", "payload": {"type": "token_count"}},
        ]
        src.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        counts = sanitize_session(src, dest, "codex", {})
        text = dest.read_text()
        for gone in ("BASE PROMPT", "acct-9", "user-9", "DEVELOPER TEXT", "environment_context", "PRIVATE REASONING",
                     self.ANTHROPIC, "token_count", "sandbox_policy"):
            self.assertNotIn(gone, text, gone)
        for kept in ("count the lines", '"call_id": "c1"', "403", "[REDACTED:anthropic-key]", '"cwd": "/w"'):
            self.assertIn(kept, text, kept)
        self.assertEqual(counts["kept records"], 4)


REVIEWER = r'''#!/usr/bin/env python3
"""A fake Claude Code / Codex reviewer: redacts a name in the copy the prompt names; logs its argv."""
import json, os, re, sys
argv = sys.argv[1:]
log = os.environ["FAKE_REVIEW_LOG"]
if argv[:2] == ["debug", "prompt-input"]:          # codex: report the profile it was given
    prof = next((a.split("=", 1)[1].strip('"') for a in argv if a.startswith("default_permissions=")), None)
    fs = next((a for a in argv if a.startswith(f"permissions.{prof}.filesystem=")), "")
    writes = re.findall(r'"([^"]+)"="write"', fs)
    deny = " The permission profile denies reading these paths." if '"deny"' in fs else ""
    roots = ", ".join(f"`{w}`" for w in writes)
    word = "root is" if len(writes) == 1 else "roots are"
    print(f"`sandbox_mode` is `workspace-write`. Network access is restricted. The writable {word} {roots}.{deny}")
    sys.exit(0)
with open(log, "a") as f:
    f.write(json.dumps({"argv": argv}) + "\n")
prompt = sys.stdin.read()
with open(log + ".prompt", "w") as f:
    f.write(prompt)
copy = re.search(r"The copy: (\S+)", prompt).group(1)
if "cannot change files" not in prompt:
    text = open(copy).read().replace("Ada Lovelace", "[REDACTED:name]")
    if os.environ.get("FAKE_BREAK_JSONL"):
        text += "{not json\n"
    open(copy, "w").write(text)
answer = "- names: 1 replaced with [REDACTED:name]"
if "-o" in argv:                                   # codex exec
    open(argv[argv.index("-o") + 1], "w").write(answer)
    print(json.dumps({"type": "thread.started", "thread_id": "codex-review-1"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": answer}}))
else:                                              # claude -p
    sid = argv[argv.index("--session-id") + 1]
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": answer}))
'''


class TraceReviewTests(unittest.TestCase):
    """`agentledger trace review` runs Claude Code or Codex over the prepared copy, able to change only its folder."""

    def setUp(self):
        from agentledger import sandbox
        self.tmp = Path(tempfile.mkdtemp())
        self.claude = self.tmp / "claude"
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.store = Store(init_store(self.tmp / "store", name="R"))
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name in ("claude", "codex"):
            (bin_dir / name).write_text(REVIEWER)
            (bin_dir / name).chmod(0o755)
        self.log = self.tmp / "review.log"
        self.saved_tempdir = tempfile.tempdir
        (self.tmp / "t").mkdir()
        tempfile.tempdir = str(self.tmp / "t")
        self.env = mock.patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "CLAUDE_CONFIG_DIR": str(self.claude),
            "CODEX_HOME": str(self.tmp / "codex"), "HOME": str(self.tmp / "home"), "FAKE_REVIEW_LOG": str(self.log)})
        self.env.start()
        for k in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "AGENTLEDGER_ASK_SANDBOX", "FAKE_BREAK_JSONL"):
            os.environ.pop(k, None)
        sandbox._AVAILABLE.clear()
        sandbox._AVAILABLE.update({"claude": True, "codex": True})
        fake_claude_session(self.claude, self.work, lines=[
            {"type": "user", "sessionId": SID, "message": {"role": "user", "content": "ask Ada Lovelace about it"}}])
        self.cwd = os.getcwd()
        os.chdir(self.work)
        code, out, err = run_err("--store", str(self.store.root), "trace", "prepare", "p/r", "--link-session", SID)
        self.assertEqual(code, 0, err)
        self.copy = Path(load_trace_state(self.store.root, "p/r")["path"])
        self.folder = prepared_dir(self.store.root, "p/r").resolve()

    def tearDown(self):
        from agentledger import sandbox
        os.chdir(self.cwd)
        self.env.stop()
        tempfile.tempdir = self.saved_tempdir
        sandbox._AVAILABLE.clear()
        shutil.rmtree(self.tmp)

    def review(self, agent):
        return run_err("--store", str(self.store.root), "trace", "review", "p/r", "--agent", agent)

    def argv(self):
        return json.loads(self.log.read_text().splitlines()[-1])["argv"]

    def test_claude_reviews_in_its_sandbox_and_may_edit_only_the_copy(self):
        code, out, err = self.review("claude")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Ada Lovelace", self.copy.read_text())
        self.assertIn("[REDACTED:name]", self.copy.read_text())
        argv = self.argv()
        self.assertIn("Edit", argv[argv.index("--tools") + 1:argv.index("--allowedTools")])
        self.assertIn("Bash", argv[argv.index("--tools") + 1:argv.index("--allowedTools")])
        self.assertIn(f"Edit(/{self.folder}/**)", argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")])
        denied = argv[argv.index("--disallowedTools") + 1:argv.index("--permission-mode")]
        self.assertNotIn("Edit", denied)
        self.assertIn("Write", denied)
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertIn(str(self.folder), settings["sandbox"]["filesystem"]["allowWrite"])
        self.assertIn(f"Edit(/{self.folder}/**)", settings["permissions"]["allow"])
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(self.folder))     # not the whole store
        state = load_trace_state(self.store.root, "p/r")
        self.assertTrue(state["reviewed"]["changed"])
        self.assertTrue((self.folder / "review.md").is_file())
        self.assertIn("trace approve p/r", out)
        prompt = (self.tmp / "review.log.prompt").read_text()
        self.assertIn("personal data", prompt)
        self.assertIn("Edit only that file", prompt)

    def test_a_review_resets_an_earlier_approval(self):
        self.assertEqual(run_err("--store", str(self.store.root), "trace", "approve", "p/r")[0], 0)
        self.assertEqual(self.review("claude")[0], 0)
        draft = self.tmp / "r.md"
        draft.write_text("# R\n\n## One {#one}\n\nx\n")
        code, out, err = run_err("--store", str(self.store.root), "publish", "p/r", str(draft), "--link-session", SID,
                                 "--trace", "auto")
        self.assertEqual(code, 2)
        self.assertIn("not been reviewed and approved", err)
        self.assertEqual(run_err("--store", str(self.store.root), "trace", "approve", "p/r")[0], 0)
        code, out, err = run_err("--store", str(self.store.root), "publish", "p/r", str(draft), "--link-session", SID,
                                 "--trace", "auto")
        self.assertEqual(code, 0, err)

    def test_codex_reviews_under_a_profile_that_writes_the_copy(self):
        code, out, err = self.review("codex")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Ada Lovelace", self.copy.read_text())
        argv = self.argv()
        fs = next(a for a in argv if a.startswith("permissions.agentledger-ask.filesystem="))
        self.assertIn(f'"{self.folder}"="write"', fs)
        self.assertNotIn("--sandbox", argv)

    def test_without_a_sandbox_claude_still_edits_and_codex_only_lists(self):
        os.environ["AGENTLEDGER_ASK_SANDBOX"] = "off"
        code, out, err = self.review("claude")
        self.assertEqual(code, 0, err)
        argv = self.argv()
        tools = argv[argv.index("--tools") + 1:argv.index("--allowedTools")]
        self.assertEqual(tools, ["Read", "Grep", "Glob", "Edit"])               # no Bash without the sandbox
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("sandbox", json.loads(argv[argv.index("--settings") + 1]))
        self.assertNotIn("Ada Lovelace", self.copy.read_text())
        code, out, err = self.review("codex")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.argv()[self.argv().index("--sandbox") + 1], "read-only")
        self.assertIn("could not change the copy", out)
        self.assertIn("cannot change files", (self.tmp / "review.log.prompt").read_text())

    def test_a_review_that_breaks_the_file_is_an_error(self):
        os.environ["FAKE_BREAK_JSONL"] = "1"
        code, out, err = self.review("claude")
        self.assertEqual(code, 2)
        self.assertIn("is not valid JSON", err)

    def test_nothing_to_review(self):
        shutil.rmtree(self.folder)
        code, out, err = self.review("claude")
        self.assertEqual(code, 2)
        self.assertIn("no prepared trace", err)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "store"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_cli_round_trip(self):
        s = str(self.root)
        self.assertEqual(run("init", s, "--name", "CLI")[0], 0)
        fake_claude_session(self.tmp / "claude", self.tmp)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "claude"), "CLAUDE_CODE_SESSION_ID": SID}):
            self.assertEqual(run("--store", s, "publish", "demo/benchmark", str(DEMO / "benchmark-v1.md"), "--author", "a",
                                 "--link-session", "auto")[0], 0)
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
        fake_claude_session(self.tmp / "claude", work)
        try:
            os.chdir(work)
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "claude"), "CLAUDE_CODE_SESSION_ID": SID}):
                code, out = run("publish", "work-repo/findings", str(DEMO / "dna-primer.md"), "--link-session", "auto")
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
        saved_path = os.environ["PATH"]
        os.environ["PATH"] = str(self.tmp)  # no claude or codex, as on CI: printing the prompt must not need one
        try:
            code, out = run("--store", str(self.root), "ask", "c_demo1", "--print-prompt")
        finally:
            os.environ["PATH"] = saved_path
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
with open(os.environ["FAKE_ARGV_LOG"] + ".entrypoint", "a") as f:
    f.write(str(os.environ.get("CLAUDE_CODE_ENTRYPOINT")) + "\\n")
prompt = sys.stdin.read()
with open(os.environ["FAKE_ARGV_LOG"] + ".prompts", "a") as f:
    f.write(json.dumps(prompt) + "\\n")
if "--resume" in argv and os.environ.get("FAKE_FAIL_RESUME"):
    print("Error: session is busy", file=sys.stderr)
    sys.exit(1)
boxed = "--settings" in argv and '"sandbox"' in argv[argv.index("--settings") + 1]
if boxed and os.environ.get("FAKE_FAIL_SANDBOX"):
    print("Error: sandbox.failIfUnavailable is set but the sandbox could not start (bubblewrap)", file=sys.stderr)
    sys.exit(1)
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else argv[argv.index("--resume") + 1]
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}))
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x/report.md"}}]}}))
if boxed and os.environ.get("FAKE_SANDBOX_INIT_FAIL"):
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo hi"}}]}}))
    print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
          "content": "Sandbox is required but failed to initialize: Failed to create bridge sockets after 5 attempts."}]}}))
if os.environ.get("FAKE_READS_THE_PHRASE"):   # a file the agent reads quotes the message (a test log, a report)
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "/x/log"}}]}}))
    print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "r1",
          "content": "92 -> Sandbox is required but failed to initialize: Failed to create bridge sockets"}]}}))
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "head log"}}]}}))
    print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "b1", "is_error": False,
          "content": "Sandbox is required but failed to initialize: quoted by the file, printed by a command that ran"}]}}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
                  "result": "fake claude answer" + (" (resumed)" if "--resume" in argv else "")}))
"""


class SessionAskTests(unittest.TestCase):
    """Ask continues the session that wrote the report: forked once per thread, then continued."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = {k: os.environ.get(k) for k in ("PATH", "CLAUDE_CONFIG_DIR", "FAKE_ARGV_LOG", "FAKE_FAIL_RESUME",
                                                   "CLAUDE_CODE_ENTRYPOINT", "AGENTLEDGER_ASK_ENTRYPOINT",
                                                   "AGENTLEDGER_ASK_SANDBOX", "FAKE_FAIL_SANDBOX", "HOME")}
        from agentledger import sandbox
        sandbox._AVAILABLE.clear()
        os.environ["AGENTLEDGER_ASK_SANDBOX"] = "off"  # these tests cover sessions; SandboxAskTests covers the sandbox
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
        from agentledger import sandbox
        sandbox._AVAILABLE.clear()
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

    def test_one_review_session_per_version(self):
        """The first question forks the session that wrote the report; every later one, in any thread, continues it."""
        from agentledger.ask import answer_thread, review_session
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
        self.assertEqual(review_session(self.store, {"report": "p/r", "version": "1"})["id"], fork)
        self.store.reply(self.cid, "and then?", author="carol", role="human")
        answer_thread(self.store, self.cid, backend)
        other = self.store.add_comment("p/r", "and this part?", module="outcome", author="dave")["id"]
        answer_thread(self.store, other, backend)
        for later in self.argv_log()[-2:]:
            self.assertEqual(later[later.index("--resume") + 1], fork)
            self.assertNotIn("--fork-session", later)
            self.assertNotIn("--session-id", later)
        self.assertIn("another question", self.prompts()[-1])

    def test_questions_at_once_share_one_fork(self):
        from agentledger.ask import answer_thread
        backend = resolve_backend("auto")
        cids = [self.cid] + [self.store.add_comment("p/r", f"q{i}?", module="outcome", author="carol")["id"] for i in range(3)]
        workers = [threading.Thread(target=answer_thread, args=(self.store, c, backend)) for c in cids]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        self.assertEqual(sum("--fork-session" in a for a in self.argv_log()), 1)

    def prompts(self):
        return [json.loads(ln) for ln in (self.tmp / "argv.log.prompts").read_text().splitlines()]

    def entrypoints(self):
        return (self.tmp / "argv.log.entrypoint").read_text().split()

    def test_sessions_are_listed_and_named(self):
        """Claude Code hides sdk-cli sessions from `claude --resume` and the IDE; Ask's are visible and named."""
        from agentledger.ask import answer_thread
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"   # serve started from inside an IDE session
        backend = resolve_backend("auto")
        answer_thread(self.store, self.cid, backend)
        first = self.argv_log()[-1]
        self.assertEqual(first[first.index("--name") + 1], "AgentLedger: p/r v1")
        self.store.reply(self.cid, "and then?", author="carol", role="human")
        answer_thread(self.store, self.cid, backend)
        self.assertNotIn("--name", self.argv_log()[-1])          # continuing keeps the session and its name
        self.assertEqual(self.entrypoints(), ["agentledger-ask", "agentledger-ask"])
        os.environ["AGENTLEDGER_ASK_ENTRYPOINT"] = "sdk-cli"
        answer_thread(self.store, self.store.add_comment("p/r", "again?", module="outcome", author="carol")["id"], backend)
        self.assertEqual(self.entrypoints()[-1], "sdk-cli")

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


FAKE_CODEX = """#!/usr/bin/env python3
import json, os, re, sys
argv = sys.argv[1:]
if argv[:2] == ["debug", "prompt-input"]:
    with open(os.environ["FAKE_ARGV_LOG"] + ".verify", "a") as f:
        f.write(json.dumps(argv) + "\\n")
    if os.environ.get("FAKE_CODEX_IGNORE_PROFILE"):
        print("`sandbox_mode` is `danger-full-access`: no restrictions. Network access is enabled.")
        sys.exit(0)
    prof = next((a.split("=", 1)[1].strip('"') for a in argv if a.startswith("default_permissions=")), None)
    fs = next((a for a in argv if a.startswith(f"permissions.{prof}.filesystem=")), "")
    writes = re.findall(r'"([^"]+)"="write"', fs)
    deny = " The permission profile denies reading these paths/globs." if '"deny"' in fs else ""
    if writes:
        print(f"`sandbox_mode` is `workspace-write`. Network access is restricted. The writable root is `{writes[0]}`.{deny}")
    else:
        print(f"`sandbox_mode` is `read-only`: The sandbox only permits reading files. Network access is restricted.{deny}")
    sys.exit(0)
with open(os.environ["FAKE_ARGV_LOG"] + ".codex", "a") as f:
    f.write(json.dumps({"argv": argv, "tmpdir": os.environ.get("TMPDIR"), "tmpprefix": os.environ.get("TMPPREFIX")}) + "\\n")
prompt = sys.stdin.read()
with open(os.environ["FAKE_ARGV_LOG"] + ".prompts", "a") as f:
    f.write(json.dumps(prompt) + "\\n")
out = argv[argv.index("-o") + 1]
open(out, "w").write("fake codex answer")
print(json.dumps({"type": "thread.started", "thread_id": "codex-thread-1"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "fake codex answer"}}))
"""


class SandboxAskTests(unittest.TestCase):
    """Ask runs programs in the agent's sandbox when it is available, and reads only when it is not."""

    argv_log = SessionAskTests.argv_log
    prompts = SessionAskTests.prompts

    def tearDown(self):
        tempfile.tempdir = self.saved_tempdir
        SessionAskTests.tearDown(self)

    def setUp(self):
        SessionAskTests.setUp(self)
        self.saved_tempdir = tempfile.tempdir
        (self.tmp / "t").mkdir()
        tempfile.tempdir = str(self.tmp / "t")   # scratch folders stay inside the test's own folder
        from agentledger import sandbox
        os.environ.pop("AGENTLEDGER_ASK_SANDBOX", None)
        sandbox._AVAILABLE.update({"claude": True, "codex": True})   # as if the probe had passed
        home = self.tmp / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "id_ed25519").write_text("secret")
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text("{}")
        os.environ["HOME"] = str(home)
        (self.repo / ".env").write_text("KEY=1")
        (self.tmp / "bin" / "codex").write_text(FAKE_CODEX)
        (self.tmp / "bin" / "codex").chmod(0o755)

    def settings(self, argv):
        return json.loads(argv[argv.index("--settings") + 1])

    def test_claude_runs_bash_in_its_sandbox(self):
        from agentledger.ask import answer_thread
        from agentledger import sandbox
        answer_thread(self.store, self.cid, resolve_backend("claude"))
        argv = self.argv_log()[-1]
        self.assertEqual(argv[argv.index("--tools") + 1:argv.index("--allowedTools")], ["Read", "Grep", "Glob", "Bash"])
        self.assertEqual(argv.count("--tools"), 1)
        self.assertIn("--strict-mcp-config", argv)
        allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
        denied = argv[argv.index("--disallowedTools") + 1:argv.index("--permission-mode")]
        self.assertEqual(allowed, ["Read", "Grep", "Glob", "Bash"])
        self.assertEqual(denied, ["Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        box = self.settings(argv)["sandbox"]
        self.assertTrue(box["enabled"] and box["failIfUnavailable"])
        self.assertFalse(box["allowUnsandboxedCommands"])
        scratch = sandbox.scratch_dir(self.root, "p/r@v1").resolve()
        self.assertEqual(box["filesystem"]["allowWrite"], [str(scratch)])
        self.assertTrue({str(self.root.resolve()), str(self.repo.resolve())} <= set(box["filesystem"]["denyWrite"]))
        shared = Path("/tmp") / f"claude-{os.getuid()}"
        self.assertTrue({str(shared), str(shared.resolve())} <= set(box["filesystem"]["denyWrite"]))
        home = Path(os.environ["HOME"]).resolve()
        for secret in (home / ".ssh", home / ".codex" / "auth.json", self.repo.resolve() / ".env"):
            self.assertIn(str(secret), box["filesystem"]["denyRead"])
        rules = self.settings(argv)["permissions"]["deny"]
        self.assertIn(f"Read(/{home}/.ssh/**)", rules)
        self.assertIn(f"Read(/{self.repo.resolve()}/.env)", rules)
        self.assertEqual(box["network"]["allowedDomains"], [])
        prompt = self.prompts()[-1]
        self.assertIn("Find evidence with Read, Grep and Glob first", prompt)
        self.assertIn("Run commands with Bash only when searching does not turn up the evidence", prompt)
        self.assertIn(str(scratch), prompt)

    def test_codex_uses_a_permissions_profile(self):
        from agentledger.ask import answer_thread
        from agentledger import sandbox
        row = answer_thread(self.store, self.cid, resolve_backend("codex"), choice="codex")
        self.assertEqual(row["body"], "fake codex answer")
        call = json.loads((self.tmp / "argv.log.codex").read_text().splitlines()[-1])
        argv = call["argv"]
        self.assertNotIn("--sandbox", argv)
        self.assertIn('default_permissions="agentledger-ask"', argv)
        self.assertIn('web_search="disabled"', argv)
        self.assertIn("permissions.agentledger-ask.network.enabled=false", argv)
        self.assertIn("mcp_servers={}", argv)
        disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
        self.assertTrue({"apps", "browser_use", "plugins"} <= disabled)   # the apps connector can search the web
        fs = next(a for a in argv if a.startswith("permissions.agentledger-ask.filesystem="))
        scratch = sandbox.scratch_dir(self.root, "p/r@v1").resolve()
        home = Path(os.environ["HOME"]).resolve()
        self.assertIn('":root"="read"', fs)
        self.assertIn(f'"{scratch}"="write"', fs)
        self.assertIn(f'"{home / ".ssh"}"="deny"', fs)
        self.assertEqual(call["tmpdir"], str(scratch))
        self.assertEqual(call["tmpprefix"], str(scratch / "zsh"))   # zsh here-documents go there, not under TMPDIR
        prompt = self.prompts()[-1]
        self.assertIn("rg, sed -n, cat", prompt)
        self.assertIn("Run programs (python, scripts) only when", prompt)

    def test_falls_back_to_read_only_when_the_sandbox_will_not_start(self):
        from agentledger.ask import answer_thread
        from agentledger import sandbox
        os.environ["FAKE_FAIL_SANDBOX"] = "1"
        row = answer_thread(self.store, self.cid, resolve_backend("claude"))
        self.assertTrue(row["body"].startswith("fake claude answer"))
        tried, answered = self.argv_log()[-2:]
        self.assertIn("sandbox", self.settings(tried))
        self.assertNotIn("sandbox", self.settings(answered))                  # read-only, secrets still denied:
        self.assertIn(f"Read(/{Path(os.environ['HOME']).resolve()}/.ssh/**)", self.settings(answered)["permissions"]["deny"])
        self.assertIn("Bash", answered[answered.index("--disallowedTools"):])
        self.assertIn("not run programs in this session", self.prompts()[-1])
        self.assertFalse(sandbox.available("claude"))   # later questions go straight to read-only
        self.assertEqual(answered[answered.index("--tools") + 1:answered.index("--allowedTools")], ["Read", "Grep", "Glob"])

    def test_codex_runs_only_a_profile_it_has_verified(self):
        """Before each run, `codex debug prompt-input` shows what Codex resolved from all its config layers plus Ask's
        overrides; a profile that did not take hold is not used, whatever the config files say."""
        from agentledger.ask import answer_thread
        answer_thread(self.store, self.cid, resolve_backend("codex"), choice="codex")
        verify = [json.loads(l) for l in (self.tmp / "argv.log.verify").read_text().splitlines()]
        self.assertIn('default_permissions="agentledger-ask"', verify[-1])
        self.assertIn("--disable", verify[-1])                               # checked with the same flags as the run
        os.environ["FAKE_CODEX_IGNORE_PROFILE"] = "1"   # e.g. a config layer that wins over -c in some Codex version
        try:
            self.store.reply(self.cid, "and now?", author="carol", role="human")
            answer_thread(self.store, self.cid, resolve_backend("codex"), choice="codex")
        finally:
            os.environ.pop("FAKE_CODEX_IGNORE_PROFILE", None)
        argv = json.loads((self.tmp / "argv.log.codex").read_text().splitlines()[-1])["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")    # last resort: writes nothing
        self.assertFalse(any(a.startswith("default_permissions=") for a in argv))
        self.assertIn("cannot write any file", self.prompts()[-1])

    def test_claude_ignores_project_settings_and_widening_user_settings(self):
        from agentledger.ask import answer_thread
        from agentledger import sandbox
        answer_thread(self.store, self.cid, resolve_backend("claude"))
        argv = self.argv_log()[-1]
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")   # no project hooks or exceptions
        (self.tmp / "claude" / "settings.json").write_text(json.dumps({"sandbox": {"excludedCommands": ["docker"]}}))
        self.assertIn("excludedCommands", sandbox.claude_settings_widen())
        self.assertFalse(sandbox.usable("claude", self.repo))
        self.store.reply(self.cid, "and now?", author="carol", role="human")
        answer_thread(self.store, self.cid, resolve_backend("claude"))
        self.assertNotIn("sandbox", self.settings(self.argv_log()[-1]))     # read-only instead
        (self.tmp / "claude" / "settings.json").write_text(json.dumps({"sandbox": {"excludedCommands": []}, "theme": "x"}))
        self.assertIsNone(sandbox.claude_settings_widen())
        # measured: a user's allowRead reopens a denied folder, allowWrite/allowedDomains add to Ask's lists
        for widening in ({"filesystem": {"allowRead": ["~/.ssh"]}}, {"filesystem": {"allowWrite": ["/x"]}},
                         {"network": {"allowedDomains": ["example.com"]}}):
            (self.tmp / "claude" / "settings.json").write_text(json.dumps({"sandbox": widening}))
            self.assertIsNotNone(sandbox.claude_settings_widen(), widening)

    def test_a_sandbox_that_fails_mid_run_means_answering_read_only(self):
        """Commands refused because the sandbox did not initialize: answer again read-only, not without evidence."""
        from agentledger.ask import answer_thread
        from agentledger import sandbox
        os.environ["FAKE_SANDBOX_INIT_FAIL"] = "1"
        try:
            row = answer_thread(self.store, self.cid, resolve_backend("claude"))
        finally:
            os.environ.pop("FAKE_SANDBOX_INIT_FAIL", None)
        tried, answered = self.argv_log()[-2:]
        self.assertIn("sandbox", self.settings(tried))
        self.assertNotIn("sandbox", self.settings(answered))
        self.assertTrue(row["body"].startswith("fake claude answer"))
        self.assertFalse(sandbox.available("claude"))

    def test_quoting_the_failure_message_is_not_a_sandbox_failure(self):
        """Reading a trace or report that quotes Claude Code's sandbox error, even printing it from Bash, is not one."""
        from agentledger.ask import answer_thread
        from agentledger import sandbox
        os.environ["FAKE_READS_THE_PHRASE"] = "1"
        try:
            row = answer_thread(self.store, self.cid, resolve_backend("claude"))
            self.store.reply(self.cid, "and read-only?", author="carol", role="human")
            os.environ["AGENTLEDGER_ASK_SANDBOX"] = "off"
            answer_thread(self.store, self.cid, resolve_backend("claude"))
        finally:
            os.environ.pop("FAKE_READS_THE_PHRASE", None)
        self.assertTrue(row["body"].startswith("fake claude answer"))
        first, second = self.argv_log()[-2:]
        self.assertIn("sandbox", self.settings(first))            # answered in the sandbox, no read-only retry
        self.assertNotIn("sandbox", self.settings(second))
        os.environ.pop("AGENTLEDGER_ASK_SANDBOX", None)
        self.assertTrue(sandbox.available("claude"))              # and the sandbox stays on for later questions

    def test_scratch_paths_stay_short_for_claude_sockets(self):
        from agentledger import sandbox
        long_key = "some-project/a-rather-long-report-name/with-a-part@v123"
        d = sandbox.scratch_dir(self.root, long_key)
        self.assertEqual(len(d.name), sandbox.TAG_LEN)
        self.assertLessEqual(len(str(sandbox.claude_uid_tmp(d.resolve())).encode()), sandbox.MAX_CLAUDE_TMP)
        self.assertTrue(sandbox.claude_paths_fit())
        self.assertNotEqual(d, sandbox.scratch_dir(self.root, "p/r@v1"))
        pol = sandbox.policy(self.root, long_key, self.repo, self.repo)
        self.assertEqual((pol.scratch / ".key").read_text().splitlines()[0], long_key)

    def test_claude_reads_only_when_its_temp_folder_would_not_fit(self):
        """Past 44 bytes Claude Code would hand Bash the shared /tmp/claude-<uid>; Ask reads only instead."""
        from agentledger.ask import answer_thread
        from agentledger import sandbox
        saved = sandbox.MAX_CLAUDE_TMP
        sandbox.MAX_CLAUDE_TMP = 10
        try:
            self.assertFalse(sandbox.claude_paths_fit())
            answer_thread(self.store, self.cid, resolve_backend("claude"))
        finally:
            sandbox.MAX_CLAUDE_TMP = saved
        argv = self.argv_log()[-1]
        self.assertNotIn("sandbox", self.settings(argv))
        self.assertNotIn("Bash", argv[argv.index("--tools") + 1:argv.index("--allowedTools")])

    def test_off_switch_keeps_ask_read_only(self):
        """Read-only still denies the secrets: Codex through a read-only profile, Claude through Read deny rules."""
        from agentledger.ask import answer_thread
        os.environ["AGENTLEDGER_ASK_SANDBOX"] = "off"
        answer_thread(self.store, self.cid, resolve_backend("codex"), choice="codex")
        call = json.loads((self.tmp / "argv.log.codex").read_text().splitlines()[-1])
        argv = call["argv"]
        self.assertNotIn("--sandbox", argv)
        self.assertIn('default_permissions="agentledger-ask-ro"', argv)
        fs = next(a for a in argv if a.startswith("permissions.agentledger-ask-ro.filesystem="))
        self.assertNotIn('"write"', fs)
        self.assertIn(f'"{Path(os.environ["HOME"]).resolve() / ".ssh"}"="deny"', fs)
        self.assertEqual(call["tmpdir"], os.environ.get("TMPDIR"))              # left alone: nothing to write
        self.assertIn("cannot write any file", self.prompts()[-1])
        self.store.reply(self.cid, "and with claude?", author="carol", role="human")
        answer_thread(self.store, self.cid, resolve_backend("claude"), choice="claude")
        argv = self.argv_log()[-1]
        self.assertNotIn("sandbox", self.settings(argv))
        self.assertIn(f"Read(/{Path(os.environ['HOME']).resolve()}/.ssh/**)", self.settings(argv)["permissions"]["deny"])
        self.assertIn("not run programs in this session", self.prompts()[-1])

    def test_secret_paths_resolve_links_and_skip_missing(self):
        from agentledger import sandbox
        home = Path(os.environ["HOME"])
        (home / ".aws-real").mkdir()
        (home / ".aws").symlink_to(home / ".aws-real")
        (home / ".codex" / "state_5.sqlite").write_text("")
        (home / ".zsh_sessions").mkdir()
        found = sandbox.secret_paths([self.repo])
        self.assertIn((home / ".zsh_sessions").resolve(), found)              # macOS Terminal's per-window history
        self.assertIn((home / ".codex" / "state_5.sqlite").resolve(), found)   # globbed
        self.assertTrue((home / ".codex" / "sessions").is_dir())              # made first, so it is denied from the start
        self.assertIn((home / ".codex" / "sessions").resolve(), found)
        elsewhere = self.tmp / "elsewhere"
        (elsewhere / "claude-cfg").mkdir(parents=True)
        (elsewhere / "codex-home").mkdir()
        (elsewhere / "codex-home" / "auth.json").write_text("{}")
        os.environ["CLAUDE_CONFIG_DIR"] = str(elsewhere / "claude-cfg")
        os.environ["CODEX_HOME"] = str(elsewhere / "codex-home")
        try:
            moved = sandbox.secret_paths([])
        finally:
            os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "claude")
            os.environ.pop("CODEX_HOME", None)
        self.assertIn((elsewhere / "claude-cfg").resolve(), moved)          # where the credentials really are
        self.assertIn((elsewhere / "codex-home" / "auth.json").resolve(), moved)
        self.assertIn((elsewhere / "codex-home" / "sessions").resolve(), moved)
        self.assertIn((home / ".aws-real").resolve(), found)
        self.assertNotIn(home / ".gnupg", found)
        self.assertIn((self.repo / ".env").resolve(), found)

    @unittest.skipUnless(shutil.which("codex") and sys.platform.startswith(("linux", "darwin")),
                         "needs the real codex on Linux or macOS")
    def test_real_codex_sandbox_holds(self):
        """The real `codex sandbox` with Ask's profile: scratch writable, everything else not (no model involved)."""
        from agentledger import sandbox
        os.environ["PATH"] = os.environ["PATH"].split(os.pathsep, 1)[1]   # drop the fake codex
        os.environ["HOME"] = self.saved["HOME"]   # codex keeps helpers under its real home, never under /tmp
        sandbox._AVAILABLE.clear()
        self.assertTrue(sandbox._codex_ok())
        pol = sandbox.policy(self.root, "p/r@v1", self.repo, self.repo)
        self.assertTrue(sandbox.codex_verified(self.repo, pol))
        self.assertTrue(sandbox.codex_verified(self.repo, sandbox.readonly_policy(self.root, self.repo, self.repo)))
        # a profile displaced after Ask's overrides (nothing can follow them in a real run): not taken as Ask's
        for later in ('default_permissions=":danger-full-access"', 'default_permissions=":workspace"'):
            r = subprocess.run(["codex", "debug", "prompt-input", *sandbox.codex_overrides(pol), "-c", later, "."],
                               cwd=self.repo, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0)
            self.assertFalse(sandbox.profile_holds(r.stdout, pol), later)
        if shutil.which("zsh"):   # macOS's default shell: here-documents need TMPPREFIX inside the scratch
            env = dict(os.environ, TMPDIR=str(pol.scratch), TMPPREFIX=str(pol.scratch / "zsh"))
            r = subprocess.run(["codex", "sandbox", *sandbox.codex_overrides(pol), "-P", sandbox.codex_profile(pol),
                                "-C", str(self.repo), "--", "zsh", "-c", "cat <<'EOF'\nheredoc-ok\nEOF"],
                               cwd=self.repo, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.stdout.strip(), "heredoc-ok", r.stderr)


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


class ShareTests(unittest.TestCase):
    """One report travels as a file: the reader opens it, comments, asks their own agent; feedback comes back as CSV."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = {k: os.environ.get(k) for k in ("HOME",)}
        os.environ["HOME"] = str(self.tmp / "owner-home")
        self.owner = Store(init_store(self.tmp / "owner", name="Owner", repo="me/private-ledger"))
        trace = self.tmp / "session.jsonl"
        trace.write_text('{"note": "measured 4721 us"}\n')
        self.owner.publish("demo/benchmark", DEMO / "benchmark-v1.md", traces=[trace])
        self.owner.publish("demo/benchmark/dna", DEMO / "dna-primer.md")
        self.owner.publish("demo/other", DEMO / "dna-primer.md")          # must not travel
        self.owner.remember_local("demo/benchmark", 1, {"repo_path": "/somewhere/private"})
        self.owner.save_rows(cm.parse((DEMO / "comments-v1.csv").read_text()))
        build(self.owner)
        self.bundle = self.tmp / "out" / "benchmark.agentledger.zip"

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(self.tmp)

    def test_bundle_holds_only_that_report(self):
        code, out = run("--store", str(self.owner.root), "share", "demo/benchmark", "-o", str(self.bundle))
        self.assertEqual(code, 0, out)
        names = zipfile.ZipFile(self.bundle).namelist()
        self.assertIn("projects/demo/benchmark/v001/traces/session.jsonl.gz", names)
        self.assertIn("projects/demo/benchmark/dna/v001/report.md", names)
        self.assertFalse([n for n in names if n.startswith("projects/demo/other") or n.startswith(".cache")])
        config = json.loads(zipfile.ZipFile(self.bundle).read("agentledger.json"))
        self.assertNotIn("github", config)
        self.assertNotIn("/somewhere/private", zipfile.ZipFile(self.bundle).read("projects/demo/benchmark/v001/meta.json").decode())
        code, out = run("--store", str(self.owner.root), "share", "demo/benchmark", "-o", str(self.tmp / "lean.zip"), "--no-traces", "--no-comments")
        lean = zipfile.ZipFile(self.tmp / "lean.zip")
        self.assertFalse([n for n in lean.namelist() if "/traces/" in n])
        self.assertEqual(cm.parse(lean.read("projects/demo/benchmark/v001/comments.csv").decode()), [])

    def test_reader_comments_asks_and_sends_feedback(self):
        from agentledger import share
        share.pack(self.owner, "demo/benchmark", self.bundle)
        os.environ["HOME"] = str(self.tmp / "reader-home")
        reader, info = share.unpack(self.bundle)
        self.assertTrue(str(reader.root.resolve()).startswith(str((self.tmp / "reader-home").resolve())))
        self.assertEqual(info["report"], "demo/benchmark")
        row = reader.add_comment("demo/benchmark", "Where does this number come from?", module="results",
                                 quote="Harness out tok", author="dan")
        agent = self.tmp / "agent.py"
        agent.write_text("import sys; p = sys.stdin.read(); print('from trace' if '.cache/projects/demo/benchmark/v001/traces/session.jsonl' in p else 'no trace')")
        code, out = run("--store", str(reader.root), "ask", row["id"], "--cmd", f"{sys.executable} {agent}")
        self.assertEqual(code, 0, out)
        self.assertIn("from trace", out)
        feedback = self.tmp / "feedback.csv"
        self.assertEqual(run("--store", str(reader.root), "comments", "export", "demo/benchmark", "-o", str(feedback))[0], 0)
        # back on the owner's machine
        os.environ["HOME"] = str(self.tmp / "owner-home")
        self.assertEqual(run("--store", str(self.owner.root), "comments", "import", str(feedback))[0], 0)
        mine = [r for r in self.owner.comments() if r["id"] == row["id"] or r["reply_to"] == row["id"]]
        self.assertEqual(len(mine), 2)
        # the owner replies and ships a new version; the reader's own thread survives re-opening
        self.owner.reply("c_demo1", "re-tokenized", author="claude-code")
        self.owner.publish("demo/benchmark", DEMO / "benchmark-v2.md")
        share.pack(self.owner, "demo/benchmark", self.bundle)
        os.environ["HOME"] = str(self.tmp / "reader-home")
        local = reader.add_comment("demo/benchmark", "unsent note", author="dan")
        reader, _ = share.unpack(self.bundle)
        ids = {r["id"] for r in reader.comments()}
        self.assertTrue({local["id"], row["id"]} <= ids)
        self.assertEqual(len(reader.report("demo/benchmark").versions), 2)

    def test_hostile_bundles_are_refused_or_defused(self):
        from agentledger import share
        share.pack(self.owner, "demo/benchmark", self.bundle)
        evil = self.tmp / "evil.agentledger.zip"
        with zipfile.ZipFile(self.bundle) as src, zipfile.ZipFile(evil, "w") as z:
            for n in src.namelist():
                z.writestr(n, src.read(n))
            z.writestr("../outside.txt", "x")
        with self.assertRaises(LedgerError):
            share.unpack(evil, self.tmp / "evil-open")
        self.assertFalse((self.tmp / "outside.txt").exists())
        plain = self.tmp / "plain.zip"
        with zipfile.ZipFile(plain, "w") as z:
            z.writestr("hello.txt", "x")
        with self.assertRaises(LedgerError):
            share.unpack(plain, self.tmp / "plain-open")
        scripted = self.tmp / "scripted.agentledger.zip"
        with zipfile.ZipFile(self.bundle) as src, zipfile.ZipFile(scripted, "w") as z:
            for n in src.namelist():
                data = src.read(n)
                if n == "projects/demo/benchmark/v001/meta.json":
                    meta = json.loads(data)
                    meta["allow_scripts"] = True
                    data = json.dumps(meta).encode()
                if n == "projects/demo/benchmark/v001/report.md":
                    data += b"\n<script>alert(1)</script>\n"
                z.writestr(n, data)
        reader, _ = share.unpack(scripted, self.tmp / "scripted-open")
        page = (reader.root / "projects/demo/benchmark/v001/index.html").read_text()
        self.assertNotIn("alert(1)", page)

    def test_link_points_ask_at_a_checkout(self):
        from agentledger.ask import build_prompt
        repo = self.tmp / "my-checkout"
        repo.mkdir()
        self.assertEqual(run("--store", str(self.owner.root), "link", "demo/benchmark", str(repo))[0], 0)
        _, cwd = build_prompt(self.owner, "c_demo1")
        self.assertEqual(cwd, repo.resolve())

    def test_link_carries_to_later_versions(self):
        from agentledger.ask import build_prompt
        repo = self.tmp / "my-checkout"
        repo.mkdir()
        self.assertEqual(run("--store", str(self.owner.root), "link", "demo/benchmark", str(repo))[0], 0)
        self.owner.publish("demo/benchmark", DEMO / "benchmark-v2.md")  # published after the link, with no checkout
        latest = self.owner.report("demo/benchmark").latest.number
        self.assertEqual(self.owner.local_info("demo/benchmark", latest).get("repo_path"), None)
        row = self.owner.add_comment("demo/benchmark", "on the new version", author="dan")
        _, cwd = build_prompt(self.owner, row["id"])
        self.assertEqual(cwd, repo.resolve())


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
