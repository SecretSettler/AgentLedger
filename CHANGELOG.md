# Changelog

## Unreleased

- **Follow-up questions in the thread.** Once the agent has answered, a box under the answer takes the next
  question: Enter asks (Shift+Enter for a new line, IME composition is left alone), **Reply only** saves without
  asking. The text being typed survives the panel refreshing, and the cursor returns to the box when the answer
  arrives.

## 0.4.0

- **Link the session, share a reviewed copy.** `publish --link-session auto|PATH|ID` is required: it records which
  Claude Code or Codex session wrote the version (`auto`: the one running the command via `CLAUDE_CODE_SESSION_ID`
  or `CODEX_THREAD_ID`, else the directory's most recently active one, never Ask's own), so Ask continues it, and
  shares nothing. `--trace auto` now shares only a reviewed copy: `agentledger trace prepare` keeps the conversation
  (no system text, attached files, reasoning, account ids) and redacts secrets (`.env` and credential-named
  environment values, common key formats, URL credentials), the agent reviews it, `agentledger trace approve`
  re-checks and records it, and publish refuses a copy that is missing, unapproved, changed since, or of another
  session. `--trace <path>` attaches as before, with a warning when it spots a credential. `agentledger trace check`
  scans any file, and `agentledger trace review` has Claude Code or Codex review the copy from the command line, in
  its sandbox and able to change only the copy (Claude Code: the Edit tool limited to it; Codex: a profile that
  writes it, verified; without a sandbox Codex only lists what to change).
- **HTML reports look as written.** The viewer's reading typography (serif headings, boxed inline `code`, table
  theme, paragraph spacing) now applies to Markdown reports only, so an HTML report's own CSS is not overridden where
  it leaves a property unset. HTML reports get a 980px column (`<meta name="agentledger:width">` sets another),
  keep the `<style>` and stylesheet `<link>` from their `<head>`, and when they open with their own `<h1>` the page
  header no longer repeats the title and summary. The skill says how to write HTML for AgentLedger.
- **Ask can compute.** The agent still answers from what it can read (Claude Code: Read, Grep, Glob), but when
  searching cannot settle a question or the answer is a derived value, it runs programs and shows the commands and
  output. They run in the agent's own sandbox: Claude Code's Bash sandbox (`failIfUnavailable`, no unsandboxed
  retries, `dontAsk`) or a Codex permissions profile. Reads exclude credentials, agent transcripts and state, `.env`
  files and the Docker socket; writes go only to a per-version scratch folder (Claude Code's temp folder included);
  no network. Where the sandbox is unavailable, Ask stays read-only; `serve --ask-read-only`, `ask --read-only` and
  `AGENTLEDGER_ASK_SANDBOX=off` force it.
- **Fewer ways out, in both modes.** Claude Code loads only Read, Grep and Glob (plus Bash in the sandbox), no MCP
  servers, no claude.ai connectors, and only the user's own settings: a reviewed repository's `.claude/settings.json`
  could otherwise add hooks, which run outside any sandbox (`claude -p` skips the trust prompt). User settings that
  widen the sandbox make Ask read-only: measured, a user's `filesystem.allowWrite` and `network.allowedDomains` add to
  Ask's lists and `filesystem.allowRead` reopens a denied folder (`sandbox.enabled: false`,
  `allowUnsandboxedCommands: true` and a permissive `permissions` block do not override Ask's settings).
- **Codex: verified, not guessed.** Before each Codex run Ask renders `codex debug prompt-input` with its overrides
  (no model call) and uses its permissions profile only if Codex resolved exactly that profile from every config
  layer. Measured with Codex 0.158: no user or project config (`sandbox_mode`, `default_permissions`, a profile of the
  same name, web search, apps) displaces Ask's `-c` overrides, so a config that sets `sandbox_mode` no longer sends
  Ask to read-only.
- **Read-only keeps secrets unreadable too.** Claude Code's read-only run gets the same Read deny rules; Codex's uses
  a read-only permissions profile instead of `--sandbox read-only` (which reads everything), falling back to that flag
  only when the profile cannot be verified.
- **Short scratch paths, and never the shared temp folder.** Claude Code hands sandboxed Bash its per-user temp
  folder (`<CLAUDE_CODE_TMPDIR>/claude-<uid>`) only while that path is at most 44 bytes; past that it silently falls
  back to `/tmp/claude-<uid>`, which every Claude Code session of the user writes to (measured on macOS with Claude
  Code 2.1.312). Scratch folders are now `<tmp>/al-<uid>/<8 hex>` (`.key` inside names the report version), measured
  on the real path (`/tmp` is `/private/tmp` on macOS); when even that does not fit, Ask reads only. The shared
  folder is denied from writes as well, so a Claude Code that falls back anyway refuses commands instead of writing
  there. A run whose commands were refused because the sandbox failed to initialize is answered again read-only.
  Codex runs without web search, MCP servers, the apps connector (which can search the web), browser, computer use,
  plugins or hooks.
- **macOS.** Codex runs commands in zsh, which puts here-document files at `$TMPPREFIX` (`/tmp/zsh`), not under
  `TMPDIR`; Ask points it into the scratch folder, so `python3 - <<'EOF'` works in the sandbox. macOS Terminal's
  per-window histories (`~/.zsh_sessions`, `~/.bash_sessions`) are denied like `~/.zsh_history`. The real-Codex
  sandbox test runs on macOS too.
- **Answers keep the reviewer's language** even when the evidence is in another one.
- **A link covers later versions.** A version published after `agentledger link`, or from outside a git repo, now uses
  the nearest version's checkout, so Ask still runs in (and reads) the linked repository instead of the store.
- **One review session per version.** The first Ask on a version forks the session that wrote it (or starts fresh),
  and every later question on that version, in any thread, continues that review session instead of forking again.
  Questions on one version are answered one at a time, so two never continue the session at once.
- **Open the session behind an answer.** Ask's Claude Code sessions are named `AgentLedger: <report> v<N>` and
  appear in `claude --resume` and the IDE's session history. Claude Code lists no `claude -p` session (entrypoint
  `sdk-cli`), so Ask records its own entrypoint, `agentledger-ask`; `AGENTLEDGER_ASK_ENTRYPOINT=sdk-cli` restores the
  old behaviour.

## 0.3.0

- **Share one report.** `agentledger share <report>` packs a report (every version, sub-reports, traces, threads)
  into a `.agentledger.zip`; nothing else from the store travels. The recipient runs `agentledger open <file>`,
  comments, and asks their own Claude Code or Codex, which answers from the shared traces. Their feedback returns as
  a CSV for `agentledger comments import`; opening a newer bundle keeps their comments. Bundles are unpacked with
  path checks and always rendered sanitised.
- `agentledger link <report> <checkout>` lets Ask read a local checkout of the code a report describes.

## 0.2.0

- **Ask the session that wrote it.** `publish --trace auto` records the Claude Code / Codex session; Ask resumes it
  read-only (Claude Code as a fork per review thread, Codex in place), follow-ups continue the thread's session, and a
  fresh read-only run answers from the trace when the session is not on this machine.
- **Shared mode.** `serve --share` lets others on your network read and comment; Ask stays local-only. Shared mode
  accepts IP addresses and this machine's names, blocking DNS rebinding.
- **One-command setup.** `install.sh`; `agentledger setup` creates a private store when you have none and installs the
  skill for Claude Code and Codex; `agentledger open`, `agentledger demo`, and a status screen for bare `agentledger`.
- **Claude Code plugin.** The repo is a plugin marketplace: the skill plus `/agentledger:report`, `:review`, `:open`.
- Logo, favicon, and a README with screenshots, a diagram and a demo GIF.

## 0.1.0

- Modular, versioned reports in a fixed store layout; self-contained report pages and a dashboard.
- Select-to-comment review panel with CSV export/import, save to the store or GitHub, copy for agent.
- Timeline rail, navigator pad with minimap and version chain, project map.
- Ask with Claude Code / Codex; trace capture on publish.
