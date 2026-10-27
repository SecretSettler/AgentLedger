"""Review comments: the CSV schema shared by the browser, the CLI and agents.

Every report version folder may hold a ``comments.csv``.  A thread lives in the
folder of the version it was opened on; replies (``reply_to`` = root id) are
stored next to their root.  Rows merge by ``id`` and the newest ``updated``
wins, so CSVs exported from different browsers can be imported in any order.
Deletions are tombstones (``status=deleted``) so they survive a merge.
"""

from __future__ import annotations

import csv
import io
import random
import string
import time
from pathlib import Path

FIELDS = [
    "id", "reply_to", "report", "version", "module", "kind", "quote", "prefix", "suffix",
    "body", "author", "role", "status", "created", "updated", "resolved_in",
]
STATUSES = {"open", "resolved", "wontfix", "deleted"}
KINDS = {"question", "issue", "suggestion", "note", "praise"}


def new_id() -> str:
    def base36(n: int) -> str:
        chars = string.digits + string.ascii_lowercase
        out = ""
        while n:
            n, r = divmod(n, 36)
            out = chars[r] + out
        return out or "0"

    return "c_" + base36(int(time.time() * 1000)) + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


def _num(value: str) -> str:
    value = value.strip().lstrip("vV")
    return str(int(value)) if value.isdigit() else ""


def normalize(row: dict) -> dict:
    out = {f: ("" if row.get(f) is None else str(row.get(f))) for f in FIELDS}
    out["version"] = _num(out["version"])
    out["resolved_in"] = _num(out["resolved_in"])
    if not out["reply_to"] and not out["status"]:
        out["status"] = "open"
    if not out["updated"]:
        out["updated"] = out["created"]
    return out


def parse(text: str) -> list[dict]:
    text = text.lstrip("﻿")
    if not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for raw in reader:
        if not raw.get("id"):
            continue
        rows.append(normalize(raw))
    return rows


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return parse(path.read_text(encoding="utf-8"))


def dumps(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in sort_rows(rows):
        writer.writerow(normalize(row))
    return buf.getvalue()


def sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r.get("created", ""), r.get("id", "")))


def merge(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int]:
    """Merge rows by id; the most recently updated copy wins. Returns (rows, changed)."""
    by_id = {r["id"]: normalize(r) for r in existing}
    changed = 0
    for row in incoming:
        row = normalize(row)
        cur = by_id.get(row["id"])
        if cur is None or row["updated"] > cur["updated"]:
            if cur != row:
                changed += 1
            by_id[row["id"]] = row
    return sort_rows(list(by_id.values())), changed


def write(path: Path, rows: list[dict]) -> bool:
    """Write rows if the content changed. Returns True when the file was written."""
    text = dumps(rows)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def threads(rows: list[dict]) -> list[dict]:
    """Group rows into ``{"root": row, "replies": [rows]}``, skipping deleted roots."""
    roots = [r for r in rows if not r["reply_to"] and r["status"] != "deleted"]
    replies: dict[str, list[dict]] = {}
    for r in rows:
        if r["reply_to"] and r["status"] != "deleted":
            replies.setdefault(r["reply_to"], []).append(r)
    return [{"root": r, "replies": sort_rows(replies.get(r["id"], []))} for r in sort_rows(roots)]
