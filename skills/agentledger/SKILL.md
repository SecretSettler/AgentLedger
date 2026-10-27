---
name: agentledger
description: Publish agent reports into an AgentLedger store and work through the review comments people leave on them. Use when asked to write up an analysis, audit or experiment as a report, to file a report in the ledger, or to address, answer or resolve review comments / review threads on a report.
---

# AgentLedger

The store is the folder containing `agentledger.json`. Reports live only under
`projects/<project>/<report>[/<sub-report>]/vNNN/` and are written only by the CLI.
Never create, move or edit files under `projects/` by hand, apart from `report.md` drafts you then publish.

If `agentledger` is not on PATH: `pip install -e <path-to-AgentLedger>` or
`pip install git+https://github.com/SecretSettler/AgentLedger`.

## Publishing a report

1. Pick the id. `<project>` is the codebase or effort, `<report>` is the topic, a slug saying what the report is about
   (`harness-vs-paper`, not `report-2`). Run `agentledger tree` first and reuse an existing project and report when the
   topic matches; a new version of an existing report beats a new report. Use a sub-report id
   (`<project>/<report>/<part>`) for a self-contained part of a larger report.
2. Write Markdown outside `projects/` (e.g. `.drafts/` or a temp file). `agentledger template` prints a skeleton.
   - First line `# Title`; front matter `summary:` (one or two sentences, the answer first) and `about:`.
   - One `##` per module, with a stable id: `## Results {#results}`. Keep ids unchanged across versions so
     comments and change tracking follow the module.
   - Put the evidence (numbers, tables, commands, file paths) next to each claim. Mark modules that need a human
     check with `status=needs-review`.
3. Publish: `agentledger publish <id> draft.md --author <your-name> [--asset <file-or-dir>]`.
4. Commit and push the store if it is a git repo. Tell the user the path of the new `vNNN/index.html`.

## Working through review comments

1. Sync: `git pull` in the store. If the user gave you a CSV or pasted a CSV block, import it first:
   `agentledger comments import <file>` or `agentledger comments import - <<'CSV' ... CSV`.
2. List: `agentledger comments list --status waiting` (open threads where the agent has not had the last word).
   Each thread shows the quoted text, the module, and `path:line` in the source it was made on.
3. For every thread, investigate before answering: re-read the source, re-run the analysis, check the data.
   Do not just agree with the reviewer.
4. Answer each thread in place:
   - `agentledger comments reply <id> "<answer with evidence>"` when you are answering or asking back;
   - add `--resolve` when the answer settles it without changing the report;
   - add `--wontfix` with the reason when you decline.
5. When the report itself must change: `agentledger draft <id> -o .drafts/<name>.md`, edit, then
   `agentledger publish <id> .drafts/<name>.md --changes "<what changed and why>" --resolves <id1>,<id2>`.
   Reply on each resolved thread saying where the fix is (module and version).
6. `agentledger check`, then commit (`<report id> vN: address review`) and push.

Keep replies short and specific: what you checked, what you found, what changed.
