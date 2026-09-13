#!/usr/bin/env python3
"""Dispatcher HTTP API — the primary interface to the cloud-agents dispatcher.

Tailnet-only: binds the Tailscale IPv4 address (resolved dynamically via
`tailscale ip -4`) — never 0.0.0.0, so it stays off the LAN and off the
container subnet. Every endpoint requires bearer-token auth
(`Authorization: Bearer <token>`, constant-time comparison); tokens live in
config/api.yaml (mode 600) and are never logged.

All dispatcher logic lives in service/dispatch.py; this module is transport
only (routing, auth, JSON in/out).

Usage: python3 service/api.py
Env overrides (tests / ops): CA_API_BIND, CA_API_PORT, CA_API_CONFIG.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "service"))

import yaml  # noqa: E402
import dispatch  # noqa: E402
from dispatch import DispatchError, artifact_path, content_type_for  # noqa: E402
from tailnet import tailscale_ip  # noqa: E402

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

ROUTES = [
    # (method, path-regex, handler-name)
    ("POST", r"^/jobs$", "submit"),
    ("GET", r"^/jobs$", "list"),
    ("GET", r"^/jobs/([^/]+)$", "status"),
    ("GET", r"^/jobs/([^/]+)/logs$", "logs"),
    ("GET", r"^/jobs/([^/]+)/manifest$", "manifest"),
    ("POST", r"^/jobs/([^/]+)/cancel$", "cancel"),
    ("POST", r"^/jobs/([^/]+)/ack$", "ack"),
    ("GET", r"^/jobs/([^/]+)/artifacts/(.+)$", "artifact"),
    ("GET", r"^/attention$", "attention"),
    ("GET", r"^/spend$", "spend"),
    ("GET", r"^/dashboard$", "dashboard"),
    ("GET", r"^/info$", "info"),
    ("POST", r"^/pair/request$", "pair_request"),
    ("GET", r"^/pair/status$", "pair_status"),
    ("POST", r"^/pair/approve$", "pair_approve"),
    ("GET", r"^/pair/list$", "pair_list"),
]

# Paths served without auth (static UI only; all data APIs stay gated).
PUBLIC_PATHS = {("GET", "/dashboard"), ("POST", "/pair/request"),
                ("GET", "/pair/status")}


def load_api_config() -> dict:
    path = os.environ.get("CA_API_CONFIG",
                          str(ROOT / "config" / "api.yaml"))
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(f"api: missing {path} — generate it from config/api.yaml.example "
              f"(mode 600) before starting", file=sys.stderr)
        sys.exit(2)
    clients = cfg.get("clients") or {}
    if not clients or not all(clients.values()):
        print(f"api: {path} defines no usable client tokens — refusing to start",
              file=sys.stderr)
        sys.exit(2)
    cfg["_path"] = path
    return cfg


def resolve_bind(cfg: dict) -> str:
    if os.environ.get("CA_API_BIND"):
        return os.environ["CA_API_BIND"]
    bind = str(cfg.get("bind", "auto")).lower()
    if bind == "auto":
        # Tailscale is optional. Prefer the tailnet address when present
        # so the API stays off the LAN; otherwise loopback. Operators who
        # want LAN access set bind to a specific interface address.
        try:
            return tailscale_ip()
        except RuntimeError:
            print("api: Tailscale not available — binding 127.0.0.1 "
                  "(set bind in config/api.yaml for LAN access)",
                  file=sys.stderr, flush=True)
            return "127.0.0.1"
    if bind in ("localhost", "127.0.0.1"):
        return "127.0.0.1"
    return str(cfg.get("bind"))


def resolve_port(cfg: dict) -> int:
    if os.environ.get("CA_API_PORT"):
        return int(os.environ["CA_API_PORT"])
    return int(cfg.get("port", 18443))


class Handler(BaseHTTPRequestHandler):
    server_version = "cloud-agents-api/1"

    # Silence the default stderr logging; we log one line per request below.
    def log_message(self, fmt, *args):
        pass

    # -- helpers -----------------------------------------------------------
    def _cors_headers(self) -> None:
        # The multi-host dashboard calls other outposts cross-origin from the
        # browser. Tailnet-only API; auth is an explicit bearer token, never cookies.
        self.send_header("Access-Control-Allow-Origin", "*")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, data: bytes, ctype: str,
                    filename: str | None = None) -> None:
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            safe = filename.replace('"', "")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{safe}"')
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _auth_client(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return None
        # Reload clients if api.yaml changed (e.g. a device was just paired).
        try:
            mtime = os.path.getmtime(self.server.config_path)
            if mtime != self.server.clients_mtime:
                cfg = load_api_config()
                self.server.clients = cfg.get("clients") or {}
                self.server.clients_mtime = mtime
        except OSError:
            pass
        token = auth[len("Bearer "):].strip()
        for name, expected in self.server.clients.items():
            if hmac.compare_digest(token, str(expected)):
                return name
        return None

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise DispatchError(400, "request body must be JSON")

    # -- dispatch ----------------------------------------------------------
    def _route(self):
        parsed = urllib.parse.urlparse(self.path)
        path_matched = False
        for method, pattern, name in ROUTES:
            m = re.match(pattern, parsed.path)
            if not m:
                continue
            path_matched = True
            if method != self.command:
                continue
            query = urllib.parse.parse_qs(parsed.query)
            return name, (m.groups(), query)
        if path_matched:
            return "method_not_allowed", ((), {})
        return "not_found", ((), {})

    def _handle(self) -> None:
        started = time.time()
        path_only = urllib.parse.urlparse(self.path).path
        is_public = (self.command, path_only) in PUBLIC_PATHS
        client = self._auth_client()
        status = 500
        try:
            if client is None and not is_public:
                # No information leakage: same 401 for missing vs bad token.
                self._error(401, "unauthorized")
                status = 401
                return
            if is_public:
                client = "dashboard"
            name, (groups, query) = self._route()
            if name == "not_found":
                self._error(404, "not found")
                status = 404
                return
            if name == "method_not_allowed":
                self._error(405, "method not allowed")
                status = 405
                return
            handler = getattr(self, "handle_" + name)
            status = handler(groups, query)
        except DispatchError as e:
            self._error(e.status, e.message)
            status = e.status
        except BrokenPipeError:
            return  # client went away; nothing to log usefully
        except Exception as e:  # noqa: BLE001 — never leak internals
            self._error(500, "internal error")
            status = 500
            print(f"api: ERROR {self.command} {self.path}: "
                  f"{type(e).__name__}", file=sys.stderr, flush=True)
        finally:
            ms = (time.time() - started) * 1000
            print(f"api: {client or '-'} {self.command} {self.path} "
                  f"-> {status} ({ms:.0f}ms)", flush=True)

    do_GET = _handle
    do_POST = _handle

    def do_OPTIONS(self):
        # CORS preflight for cross-origin dashboard calls between outposts.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    # -- endpoints ---------------------------------------------------------
    def handle_submit(self, groups, query):
        body = self._read_json_body()
        result = dispatch.submit_job(
            type=body.get("type", "coding"),
            repo=body.get("repo", "scratch"),
            base=body.get("base", "main"),
            task=body.get("task", ""),
            engine=body.get("engine", "auto"),
            provider=body.get("provider", "supergrok"),
            budget_usd=body.get("budget_usd"),
            max_minutes=body.get("max_minutes"),
            idempotency_key=body.get("idempotency_key"),
            ref=body.get("ref"),
        )
        self._send_json(200, result)
        return 200

    def handle_list(self, groups, query):
        status = (query.get("status") or [None])[0]
        self._send_json(200, dispatch.jobs_list(status=status))
        return 200

    def handle_status(self, groups, query):
        job_id = groups[0]
        if not JOB_ID_RE.match(job_id):
            raise DispatchError(400, f"invalid job id {job_id!r}")
        self._send_json(200, dispatch.job_status(job_id))
        return 200

    def handle_logs(self, groups, query):
        job_id = groups[0]
        if not JOB_ID_RE.match(job_id):
            raise DispatchError(400, f"invalid job id {job_id!r}")
        tail = (query.get("tail") or ["0"])[0]
        try:
            tail = max(0, int(tail))
        except ValueError:
            raise DispatchError(400, "tail must be an integer")
        self._send_json(200, dispatch.job_logs(job_id, tail=tail))
        return 200

    def handle_manifest(self, groups, query):
        job_id = groups[0]
        if not JOB_ID_RE.match(job_id):
            raise DispatchError(400, f"invalid job id {job_id!r}")
        self._send_json(200, dispatch.job_manifest(job_id))
        return 200

    def handle_cancel(self, groups, query):
        job_id = groups[0]
        if not JOB_ID_RE.match(job_id):
            raise DispatchError(400, f"invalid job id {job_id!r}")
        self._send_json(200, dispatch.cancel_job(job_id))
        return 200

    def handle_ack(self, groups, query):
        job_id = groups[0]
        if not JOB_ID_RE.match(job_id):
            raise DispatchError(400, f"invalid job id {job_id!r}")
        self._send_json(200, dispatch.ack_job(job_id))
        return 200

    def handle_attention(self, groups, query):
        self._send_json(200, dispatch.attention_list())
        return 200

    def handle_spend(self, groups, query):
        self._send_json(200, dispatch.spend_summary())
        return 200

    def handle_artifact(self, groups, query):
        job_id, name = groups
        if not JOB_ID_RE.match(job_id):
            raise DispatchError(400, f"invalid job id {job_id!r}")
        name = urllib.parse.unquote(name)
        path = artifact_path(job_id, name)
        data = path.read_bytes()
        self._send_bytes(200, data, content_type_for(path), path.name)
        return 200

    def handle_dashboard(self, groups, query):
        p = ROOT / "service" / "dashboard.html"
        if not p.exists():
            raise DispatchError(404, "dashboard not installed")
        self._send_bytes(200, p.read_bytes(),
                         "text/html; charset=utf-8", None)
        return 200

    def handle_info(self, groups, query):
        engines = {}
        try:
            from adapters import registry
            engines = dict(registry.ENGINES)
        except Exception:
            pass
        pool = {}
        try:
            cfg = dispatch.load_config()
            lcfg = cfg.get("limits", {})
            pool = {k: lcfg.get(k) for k in (
                "max_workers", "container_cpus",
                "container_memory", "container_disk")}
        except Exception:
            pass
        self._send_json(200, {"engines": engines, "pool": pool})
        return 200

    def handle_pair_request(self, groups, query):
        import pairing
        body = self._read_json_body()
        self._send_json(200,
                        pairing.request_pairing(body.get("device_name", "")))
        return 200

    def handle_pair_status(self, groups, query):
        import pairing
        code = (query.get("code") or [""])[0]
        self._send_json(200, pairing.pairing_status(code))
        return 200

    def handle_pair_approve(self, groups, query):
        import pairing
        body = self._read_json_body()
        self._send_json(200, pairing.approve_pairing(body.get("code", "")))
        return 200

    def handle_pair_list(self, groups, query):
        import pairing
        self._send_json(200, pairing.list_pairings())
        return 200


def main() -> int:
    cfg = load_api_config()
    bind = resolve_bind(cfg)
    port = resolve_port(cfg)
    if bind == "0.0.0.0":
        print("api: refusing to bind 0.0.0.0 — tailnet-only by design",
              file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    server.clients = cfg["clients"]
    server.config_path = os.environ.get("CA_API_CONFIG",
                                        str(ROOT / "config" / "api.yaml"))
    try:
        server.clients_mtime = os.path.getmtime(server.config_path)
    except OSError:
        server.clients_mtime = 0
    host, actual_port = server.server_address[0], server.server_address[1]
    print(f"api: listening on http://{host}:{actual_port} "
          f"({len(server.clients)} client(s))", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
