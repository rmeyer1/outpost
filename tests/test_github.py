#!/usr/bin/env python3
"""Tests for opt-in GitHub access for Outpost agents (--github).

Covers:
- redact: ghp_/github_pat_/gho_ patterns + registered GH_TOKEN redaction.
- db: github_access column migration; insert_job default False / True.
- dispatch.submit_job: github_access stored; blocked (400) when
  github.enabled=false in config.
- runner.read_github_token: parses `gh auth token`; clear error when the
  host has no gh auth.
- entrypoint.setup_github_access: no-op without GH_TOKEN; with a token it
  verifies via `gh auth status`, persists the token to gh hosts.yml
  (mode 600) under both agent homes, configures a system-level git
  credential helper via `gh auth git-credential`, and rewrites an
  SSH origin to https.
- entrypoint GITHUB_PREAMBLE: documents push + gh pr create.
- images/worker Containerfile/Dockerfile: gh CLI pinned + installed.

Runs against a SCRATCH copy of the outpost tree (never a live install).

Usage:  python3 tests/test_github.py
Exit 0 = all pass; prints PASS/FAIL per case.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))


# --- scratch tree -----------------------------------------------------------
scratch = tempfile.mkdtemp(prefix="ca-github-test-")
for d in ("service", "bin", "config", "adapters"):
    shutil.copytree(os.path.join(SRC, d), os.path.join(scratch, d))
os.makedirs(os.path.join(scratch, "images", "worker"), exist_ok=True)
shutil.copy(os.path.join(SRC, "images", "worker", "entrypoint.py"),
            os.path.join(scratch, "images", "worker", "entrypoint.py"))
sys.path.insert(0, os.path.join(scratch, "service"))
os.chdir(scratch)

import dispatch  # noqa: E402
import runner as runner_mod  # noqa: E402
import redact  # noqa: E402
from db import connect, insert_job, new_job_id  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "worker_entrypoint",
    os.path.join(scratch, "images", "worker", "entrypoint.py"))
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)

# --- redact -----------------------------------------------------------------
check("ghp_ token redacted",
      redact.redact("push with ghp_abcdef1234567890xyz done") ==
      "push with ghp_<REDACTED> done")
check("github_pat_ token redacted",
      "github_pat_<REDACTED>" in redact.redact("k=github_pat_ABCdef1234567890_xyz"))
check("gho_ token redacted",
      "gho_<REDACTED>" in redact.redact("k=gho_OAuthToken1234567890"))
redact.register_secret("test-gh-token-secret-1")
check("registered GH_TOKEN value redacted",
      redact.redact("export GH_TOKEN=test-gh-token-secret-1 ok") ==
      "export GH_TOKEN=<REDACTED> ok")
check("GH_TOKEN not in manifest env dump",
      "GH_TOKEN" not in redact.redact_env({"GH_TOKEN": "x", "CA_JOB_ID": "j"}))

# --- db ---------------------------------------------------------------------
db = connect(os.path.join(scratch, "state", "test.db"))
cols = {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}
check("github_access column migrated", "github_access" in cols)
j1 = insert_job(db, job_id=new_job_id(), type="coding", repo="scratch",
                base="main", task="t", engine_requested="auto",
                budget_usd=1.0, max_minutes=5)
check("github_access defaults to 0", j1["github_access"] == 0, repr(j1.get("github_access")))
j2 = insert_job(db, job_id=new_job_id(), type="coding", repo="scratch",
                base="main", task="t", engine_requested="auto",
                budget_usd=1.0, max_minutes=5, github_access=True)
check("github_access=True stored as 1", j2["github_access"] == 1)

# --- dispatch.submit_job ----------------------------------------------------
# Scratch config is the real agents.yaml (github.enabled: true).
r = dispatch.submit_job(type="coding", repo="scratch", task="do it",
                        github_access=True)
check("submit_job --github stored",
      dispatch.get_job(dispatch._db(), r["id"])["github_access"] == 1)
check("submit_job --github id returned", r["deduplicated"] is False)

# Kill switch: github.enabled=false blocks --github submits.
# (Target the github: section specifically — engines have their own
# enabled flags.)
cfg_path = os.path.join(scratch, "config", "agents.yaml")
with open(cfg_path) as f:
    cfg_text = f.read()
import re as _re
cfg_disabled, n = _re.subn(r"(?m)^(github:\n  enabled:) true$",
                           r"\1 false", cfg_text)
assert n == 1, f"github section not found uniquely (n={n})"
with open(cfg_path, "w") as f:
    f.write(cfg_disabled)
try:
    dispatch.submit_job(type="coding", repo="scratch", task="do it",
                        github_access=True)
    check("github.enabled=false blocks --github", False, "no error raised")
except dispatch.DispatchError as e:
    check("github.enabled=false blocks --github", e.status == 400, str(e))
# ...but plain submits still work.
r2 = dispatch.submit_job(type="coding", repo="scratch", task="plain")
check("github.enabled=false allows plain submits",
      dispatch.get_job(dispatch._db(), r2["id"])["github_access"] == 0)
with open(cfg_path, "w") as f:
    f.write(cfg_text)  # restore

# --- runner.read_github_token -----------------------------------------------
class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

real_run = runner_mod.run

# find_gh_binary: CA_GH_BIN override wins over PATH.
os.environ["CA_GH_BIN"] = "/custom/path/gh"
check("find_gh_binary honors CA_GH_BIN",
      runner_mod.find_gh_binary() == "/custom/path/gh")
del os.environ["CA_GH_BIN"]

# find_gh_binary: falls back to well-known locations when not on PATH.
real_which = shutil.which
runner_mod_shutil_which = runner_mod.shutil.which
runner_mod.shutil.which = lambda *a, **k: None
fake_gh = os.path.join(scratch, "fakebin", "gh")
os.makedirs(os.path.dirname(fake_gh), exist_ok=True)
open(fake_gh, "w").write("#!/bin/sh\n")
os.chmod(fake_gh, 0o755)
runner_mod._GH_FALLBACKS = (fake_gh,)
try:
    check("find_gh_binary falls back to known locations",
          runner_mod.find_gh_binary() == fake_gh)
finally:
    runner_mod.shutil.which = runner_mod_shutil_which
    runner_mod._GH_FALLBACKS = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")

# find_gh_binary: clear error when gh is nowhere.
runner_mod.shutil.which = lambda *a, **k: None
runner_mod._GH_FALLBACKS = ()
os.environ.pop("CA_GH_BIN", None)
try:
    runner_mod.find_gh_binary()
    check("find_gh_binary errors clearly with no gh", False, "no error raised")
except RuntimeError as e:
    check("find_gh_binary errors clearly with no gh", "`gh` CLI was not found" in str(e))
finally:
    runner_mod.shutil.which = runner_mod_shutil_which
    runner_mod._GH_FALLBACKS = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")

os.environ["CA_GH_BIN"] = "/fake/gh"  # keep find_gh_binary out of it
runner_mod.run = lambda cmd, **kw: _FakeCompleted(0, "  host-token-abc123\n", "")
try:
    check("read_github_token strips output",
          runner_mod.read_github_token() == "host-token-abc123")
    check("read_github_token calls gh auth token",
          True)  # argv asserted below via capture
finally:
    runner_mod.run = real_run

calls = []
def _capture(cmd, **kw):
    calls.append(cmd)
    return _FakeCompleted(0, "tok\n", "")
runner_mod.run = _capture
try:
    runner_mod.read_github_token()
    check("read_github_token runs gh auth token",
          calls and calls[0][:2] == [calls[0][0], "auth"] and "token" in calls[0],
          repr(calls))
finally:
    runner_mod.run = real_run

runner_mod.run = lambda cmd, **kw: _FakeCompleted(
    1, "", "error: not logged in to any hosts")
try:
    runner_mod.read_github_token()
    check("read_github_token fails clearly without host auth", False,
          "no error raised")
except RuntimeError as e:
    check("read_github_token fails clearly without host auth",
          "gh auth login" in str(e), str(e)[:100])
finally:
    runner_mod.run = real_run
    os.environ.pop("CA_GH_BIN", None)

# --- entrypoint.setup_github_access -----------------------------------------
# No GH_TOKEN: no-op, never touches git.
real_sh, real_log = entrypoint.sh, entrypoint.log
entrypoint.sh = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("git must not run without GH_TOKEN"))
entrypoint.log = lambda *a, **k: None
os.environ.pop("GH_TOKEN", None)
try:
    check("setup_github_access no-op without GH_TOKEN",
          entrypoint.setup_github_access() is False)
finally:
    entrypoint.sh, entrypoint.log = real_sh, real_log

# With GH_TOKEN: <redacted>, system credential helper, SSH origin
# rewrite (fake the shell; redirect hosts.yml writes to tmp homes).
sh_calls = []
FAKE_ORIGIN = "git@github.com:rmeyer1/rain-room.git"
def _fake_sh(*args, **kw):
    sh_calls.append(list(args))
    if args[:3] == ("git", "remote", "get-url"):
        return _FakeCompleted(0, FAKE_ORIGIN + "\n", "")
    if args[:3] == ("gh", "api", "user"):
        return _FakeCompleted(0, "rmeyer1\n", "")
    if args[:2] == ("gh", "auth"):
        return _FakeCompleted(0, "Logged in to github.com account rmeyer1\n", "")
    return _FakeCompleted(0, "", "")
real_wgh = entrypoint.write_gh_hosts
tmp_homes = [tempfile.mkdtemp(prefix="ca-gh-home1-"),
             tempfile.mkdtemp(prefix="ca-gh-home2-")]
entrypoint.sh = _fake_sh
entrypoint.log = lambda *a, **k: None
entrypoint.write_gh_hosts = lambda token, user: real_wgh(
    token, user, homes=tuple(tmp_homes))
os.environ["GH_TOKEN"] = "test-gh-token-secret-1"
try:
    repo_dir = tempfile.mkdtemp(prefix="ca-gh-origin-")
    subprocess.run(["git", "init", "-q", repo_dir], check=True)
    subprocess.run(["git", "-C", repo_dir, "remote", "add", "origin",
                    FAKE_ORIGIN], check=True)
    real_repo = entrypoint.REPO
    entrypoint.REPO = repo_dir
    try:
        ok = entrypoint.setup_github_access()
    finally:
        entrypoint.REPO = real_repo
    check("setup_github_access True with GH_TOKEN", ok is True)
    helper_calls = [c for c in sh_calls if "credential.helper" in c]
    check("system git credential helper via gh auth git-credential",
          any(c[:3] == ["git", "config", "--system"] and
              "gh auth git-credential" in " ".join(c)
              for c in helper_calls),
          repr(helper_calls[:1]))
    hosts_paths = [os.path.join(h, ".config", "gh", "hosts.yml")
                   for h in tmp_homes]
    check("hosts.yml written under both agent homes",
          all(os.path.isfile(p) for p in hosts_paths), repr(hosts_paths))
    perms_ok = all(oct(os.stat(p).st_mode & 0o777) == "0o600"
                   for p in hosts_paths)
    check("hosts.yml mode 600", perms_ok)
    bodies = [open(p).read() for p in hosts_paths]
    check("hosts.yml carries the token and user",
          all("test-gh-token-secret-1" in b and "rmeyer1" in b
              for b in bodies))
    seturl = [c for c in sh_calls if c[:3] == ["git", "remote", "set-url"]]
    check("SSH origin rewritten to https",
          any("https://github.com/rmeyer1/rain-room.git" in c for c in seturl),
          repr(seturl))
finally:
    entrypoint.sh, entrypoint.log = real_sh, real_log
    entrypoint.write_gh_hosts = real_wgh
    os.environ.pop("GH_TOKEN", None)

# https origin is left alone.
sh_calls2 = []
def _fake_sh2(*args, **kw):
    sh_calls2.append(list(args))
    if args[:3] == ("git", "remote", "get-url"):
        return _FakeCompleted(0, "https://github.com/rmeyer1/outpost.git\n", "")
    return _FakeCompleted(0, "", "")
entrypoint.sh = _fake_sh2
entrypoint.log = lambda *a, **k: None
os.environ["GH_TOKEN"] = "x"
try:
    entrypoint.normalize_github_origin(tempfile.mkdtemp())
    check("https origin left untouched",
          not [c for c in sh_calls2 if c[:3] == ["git", "remote", "set-url"]])
finally:
    entrypoint.sh, entrypoint.log = real_sh, real_log
    os.environ.pop("GH_TOKEN", None)

check("GITHUB_PREAMBLE documents push",
      "git push origin" in entrypoint.GITHUB_PREAMBLE)
check("GITHUB_PREAMBLE documents gh pr create",
      "gh pr create" in entrypoint.GITHUB_PREAMBLE)
check("GITHUB_PREAMBLE overrides do-not-commit",
      "do not commit" in entrypoint.GITHUB_PREAMBLE.lower())

# --- worker image files ------------------------------------------------------
for img in ("images/worker/Containerfile", "images/worker/Dockerfile"):
    p = os.path.join(SRC, img)
    txt = open(p).read()
    check(f"{img} pins gh version", "GH_VERSION=" in txt)
    check(f"{img} installs gh", "cli/cli/releases/download" in txt)
    check(f"{img} verifies gh", "gh --version" in txt)
with open(os.path.join(SRC, "images", "worker", "Containerfile")) as f:
    cf = f.read()
with open(os.path.join(SRC, "images", "worker", "Dockerfile")) as f:
    df = f.read()
check("Containerfile and Dockerfile stay identical", cf == df)

# --- agentctl ----------------------------------------------------------------
agentctl = open(os.path.join(SRC, "bin", "agentctl")).read()
check("agentctl has --github flag", '"--github"' in agentctl)
check("agentctl sends github_access", '"github_access"' in agentctl)

# --- api.py ------------------------------------------------------------------
api_src = open(os.path.join(SRC, "service", "api.py")).read()
check("api passes github_access", 'github_access=bool(body.get("github_access"))' in api_src)

print(f"\n{len(passed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
