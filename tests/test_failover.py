#!/usr/bin/env python3
"""Tests for mid-flight SuperGrok -> OpenRouter failover (Tier 1 policy).

Covers: FailoverTracker trigger semantics, usage extraction from JSON and
SSE responses, broker request rewriting, the transparent 429-driven switch
to a mock OpenRouter upstream (integration), no-flap / no-failback,
cap-hit blocking with needs_attention, and the unreachable-usage-endpoint
path. Self-contained: runs against a scratch copy of the service tree.

Run: python3 tests/test_failover.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "service"))

SCRATCH = Path(tempfile.mkdtemp(prefix="ca-failover-"))
shutil.copytree(ROOT / "service", SCRATCH / "service")
shutil.copytree(ROOT / "config", SCRATCH / "config")
sys.path.insert(0, str(SCRATCH / "service"))
for m in [m for m in list(sys.modules) if m in
          ("db", "openrouter", "redact", "pricing", "proxy_broker", "timeouts")]:
    del sys.modules[m]

import db  # noqa: E402
import openrouter  # noqa: E402
import pricing  # noqa: E402
import proxy_broker  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{detail}]" if detail and not cond else ""))


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_tcp(port, deadline=15.0):
    import socket
    end = time.time() + deadline
    while time.time() < end:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


# ---------------------------------------------------------------- unit: tracker

t = proxy_broker.FailoverTracker(threshold=3, window=60)
now = 1000.0
for i in range(2):
    t.note_429(now + i)
fire, reason = t.triggered(now + 2)
check("tracker: 2x429 does not trigger", not fire)
t.note_429(now + 2)
fire, reason = t.triggered(now + 2)
check("tracker: 3rd consecutive 429 triggers", fire, reason)

t2 = proxy_broker.FailoverTracker(threshold=3, window=60)
for i in range(2):
    t2.note_429(now + i)
t2.note_ok(now + 2)  # an OK breaks the consecutive chain
t2.note_429(now + 3)
fire, _ = t2.triggered(now + 3)
check("tracker: ok resets consecutive count", not fire)

t3 = proxy_broker.FailoverTracker(threshold=3, window=60)
t3.note_429(now)
t3.note_429(now + 120)
t3.note_429(now + 121)
fire, reason = t3.triggered(now + 121)
check("tracker: 429 episode spanning >window fires (persistence)", fire,
      reason)

t3b = proxy_broker.FailoverTracker(threshold=3, window=60)
t3b.note_429(now)          # ancient, outside the trailing window
t3b.note_429(now + 1000)   # only one 429 inside the window
fire, _ = t3b.triggered(now + 1000)
check("tracker: lone recent 429 does not trigger", not fire)

t4 = proxy_broker.FailoverTracker(threshold=2, window=3600)
t4.note_429(now)
t4.note_429(now + 3000)   # within 1h window, but not "consecutive"
fire, reason = t4.triggered(now + 3000)
check("tracker: window persistence triggers despite gap", fire, reason)

# ---------------------------------------------------------------- unit: usage parse

check("json usage parse",
      proxy_broker.parse_usage_from_json(
          json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 4,
                                "total_tokens": 14}}).encode()) == (10, 4))
check("json usage missing -> None",
      proxy_broker.parse_usage_from_json(b'{"id":"x"}') is None)
check("json malformed -> None",
      proxy_broker.parse_usage_from_json(b'not json') is None)
check("json usage zero values",
      proxy_broker.parse_usage_from_json(
          json.dumps({"usage": {"prompt_tokens": 0,
                                "completion_tokens": 0}}).encode()) == (0, 0))

sse = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
       b'data: {"usage":{"prompt_tokens":30,"completion_tokens":7}}\n\n'
       b'data: [DONE]\n\n')
check("sse usage parse", proxy_broker.parse_usage_from_sse(sse) == (30, 7))
check("sse without usage -> None",
      proxy_broker.parse_usage_from_sse(b'data: {"a":1}\n\ndata: [DONE]\n\n')
      is None)
check("sse malformed data line -> None",
      proxy_broker.parse_usage_from_sse(b'data: not json\n\ndata: [DONE]\n\n')
      is None)

# ---------------------------------------------------------------- unit: rewrite

cfg = proxy_broker.load_broker_config(str(SCRATCH / "config" / "spend.yaml"))
check("spend.yaml failover config loads",
      cfg["fo_enabled"] and cfg["fo_consecutive"] == 3
      and cfg["fo_window"] == 60 and cfg["fo_codes"] == [429]
      and cfg["or_model"] == "deepseek/deepseek-v4-flash-0731"
      and cfg["or_path_prefix"] == "/api/v1"
      and cfg["inject_stream_usage"] is True)
check("pricing config loads",
      pricing.lookup_price(cfg["spend_cfg"], "supergrok", "grok-4.6")
      == (0.0, 0.0, "subscription"))

db_path = str(SCRATCH / "state.sqlite")
conn = db.connect(db_path)
st = proxy_broker.BrokerState(cfg, "job-1", db_path,
                              str(SCRATCH / "broker.log"), "secrettoken", 19999)
# supergrok mode: auth untouched, path untouched, stream_options injected
path2, headers2, body2 = st.rewrite(
    "POST", "/v1/chat/completions",
    [("authorization", "Bearer secrettoken"), ("content-type", "application/json")],
    json.dumps({"model": "grok-4.6", "messages": [], "stream": True}).encode())
b2 = json.loads(body2)
check("sg mode: auth passed through",
      ("authorization", "Bearer secrettoken") in headers2)
check("sg mode: path unchanged", path2 == "/v1/chat/completions")
check("sg mode: stream_options injected for telemetry",
      b2.get("stream_options", {}).get("include_usage") is True)
check("sg mode: model untouched", b2["model"] == "grok-4.6")

# openrouter mode: auth replaced, model rewritten, path remapped
st.mode = "openrouter"
st.or_key = "sk-or-fake"
path2, headers2, body2 = st.rewrite(
    "POST", "/v1/chat/completions",
    [("authorization", "Bearer secrettoken"), ("content-type", "application/json")],
    json.dumps({"model": "grok-4.6", "messages": []}).encode())
auths = [v for k, v in headers2 if k.lower() == "authorization"]
check("or mode: auth replaced with OR key",
      auths == ["Bearer sk-or-fake"])
check("or mode: model rewritten to deepseek",
      json.loads(body2)["model"] == "deepseek/deepseek-v4-flash-0731")
check("or mode: path remapped to /api/v1",
      path2 == "/api/v1/chat/completions")
conn.close()


# ------------------------------------------------- integration: live failover

class MockSuperGrok(BaseHTTPRequestHandler):
    requests = 0
    lock = threading.Lock()

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        with MockSuperGrok.lock:
            MockSuperGrok.requests += 1
            n = MockSuperGrok.requests
        if n <= 2:
            self._send(429, b'{"error":{"message":"rate limit"}}')
        else:
            self._send(200, json.dumps(
                {"choices": [], "usage": {"prompt_tokens": 1,
                                          "completion_tokens": 1}}).encode())

    def log_message(self, *a):
        pass


class MockOpenRouter(BaseHTTPRequestHandler):
    seen = []
    lock = threading.Lock()

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        with MockOpenRouter.lock:
            MockOpenRouter.seen.append({
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": json.loads(body.decode()),
            })
        self._send(200, json.dumps(
            {"choices": [{"message": {"content": "ok"}}],
             "usage": {"prompt_tokens": 100,
                       "completion_tokens": 20}}).encode())

    def log_message(self, *a):
        pass


sg_port = free_port()
or_port = free_port()
listen_port = free_port()
sg_srv = HTTPServer(("127.0.0.1", sg_port), MockSuperGrok)
or_srv = HTTPServer(("127.0.0.1", or_port), MockOpenRouter)
threading.Thread(target=sg_srv.serve_forever, daemon=True).start()
threading.Thread(target=or_srv.serve_forever, daemon=True).start()

# scratch job row (running) so failover/flag paths can write
job_id = "job-fo1"
conn = db.connect(db_path)
db.insert_job(conn, job_id=job_id, type="code", repo="https://x/y",
              base="main", task="t", engine_requested="hermes",
              budget_usd=0, max_minutes=60, provider="supergrok")
db.update_job(conn, job_id, status="running")
conn.close()

key_file = SCRATCH / "or_key.env"
key_file.write_text("OPENROUTER_API_KEY: sk-test-fake-key\n")
env = dict(__import__("os").environ)
env.update({
    "CA_FO_CONSECUTIVE_429S": "2",          # faster trigger in test
    "CA_FO_WINDOW_SECONDS": "60",
    "CA_OR_HOST": "127.0.0.1",
    "CA_OR_PORT": str(or_port),
    "CA_OR_TLS": "0",
    "CA_OR_KEY_FILE": str(key_file),
    "CA_OR_USAGE_MOCK": "1.5",              # mock key-usage baseline
})
broker = subprocess.Popen(
    [sys.executable, str(SCRATCH / "service" / "proxy_broker.py"),
     str(listen_port), str(sg_port), "secrettoken", job_id, db_path,
     str(SCRATCH / "config" / "spend.yaml"), str(SCRATCH / "broker.log")],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True)
assert wait_tcp(listen_port), "broker did not start"


def client_post():
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", listen_port, timeout=15)
    payload = json.dumps({"model": "grok-4.6", "messages": [
        {"role": "user", "content": "hi"}]})
    c.request("POST", "/v1/chat/completions", body=payload,
              headers={"Authorization": "Bearer secrettoken",
                       "Content-Type": "application/json"})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


s1, _ = client_post()
s2, _ = client_post()
check("pre-failover: 429s pass through transparently", (s1, s2) == (429, 429))
time.sleep(0.5)  # let the failover commit land in the DB
s3, b3 = client_post()
check("post-failover: request succeeds via OR", s3 == 200, f"got {s3}")
s4, _ = client_post()
check("no flap: further requests stay on OR", s4 == 200)
time.sleep(0.3)
with MockSuperGrok.lock:
    sg_hits = MockSuperGrok.requests
with MockOpenRouter.lock:
    or_hits = list(MockOpenRouter.seen)
check("supergrok upstream saw only the 2 real 429s", sg_hits == 2,
      f"saw {sg_hits}")
check("openrouter upstream got 2 requests", len(or_hits) == 2,
      f"saw {len(or_hits)}")
if or_hits:
    check("OR: job token NOT forwarded (secret stays host-side)",
          all(h["auth"] != "Bearer secrettoken" for h in or_hits))
    check("OR: real OR key used",
          all(h["auth"] == "Bearer sk-test-fake-key" for h in or_hits))
    check("OR: model rewritten to deepseek",
          all(h["body"]["model"] == "deepseek/deepseek-v4-flash-0731"
              for h in or_hits))
    check("OR: path remapped to /api/v1",
          all(h["path"] == "/api/v1/chat/completions" for h in or_hits))

conn = db.connect(db_path)
j = db.get_job(conn, job_id)
check("db: provider flipped to openrouter", j["provider"] == "openrouter",
      j["provider"])
check("db: failover fields set",
      j["failover_from"] == "supergrok" and j["failover_to"] == "openrouter"
      and bool(j["failover_at"]) and bool(j["failover_reason"]))
check("db: baseline recorded", j["or_baseline_usd"] == 1.5,
      j["or_baseline_usd"])
toks = db.token_totals(conn, job_id)
check("db: token telemetry recorded (2 OR requests)",
      toks["requests"] == 2 and toks["prompt_tokens"] == 200
      and toks["completion_tokens"] == 40, str(toks))
brk = db.token_breakdown(conn, job_id)
check("db: telemetry attributed to openrouter/deepseek",
      len(brk) == 1 and brk[0]["provider"] == "openrouter"
      and brk[0]["model"] == "deepseek/deepseek-v4-flash-0731"
      and brk[0]["prompt_tokens"] == 200, str(brk))
ev = [r for r in conn.execute(
    "SELECT kind FROM events WHERE job_id=? ORDER BY seq DESC LIMIT 50",
    (job_id,)).fetchall() if r["kind"] == "failover"]
check("db: failover event logged", len(ev) >= 1)
conn.close()
broker.terminate()
broker.wait(timeout=10)
sg_srv.shutdown()
sg_srv.server_close()
or_srv.shutdown()
or_srv.server_close()

# ------------------------------------------------- integration: cap-hit blocks

job_id2 = "job-fo2"
conn = db.connect(db_path)
db.insert_job(conn, job_id=job_id2, type="code", repo="https://x/y",
              base="main", task="t", engine_requested="hermes",
              budget_usd=0, max_minutes=60, provider="supergrok")
db.update_job(conn, job_id2, status="running")
# seed $30 of recent or_key_delta spend -> over the $25 cap
db.record_spend(conn, job_id2, 30.0, "or_key_delta", "seeded cap")
conn.close()

MockSuperGrok.requests = 0
sg_srv2 = HTTPServer(("127.0.0.1", sg_port), MockSuperGrok)
threading.Thread(target=sg_srv2.serve_forever, daemon=True).start()
listen2 = free_port()
broker2 = subprocess.Popen(
    [sys.executable, str(SCRATCH / "service" / "proxy_broker.py"),
     str(listen2), str(sg_port), "secrettoken", job_id2, db_path,
     str(SCRATCH / "config" / "spend.yaml"), str(SCRATCH / "broker2.log")],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True)
assert wait_tcp(listen2), "broker2 did not start"


def client_post2():
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", listen2, timeout=15)
    c.request("POST", "/v1/chat/completions", body="{}",
              headers={"Authorization": "Bearer secrettoken",
                       "Content-Type": "application/json"})
    r = c.getresponse()
    r.read()
    st = r.status
    c.close()
    return st


client_post2()
client_post2()  # 2x429 would trigger, but cap is hit
time.sleep(0.5)
conn = db.connect(db_path)
j2 = db.get_job(conn, job_id2)
check("cap-hit: no failover committed", j2["provider"] == "supergrok"
      and not j2["failover_at"], j2["provider"])
check("cap-hit: job flagged needs_attention",
      j2["status"] == "needs_attention", j2["status"])
check("cap-hit: no token rows written to OR",
      db.token_totals(conn, job_id2)["requests"] == 0)
conn.close()
broker2.terminate()
broker2.wait(timeout=10)
sg_srv2.shutdown()
sg_srv2.server_close()

# ------------------------------------------------- unit: unreachable usage endpoint

# Fresh DB so the seeded $30 cap data from the previous test can't skew it.
u_db = str(SCRATCH / "unreachable.sqlite")
conn = db.connect(u_db)
job_id3 = "job-fo3"
db.insert_job(conn, job_id=job_id3, type="code", repo="https://x/y",
              base="main", task="t", engine_requested="hermes",
              budget_usd=0, max_minutes=60, provider="supergrok")
db.update_job(conn, job_id3, status="running")
import os as _os
_old_url = _os.environ.get("CA_OR_USAGE_URL")
_old_key = _os.environ.get("CA_OR_KEY_FILE")
_os.environ["CA_OR_USAGE_URL"] = "https://127.0.0.1:1/this/is/dead"
_os.environ["CA_OR_KEY_FILE"] = str(key_file)
try:
    ok, why, baseline = openrouter.failover_precheck(conn, {}, job_id3)
finally:
    if _old_url is None:
        del _os.environ["CA_OR_USAGE_URL"]
    else:
        _os.environ["CA_OR_USAGE_URL"] = _old_url
    if _old_key is None:
        del _os.environ["CA_OR_KEY_FILE"]
    else:
        _os.environ["CA_OR_KEY_FILE"] = _old_key
conn.close()
check("unreachable usage endpoint: failover allowed but flagged",
      ok is True and baseline is None, why)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if FAIL else 0)
