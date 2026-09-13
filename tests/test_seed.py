#!/usr/bin/env python3
"""Tests for host-side repo seeding of private repos (no credentials in containers).

Covers:
- dispatch.validate_ref: strict ref charset (branch/tag/pull-N-head), rejects
  traversal, absolute paths, shell metacharacters, overlong input.
- runner.host_seed_clone_url: config lookup for host_seeded repos.
- entrypoint.wait_for_host_seed: waits for the injected tree, times out clearly.
- dispatch.submit_job stores ref; API POST /jobs accepts/rejects ref.

Runs against a SCRATCH copy of the outpost tree (never the live
~/outpost install).

Usage (on the host):  python3 tests/test_seed.py
Exit 0 = all pass; prints PASS/FAIL per case.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)  # build/outpost

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))


# --- scratch tree -----------------------------------------------------------
scratch = tempfile.mkdtemp(prefix="ca-seed-test-")
for d in ("service", "bin", "config", "adapters"):
    shutil.copytree(os.path.join(SRC, d), os.path.join(scratch, d))
os.makedirs(os.path.join(scratch, "images", "worker"), exist_ok=True)
shutil.copy(os.path.join(SRC, "images", "worker", "entrypoint.py"),
            os.path.join(scratch, "images", "worker", "entrypoint.py"))
sys.path.insert(0, os.path.join(scratch, "service"))
os.chdir(scratch)

import dispatch  # noqa: E402
import runner as runner_mod  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "worker_entrypoint",
    os.path.join(scratch, "images", "worker", "entrypoint.py"))
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)

# --- validate_ref -----------------------------------------------------------
for good in ("main", "v1.2.3", "feature/foo-bar_baz", "release-2026.09",
             "pull/8/head", "pull/123/head", "a"):
    try:
        out = dispatch.validate_ref(good)
        check(f"ref accepted: {good}", out == good, repr(out))
    except dispatch.DispatchError as e:
        check(f"ref accepted: {good}", False, str(e))

for bad in ("..", "../x", "a/../b", "/etc/passwd", "main; rm -rf /",
            "$(evil)", "`evil`", "a b", "pull/8/head|cat", "x" * 200,
            "main\ncheckout", "feature\\x", "--upload-pack=x"):
    try:
        dispatch.validate_ref(bad)
        check(f"ref rejected: {bad[:20]!r}", False, "no error raised")
    except dispatch.DispatchError as e:
        check(f"ref rejected: {bad[:20]!r}", e.status == 400, str(e))

check("ref None -> None", dispatch.validate_ref(None) is None)
check("ref empty -> None", dispatch.validate_ref("") is None)
try:
    dispatch.validate_ref(123)
    check("ref non-string rejected", False, "no error raised")
except dispatch.DispatchError:
    check("ref non-string rejected", True)

# --- host_seed_clone_url ----------------------------------------------------
cfg_none = {"repos": {"allowlist": ["scratch"]}}
cfg_seed = {"repos": {"allowlist": ["scratch", "https://github.com/acme/priv.git"],
                      "host_seeded": {"https://github.com/acme/priv.git":
                                      {"clone_url": "git@github.com:acme/priv.git"}}}}
check("seeded repo -> clone url",
      runner_mod.host_seed_clone_url(cfg_seed, "https://github.com/acme/priv.git")
      == "git@github.com:acme/priv.git")
check("scratch -> None",
      runner_mod.host_seed_clone_url(cfg_seed, "scratch") is None)
check("unknown repo -> None",
      runner_mod.host_seed_clone_url(cfg_seed, "https://x/y.git") is None)
check("no host_seeded key -> None",
      runner_mod.host_seed_clone_url(cfg_none, "scratch") is None)
check("malformed entry -> None",
      runner_mod.host_seed_clone_url(
          {"repos": {"host_seeded": {"r": "not-a-dict"}}}, "r") is None)
check("empty clone_url -> None",
      runner_mod.host_seed_clone_url(
          {"repos": {"host_seeded": {"r": {"clone_url": ""}}}}, "r") is None)

# --- wait_for_host_seed -----------------------------------------------------
d1 = tempfile.mkdtemp(prefix="ca-seedwait-")
def _plant():
    time.sleep(1)
    os.makedirs(os.path.join(d1, ".git"), exist_ok=True)
threading.Thread(target=_plant, daemon=True).start()
t0 = time.time()
try:
    entrypoint.wait_for_host_seed(d1, timeout_s=30)
    check("wait_for_host_seed returns when .git appears",
          time.time() - t0 < 10, f"took {time.time()-t0:.1f}s")
except RuntimeError as e:
    check("wait_for_host_seed returns when .git appears", False, str(e))
shutil.rmtree(d1, ignore_errors=True)

d2 = tempfile.mkdtemp(prefix="ca-seedwait-timeout-")
t0 = time.time()
try:
    entrypoint.wait_for_host_seed(d2, timeout_s=3)
    check("wait_for_host_seed times out clearly", False, "no error raised")
except RuntimeError as e:
    check("wait_for_host_seed times out clearly",
          time.time() - t0 >= 3 and "timed out" in str(e), str(e))
shutil.rmtree(d2, ignore_errors=True)

# --- submit_job stores ref --------------------------------------------------
res = dispatch.submit_job(type="coding", repo="scratch", task="seed test",
                          ref="pull/8/head",
                          idempotency_key="seed-test-ref-1")
jid = res["id"]
view = dispatch.job_status(jid)
check("submit stores ref", view.get("ref") == "pull/8/head", view.get("ref"))
res2 = dispatch.submit_job(type="coding", repo="scratch", task="seed test 2",
                           idempotency_key="seed-test-ref-2")
check("ref defaults to None",
      dispatch.job_status(res2["id"]).get("ref") is None)
try:
    dispatch.submit_job(type="coding", repo="scratch", task="x", ref="../evil",
                        idempotency_key="seed-test-ref-3")
    check("submit rejects bad ref", False, "no error raised")
except dispatch.DispatchError as e:
    check("submit rejects bad ref", e.status == 400, str(e))

# --- API: POST /jobs with ref -----------------------------------------------
API_CFG = os.path.join(scratch, "config", "api.yaml")
with open(API_CFG, "w") as f:
    f.write("port: 0\nbind: 127.0.0.1\ndefault_client: atlas\n"
            "clients:\n  atlas: test-token-atlas-0001\n")
os.chmod(API_CFG, 0o600)

env = dict(os.environ, CA_API_BIND="127.0.0.1", CA_API_PORT="0",
           CA_API_CONFIG=API_CFG)
proc = subprocess.Popen(
    [sys.executable, os.path.join(scratch, "service", "api.py")],
    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
BASE = None
try:
    line = proc.stdout.readline()
    import re
    m = re.search(r"http://(\S+):(\d+)", line or "")
    assert m, f"server did not print a listen line: {line!r}"
    BASE = f"http://{m.group(1)}:{m.group(2)}"
except Exception as e:
    proc.kill()
    print(f"FAIL server startup — {e}")
    sys.exit(1)


def jpost(path, body):
    r = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer test-token-atlas-0001"})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except Exception:
            return e.code, {"error": raw}


def jget(path):
    r = urllib.request.Request(
        BASE + path,
        headers={"Authorization": "Bearer test-token-atlas-0001"})
    with urllib.request.urlopen(r, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode() or "{}")


s, p = jpost("/jobs", {"type": "coding", "repo": "scratch", "task": "api ref",
                       "ref": "pull/8/head",
                       "idempotency_key": "seed-test-api-ref-1"})
check("API 200 submit with ref", s == 200 and "id" in p, f"{s} {p}")
s2, v = jget(f"/jobs/{p['id']}")
check("API status shows ref",
      s2 == 200 and v.get("ref") == "pull/8/head", f"{s2} {v.get('ref')}")
s, p = jpost("/jobs", {"type": "coding", "repo": "scratch", "task": "x",
                       "ref": "a; rm -rf /",
                       "idempotency_key": "seed-test-api-ref-2"})
check("API 400 bad ref", s == 400 and "ref" in p.get("error", ""), f"{s} {p}")

proc.terminate()
try:
    proc.wait(timeout=10)
except Exception:
    proc.kill()

# --- summary ----------------------------------------------------------------
print(f"\n{len(passed)} passed, {len(failed)} failed")
shutil.rmtree(scratch, ignore_errors=True)
sys.exit(1 if failed else 0)
