"""``agentledger serve``: a local static server that also accepts comment saves.

Pages served from it detect ``/__agentledger/ping`` and switch their Save button
to write straight into the store's ``comments.csv`` files, then the store is
rebuilt.  Handy when the agent runs on a remote host: forward the port over SSH
and review there, and the agent sees comments the moment they are saved.

With an ask backend (``--ask``), pages can also send a review thread to the local
agent CLI (``POST /__agentledger/ask``) and poll for its answer.

Only same-origin requests carrying ``X-AgentLedger: 1`` from an allowed Host are
accepted, which blocks cross-site form posts and DNS-rebinding.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
from functools import partial
from typing import Optional
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import __version__
from .ask import AskService, Backend
from .build import build
from .store import LedgerError, Store

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "[::1]"}
MAX_BODY = 4 * 1024 * 1024


def machine_names() -> set[str]:
    names = {socket.gethostname().lower(), socket.getfqdn().lower()}
    return names | {n.split(".")[0] for n in names} | {n + ".local" for n in names if "." not in n}
MAX_FIELD = 50_000


class LedgerHandler(SimpleHTTPRequestHandler):
    server_version = f"agentledger/{__version__}"

    def __init__(self, *args, store: Store, hosts: set[str], lock: threading.Lock, quiet: bool,
                 ask: Optional[AskService] = None, share: bool = False, **kwargs):
        self.store = store
        self.hosts = hosts
        self.share = share
        self.lock = lock
        self.quiet = quiet
        self.ask = ask
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

    def _local_client(self) -> bool:
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return ip.is_loopback or bool(getattr(ip, "ipv4_mapped", None) and ip.ipv4_mapped.is_loopback)

    def _ask(self) -> Optional[AskService]:
        """Ask runs the owner's agent: only for this machine (or an SSH tunnel to it), addressed as localhost."""
        if not self.ask or not self._local_client() or self._host_name() not in LOOPBACK_NAMES:
            return None
        return self.ask

    def _host_name(self) -> str:
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if name.startswith("[") and "]" in name:
            name = name[: name.index("]") + 1]
        return name

    def _host_ok(self) -> bool:
        name = self._host_name()
        if name in self.hosts:
            return True
        if self.share:
            # Teammates reach a shared server by IP or by this machine's name; other names could be DNS rebinding.
            try:
                ipaddress.ip_address(name.strip("[]"))
                return True
            except ValueError:
                return name in machine_names()
        return False

    def _guard(self) -> bool:
        """Same-origin requests only: allowed Host, custom header, matching Origin."""
        if not self._host_ok():
            self._json(403, {"ok": False, "error": "host not allowed"})
            return False
        if self.headers.get("X-AgentLedger") != "1":
            self._json(403, {"ok": False, "error": "missing X-AgentLedger header"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "null" and urlsplit(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
            self._json(403, {"ok": False, "error": "cross-origin request refused"})
            return False
        return True

    def _payload(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(413, {"ok": False, "error": "empty or oversized body"})
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            rows = payload.get("rows", [])
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ValueError("rows must be a list of objects")
            for r in rows:
                for k, v in r.items():
                    if not isinstance(v, (str, int, float, bool)) and v is not None:
                        raise ValueError(f"field {k} must be a scalar")
                    if isinstance(v, str) and len(v) > MAX_FIELD:
                        raise ValueError(f"field {k} is too long")
        except (ValueError, AttributeError, UnicodeDecodeError) as exc:
            self._json(400, {"ok": False, "error": f"bad request: {exc}"})
            return None
        return payload

    def _save(self, rows: list[dict]) -> tuple[int, list[dict]]:
        with self.lock:
            self.store.invalidate()
            changed = self.store.save_rows(rows) if rows else 0
            if changed:
                build(self.store)
            touched = {r.get("report") for r in rows}
            return changed, [row for row in self.store.comments() if row["report"] in touched]

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/__agentledger/ping":
            if not self._host_ok():
                return self._json(403, {"ok": False, "error": "host not allowed"})
            ask = self._ask()
            return self._json(200, {"ok": True, "agentledger": __version__, "store": self.store.name,
                                    "ask": ask.name if ask else None, "shared": self.share,
                                    "local": self._local_client()})
        if path.startswith("/__agentledger/ask/"):
            if not self._guard():
                return None
            job = self._ask().get(path.rsplit("/", 1)[-1]) if self._ask() else None
            return self._json(200, {"ok": True, **job}) if job else self._json(404, {"ok": False, "error": "no such job"})
        return super().do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/__agentledger/comments", "/__agentledger/ask"):
            return self._json(404, {"ok": False, "error": "not found"})
        if not self._guard():
            return None
        if path == "/__agentledger/ask" and self.ask and not self._ask():
            return self._json(403, {"ok": False, "error": "Ask only works on the machine running agentledger (open it as localhost); shared viewers can comment"})
        payload = self._payload()
        if payload is None:
            return None
        rows = payload.get("rows", [])
        try:
            changed, merged = self._save(rows)
            if path == "/__agentledger/comments":
                if not self.quiet:
                    self.log_message("saved %d comment row(s), %d changed", len(rows), changed)
                return self._json(200, {"ok": True, "changed": changed, "rows": merged})
            if not self.ask:
                return self._json(409, {"ok": False, "error": "asking is off: restart with `agentledger serve --ask claude` (or codex)"})
            thread = str(payload.get("thread") or "")
            job = self.ask.start(thread)
            if not self.quiet:
                self.log_message("ask on %s (job %s)", thread, job.id)
            return self._json(200, {"ok": True, "job": job.id, "backend": job.backend, "rows": merged})
        except LedgerError as exc:
            return self._json(400, {"ok": False, "error": str(exc)})


def serve(store: Store, host: str = "127.0.0.1", port: int = 8765, extra_hosts: tuple[str, ...] = (), quiet: bool = False,
          backend: Optional[Backend] = None, ask_timeout: float = 900, share: bool = False, ask_choice: str = "auto"):
    """``share=True`` lets anyone who can reach the port read and comment; Ask stays local."""
    hosts = {*LOOPBACK_NAMES, *(h.lower() for h in extra_hosts)}
    if host not in ("0.0.0.0", "::", ""):
        hosts.add(host.lower() if ":" not in host else f"[{host.lower()}]")
    lock = threading.Lock()
    ask = AskService(store, backend, lock, timeout=ask_timeout, choice=ask_choice) if backend else None
    handler = partial(LedgerHandler, store=store, hosts=hosts, lock=lock, quiet=quiet, ask=ask, share=share)
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd
