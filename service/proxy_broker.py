#!/usr/bin/env python3
"""Per-job HTTP-aware model broker with SuperGrok -> OpenRouter failover.

Network path (Tier 1): container -> this broker (0.0.0.0:<listen-port>,
reachable via the container gateway) -> `hermes proxy` on 127.0.0.1
(which attaches the real SuperGrok credential).

Unlike a blind TCP relay, this broker speaks HTTP/1.1 in both directions:

  * Auth: every request must carry `Authorization: Bearer <job-token>`
    (anything else gets a 401). The token grants nothing by itself.
  * 429 detection: upstream status codes are visible host-side. N
    consecutive 429s (default 3) or 429s persisting > window_seconds
    (default 60s) triggers a mid-flight failover to OpenRouter — the
    broker re-points its upstream and the in-container agent just sees its
    requests start succeeding again. No container changes, no restart.
  * Failover guards: the OpenRouter rolling-cap pre-check runs first; if
    the $25 cap is already hit the job is flagged needs_attention instead
    of failing over. One failover per job, no fail-back, no flapping.
  * Token telemetry: `usage` is extracted from every upstream response
    (plain JSON and SSE streams) and recorded per job for spend
    attribution.

On the OpenRouter path the broker attaches the real OpenRouter key
itself (read server-side only, never logged) and rewrites the request's
`model` field to the configured overflow model.

Usage:
  proxy_broker.py <listen-port> <supergrok-port> <bearer-token> <job-id>
                  <db-path> <spend-yaml-path> <log-path>
Runs until killed.

Test seams (env):
  CA_FO_ENABLED / CA_FO_CONSECUTIVE_429S / CA_FO_WINDOW_SECONDS
  CA_OR_HOST / CA_OR_PORT / CA_OR_TLS (0/1) — point the overflow upstream
      at a mock in tests
  CA_TELEMETRY_INJECT_STREAM_USAGE (0/1)
  (see also CA_OR_USAGE_MOCK / CA_OR_KEY_FILE in service/openrouter.py)
"""
from __future__ import annotations

import hmac
import http.client
import json
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import db as dbmod  # noqa: E402
import openrouter as or_provider  # noqa: E402
from redact import redact, register_secret  # noqa: E402

CLIENT_TIMEOUT = 900
UPSTREAM_TIMEOUT = 600
READ_CHUNK = 32768

# Hop-by-hop headers never forwarded between client and upstream.
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailer", "transfer-encoding",
               "upgrade", "content-length"}


# ---------------------------------------------------------------------------
# Pure, importable pieces (unit-tested).
# ---------------------------------------------------------------------------

def _try_json(payload):
    try:
        return json.loads(payload)
    except Exception:
        return None


def usage_of(obj) -> tuple[int, int] | None:
    """Extract (prompt_tokens, completion_tokens) from a decoded JSON body."""
    if not isinstance(obj, dict):
        return None
    u = obj.get("usage")
    if not isinstance(u, dict):
        return None
    try:
        p = int(u.get("prompt_tokens") or 0)
        c = int(u.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        return None
    return (p, c)


def parse_usage_from_json(data: bytes) -> tuple[int, int] | None:
    if not data:
        return None
    return usage_of(_try_json(data))


def parse_usage_from_sse(data: bytes) -> tuple[int, int] | None:
    """Extract usage from a Server-Sent-Events body.

    Providers put the cumulative `usage` object in the final data chunk
    (requires `stream_options.include_usage`, which the broker injects).
    The LAST chunk carrying usage wins.
    """
    last = None
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        u = usage_of(_try_json(payload))
        if u is not None:
            last = u
    return last


class FailoverTracker:
    """Counts upstream 429s and decides when the failover trigger fires.

    Trigger: `threshold` 429s with no successful response between them, all
    within the trailing `window` seconds (burst); OR at least 2 429s inside
    the trailing window while the current 429 episode (everything since the
    last non-429 response) spans longer than `window` (persistence — covers
    slow-drip rate limiting that never reaches the burst threshold). Any
    non-429 upstream response resets the episode.
    """

    def __init__(self, threshold: int, window: float):
        self.threshold = max(1, int(threshold))
        self.window = float(window)
        self.run: list[float] = []  # 429 timestamps since the last non-429

    def note_429(self, now: float) -> None:
        # Light pruning for memory; jobs are capped at 3h anyway.
        cut = now - 86400.0
        self.run = [t for t in self.run if t >= cut]
        self.run.append(now)

    def note_ok(self, now: float) -> None:
        self.run = []

    def triggered(self, now: float) -> tuple[bool, str]:
        w = self.window
        recent = [t for t in self.run if t >= now - w]
        n = len(recent)
        if n >= self.threshold:
            return True, (f"{n} consecutive upstream 429s within {w:g}s "
                          f"(threshold {self.threshold})")
        if n >= 2 and self.run and self.run[-1] - self.run[0] > w:
            span = self.run[-1] - self.run[0]
            return True, (f"upstream 429s persisting {span:.0f}s "
                          f"(> {w:g}s window)")
        return False, ""


# ---------------------------------------------------------------------------
# HTTP plumbing.
# ---------------------------------------------------------------------------

def _read_exact(rfile, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = rfile.read(n - len(buf))
        if not chunk:
            raise EOFError("short read")
        buf += chunk
    return buf


def _dechunk(rfile) -> bytes:
    out = []
    while True:
        line = rfile.readline(8192)
        if not line:
            raise EOFError("truncated chunk header")
        size = int(line.decode("latin1").split(";")[0].strip(), 16)
        if size == 0:
            rfile.readline()  # consume trailing CRLF (trailers ignored)
            break
        out.append(_read_exact(rfile, size))
        crlf = rfile.read(2)
        if crlf != b"\r\n":
            raise ValueError("bad chunk framing")
    return b"".join(out)


def read_request(rfile):
    """Read one HTTP/1.1 request. Returns (method, path, version, headers, body)
    where headers is a list of (name, value) preserving order/case, or None
    on clean EOF."""
    line = rfile.readline(8192)
    if not line:
        return None
    parts = line.decode("latin1").strip().split()
    if len(parts) != 3:
        raise ValueError(f"bad request line: {line!r}")
    method, path, version = parts
    headers = []
    while True:
        hline = rfile.readline(8192)
        if not hline:
            raise EOFError("truncated headers")
        hline = hline.decode("latin1")
        if hline in ("\r\n", "\n", ""):
            break
        name, _, value = hline.partition(":")
        headers.append((name.strip(), value.strip()))
    lower = {k.lower(): v for k, v in headers}
    body = b""
    if lower.get("transfer-encoding", "").lower() == "chunked":
        body = _dechunk(rfile)
    elif "content-length" in lower:
        try:
            body = _read_exact(rfile, int(lower["content-length"]))
        except (ValueError, EOFError):
            body = b""
    return method, path, version, headers, body


def header_get(headers, name: str, default=None):
    name = name.lower()
    for k, v in headers:
        if k.lower() == name:
            return v
    return default


# ---------------------------------------------------------------------------
# Broker state (shared across connection threads).
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    import os
    try:
        return int(float(str(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    import os
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    import os
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")


class BrokerState:
    def __init__(self, cfg: dict, job_id: str, db_path: str, log_path: str,
                 token: str, sg_port: int):
        self.cfg = cfg
        self.job_id = job_id
        self.db_path = db_path
        self.log_path = log_path
        self.token = token
        self.sg_port = sg_port
        self.lock = threading.Lock()
        self.mode = "supergrok"          # or "openrouter" after failover
        self.failed_over = False
        self.failover_blocked = False    # cap hit: don't re-attempt
        self.or_key: str | None = None
        self.tracker = FailoverTracker(cfg["fo_consecutive"], cfg["fo_window"])
        self.n_429 = 0
        register_secret(token)

    # -- persistence helpers: each op opens a fresh short-lived connection.
    # _dbop serializes on self.lock; callers must NOT hold the lock already.
    def _dbop(self, fn):
        with self.lock:
            conn = dbmod.connect(self.db_path)
            try:
                return fn(conn)
            finally:
                conn.close()

    def log_event(self, kind: str, msg: str) -> None:
        msg = redact(msg)
        def _op(conn):
            dbmod.log_event(conn, self.job_id, kind, msg[:2000])
        try:
            self._dbop(_op)
        except Exception:
            pass
        try:
            with open(self.log_path, "a") as f:
                f.write(json.dumps({"ts": time.time(), "kind": kind,
                                    "msg": msg}) + "\n")
        except Exception:
            pass

    def record_tokens(self, prompt: int, completion: int, provider: str,
                      model: str | None) -> None:
        def _op(conn):
            dbmod.record_tokens(conn, self.job_id, prompt, completion,
                                provider, model or "")
        try:
            self._dbop(_op)
        except Exception:
            pass

    # -- upstream selection ---------------------------------------------
    def upstream(self):
        """Return (scheme, host, port) for the current mode."""
        if self.mode == "openrouter":
            scheme = "https" if self.cfg["or_tls"] else "http"
            return scheme, self.cfg["or_host"], self.cfg["or_port"]
        return "http", "127.0.0.1", self.sg_port

    # -- request rewriting ----------------------------------------------
    def rewrite(self, method: str, path: str, headers: list,
                body: bytes) -> tuple[str, list, bytes]:
        """Rewrite a client request for the current upstream.

        SuperGrok path: only Host is touched (plus optional
        stream_options injection for usage telemetry).
        OpenRouter path: path prefix remap (/v1 -> /api/v1), Authorization
        replaced with the server-side key, model field rewritten.
        """
        out = [(k, v) for k, v in headers
               if k.lower() not in _HOP_BY_HOP and k.lower() != "host"]
        if self.mode == "openrouter":
            prefix = self.cfg["or_path_prefix"]
            if path == "/v1":
                path = prefix
            elif path.startswith("/v1/"):
                path = prefix + path[3:]
            out = [(k, v) for k, v in out if k.lower() != "authorization"]
            out.append(("Authorization", "Bearer " + (self.or_key or "")))
            out.append(("X-Title", "outpost"))
            host = self.cfg["or_host"]
            port = self.cfg["or_port"]
            out.append(("Host", f"{host}:{port}" if port not in (80, 443)
                        else host))
            body = self._rewrite_body(body, for_openrouter=True)
        else:
            out.append(("Host", f"127.0.0.1:{self.sg_port}"))
            body = self._rewrite_body(body, for_openrouter=False)
        out.append(("Connection", "close"))
        return path, out, body

    def _rewrite_body(self, body: bytes, for_openrouter: bool) -> bytes:
        if not body or not self.cfg["inject_stream_usage"] and not for_openrouter:
            return body
        obj = _try_json(body)
        if not isinstance(obj, dict):
            return body
        changed = False
        if for_openrouter and "model" in obj:
            obj["model"] = self.cfg["or_model"]
            changed = True
        if self.cfg["inject_stream_usage"] and obj.get("stream") is True:
            so = obj.get("stream_options")
            if not isinstance(so, dict):
                so = {}
                obj["stream_options"] = so
                changed = True
            if so.get("include_usage") is not True:
                so["include_usage"] = True
                changed = True
        return json.dumps(obj).encode("utf-8") if changed else body

    # -- 429 handling + failover -----------------------------------------
    def note_upstream_status(self, status: int) -> None:
        """Called under no lock; takes the lock internally."""
        now = time.time()
        with self.lock:
            if self.mode != "supergrok":
                return
            if status in self.cfg["fo_codes"]:
                self.n_429 += 1
                self.tracker.note_429(now)
                n = self.n_429
            else:
                self.tracker.note_ok(now)
                return
        if status in self.cfg["fo_codes"]:
            self.log_event("ratelimit",
                           f"upstream 429 #{n} from supergrok proxy")
            self.maybe_failover(now)

    def maybe_failover(self, now: float) -> None:
        # Phase 1: decide under lock, then run the (slow) cap pre-check
        # WITHOUT the lock held.
        with self.lock:
            if (not self.cfg["fo_enabled"] or self.mode != "supergrok"
                    or self.failed_over or self.failover_blocked):
                return
            fire, reason = self.tracker.triggered(now)
            if not fire:
                return
            cfg, job_id = self.cfg, self.job_id
        pre_conn = dbmod.connect(self.db_path)
        try:
            try:
                ok, why, baseline = or_provider.failover_precheck(
                    pre_conn, cfg["spend_cfg"], job_id)
            except Exception as exc:
                # Defensive: never let a pre-check crash break the in-flight
                # request path — block the failover and surface it instead.
                ok, why, baseline = (
                    False, f"failover pre-check errored: {type(exc).__name__}",
                    None)
        finally:
            pre_conn.close()
        # Read the key before committing (fail -> blocked, no failover).
        or_key = None
        key_err = None
        if ok:
            try:
                or_key = or_provider.read_key(cfg["spend_cfg"])
            except Exception as exc:
                key_err = exc
        # Phase 2: commit under lock. A racing thread can only win once —
        # the loser sees failed_over and backs off.
        with self.lock:
            if self.mode != "supergrok" or self.failed_over:
                return  # raced with another thread; only one failover ever
            if not ok or key_err is not None:
                self.failover_blocked = True
                blocked_reason = (why if not ok
                                  else f"could not read openrouter key: {key_err}")
            else:
                self.or_key = or_key
                register_secret(or_key)
                self.mode = "openrouter"
                self.failed_over = True
                blocked_reason = None
            ts = time.time()
        # Phase 3: persistence + logging without the lock held (_dbop
        # takes it internally).
        if blocked_reason is not None:
            self.log_event("failover_blocked", blocked_reason)
            try:
                def _op(conn):
                    dbmod.flag_attention(conn, job_id,
                                         "rate-limited with no overflow budget: "
                                         + blocked_reason[:500])
                self._dbop(_op)
            except Exception:
                pass
            return

        def _op(conn):
            dbmod.update_job(
                conn, job_id, provider="openrouter",
                or_baseline_usd=baseline, failover_at=ts,
                failover_from="supergrok", failover_to="openrouter",
                failover_reason=reason)
            dbmod.log_event(conn, job_id, "failover",
                            f"{reason}; provider supergrok -> openrouter "
                            f"({cfg['or_model']}). {why}")
        try:
            self._dbop(_op)
        except Exception as exc:
            self.log_event("failover",
                           "WARNING: failover committed in memory but DB "
                           f"write failed: {exc}")
        self.log_event("failover",
                       f"FAILOVER: {reason}. Upstream is now OpenRouter "
                       f"({cfg['or_model']}); container unaffected.")


# ---------------------------------------------------------------------------
# Connection handling.
# ---------------------------------------------------------------------------

def send_response(wfile, status: int, reason: str, headers: list,
                  body: bytes) -> None:
    wfile.write(f"HTTP/1.1 {status} {reason}\r\n".encode("latin1"))
    for k, v in headers:
        wfile.write(f"{k}: {v}\r\n".encode("latin1"))
    wfile.write(b"\r\n")
    if body:
        wfile.write(body)
    wfile.flush()


def handle_connection(state: BrokerState, client: socket.socket) -> None:
    rfile = client.makefile("rb")
    wfile = client.makefile("wb")
    try:
        req = read_request(rfile)
        if req is None:
            return
        method, path, _version, headers, body = req
        auth = header_get(headers, "authorization", "")
        if not hmac.compare_digest(auth, "Bearer " + state.token):
            send_response(wfile, 401, "Unauthorized",
                          [("Content-Length", "0"), ("Connection", "close")],
                          b"")
            return
        with state.lock:
            mode = state.mode
            path2, headers2, body2 = state.rewrite(method, path, headers, body)
            scheme, host, port = state.upstream()
        if scheme == "https":
            uconn = http.client.HTTPSConnection(host, port,
                                                timeout=UPSTREAM_TIMEOUT)
        else:
            uconn = http.client.HTTPConnection(host, port,
                                               timeout=UPSTREAM_TIMEOUT)
        try:
            uconn.request(method, path2, body=body2 or None,
                          headers={k: v for k, v in headers2})
            resp = uconn.getresponse()
            status, reason = resp.status, resp.reason
        except Exception as exc:
            state.log_event("proxy_error",
                            f"upstream unreachable ({mode}): {type(exc).__name__}")
            send_response(wfile, 502, "Bad Gateway",
                          [("Content-Length", "0"), ("Connection", "close")],
                          b"")
            return

        # 429 detection happens on the status line, before the body.
        state.note_upstream_status(status)

        ctype = (resp.getheader("Content-Type") or "").lower()
        resp_headers = [(k, v) for k, v in resp.getheaders()
                        if k.lower() not in _HOP_BY_HOP]
        is_sse = "text/event-stream" in ctype
        # Model identity for telemetry: read from the REWRITTEN body so a
        # post-failover request attributes to the overflow model, not the
        # grok model the container asked for.
        model_used = None
        if body2:
            m = _try_json(body2)
            if isinstance(m, dict) and isinstance(m.get("model"), str):
                model_used = m["model"]
        if mode == "openrouter" and not model_used:
            model_used = state.cfg["or_model"]

        if status in (204, 304) or method.upper() == "HEAD":
            resp.read()
            resp_headers.append(("Connection", "close"))
            send_response(wfile, status, reason, resp_headers, b"")
        elif is_sse:
            # Pass the stream through chunk-by-chunk (preserves real-time
            # streaming for the agent) while sniffing for usage.
            resp_headers.append(("Transfer-Encoding", "chunked"))
            resp_headers.append(("Connection", "close"))
            send_response(wfile, status, reason, resp_headers, b"")
            sniff = []
            try:
                while True:
                    chunk = resp.read(READ_CHUNK)
                    if not chunk:
                        break
                    sniff.append(chunk)
                    wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    wfile.write(chunk)
                    wfile.write(b"\r\n")
                    wfile.flush()
            finally:
                wfile.write(b"0\r\n\r\n")
                wfile.flush()
            usage = parse_usage_from_sse(b"".join(sniff))
            if usage:
                state.record_tokens(usage[0], usage[1], mode, model_used)
        else:
            data = resp.read()
            usage = parse_usage_from_json(data)
            if usage:
                state.record_tokens(usage[0], usage[1], mode, model_used)
            resp_headers.append(("Content-Length", str(len(data))))
            resp_headers.append(("Connection", "close"))
            send_response(wfile, status, reason, resp_headers, data)
        try:
            uconn.close()
        except Exception:
            pass
    except (EOFError, ValueError, ConnectionError, OSError):
        pass
    except Exception as exc:  # never let one bad request kill the broker
        try:
            state.log_event("proxy_error",
                            f"request handling failed: {type(exc).__name__}")
        except Exception:
            pass
    finally:
        for f in (rfile, wfile):
            try:
                f.close()
            except Exception:
                pass
        try:
            client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Config + entrypoint.
# ---------------------------------------------------------------------------

def load_broker_config(spend_yaml_path: str) -> dict:
    import os
    with open(spend_yaml_path) as f:
        spend_cfg = yaml.safe_load(f)
    fo = spend_cfg.get("failover") or {}
    tel = spend_cfg.get("telemetry") or {}
    or_tier = (spend_cfg.get("tiers") or {}).get("openrouter") or {}
    codes = fo.get("status_codes", [429])
    return {
        "spend_cfg": spend_cfg,
        "fo_enabled": _env_bool("CA_FO_ENABLED", fo.get("enabled", True)),
        "fo_consecutive": _env_int("CA_FO_CONSECUTIVE_429S",
                                   fo.get("consecutive_429s", 3)),
        "fo_window": _env_float("CA_FO_WINDOW_SECONDS",
                                fo.get("window_seconds", 60)),
        "fo_codes": [int(c) for c in codes],
        "or_host": os.environ.get("CA_OR_HOST",
                                  or_tier.get("api_host", "openrouter.ai")),
        "or_port": _env_int("CA_OR_PORT", or_tier.get("api_port", 443)),
        "or_tls": _env_bool("CA_OR_TLS", or_tier.get("api_tls", True)),
        "or_path_prefix": or_tier.get("path_prefix", "/api/v1"),
        "or_model": or_tier.get("model", "deepseek/deepseek-v4-flash-0731"),
        "inject_stream_usage": _env_bool(
            "CA_TELEMETRY_INJECT_STREAM_USAGE",
            tel.get("inject_stream_usage", True)),
    }


def main() -> int:
    (listen_port, sg_port, token, job_id, db_path, spend_yaml_path,
     log_path) = (int(sys.argv[1]), int(sys.argv[2]), sys.argv[3],
                  sys.argv[4], sys.argv[5], sys.argv[6], sys.argv[7])
    cfg = load_broker_config(spend_yaml_path)
    state = BrokerState(cfg, job_id, db_path, log_path, token, sg_port)
    # If a previous broker incarnation already failed this job over (e.g.
    # after a runner restart), resume in openrouter mode immediately.
    try:
        conn = dbmod.connect(db_path)
        try:
            job = dbmod.get_job(conn, job_id)
        finally:
            conn.close()
        if job and job.get("failover_to") == "openrouter":
            state.or_key = or_provider.read_key(cfg["spend_cfg"])
            register_secret(state.or_key)
            state.mode = "openrouter"
            state.failed_over = True
            state.log_event("failover",
                            "resuming in openrouter mode (failover recorded "
                            "by an earlier broker)")
    except Exception as exc:
        state.log_event("proxy_error",
                        f"failover-resume check failed: {type(exc).__name__}")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen_port))
    srv.listen(64)
    print(f"broker: listening on 0.0.0.0:{listen_port} "
          f"(job {job_id}, http-aware, failover={'on' if cfg['fo_enabled'] else 'off'})",
          flush=True)
    state.log_event("proxy", "broker ready (http-aware, failover armed)")
    while True:
        client, _ = srv.accept()
        client.settimeout(CLIENT_TIMEOUT)
        threading.Thread(target=handle_connection, args=(state, client),
                         daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
