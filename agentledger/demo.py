"""``agentledger demo``: a sample store to try the review loop in one command.

The numbers in the sample reports are made up.  When no Claude Code or Codex is
installed, a stand-in agent answers Ask questions and says that it is a demo.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from . import comments as cm
from .build import build
from .store import Store, init_store, write_json

DATA = Path(__file__).parent / "demo"
MARKER = ".agentledger-demo"


def default_dir() -> Path:
    return Path.home() / ".cache" / "agentledger" / "demo-store"


def create(target: Path) -> Store:
    """(Re)build the demo store at ``target``; only ever deletes a previous demo store."""
    target = Path(target).expanduser().resolve()
    if target.exists():
        if not (target / MARKER).exists():
            raise SystemExit(f"agentledger: {target} exists and is not a demo store; pick another --dir")
        shutil.rmtree(target)
    init_store(target, name="AgentLedger demo")
    (target / MARKER).write_text("created by `agentledger demo`; safe to delete\n", encoding="utf-8")
    store = Store(target)
    write_json(target / "projects" / "demo" / "project.json", {
        "title": "Demo project",
        "description": "Sample data to try AgentLedger: the numbers are made up.",
    })
    store.publish("demo/benchmark", DATA / "benchmark-v1.md", author="claude-code")
    store.save_rows(cm.parse((DATA / "comments-v1.csv").read_text(encoding="utf-8")))
    store.reply("c_demo1", "Investigating: the harness re-emits the full C source on every call, which the recorded "
                "agent never does.", author="claude-code")
    store.publish("demo/benchmark", DATA / "benchmark-v2.md", author="claude-code", resolves=["c_demo1", "c_demo2"],
                  changes="Explained the output-token gap (new subsection under Results) and re-tokenized the recorded logs.")
    store.reply("c_demo1", "Fixed in v2: see *Why harness output tokens run higher* under Results. Excluding re-emitted "
                "source, harness output is 48,310 tokens, below the recorded 177,399.", author="claude-code")
    store.publish("demo/benchmark/dna-primer", DATA / "dna-primer.md", author="claude-code")
    build(store)
    return store


def agent_main() -> int:
    """Stand-in for Claude Code / Codex in the demo: reads the prompt, answers that it is a demo."""
    prompt = sys.stdin.read()
    lines = prompt.splitlines()
    quote = next((ln[2:] for ln in lines if ln.startswith("> ")), "")
    asked = [ln for ln in lines if ln.startswith("- ") and "(human)" in ln]
    question = asked[-1].split(": ", 1)[-1] if asked else ""
    time.sleep(1.2)
    print(
        "**This is the demo agent.** With Claude Code or Codex installed, the agent that wrote the report answers here, "
        "read-only, from the report source, its traces and the repository.\n\n"
        f"- You asked: {question}\n"
        + (f"- About: “{quote}”\n" if quote else "")
        + "- Try it for real: `agentledger demo --ask claude` (or `--ask codex`)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(agent_main())
