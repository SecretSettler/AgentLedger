#!/bin/sh
# AgentLedger one-line install:
#
#   curl -fsSL https://raw.githubusercontent.com/SecretSettler/AgentLedger/main/install.sh | sh
#
# 1. installs the `agentledger` CLI (pipx, else uv, else pip --user)
# 2. runs `agentledger setup`: links your private store (or creates ~/agentledger-store)
#    and installs the skill for Claude Code (~/.claude/skills) and Codex (~/.agents/skills)
#
# Use an existing clone of your store:   ... | sh -s -- ~/path/to/your-store
set -eu

SRC="${AGENTLEDGER_SRC:-git+https://github.com/SecretSettler/AgentLedger}"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf 'agentledger install: %s\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "Python 3.9+ is required (python3 not found)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9+ is required"

if command -v pipx >/dev/null 2>&1; then
  say "Installing agentledger with pipx"
  pipx install --force "$SRC"
elif command -v uv >/dev/null 2>&1; then
  say "Installing agentledger with uv"
  uv tool install --force "$SRC"
else
  say "Installing agentledger with pip --user"
  python3 -m pip install --user --upgrade "$SRC" ||
    die "pip refused (an externally managed Python?). Install pipx (https://pipx.pypa.io) and run this again."
fi

BIN=agentledger
if ! command -v agentledger >/dev/null 2>&1; then
  for d in "$HOME/.local/bin" "$(python3 -m site --user-base 2>/dev/null)/bin"; do
    [ -x "$d/agentledger" ] && BIN="$d/agentledger" && break
  done
  [ "$BIN" = agentledger ] && die "installed, but agentledger is not on PATH; add ~/.local/bin to PATH and run: agentledger setup"
  say "Note: add $(dirname "$BIN") to your PATH so Claude Code and Codex can run agentledger"
fi

say "Setting up your store and the Claude Code / Codex skill"
"$BIN" setup "$@"
