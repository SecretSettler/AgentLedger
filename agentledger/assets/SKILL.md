---
name: agentledger
description: Publish every report, write-up, analysis, audit, investigation, benchmark or experiment summary into the AgentLedger store instead of leaving it only in chat or as a loose file in the repo, and work through the review comments people leave on those reports. Use whenever the user asks for a report, a write-up of findings, to "write this up", or to address, answer or resolve review comments on a report.
---

# AgentLedger reports

Reports live in the ledger store: a git repo containing `agentledger.json`, found through `$AGENTLEDGER_STORE` from any
directory. Never leave a report only in chat, and never write report files into the repository you are working in.
Only the `agentledger` CLI writes under the store's `projects/` folder.

If `agentledger` is missing, or `agentledger tree` says no store is configured, tell the user to run
`agentledger setup --store <path-to-store-clone>` and stop.

## Writing a report

1. `agentledger sync` — pulls the store, so you see comments and versions that arrived since last time.
2. Choose the id `<project>/<report>`:
   - project: the slug of the repository you are working in (lowercase name of
     `git rev-parse --show-toplevel`, other characters replaced by `-`), unless the user names a project;
   - report: a slug for the topic (`harness-vs-paper`, not `report-2`). Run `agentledger tree` first and reuse an
     existing report when the topic matches: a new version beats a new report on the same topic.
     Use `<project>/<report>/<part>` for a self-contained part of a larger report.
3. Write Markdown to `$AGENTLEDGER_STORE/.drafts/<project>__<report>.md` (`agentledger template` prints a skeleton):
   - `# Title` on the first line; front matter `summary:` (the answer, one or two sentences) and `about:`;
   - one `##` section per module with a stable id, e.g. `## Results {#results}`; keep ids across versions;
   - evidence next to every claim: numbers, tables, commands, file paths, commit hashes;
   - mark modules a human should check with `status=needs-review`, e.g. `## Results {#results status=needs-review}`;
   - images or data files: save them next to the draft and pass each with `--asset <path>`.
4. Publish from the repository the report is about, so it records that repo and commit:
   `agentledger publish <id> <draft> --summary "<one line>"` (for a revision add `--changes "<what changed>"`).
5. `agentledger sync -m "<id> vN: <short title>"` — commits and pushes the store.
6. Answer in chat with a short summary of the findings, the version, and the `github:` link printed by publish.

## Working through review comments

1. `agentledger sync`. If the user handed you a CSV, or pasted a block ending in `CSV`, import it first:
   `agentledger comments import <file>` or `agentledger comments import - <<'CSV' ... CSV`.
2. `agentledger comments list --status waiting [<report-id>]` lists open threads where the agent has not had the last
   word, each with the quoted text, its module, and `path:line` in the source it was made on.
3. Investigate every thread before answering: re-read the source, re-run the analysis, check the data.
   Do not simply agree with the reviewer.
4. Answer each thread in place:
   - `agentledger comments reply <id> "<answer with evidence>"` to answer or ask back;
   - add `--resolve` when the answer settles it without changing the report;
   - add `--wontfix` with the reason when you decline.
5. When the report must change: `agentledger draft <report-id> -o "$AGENTLEDGER_STORE/.drafts/<name>.md"`, edit, then
   `agentledger publish <report-id> <draft> --changes "<what changed and why>" --resolves <id1>,<id2>`.
   Reply on each resolved thread with where the fix is (module and version).
6. `agentledger sync -m "<report-id> vN: address review"`.

Keep replies short and specific: what you checked, what you found, what changed.
