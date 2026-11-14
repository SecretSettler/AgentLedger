"""Where Ask may run programs: a sandbox that reads the machine but not its secrets, and writes one scratch folder.

Ask answers from the report, its traces and the repository it describes.  Most questions are settled by reading;
some need a computed value (a count, a ratio, a script re-run on the data).  When the agent's own sandbox is
available, Ask lets it run commands inside it:

* reads: everything except credentials and agent transcripts (``SECRET_PATHS``, plus ``.env`` files near the
  repository and the store) and the container sockets that would hand out root;
* writes: only the version's scratch folder (Claude Code's own temp folder, which it must be able to write, is
  moved inside it); the repository, the store and the rest of the machine are read-only;
* no network, and no web tools.

When the sandbox cannot start, Ask falls back to reading only.  ``AGENTLEDGER_ASK_SANDBOX=off`` (or
``serve --ask-read-only``) forces that.  Reading only still keeps the secrets unreadable: Claude Code through
Read deny rules, Codex through a read-only permissions profile.  Before each Codex run Ask checks, with
``codex debug prompt-input`` (no model call), that the profile it asked for is the one Codex resolved from all its
config layers; if not, Codex runs with ``--sandbox read-only``, which writes nothing but can read everything.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SANDBOX_ENV = "AGENTLEDGER_ASK_SANDBOX"
PROFILE = "agentledger-ask"        # the Codex permissions profile Ask defines on the command line
PROFILE_RO = "agentledger-ask-ro"  # the same without the scratch folder: nothing writable

# Relative to the home folder (globs allowed): credentials, and the agents' own state, whose transcripts, logs and
# shell snapshots can hold pasted keys.  All of ~/.claude is denied; Codex's sandbox runs its helper binaries out
# of ~/.codex, so only its state files are.
SECRET_PATHS = (
    ".ssh", ".gnupg", ".aws", ".azure", ".config/gcloud", ".kube", ".docker", ".config/gh", ".config/hub",
    ".git-credentials", ".config/git/credentials", ".netrc", ".npmrc", ".pypirc", ".cargo/credentials.toml",
    ".vault-token", ".password-store", ".local/share/keyrings", ".pgpass", ".my.cnf",
    ".bash_history", ".zsh_history", ".python_history", ".psql_history", ".mysql_history", ".lesshst",
    # macOS Terminal keeps one history file per window here, next to ~/.zsh_history (zsh) or for bash 3.2.
    ".zsh_sessions", ".bash_sessions",
    ".claude", ".claude.json", ".claude.json.backup",
    ".codex/auth.json", ".codex/sessions", ".codex/archived_sessions", ".codex/history.jsonl", ".codex/log",
    ".codex/shell_snapshots", ".codex/memories", ".codex/*.sqlite", ".codex/*.sqlite-wal", ".codex/*.sqlite-shm",
    ".pi/agent/auth.json", ".pi/agent/mcp.json", ".pi/agent/models.json", ".pi/agent/sessions",
    ".gemini/oauth_creds.json", ".gemini/google_accounts.json", ".gemini/mcp_config.json",
    ".gemini/antigravity", ".gemini/antigravity-cli", ".gemini/jetski",
)
# Folders an agent creates on its first run; made up front (when the agent is installed) so they are denied from it.
CREATE_FIRST = (".codex/sessions", ".pi/agent/sessions")
# Container and session sockets: connecting to the Docker socket is as good as root.
SOCKETS = ("/run/docker.sock", "/var/run/docker.sock", "/run/containerd", "/run/podman", "/run/user/{uid}")
ENV_FILES = (".env", ".env.*", "*/.env", "*/.env.*")


@dataclass
class Policy:
    """What one Ask run may touch."""

    scratch: Optional[Path]    # the only writable folder; None when the run only reads
    read_only: list[Path]      # writable by default (cwd, --add-dir) but kept read-only
    secrets: list[Path]        # never readable
    # folders the run may also change, e.g. the session copy `agentledger trace review` edits (Claude Code: with
    # the Edit tool, limited to them, and sandboxed Bash; Codex: writable in its profile)
    writes: list[Path] = field(default_factory=list)

    @property
    def writable(self) -> bool:
        return self.scratch is not None

    def summary(self) -> str:
        if self.writable:
            return f"sandbox: writes only {self.scratch}, no network"
        return "read-only: reads files (not credentials), runs nothing that writes"


def wanted() -> bool:
    return os.environ.get(SANDBOX_ENV, "").strip().lower() not in ("off", "0", "false", "no", "read-only")


# Claude Code gives sandboxed Bash its per-user temp folder, <CLAUDE_CODE_TMPDIR>/claude-<uid>, as TMPDIR and keeps it
# writable, but only while that path is at most 44 bytes (Unix socket paths are short). A longer one silently falls
# back to the shared /tmp/claude-<uid>, which every Claude Code session of the user writes to (Claude Code 2.1.312:
# `cfr=44` in its temp-folder code). So the scratch folder is short, <tmp>/al-<uid>/<8 hex>, Claude's temp folder
# inside it is "t", a long system temp folder is replaced by /tmp, and when the real (symlink-resolved: /tmp is
# /private/tmp on macOS) path does not fit, Ask reads only. The shared folder is also denied from writes, so a
# Claude Code that falls back anyway refuses the commands instead of writing there.
CLAUDE_TMP = "t"
MAX_CLAUDE_TMP = 44   # bytes, for <scratch>/t/claude-<uid>
TAG_LEN = 8


def _uid() -> str:
    return str(os.getuid()) if hasattr(os, "getuid") else "user"


def scratch_root() -> Path:
    """<tmp>/al-<uid>, or /tmp/al-<uid> when the system temp folder is too long for Claude Code's (see above)."""
    root = Path(tempfile.gettempdir()) / f"al-{_uid()}"
    if not _fits(root) and os.path.isdir("/tmp"):
        root = Path("/tmp") / f"al-{_uid()}"
    return root


def scratch_dir(store_root: Path, key: str) -> Path:
    """One scratch folder per report version and store, outside the repository and the store (``.key`` says whose)."""
    tag = hashlib.sha256(f"{store_root.resolve()}\n{key}".encode()).hexdigest()[:TAG_LEN]
    return scratch_root() / tag


def claude_tmp(scratch: Path) -> Path:
    return scratch / CLAUDE_TMP


def claude_uid_tmp(scratch: Path) -> Path:
    """The folder Claude Code hands sandboxed Bash as TMPDIR when ``claude_tmp(scratch)`` is its CLAUDE_CODE_TMPDIR."""
    return claude_tmp(scratch) / f"claude-{_uid()}"


def _fits(root: Path) -> bool:
    return len(str(claude_uid_tmp(root.resolve() / ("0" * TAG_LEN))).encode()) <= MAX_CLAUDE_TMP


def claude_paths_fit() -> bool:
    """Whether Claude Code's temp folder, <scratch>/t/claude-<uid>, is short enough that Claude Code uses it."""
    return _fits(scratch_root())


def shared_claude_tmp() -> list[Path]:
    """Claude Code's shared per-user temp folder, where it falls back when its own is too long (as written, and real)."""
    p = Path("/tmp") / f"claude-{_uid()}"
    return list(dict.fromkeys([p, p.resolve()]))


def _real(p: Path) -> Optional[Path]:
    try:
        return p.resolve() if p.exists() else None
    except OSError:
        return None


def secret_paths(roots: list[Path]) -> list[Path]:
    """Existing secret files and folders (resolved, so symlinked paths are denied where they really are)."""
    home = Path.home()
    uid = str(os.getuid()) if hasattr(os, "getuid") else ""
    # The agents' state lives in their configured home folders when env overrides are set.
    bases = {".claude": [home / ".claude"], ".codex": [home / ".codex"], ".gemini": [home / ".gemini"]}
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        bases[".claude"].append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser())
    if os.environ.get("CODEX_HOME"):
        bases[".codex"].append(Path(os.environ["CODEX_HOME"]).expanduser())
    for env in ("ANTIGRAVITY_HOME", "GEMINI_CLI_HOME"):
        if os.environ.get(env):
            bases[".gemini"].append(Path(os.environ[env]).expanduser())

    def places(rel: str) -> list[Path]:
        head, _, rest = rel.partition("/")
        if head in bases:
            return [b / rest if rest else b for b in bases[head]]
        return [home / rel]

    for rel in CREATE_FIRST:
        for p in places(rel):
            if p.parent.is_dir():
                try:
                    p.mkdir(mode=0o700, exist_ok=True)
                except OSError:
                    pass
    found: list[Path] = []
    for rel in SECRET_PATHS:
        for p in places(rel):
            found += sorted(p.parent.glob(p.name)) if "*" in p.name else [p]
    if os.environ.get("PI_CODING_AGENT_DIR"):
        pi_cfg = Path(os.environ["PI_CODING_AGENT_DIR"]).expanduser()
        if pi_cfg.is_dir():
            try:
                (pi_cfg / "sessions").mkdir(mode=0o700, exist_ok=True)
            except OSError:
                pass
        found += [pi_cfg / n for n in ("auth.json", "mcp.json", "models.json", "sessions")]
    if os.environ.get("PI_CODING_AGENT_SESSION_DIR"):
        found.append(Path(os.environ["PI_CODING_AGENT_SESSION_DIR"]).expanduser())
    found += [Path(s.format(uid=uid)) for s in SOCKETS]
    for root in roots:
        for pattern in ENV_FILES:
            found += [p for p in root.glob(pattern) if p.is_file()]
    out: list[Path] = []
    for p in found:
        r = _real(p)
        if r and r not in out:
            out.append(r)
    return out


def policy(store_root: Path, key: str, cwd: Path, repo: Optional[Path]) -> Policy:
    scratch = scratch_dir(store_root, key)
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        (scratch / ".key").write_text(f"{key}\n{store_root.resolve()}\n", encoding="utf-8")
    except OSError:
        pass
    roots = _roots(store_root, cwd, repo)
    return Policy(scratch.resolve(), roots, secret_paths(roots))


def readonly_policy(store_root: Path, cwd: Path, repo: Optional[Path]) -> Policy:
    """Reading only, with the same secrets unreadable."""
    roots = _roots(store_root, cwd, repo)
    return Policy(None, roots, secret_paths(roots))


def _roots(store_root: Path, cwd: Path, repo: Optional[Path]) -> list[Path]:
    return [r for r in dict.fromkeys([store_root.resolve(), cwd.resolve()] + ([repo.resolve()] if repo else []))]


# ------------------------------------------------------------------ Claude Code
def _rule(p: Path) -> str:
    """A Read permission rule for an absolute path (``//abs/path``), covering a folder's contents too."""
    s = "//" + str(p).lstrip("/")
    return f"Read({s}/**)" if p.is_dir() else f"Read({s})"


def claude_settings(pol: Policy) -> dict:
    """Settings for ``claude --settings``: Bash sandboxed, fail closed, no unsandboxed retries, secrets unreadable."""
    secrets = [str(p) for p in pol.secrets]
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "filesystem": {
                "allowWrite": [str(pol.scratch)] + [str(p) for p in pol.writes],
                # The shared Claude Code temp folder: other sessions' files (see shared_claude_tmp).
                "denyWrite": [str(p) for p in pol.read_only] + [str(p) for p in shared_claude_tmp()],
                "denyRead": secrets,
            },
            "network": {"allowedDomains": []},
        },
        # The sandbox covers Bash only; Read, Grep and Glob follow permission rules.
        "permissions": {"deny": [_rule(p) for p in pol.secrets], **({"allow": _edit_rules(pol)} if pol.writes else {})},
    }


def _edit_rules(pol: Policy) -> list[str]:
    """Edit permission rules for ``pol.writes``: the only files the Edit tool may change."""
    return [f"Edit(//{str(p).lstrip('/')}/**)" for p in pol.writes]


def _with_edit(argv: list[str], pol: Policy) -> list[str]:
    """Load and allow the Edit tool, limited to ``pol.writes`` (it is not covered by the Bash sandbox)."""
    if not pol.writes:
        return argv
    out, i = list(argv), 0
    tools = out.index("--tools") + 1
    while tools < len(out) and not out[tools].startswith("--"):
        tools += 1
    out[tools:tools] = ["Edit"]
    allowed = out.index("--allowedTools") + 1
    while allowed < len(out) and not out[allowed].startswith("--"):
        allowed += 1
    out[allowed:allowed] = _edit_rules(pol)
    d = out.index("--disallowedTools") + 1
    while d < len(out) and not out[d].startswith("--"):
        if out[d] == "Edit":
            del out[d]
            continue
        d += 1
    return out


def claude_readonly_argv(argv: list[str], pol: Policy) -> list[str]:
    """The read-only Claude argv (Read, Grep, Glob) plus Read deny rules, so even reading leaves secrets alone; with
    ``pol.writes``, also the Edit tool for those folders only."""
    settings = {"permissions": {"deny": [_rule(p) for p in pol.secrets],
                                **({"allow": _edit_rules(pol)} if pol.writes else {})}}
    return _with_edit(argv, pol) + (["--permission-mode", "dontAsk"] if pol.writes else []) + \
        ["--settings", json.dumps(settings)]


def claude_argv(argv: list[str], pol: Policy) -> list[str]:
    """Turn the read-only Claude argv into the sandboxed one: Bash allowed, edits and web still disallowed."""
    out, skip = [], False
    for a in argv:
        if a in ("--tools", "--allowedTools", "--disallowedTools"):
            skip = True
            continue
        if skip and not a.startswith("--"):
            continue
        skip = False
        out.append(a)
    return _with_edit(out + [
        "--tools", "Read", "Grep", "Glob", "Bash",
        "--allowedTools", "Read", "Grep", "Glob", "Bash",
        "--disallowedTools", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch",
        # dontAsk refuses whatever is not allowed (hosts, unsandboxed retries) instead of waiting on a prompt.
        "--permission-mode", "dontAsk",
        "--settings", json.dumps(claude_settings(pol)),
    ], pol)


# ------------------------------------------------------------------ Codex
def _toml_str(s: str) -> str:
    return json.dumps(s)  # a JSON string is a valid TOML basic string


def codex_profile(pol: Policy) -> str:
    return PROFILE if pol.writable else PROFILE_RO


def codex_overrides(pol: Policy) -> list[str]:
    """``-c`` overrides defining and selecting a permissions profile: read all but secrets, write only the scratch
    (or nothing, for a read-only policy), no network."""
    fs = {":root": "read", **({str(pol.scratch): "write"} if pol.writable else {}),
          **{str(p): "write" for p in pol.writes}, **{str(p): "deny" for p in pol.secrets}}
    table = "{" + ",".join(f"{_toml_str(k)}={_toml_str(v)}" for k, v in fs.items()) + "}"
    name = codex_profile(pol)
    return ["-c", f"permissions.{name}.filesystem={table}",
            "-c", f"permissions.{name}.network.enabled=false",
            "-c", f'default_permissions="{name}"']


def codex_argv(argv: list[str], pol: Policy) -> list[str]:
    """Replace ``--sandbox read-only`` with Ask's permissions profile (the two cannot be combined)."""
    out = []
    i = 0
    while i < len(argv):
        if argv[i] in ("--sandbox", "-s"):
            i += 2
            continue
        out.append(argv[i])
        i += 1
    at = out.index("exec") + 1 if "exec" in out else len(out)
    return out[:at] + codex_overrides(pol) + out[at:]


def codex_verified(cwd: Path, pol: Policy, flags: tuple[str, ...] = ()) -> bool:
    """Whether Codex, resolving every config layer it reads in ``cwd`` (system, user, project ``.codex/``) plus Ask's
    overrides, ends up with exactly Ask's profile: the scratch as the only writable root (or read-only), no network,
    and the secrets denied.  ``codex debug prompt-input`` renders the permissions text the model would be given,
    without calling the model.  Anything else, including a change in that text, counts as not verified."""
    try:
        r = subprocess.run(["codex", "debug", "prompt-input", *flags, *codex_overrides(pol), "."], cwd=str(cwd),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and profile_holds(r.stdout, pol)


def profile_holds(text: str, pol: Policy) -> bool:
    """Whether Codex's permissions text describes exactly Ask's policy ``pol``."""
    if "Network access is restricted" not in text or "danger-full-access" in text:
        return False
    if pol.secrets and "denies reading" not in text:
        return False
    if pol.writable or pol.writes:
        m = re.search(r"writable roots? (?:is|are) ((?:`[^`]+`(?:, )?)+)", text)
        roots = set(re.findall(r"`([^`]+)`", m.group(1))) if m else set()
        want = {str(p) for p in ([pol.scratch] if pol.writable else []) + list(pol.writes)}
        return roots == want
    return "`sandbox_mode` is `read-only`" in text


# ------------------------------------------------------------------ availability
_AVAILABLE: dict[str, bool] = {}
_AVAILABLE_LOCK = threading.Lock()


def _run_ok(argv: list[str], timeout: float = 30, cwd: Optional[Path] = None) -> bool:
    try:
        return subprocess.run(argv, cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _claude_ok() -> bool:
    """Claude Code sandboxes Bash with bubblewrap and socat on Linux and Seatbelt on macOS; not on Windows."""
    system = platform.system()
    if system == "Darwin":
        return shutil.which("sandbox-exec") is not None
    if system != "Linux" or not (shutil.which("bwrap") and shutil.which("socat")):
        return False
    return _run_ok(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-net", "true"])


def _codex_ok() -> bool:
    """Run Codex's own sandbox with Ask's profile and check it holds: the scratch is writable, its parent is not."""
    with tempfile.TemporaryDirectory(prefix="agentledger-probe-") as tmp:
        scratch = Path(tmp, "scratch")
        scratch.mkdir()
        outside = Path(tmp, "outside")
        pol = Policy(scratch.resolve(), [], [])
        probe = f"echo ok > {scratch / 'in'} && ! (echo no > {outside}) 2>/dev/null"
        argv = ["codex", "sandbox", *codex_overrides(pol), "-P", PROFILE, "-C", str(scratch), "--", "sh", "-c", probe]
        ok = _run_ok(argv, timeout=60)
        return ok and (scratch / "in").exists() and not outside.exists()


def _probed(kind: str) -> bool:
    """Whether ``kind``'s sandbox works on this machine; probed once per process."""
    with _AVAILABLE_LOCK:
        if kind not in _AVAILABLE:
            _AVAILABLE[kind] = shutil.which(kind) is not None and (_claude_ok() if kind == "claude" else _codex_ok())
        return _AVAILABLE[kind]


def available(kind: str) -> bool:
    """Whether ``kind`` (claude or codex) may run commands in its sandbox here (wanted, and the sandbox works)."""
    return kind in ("claude", "codex") and wanted() and _probed(kind)


# User settings that would widen Ask's Claude Code sandbox: arrays merge with --settings instead of being replaced.
_WIDENING = (("sandbox", "excludedCommands"), ("sandbox", "network", "allowedDomains"),
             ("sandbox", "network", "allowUnixSockets"), ("sandbox", "network", "allowAllUnixSockets"),
             ("sandbox", "filesystem", "allowWrite"), ("sandbox", "filesystem", "allowRead"),
             ("sandbox", "enableWeakerNestedSandbox"))


def claude_settings_widen() -> Optional[str]:
    """The first user setting that would widen Ask's sandbox (``sandbox.excludedCommands`` run outside it), if any."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    for f in (base / "settings.json", base / "settings.local.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
        except (OSError, ValueError):
            return f"{f} (unreadable)"
        for keys in _WIDENING:
            v = data
            for k in keys:
                v = v.get(k) if isinstance(v, dict) else None
            if v:
                return f"{f}: {'.'.join(keys)}"
    return None


def usable(kind: str, cwd: Path) -> bool:
    """Whether a run of ``kind`` in ``cwd`` may get Ask's sandbox (Codex runs are also verified, see ``choose``)."""
    if not available(kind):
        return False
    if kind == "claude":
        return claude_paths_fit() and claude_settings_widen() is None
    return True


def choose(kind: str, store_root: Path, key: str, cwd: Path, repo: Optional[Path],
           codex_flags: tuple[str, ...] = ()) -> Optional[Policy]:
    """The policy for one run of ``kind`` in ``cwd``: the sandbox if usable (and, for Codex, verified), else reading
    only with secrets unreadable, else None (Codex's plain ``--sandbox read-only``)."""
    if kind not in ("claude", "codex"):
        return None
    if usable(kind, cwd):
        pol = policy(store_root, key, cwd, repo)
        if kind == "claude" or codex_verified(cwd, pol, codex_flags):
            return pol
    ro = readonly_policy(store_root, cwd, repo)
    if kind == "claude":
        return ro
    if _probed("codex") and codex_verified(cwd, ro, codex_flags):
        return ro
    return None


def mark_unavailable(kind: str) -> None:
    """A sandboxed run failed to start: read-only from now on, in this process."""
    with _AVAILABLE_LOCK:
        _AVAILABLE[kind] = False


def looks_like_sandbox_failure(message: str) -> bool:
    m = message.lower()
    return any(w in m for w in ("sandbox", "bubblewrap", "bwrap", "landlock", "seccomp", "seatbelt"))
