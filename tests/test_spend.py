#!/usr/bin/env python3
"""Tests for the two-tier spend / hard-stop policy.

Runs against a SCRATCH copy of the outpost tree (never the live
~/outpost install). Timers are simulated via the CA_* env overrides —
no real waiting.

Usage (on the host):  python3 tests/test_spend.py
Exit 0 = all pass; prints PASS/FAIL per case.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)  # build/outpost

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))


# --- scratch tree -----------------------------------------------------------
scratch = tempfile.mkdtemp(prefix="ca-test-")
for d in ("service", "bin", "config", "adapters"):
    shutil.copytree(os.path.join(SRC, d), os.path.join(scratch, d))
os.chmod(os.path.join(scratch, "bin", "agentctl"), 0o755)
sys.path.insert(0, os.path.join(scratch, "service"))
os.chdir(scratch)

import yaml  # noqa: E402
import db  # noqa: E402
import timeouts  # noqa: E402
import openrouter as orp  # noqa: E402

spend_cfg = yaml.safe_load(open(os.path.join(scratch, "config", "spend.yaml")))
DB = os.path.join(scratch, "state", "agents.db")

# fast timers for tests: check-in 1m, no-response 2m, ceiling 5m
os.environ["CA_CHECKIN_MINUTES"] = "1"
os.environ["CA_NORESPONSE_MINUTES"] = "2"
os.environ["CA_CEILING_MINUTES"] = "5"
T = timeouts.load_timeouts(spend_cfg, "supergrok")
check("env overrides resolve", T == {"checkin": 1.0, "noresponse": 2.0, "ceiling": 5.0}, str(T))


def mkjob(conn, **kw):
    jid = db.new_job_id()
    base = dict(job_id=jid, type="coding", repo="scratch", base="main",
                task="t", engine_requested="auto", budget_usd=2.0,
                max_minutes=30, provider="supergrok")
    base.update(kw)
    return db.insert_job(conn, **base)


# --- 1. check-in flag -------------------------------------------------------
conn = db.connect(DB)
j = mkjob(conn)
db.update_job(conn, j["id"], status="running",
              started_at=time.time() - 61)  # 61s > 1m check-in
act = timeouts.enforce(conn, j["id"], timeouts=T)
j = db.get_job(conn, j["id"])
check("46s-equivalent: flagged", act == "flagged" and j["status"] == "needs_attention",
      f"act={act} status={j['status']}")
check("attention flag timestamp set", bool(j["attention_flagged_at"]))
check("attention queue lists it", any(x["id"] == j["id"] for x in db.attention_queue(conn)))
ev = conn.execute("SELECT kind FROM events WHERE job_id=? ORDER BY seq DESC LIMIT 1",
                  (j["id"],)).fetchone()
check("flag event logged", ev and ev["kind"] == "attention")

# --- 2. no-response kill ------------------------------------------------------
# flagged 2.5m ago, never acked -> kill
db.update_job(conn, j["id"], attention_flagged_at=time.time() - 150)
act = timeouts.enforce(conn, j["id"], timeouts=T)
j = db.get_job(conn, j["id"])
check("unacked past window: killed", act == "killed_no_response"
      and j["cancel_requested"] == 1 and j["status"] == "cancelled",
      f"act={act} status={j['status']}")
check("kill_reason recorded", bool(j.get("kill_reason")), str(j.get("kill_reason")))

# --- 3. ack lets it survive to ceiling ----------------------------------------
j2 = mkjob(conn)
db.update_job(conn, j2["id"], status="running", started_at=time.time() - 61)
check("re-flag", timeouts.enforce(conn, j2["id"], timeouts=T) == "flagged")
check("ack works", db.ack_attention(conn, j2["id"]) is True)
j2 = db.get_job(conn, j2["id"])
check("ack timestamp set", bool(j2["attention_acked_at"]))
# 4.5m elapsed, acked -> still alive (ceiling is 5m)
db.update_job(conn, j2["id"], started_at=time.time() - 270)
act = timeouts.enforce(conn, j2["id"], timeouts=T)
check("acked job survives under ceiling", act is None, f"act={act}")
# 5.5m elapsed -> ceiling kill even though acked
db.update_job(conn, j2["id"], started_at=time.time() - 330)
act = timeouts.enforce(conn, j2["id"], timeouts=T)
j2 = db.get_job(conn, j2["id"])
check("ceiling kills acked job", act == "killed_ceiling"
      and j2["cancel_requested"] == 1, f"act={act}")

# --- 4. healthy job untouched --------------------------------------------------
j3 = mkjob(conn)
db.update_job(conn, j3["id"], status="running", started_at=time.time() - 30)
check("young job untouched", timeouts.enforce(conn, j3["id"], timeouts=T) is None)
check("terminal job untouched",
      timeouts.enforce(conn, j["id"], timeouts=T) is None)  # j is cancelled

# --- 5. attention_queue only lists needs_attention -----------------------------
q = db.attention_queue(conn)
check("queue excludes non-flagged", all(x["status"] == "needs_attention" for x in q))

# --- 6. OpenRouter: ledger + preflight -----------------------------------------
conn2 = db.connect(DB)
now = time.time()
conn2.execute("INSERT INTO spend_ledger (ts,job_id,usd,source,note) VALUES (?,?,?,?,?)",
              (now - 86400, "old1", 10.0, "or_key_delta", ""))
conn2.execute("INSERT INTO spend_ledger (ts,job_id,usd,source,note) VALUES (?,?,?,?,?)",
              (now - 8 * 86400, "ancient", 100.0, "or_key_delta", ""))  # outside window
conn2.commit()
check("rolling 7d excludes old entries",
      abs(db.rolling_spend_usd(conn2, 7.0) - 10.0) < 1e-9,
      str(db.rolling_spend_usd(conn2, 7.0)))

# mock the usage endpoint: lifetime usage $50.00
orp.key_usage_usd = lambda key, timeout=15.0: 50.0
j4 = mkjob(conn2, provider="openrouter")
db.update_job(conn2, j4["id"], status="running")
ok, why = orp.preflight(conn2, spend_cfg, j4["id"])
j4 = db.get_job(conn2, j4["id"])
check("preflight allows under cap", ok is True, why)
check("preflight records baseline", j4["or_baseline_usd"] == 50.0)

# push ledger to $24.99 -> still allowed; $25.01 -> denied
db.record_spend(conn2, "x", 14.99, "or_key_delta", "test top-up")
ok, why = orp.preflight(conn2, spend_cfg, j4["id"])
check("preflight allows at $24.99", ok is True, why)
db.record_spend(conn2, "x", 0.03, "or_key_delta", "test push over")
ok, why = orp.preflight(conn2, spend_cfg, j4["id"])
check("preflight denies over cap", ok is False, why)

# mid-job breach: baseline 50, current usage 60 -> in-flight 10; ledger 25.02
orp.key_usage_usd = lambda key, timeout=15.0: 60.0
ok, why = orp.midjob_check(conn2, spend_cfg, j4["id"], "DUMMY")
check("mid-job breach -> not ok", ok is False, why)
# reset the ledger to a small amount for the under-cap case
conn2.execute("DELETE FROM spend_ledger")
conn2.execute("INSERT INTO spend_ledger (ts,job_id,usd,source,note) VALUES (?,?,?,?,?)",
              (time.time(), "x", 1.0, "or_key_delta", "test reset"))
conn2.commit()
orp.key_usage_usd = lambda key, timeout=15.0: 50.5
ok, why = orp.midjob_check(conn2, spend_cfg, j4["id"], "DUMMY")
check("mid-job under cap -> ok", ok is True, why)

# finalize records the delta
delta = orp.finalize(conn2, spend_cfg, j4["id"], "DUMMY")
check("finalize delta = 0.5", abs(delta - 0.5) < 1e-9, str(delta))
j4 = db.get_job(conn2, j4["id"])
check("job spend_usd recorded", abs((j4["spend_usd"] or 0) - 0.5) < 1e-9)

# --- 7. unreachable endpoint: fail-safe ----------------------------------------
orp.key_usage_usd = lambda key, timeout=15.0: None
j5 = mkjob(conn2, provider="openrouter")
db.update_job(conn2, j5["id"], status="running")
ok, why = orp.preflight(conn2, spend_cfg, j5["id"])
j5 = db.get_job(conn2, j5["id"])
check("unreachable -> allowed + flagged", ok is True
      and j5["status"] == "needs_attention", f"ok={ok} status={j5['status']}")

# --- 8. provider resolution -----------------------------------------------------
class J(dict):
    pass
check("explicit openrouter", orp.resolve_provider({"provider": "openrouter"}, "hermes", spend_cfg) == "openrouter")
check("default supergrok", orp.resolve_provider({"provider": "supergrok"}, "hermes", spend_cfg) == "supergrok")
check("auto+goose -> openrouter", orp.resolve_provider({"provider": "auto"}, "goose", spend_cfg) == "openrouter")
check("auto+hermes -> supergrok", orp.resolve_provider({"provider": "auto"}, "hermes", spend_cfg) == "supergrok")

# --- 9. CLI: attention + ack (through the real HTTP API) -------------------------
# agentctl is a thin HTTP client now, so these tests exercise the full stack:
# start the API on the scratch tree and point the scratch agentctl at it.
api_cfg_path = os.path.join(scratch, "config", "api.yaml")
with open(api_cfg_path, "w") as f:
    f.write("port: 0\nbind: 127.0.0.1\ndefault_client: atlas\n"
            "clients:\n  atlas: spend-test-token\n")
os.chmod(api_cfg_path, 0o600)
api_proc = subprocess.Popen(
    [sys.executable, os.path.join(scratch, "service", "api.py")],
    env=dict(os.environ, CA_API_BIND="127.0.0.1", CA_API_PORT="0",
             CA_API_CONFIG=api_cfg_path),
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=scratch)
import re as _re
_m = _re.search(r"http://(\S+):(\d+)", api_proc.stdout.readline() or "")
assert _m, "api server did not start for CLI tests"
with open(api_cfg_path, "w") as f:  # point agentctl at the real port
    f.write(f"port: {_m.group(2)}\nbind: 127.0.0.1\ndefault_client: atlas\n"
            "clients:\n  atlas: spend-test-token\n")
os.chmod(api_cfg_path, 0o600)

def ctl(*args):
    r = subprocess.run([sys.executable, os.path.join(scratch, "bin", "agentctl"), *args],
                       capture_output=True, text=True, cwd=scratch, timeout=30)
    return r

j6 = mkjob(conn2)
db.update_job(conn2, j6["id"], status="running", started_at=time.time() - 10)
db.flag_attention(conn2, j6["id"], "cli test")
r = ctl("attention")
check("agentctl attention lists flagged job", j6["id"] in r.stdout, r.stdout[:200])
r = ctl("ack", j6["id"])
check("agentctl ack acknowledges", "acknowledged" in r.stdout, r.stdout[:200])
r = ctl("ack", "job_nonexistent")
check("agentctl ack unknown -> error", r.returncode == 2)
r = ctl("spend")
check("agentctl spend shows rolling", "rolling 7d spend" in r.stdout, r.stdout[:200])
r = ctl("submit", "--type", "coding", "--task", "hello", "--provider", "openrouter")
new_id = r.stdout.strip()
check("submit --provider openrouter", r.returncode == 0 and new_id.startswith("job_"),
      r.stdout[:100] + r.stderr[:100])
if new_id.startswith("job_"):
    check("provider persisted", db.get_job(conn2, new_id)["provider"] == "openrouter")

conn.close()
conn2.close()
api_proc.terminate()
api_proc.wait(timeout=10)
shutil.rmtree(scratch, ignore_errors=True)

print(f"\n{len(passed)} passed, {len(failed)} failed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
