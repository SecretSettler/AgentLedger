"""Make an agent session safe to share: keep the conversation, drop the rest, and redact secrets.

A Claude Code or Codex session file holds far more than the conversation: system prompts and skill text, files
attached with ``@``, file-history snapshots, raw API request copies, reasoning, account and organisation ids.  And
the conversation itself can hold secrets the agent read or was shown.  ``sanitize_session`` copies a session keeping
only what a reviewer needs (an allow-list):

* Claude Code: ``user`` messages that are not meta (text and tool results) and ``assistant`` messages (text and tool
  calls; no thinking).  ``<system-reminder>`` blocks are removed from every text.
* Codex: the session id and working directory, ``user`` and ``assistant`` messages (minus the environment and
  instruction blocks Codex injects), tool calls and their outputs, compaction summaries (no reasoning, no developer
  messages, no account ids).

Every string kept goes through ``Scrubber``: the values of known secrets (``.env`` files near the work, and
environment variables whose names say KEY, TOKEN, SECRET, PASSWORD...) are replaced by ``[REDACTED:<NAME>]``, then
common credential formats by ``[REDACTED:<kind>]``.  ``scan_file`` checks any file for what is left.

This is the program's half of the review; the agent that prepared the copy reviews the rest (personal data,
other people's data, work unrelated to the report) before ``agentledger trace approve``.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

# Credential formats, most specific first.  Each match is replaced by [REDACTED:<name>].
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S)),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer-token", re.compile(r"(?i)(?<=bearer )[A-Za-z0-9_\-.=]{24,}")),
    # (?P<v>...) marks the part replaced; the rest of the match is kept
    ("url-credentials", re.compile(r"://(?P<v>[^/\s:@\"']+:[^/\s@\"']{3,})@")),
    # account and organisation ids (Claude Code's session-owner, Codex's creator ids), wherever they are quoted
    ("account-id", re.compile(r"(?i)(?:account|organization|organisation|org|creator_user|creator_account|user)"
                              r"(?:_?uuid|_?id)[\"']?\s*[:=]\s*[\"']?"
                              r"(?P<v>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")),
    # name = value assignments: KEY=..., "api_key": "...", password: ...
    ("assigned-secret", re.compile(
        r"(?i)(?<![A-Za-z0-9])((?:[A-Za-z0-9_]*?)(?:api[_-]?key|secret|token|passw(?:or)?d|credential)[A-Za-z0-9_]*)"
        r"(\"?\s*[:=]\s*\"?)([A-Za-z0-9_\-./+=]{12,})")),
]
SECRET_NAME = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|PRIVATE)")
NOT_SECRET_NAME = re.compile(r"(?i)(_PATH|_FILE|_DIR|_URL|_HOST|_PORT|_ID|KEYBOARD|_SOCK)$")
ENV_FILES = (".env", ".env.*", "*/.env", "*/.env.*", "*/*/.env", "*/*/.env.*")
MIN_SECRET = 8
REMINDER = re.compile(r"\s*<system-reminder>.*?</system-reminder>\s*", re.S)
# Blocks Codex injects into user messages (environment, instructions), and its developer messages.
CODEX_INJECTED = re.compile(r"^\s*<(environment_context|user_instructions|skills_instructions|permissions instructions|"
                            r"multi_agent_[a-z_]+|collaboration_mode|turn_aborted|subagent_notification)\b")


def _secret_like(value: str) -> bool:
    """A value that could be a credential: letters and digits mixed, or long; not a word, a count or code."""
    has_letter, has_digit = re.search(r"[A-Za-z]", value), re.search(r"[0-9]", value)
    return bool((has_letter and has_digit) or (len(value) >= 32 and has_letter))


def _entropy(value: str) -> float:
    from math import log2
    counts = Counter(value)
    return -sum(n / len(value) * log2(n / len(value)) for n in counts.values())


def _looks_random(value: str) -> bool:
    """A generated secret: 20+ characters of a token alphabet, letters and digits mixed, high entropy."""
    return (len(value) >= 20 and re.fullmatch(r"[A-Za-z0-9_\-+=/.~]+", value) is not None
            and _secret_like(value) and _entropy(value) >= 3.5)


def _env_file_values(path: Path) -> dict[str, str]:
    out = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if value[:1] in "\"'" and value[-1:] == value[:1]:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        name = m.group(1)
        # a .env file also holds plain settings (hosts, time zones, image names): only credential-named or
        # random-looking values count as secrets
        if len(value) >= MIN_SECRET and ((SECRET_NAME.search(name) and not NOT_SECRET_NAME.search(name))
                                         or _looks_random(value)):
            out[value] = name
    return out


def known_secrets(roots: Iterable[Path], environ: Optional[dict] = None) -> dict[str, str]:
    """Secret values to redact, mapped to the name they are known by: values in ``.env`` files within three levels of
    ``roots`` and in the environment whose names look like credentials, or that look random."""
    found: dict[str, str] = {}
    for root in dict.fromkeys(Path(r) for r in roots):
        if not root.is_dir():
            continue
        for pattern in ENV_FILES:
            for f in root.glob(pattern):
                if f.is_file():
                    found.update({v: n for v, n in _env_file_values(f).items() if v not in found})
    for name, value in (environ if environ is not None else os.environ).items():
        if (SECRET_NAME.search(name) and not NOT_SECRET_NAME.search(name) and len(value) >= MIN_SECRET
                and not value.startswith(("/", "~")) and value not in found):
            found[value] = name
    return found


# not after a backslash: in JSON-escaped code, `\n@pytest.fixture` is a decorator on a new line, not an address
EMAIL = re.compile(r"\b(?<!\\)[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")


class Scrubber:
    """Replace known secret values and credential formats in strings; count what was replaced (never the values),
    and what a reviewer should look at but the program does not redact (``hints``: e-mail addresses)."""

    def __init__(self, secrets: dict[str, str]):
        self.secrets = sorted(secrets.items(), key=lambda kv: -len(kv[0]))  # longest first: no partial overlaps
        self.counts: Counter = Counter()
        self.hints: Counter = Counter()

    def __call__(self, s: str) -> str:
        for value, name in self.secrets:
            if value in s:
                self.counts[f"known secret {name}"] += s.count(value)
                s = s.replace(value, f"[REDACTED:{name}]")
        for kind, pattern in PATTERNS:
            if kind == "assigned-secret":
                def sub(m):
                    value = m.group(3)
                    # a value, not code: `secrets = known_secrets(...)` or "input_tokens": 123456789012 are not secrets,
                    # nor is `n_tokens = schedule_v2_for_x1(` (a call)
                    if (value.startswith("[REDACTED") or not _secret_like(value)
                            or m.string[m.end():m.end() + 1] == "("):
                        return m.group(0)
                    self.counts[kind] += 1
                    return f"{m.group(1)}{m.group(2)}[REDACTED:{kind}]"
                s = pattern.sub(sub, s)
            elif "v" in pattern.groupindex:
                def part(m, kind=kind):
                    if m.group("v").startswith("[REDACTED"):   # already replaced: the marker itself has a colon
                        return m.group(0)
                    self.counts[kind] += 1
                    a, b = m.span("v")
                    return m.group(0)[:a - m.start()] + f"[REDACTED:{kind}]" + m.group(0)[b - m.start():]
                s = pattern.sub(part, s)
            else:
                s, n = pattern.subn(f"[REDACTED:{kind}]", s)
                if n:
                    self.counts[kind] += n
        emails = EMAIL.findall(s)
        if emails:
            self.hints["e-mail addresses"] += len(emails)
        return s

    def deep(self, value):
        """Scrub every string inside a JSON value."""
        if isinstance(value, str):
            return self(value)
        if isinstance(value, list):
            return [self.deep(v) for v in value]
        if isinstance(value, dict):
            return {k: self.deep(v) for k, v in value.items()}
        return value


def _text(s: str, counts: Counter) -> str:
    out, n = REMINDER.subn(" ", s)
    if n:
        counts["system-reminder blocks"] += n
    return out.strip() if n else out


# ------------------------------------------------------------------ Claude Code
def _claude_content(content, scrub: Scrubber, counts: Counter, role: str):
    if isinstance(content, str):
        return scrub(_text(content, counts))
    out = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            text = _text(part.get("text", ""), counts)
            if text.strip():
                out.append({"type": "text", "text": scrub(text)})
        elif kind == "tool_use" and role == "assistant":
            out.append({"type": "tool_use", "id": part.get("id"), "name": part.get("name"),
                        "input": scrub.deep(part.get("input") or {})})
        elif kind == "tool_result" and role == "user":
            body = part.get("content")
            if isinstance(body, list):
                body = [{"type": "text", "text": scrub(_text(b.get("text", ""), counts))}
                        for b in body if isinstance(b, dict) and b.get("type") == "text"]
            elif isinstance(body, str):
                body = scrub(_text(body, counts))
            out.append({"type": "tool_result", "tool_use_id": part.get("tool_use_id"), "content": body,
                        **({"is_error": True} if part.get("is_error") else {})})
        else:
            counts[f"dropped {role} {kind} blocks"] += 1  # thinking, images, documents
    return out


def _claude_record(ev: dict, scrub: Scrubber, counts: Counter) -> Optional[dict]:
    kind = ev.get("type")
    if kind not in ("user", "assistant") or ev.get("isMeta"):
        counts[f"dropped {kind}{' (meta)' if ev.get('isMeta') else ''} records"] += 1
        return None
    msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
    content = _claude_content(msg.get("content"), scrub, counts, kind)
    if not content:
        counts[f"dropped empty {kind} records"] += 1
        return None
    keep = {k: ev[k] for k in ("type", "uuid", "parentUuid", "sessionId", "timestamp", "cwd", "isCompactSummary")
            if k in ev}
    keep["message"] = {"role": msg.get("role", kind), "content": content}
    return keep


# ------------------------------------------------------------------ Codex
def _codex_record(ev: dict, scrub: Scrubber, counts: Counter) -> Optional[dict]:
    kind = ev.get("type")
    p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
    if kind == "session_meta":
        return {"type": kind, "timestamp": ev.get("timestamp"),
                "payload": {k: p[k] for k in ("id", "session_id", "cwd", "cli_version", "originator", "source")
                            if k in p}}
    if kind == "compacted":
        return {"type": kind, "timestamp": ev.get("timestamp"), "payload": {"message": scrub(str(p.get("message", "")))}}
    if kind != "response_item":
        counts[f"dropped {kind} records"] += 1
        return None
    item = p.get("type")
    if item == "message":
        if p.get("role") not in ("user", "assistant"):
            counts[f"dropped {p.get('role')} messages"] += 1
            return None
        blocks = []
        for b in p.get("content") or []:
            text = b.get("text", "") if isinstance(b, dict) else ""
            if CODEX_INJECTED.match(text):
                counts["dropped injected context blocks"] += 1
                continue
            if text.strip():
                blocks.append({"type": b.get("type"), "text": scrub(_text(text, counts))})
        if not blocks:
            return None
        return {"type": kind, "timestamp": ev.get("timestamp"),
                "payload": {"type": "message", "role": p["role"], "content": blocks}}
    if item in ("function_call", "custom_tool_call", "local_shell_call"):
        fields = ("name", "arguments", "input", "action", "call_id", "status")
    elif item in ("function_call_output", "custom_tool_call_output"):
        fields = ("call_id", "output")
    else:
        counts[f"dropped {item} items"] += 1  # reasoning, web search results...
        return None
    return {"type": kind, "timestamp": ev.get("timestamp"),
            "payload": {"type": item, **{k: scrub.deep(p[k]) for k in fields if k in p}}}


# ------------------------------------------------------------------ files
def sanitize_session(src: Path, dest: Path, agent: str, secrets: dict[str, str]) -> Counter:
    """Write the shareable copy of session ``src`` (agent ``claude`` or ``codex``) to ``dest``; return what was
    dropped and redacted, as counts."""
    scrub, counts = Scrubber(secrets), Counter()
    record = _claude_record if agent == "claude" else _codex_record
    kept = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with src.open(encoding="utf-8", errors="replace") as fin, dest.open("w", encoding="utf-8") as fout:
        for line in fin:
            try:
                ev = json.loads(line)
            except ValueError:
                counts["dropped unreadable lines"] += 1
                continue
            out = record(ev, scrub, counts) if isinstance(ev, dict) else None
            if out is not None:
                fout.write(json.dumps(out, ensure_ascii=False) + "\n")
                kept += 1
    counts["kept records"] = kept
    counts.update(scrub.counts)
    counts.update({f"to review: {k}": n for k, n in scrub.hints.items()})
    return counts


def scan_file(path: Path, secrets: dict[str, str]) -> Counter:
    """What ``Scrubber`` would still replace in ``path`` (known secrets and credential formats), as counts."""
    scrub = Scrubber(secrets)
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            scrub(line)
    return scrub.counts
