# AgentLedger

Agent writes a report → you comment on it in the browser → the agent answers and publishes v2. Every report is modular, versioned and filed in one place.

## Use it in Claude Code or Codex

```sh
pip install "git+https://github.com/SecretSettler/AgentLedger"
git clone https://github.com/SecretSettler/agent-ledger-store ~/agent-ledger-store   # where reports are kept
agentledger setup ~/agent-ledger-store
```

`setup` remembers where the store is and copies the skill, [`skills/agentledger/SKILL.md`](skills/agentledger/SKILL.md), to
`~/.claude/skills/agentledger/` (Claude Code) and `~/.agents/skills/agentledger/` (Codex). Restart the agent, then just ask, in any repo:

- "write this up as a report" / "把这个分析写成报告"
- "address the review comments on harness-vs-paper" / "处理 harness-vs-paper 上的评论"

The agent publishes into `projects/<repo>/<topic>/vNNN/`, pushes the store, and replies with the link.

To review: open `index.html` in the store (or run `agentledger serve` in it and open http://localhost:8765), select text, press **Comment**, then **Save**.

<details><summary>Without <code>setup</code></summary>

Copy `skills/agentledger` into `~/.claude/skills/` or `~/.agents/skills/` (older Codex versions: `~/.codex/skills/`),
and `export AGENTLEDGER_STORE=~/agent-ledger-store` in your shell profile.
</details>

## What it does

1. An agent publishes a Markdown report with `agentledger publish`. Every `##` section becomes a module, the file lands in a fixed folder (`projects/<project>/<report>/v001/`), and a self-contained HTML page is generated next to it. The page records which repo and commit the report describes.
2. You read it in the browser, select any sentence or table cell, and press **Comment**, as in a doc review. Comments carry the quoted text, the module and the version they were made on.
3. Save the comments to the store (local server or GitHub), or **Export CSV** and hand the file over.
4. The agent runs `agentledger comments list`, investigates each thread, replies in place, and publishes **v2**. Changed and new modules are flagged, and resolved threads point at the version that settled them.
5. The dashboard tracks every project, report, version and open thread.

No dependencies: the CLI is pure Python 3.9+ and every page is a single HTML file that works from `file://`, any static host (GitHub Pages, githack), or after an `scp`.

## New store

```sh
agentledger init my-ledger --name "My ledger" --repo OWNER/my-ledger   # --repo enables "Save to GitHub"
```

To try the UI on sample data: `sh examples/build-demo.sh` (the numbers in it are made up).

## Store layout

The store is a plain folder, usually its own git repository. The CLI is the only thing that writes into `projects/`, so the hierarchy stays the same everywhere:

```
my-ledger/
  agentledger.json                 store config: name, GitHub repo/branch for comment saves
  index.html                       dashboard                       (generated)
  manifest.json                    machine-readable index           (generated)
  projects/
    <project>/                     a project, e.g. oneswe
      project.json                 title, description
      <report>/                    one topic: what the report is about
        report.json                title, about, tags, status
        index.html                 redirects to the latest version  (generated)
        v001/
          report.md                the source the agent published
          meta.json                author, date, summary, what changed, which threads it resolves
          comments.csv             review threads opened on this version
          index.html               the report page                  (generated)
          img/ data/ ...           assets the report references (carried into later versions)
        v002/
        <sub-report>/              same shape, nested under its parent report
```

- Names are lowercase slugs (`a-z`, `0-9`, `-`); `v001`-style names and `assets`, `index`, `projects` are reserved.
- A report id is its path under `projects/`: `oneswe/harness-vs-paper/dna-primer`.
- `agentledger check` reports stray files and malformed folders; `agentledger tree` prints the hierarchy.

## Reviewing in the browser

| | |
|---|---|
| **Comment** | Select text and press the **Comment** button that appears, or use the comment icon on a module heading. Pick a kind (question, issue, suggestion, note, praise). |
| **Review comments panel** | Your name, **Export CSV**, **Copy CSV**, **Import CSV**, **Copy for agent** (a Markdown brief you can paste into Claude Code or Slack), **Clear** (drop unsaved local edits). Filter open / resolved / all, this version or all versions. Each thread has go, reply, resolve/reopen, edit and delete. |
| **Saving** | Under `agentledger serve` the button reads **Save to store** and writes `comments.csv` on disk. On any other host with `--repo` configured it reads **Save to GitHub** and commits the merged CSV through the GitHub API with a fine-grained token (Contents: read and write). Otherwise comments stay in the browser until you export them. |
| **Timeline rail** (left) | The report as a vertical timeline: one node per module, ticks for sub-sections, badges for open threads. Drag the knob to scrub through the report. |
| **Navigator pad** (bottom right) | Click the round stick to open it; drag it up or down to scroll like a joystick. Inside: the version chain (click a circle to open that version at the same module), parent and sub-reports, and a minimap you can drag. |
| **Project map** (`m`) | Every report in the project with its version chain. |
| **Versions** | Modules that changed or are new since the previous version are marked; older versions show a banner linking to the latest. |

Shortcuts: `j`/`k` modules, `[`/`]` versions, `c` comment, `p` comments panel, `m` map, `n` pad, `d` dashboard, `?` help.

Comments made in the browser live in `localStorage` until saved, so nothing is lost on reload. The dashboard also shows them, flagged as unsaved.

## The agent loop

```sh
git pull                                              # if comments were saved to GitHub
agentledger comments import review.csv                # if they arrived as an exported CSV ('-' reads stdin)
agentledger comments list                             # open threads as Markdown, with source file:line
agentledger comments list --status waiting            # only threads whose last word is not the agent's
agentledger comments reply c_mvq0z4 "The column counts re-emitted source; see Token accounting." --resolve
agentledger draft oneswe/harness-vs-paper -o draft.md # copy the latest source to edit
agentledger publish oneswe/harness-vs-paper draft.md --changes "Explained the token gap" --resolves c_mvq0z4,c_mvq1a2
git add -A && git commit -m "harness-vs-paper v2" && git push
```

`agentledger sync [-m message]` pulls, rebuilds, commits and pushes the store in one step.

## Writing reports

- `# Title` on the first line (or `title:` in front matter). Optional front matter: `title`, `summary`, `about`, `tags`, `author`, `changes`.
- Every `## Heading` starts a module. Pin ids so comments and change tracking survive renames: `## Results {#results}`. Add a status badge: `## Results {#results status=needs-review}`.
- `###` headings become ticks on the rail.
- GitHub-flavoured Markdown: tables with alignment, task lists, fenced code, block quotes, images, raw HTML such as `<details>` or inline SVG.
- Publish a folder instead of a file to write one module per file: top-level `.md` files are concatenated in name order and everything else is copied as assets.
- Sub-reports: publish to a longer id, e.g. `agentledger publish oneswe/harness-vs-paper/dna-primer dna.md`.
- HTML reports work too (`report.html`); top-level `<h2>`s become modules.

## Commands

| Command | |
|---|---|
| `init [dir] --name --repo --branch` | Create a store |
| `publish <id> <file\|folder>` | New report or new version; records the git repo and commit it ran in. `--title --summary --about --author --changes --resolves --asset --tag --allow-scripts --no-carry --no-context` |
| `build` | Regenerate every page, the dashboard and `manifest.json` (writes only what changed) |
| `serve [--port --host --allow-host --open]` | Local server; the browser can save comments into the store |
| `sync [-m msg] [--no-pull --no-push]` | Pull, rebuild, commit and push the store |
| `setup <store>` | Remember the store; install the skill for Claude Code and Codex |
| `root` | Print the store folder |
| `tree`, `check` | Show the hierarchy; validate the layout |
| `draft <id> [-o file] [--version N]` | Copy a version's source to a working file |
| `template` | Print a report skeleton |
| `set <id> --title --about --status --tag --order` | Edit report metadata |
| `project <id> --title --description` | Create or edit a project |
| `path <id> [--source\|--comments\|--view]` | Print where a version lives |
| `comments list [id] [--status open\|waiting\|closed\|all] [--format md\|json\|csv] [-r]` | Threads, for agents |
| `comments add / reply / resolve / reopen / wontfix` | Work threads from the CLI |
| `comments import <csv\|->`, `comments export [id] [-o file]` | Move threads between browser and store |

The store is `--store`, else `$AGENTLEDGER_STORE`, else the nearest `agentledger.json` above the current directory, else the one `setup` remembered (`~/.config/agentledger/config.json`). `$AGENTLEDGER_AUTHOR` sets the default author.

## Comment CSV

One schema everywhere (browser export, `comments.csv`, CLI):

| Column | |
|---|---|
| `id` | `c_` + time + random |
| `reply_to` | root thread id for replies, empty for a thread |
| `report`, `version` | where the thread was opened |
| `module` | module id the quote is in |
| `kind` | question, issue, suggestion, note, praise |
| `quote`, `prefix`, `suffix` | the selected text and 40 characters either side, used to re-anchor it |
| `body`, `author`, `role` | the message; `role` is `human` or `agent` |
| `status` | `open`, `resolved`, `wontfix`, or `deleted` (a tombstone) on thread roots |
| `created`, `updated` | ISO 8601 UTC; on merge the newest `updated` wins |
| `resolved_in` | the version that resolved it |

## Security notes

- Report HTML is passed through an allow-list sanitiser: no scripts, event handlers, frames, forms or `javascript:` URLs. Publish with `--allow-scripts` only for reports you fully trust.
- The GitHub token is kept in memory unless you tick "remember". Pages on shared origins (githack serves everyone's pages from one origin) can read each other's `localStorage`, so don't remember it there.
- `agentledger serve` binds to `127.0.0.1` and only accepts saves with an `X-AgentLedger` header from an allowed `Host`, which blocks cross-site posts and DNS rebinding. Use `ssh -L 8765:localhost:8765 host` to review a remote store.

## Development

```sh
python -m unittest discover -s tests -v
```

Front-end code lives in `agentledger/assets/` (plain CSS and JS, no build step) and is inlined into every generated page.
