"""``agentledger serve``: a local static server that also accepts comment saves.

Pages served from it detect ``/__agentledger/ping`` and switch their Save button
to write straight into the store's ``comments.csv`` files, then the store is
rebuilt.  Handy when the agent runs on a remote host: forward the port over SSH
and review there, and the agent sees comments the moment they are saved.

Only same-origin requests carrying ``X-AgentLedger: 1`` from an allowed Host are
accepted, which blocks cross-site form posts and DNS-rebinding.
"""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import __version__
from .build import build
from .store import LedgerError, Store

MAX_BODY = 4 * 1024 * 1024
MAX_FIELD = 50_000


class LedgerHandler(SimpleHTTPRequestHandler):
    server_version = f"agentledger/{__version__}"

    def __init__(self, *args, store: Store, hosts: set[str], lock: threading.Lock, quiet: bool, **kwargs):
        self.store = store
        self.hosts = hosts
        self.lock = lock
        self.quiet = quiet
        super().__init__(*args, directory=str(store.root), **kwargs)

    def log_message(self, fmt, *args):
        if not self.quiet:
            super().log_message(fmt, *args)

    def end_headers(self):
        path = urlsplit(self.path).path
        if path.endswith((".html", ".csv", ".json", "/")):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if name.startswith("[") and "]" in name:
            name = name[: name.index("]") + 1]
        return name in self.hosts

    def do_GET(self):
        if urlsplit(self.path).path == "/__agentledger/ping":
            if not self._host_ok():
                return self._json(403, {"ok": False, "error": "host not allowed"})
            return self._json(200, {"ok": True, "agentledger": __version__, "store": self.store.name})
        return super().do_GET()

    def do_POST(self):
        if urlsplit(self.path).path != "/__agentledger/comments":
            return self._json(404, {"ok": False, "error": "not found"})
        if not self._host_ok():
            return self._json(403, {"ok": False, "error": "host not allowed"})
        if self.headers.get("X-AgentLedger") != "1":
            return self._json(403, {"ok": False, "error": "missing X-AgentLedger header"})
        origin = self.headers.get("Origin")
        if origin and origin != "null" and urlsplit(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            return self._json(403, {"ok": False, "error": "cross-origin request refused"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self._json(413, {"ok": False, "error": "empty or oversized body"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            rows = payload["rows"]
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ValueError("rows must be a list of objects")
            for r in rows:
                for k, v in r.items():
                    if not isinstance(v, (str, int, float, bool)) and v is not None:
                        raise ValueError(f"field {k} must be a scalar")
                    if isinstance(v, str) and len(v) > MAX_FIELD:
                        raise ValueError(f"field {k} is too long")
        except (ValueError, KeyError, UnicodeDecodeError) as exc:
            return self._json(400, {"ok": False, "error": f"bad request: {exc}"})
        with self.lock:
            try:
                self.store.invalidate()
                changed = self.store.save_rows(rows)
                if changed:
                    build(self.store)
                touched = {r.get("report") for r in rows}
                merged = [row for row in self.store.comments() if row["report"] in touched]
            except LedgerError as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
        if not self.quiet:
            self.log_message("saved %d comment row(s), %d changed", len(rows), changed)
        return self._json(200, {"ok": True, "changed": changed, "rows": merged})


def serve(store: Store, host: str = "127.0.0.1", port: int = 8765, extra_hosts: tuple[str, ...] = (), quiet: bool = False):
    hosts = {"localhost", "127.0.0.1", "[::1]", *(h.lower() for h in extra_hosts)}
    if host not in ("0.0.0.0", "::", ""):
        hosts.add(host.lower() if ":" not in host else f"[{host.lower()}]")
    handler = partial(LedgerHandler, store=store, hosts=hosts, lock=threading.Lock(), quiet=quiet)
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd
