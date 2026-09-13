#!/usr/bin/env python3
"""Tests for the dispatcher HTTP API (service/api.py over service/dispatch.py).

Spins the real API server on 127.0.0.1 with test tokens (CA_API_BIND /
CA_API_PORT / CA_API_CONFIG env overrides) against a SCRATCH copy of the
cloud-agents tree — never the live ~/cloud-agents install, never the live
config/api.yaml.

Usage (on the Mac):  python3 tests/test_api.py
Exit 0 = all pass; prints PASS/FAIL per case.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)  # build/cloud-agents

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))


# --- scratch tree -----------------------------------------------------------
scratch = tempfile.mkdtemp(prefix="ca-api-test-")
for d in ("service", "bin", "config", "adapters"):
    shutil.copytree(os.path.join(SRC, d), os.path.join(scratch, d))

# Test API config: throwaway tokens, never the live ones.
API_CFG = os.path.join(scratch, "config", "api.yaml")
with open(API_CFG, "w") as f:
    f.write("port: 0\nbind: 127.0.0.1\ndefault_client: atlas\n"
            "clients:\n  atlas: test-token-atlas-0001\n"
            "  grok-bot: test-token-grok-0002\n")
os.chmod(API_CFG, 0o600)

sys.path.insert(0, os.path.join(scratch, "service"))

env = dict(os.environ, CA_API_BIND="127.0.0.1", CA_API_PORT="0",
           CA_API_CONFIG=API_CFG)
proc = subprocess.Popen(
    [sys.executable, os.path.join(scratch, "service", "api.py")],
    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

BASE = None
try:
    line = proc.stdout.readline()
    # e.g. "api: listening on http://127.0.0.1:54321 (2 client(s))"
    import re
    m = re.search(r"http://(\S+):(\d+)", line or "")
    assert m, f"server did not print a listen line: {line!r}"
    BASE = f"http://{m.group(1)}:{m.group(2)}"
    print(f"api under test: {BASE}")
except Exception as e:
    proc.kill()
    print(f"FAIL server startup — {e}")
    sys.exit(1)


def req(method, path, token="test-token-atlas-0001", body=None):
    r = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, raw, ctype
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")


def jget(method, path, token="test-token-atlas-0001", body=None):
    status, raw, _ = req(method, path, token, body)
    try:
        payload = json.loads(raw.decode()) if raw else {}
    except Exception:
        payload = {}
    return status, payload


# --- auth -------------------------------------------------------------------
s, p = jget("GET", "/spend", token=None)
check("401 with no token", s == 401 and p.get("error") == "unauthorized", f"{s} {p}")
s, p = jget("GET", "/spend", token="wrong-token")
check("401 with bad token", s == 401 and p.get("error") == "unauthorized", f"{s} {p}")
s, p = jget("GET", "/jobs/job_test/cancel")
check("405 wrong method on route", s == 405, f"{s} {p}")
s, p = jget("GET", "/nope")
check("404 unknown route", s == 404, f"{s} {p}")

# --- submit validation ------------------------------------------------------
s, p = jget("POST", "/jobs", body={"type": "coding", "repo": "scratch", "task": ""})
check("400 empty task", s == 400 and "empty" in p.get("error", ""), f"{s} {p}")
s, p = jget("POST", "/jobs", body={"type": "coding", "repo": "nope", "task": "x"})
check("400 repo not allowlisted", s == 400 and "allowlist" in p.get("error", ""), f"{s} {p}")
s, p = jget("POST", "/jobs", body={"type": "coding", "repo": "scratch", "task": "x", "provider": "bogus"})
check("400 bad provider", s == 400 and "provider" in p.get("error", ""), f"{s} {p}")
s, p = jget("POST", "/jobs", body={"type": "bogus", "repo": "scratch", "task": "x"})
check("400 bad type", s == 400 and "type" in p.get("error", ""), f"{s} {p}")
s, raw, _ = req("POST", "/jobs", body=None)
# raw non-JSON body -> 400
r = urllib.request.Request(
    BASE + "/jobs", data=b"not json",
    method="POST",
    headers={"Authorization": "Bearer test-token-atlas-0001"})
try:
    with urllib.request.urlopen(r, timeout=15) as resp:
        s2 = resp.status
except urllib.error.HTTPError as e:
    s2 = e.code
check("400 malformed JSON body", s2 == 400, f"got {s2}")

# --- submit -> status round trip --------------------------------------------
s, p = jget("POST", "/jobs", body={"type": "coding", "repo": "scratch",
                                   "task": "write hello.py",
                                   "idempotency_key": "api-test-1"})
check("submit 200 with id", s == 200 and p.get("id", "").startswith("job_")
      and p.get("deduplicated") is False, f"{s} {p}")
jid = p["id"]

s, p = jget("POST", "/jobs", body={"type": "coding", "repo": "scratch",
                                   "task": "write hello.py",
                                   "idempotency_key": "api-test-1"})
check("idempotency dedupe", s == 200 and p.get("id") == jid
      and p.get("deduplicated") is True, f"{s} {p}")

s, p = jget("GET", f"/jobs/{jid}")
check("status round trip", s == 200 and p["id"] == jid
      and p["status"] == "queued" and p["provider"] == "supergrok"
      and "tokens" in p, f"{s} {p}")

s, p = jget("GET", "/jobs/job_does_not_exist")
check("404 unknown job", s == 404, f"{s} {p}")
s, p = jget("GET", "/jobs/not%20a%20valid%20id%21")
check("400 invalid job id", s == 400, f"{s} {p}")

# --- list -------------------------------------------------------------------
s, p = jget("GET", "/jobs")
check("list jobs", s == 200 and any(j["id"] == jid for j in p["jobs"]), f"{s} {p}")
s, p = jget("GET", "/jobs?status=queued")
check("list filtered", s == 200 and all(j["status"] == "queued" for j in p["jobs"]), f"{s} {p}")

# --- logs -------------------------------------------------------------------
logdir = os.path.join(scratch, "logs")
os.makedirs(logdir, exist_ok=True)
with open(os.path.join(logdir, f"{jid}.jsonl"), "w") as f:
    f.write(json.dumps({"ts": 1.0, "kind": "engine", "msg": "selected=hermes"}) + "\n")
    f.write("not-json-line\n")
    f.write(json.dumps({"ts": 2.0, "kind": "container", "msg": "started"}) + "\n")
s, p = jget("GET", f"/jobs/{jid}/logs")
ents = p.get("entries", [])
check("logs entries", s == 200 and len(ents) == 3
      and ents[0]["kind"] == "engine" and ents[1]["raw"] is True, f"{s} {p}")
s, p = jget("GET", f"/jobs/{jid}/logs?tail=1")
check("logs tail=1", s == 200 and len(p["entries"]) == 1
      and p["entries"][0]["kind"] == "container", f"{s} {p}")
s, p = jget("GET", "/jobs/job_does_not_exist/logs")
check("404 logs missing", s == 404, f"{s} {p}")

# --- manifest ---------------------------------------------------------------
jobdir = os.path.join(scratch, "jobs", jid)
os.makedirs(jobdir, exist_ok=True)
with open(os.path.join(jobdir, "manifest.json"), "w") as f:
    json.dump({"job_id": jid, "spend": {"usd": 0.0}}, f)
s, p = jget("GET", f"/jobs/{jid}/manifest")
check("manifest", s == 200 and p.get("job_id") == jid, f"{s} {p}")
s, p = jget("GET", "/jobs/job_does_not_exist/manifest")
check("404 manifest missing", s == 404, f"{s} {p}")

# --- attention / ack ----------------------------------------------------------
import db  # noqa: E402  (scratch service dir)
dbpath = os.path.join(scratch, "state", "agents.db")
conn = db.connect(dbpath)
ajid = db.new_job_id()
db.insert_job(conn, job_id=ajid, type="coding", repo="scratch", base="main",
              task="t", engine_requested="auto", budget_usd=2.0,
              max_minutes=30, provider="supergrok")
db.update_job(conn, ajid, status="running", started_at=time.time() - 3600)
assert db.flag_attention(conn, ajid, "test flag")
s, p = jget("GET", "/attention")
check("attention lists flagged", s == 200 and any(j["id"] == ajid for j in p["jobs"])
      and p["jobs"][0]["acked"] is False and p["jobs"][0]["elapsed_min"] >= 59, f"{s} {p}")
s, p = jget("POST", f"/jobs/{ajid}/ack")
check("ack ok", s == 200 and p.get("ok") is True, f"{s} {p}")
s, p = jget("POST", f"/jobs/{ajid}/ack")
check("ack twice idempotent", s == 200 and p.get("ok") is True, f"{s} {p}")
s, p = jget("POST", "/jobs/job_does_not_exist/ack")
check("ack unknown -> 404", s == 404, f"{s} {p}")
conn.close()

# --- cancel -----------------------------------------------------------------
s, p = jget("POST", "/jobs/job_does_not_exist/cancel")
check("cancel unknown -> 404", s == 404, f"{s} {p}")
s, p = jget("POST", f"/jobs/{jid}/cancel")
check("cancel queued", s == 200 and p.get("ok") is True
      and "cancel requested" in p.get("message", ""), f"{s} {p}")
conn = db.connect(dbpath)
check("cancel_requested set", db.get_job(conn, jid)["cancel_requested"] == 1)
db.update_job(conn, jid, status="completed")
conn.close()
s, p = jget("POST", f"/jobs/{jid}/cancel")
check("cancel terminal -> already", s == 200 and p.get("already_terminal") is True, f"{s} {p}")

# --- spend ------------------------------------------------------------------
conn = db.connect(dbpath)
db.record_spend(conn, jid, 1.5, "or_key_delta", "test")
db.record_spend(conn, jid, 99.0, "token_estimate", "must not count")
conn.close()
s, p = jget("GET", "/spend")
check("spend rolling cash truth", s == 200 and abs(p["rolling_usd"] - 1.5) < 1e-9
      and p["cap_usd"] == 25.0 and p["cap_window_days"] == 7.0, f"{s} {p}")

# --- artifacts --------------------------------------------------------------
artdir = os.path.join(scratch, "artifacts", jid)
os.makedirs(artdir, exist_ok=True)
with open(os.path.join(artdir, "report.xlsx"), "wb") as f:
    f.write(b"fake-xlsx-bytes")
os.makedirs(os.path.join(jobdir, "out"), exist_ok=True)
with open(os.path.join(jobdir, "out", "repo.bundle"), "wb") as f:
    f.write(b"fake-bundle")
s, raw, ctype = req("GET", f"/jobs/{jid}/artifacts/report.xlsx")
check("artifact download", s == 200 and raw == b"fake-xlsx-bytes"
      and "spreadsheetml" in ctype, f"{s} {ctype}")
s, raw, ctype = req("GET", f"/jobs/{jid}/artifacts/repo.bundle")
check("repo.bundle special", s == 200 and raw == b"fake-bundle"
      and ctype == "application/octet-stream", f"{s} {ctype}")
s, p = jget("GET", f"/jobs/{jid}/artifacts/manifest.json")
check("manifest.json special", s == 200 and p.get("job_id") == jid, f"{s} {p}")
s, p = jget("GET", f"/jobs/{jid}/artifacts/nope.txt")
check("404 missing artifact", s == 404, f"{s} {p}")
s, p = jget("GET", f"/jobs/{jid}/artifacts/..%2F..%2Fapi.yaml")
check("400 path traversal", s == 400, f"{s} {p}")
s, p = jget("GET", "/jobs/job_does_not_exist/artifacts/x.txt")
check("404 artifact unknown job", s == 404, f"{s} {p}")
s, p = jget("GET", f"/jobs/{jid}/artifacts/%2Fetc%2Fpasswd")
check("400 absolute path", s == 400, f"{s} {p}")

proc.terminate()
proc.wait(timeout=10)
shutil.rmtree(scratch, ignore_errors=True)

print(f"\n{len(passed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
