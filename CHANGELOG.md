# Changelog

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
