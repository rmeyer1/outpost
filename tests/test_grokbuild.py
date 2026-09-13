#!/usr/bin/env python3
"""Tests for the grok-build worker-engine adapter.

Covers: registry selection (explicit/auto/fallback), adapter contract
(probe/container_env/parse_result), and the entrypoint's grok helpers
(config.toml generation, --output-format json parsing).

Run: python3 tests/test_grokbuild.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))                      # for adapters.*
sys.path.insert(0, str(ROOT / "images" / "worker"))  # for entrypoint

import adapters.registry as registry  # noqa: E402
import adapters.grok_build as gb       # noqa: E402
import entrypoint as ep                # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{detail}]" if detail and not cond else ""))


ENGINE_CFG = {"model": "grok-4.6", "grok_bin": "/usr/local/bin/grok"}
CONFIG = {"image": {"name": "ca-worker:latest"},
          "engines": {"grok-build": ENGINE_CFG}}

# ------------------------------------------------------------------ registry

name, reason = registry.select("grok-build", "coding")
check("explicit grok-build selects grok-build", name == "grok-build", reason)

name, reason = registry.select("auto", "coding")
check("auto still defaults to hermes for coding", name == "hermes", reason)

name, reason = registry.select("auto", "artifact")
check("auto routes artifact lane to hermes", name == "hermes", reason)

name, reason = registry.select("claude-code", "coding")
check("claude-code now falls back to grok-build", name == "grok-build", reason)

adapter = registry.load_adapter("grok-build")
check("registry loads grok_build adapter", adapter.ENGINE_NAME == "grok-build")

# ------------------------------------------------------------- container_env

job = {"id": "job_test123", "type": "coding", "repo": "scratch", "base": "main",
       "task": "write hello.py", "max_minutes": 30}
env = gb.container_env(job, ENGINE_CFG, "http://192.168.64.1:18645/v1", CONFIG)
check("CA_ENGINE=grok-build", env["CA_ENGINE"] == "grok-build", env.get("CA_ENGINE"))
check("CA_MODEL from engine_cfg", env["CA_MODEL"] == "grok-4.6", env.get("CA_MODEL"))
check("CA_PROXY_URL passed through",
      env["CA_PROXY_URL"] == "http://192.168.64.1:18645/v1")
check("CA_CLIENT_TOKEN is placeholder", env["CA_CLIENT_TOKEN"] == gb.JOB_CLIENT_TOKEN)
check("CA_GROK_BIN set", env["CA_GROK_BIN"] == "/usr/local/bin/grok")
check("CA_BRANCH namespaced", env["CA_BRANCH"] == "agent/job_test123")
check("CA_TASK_B64 decodes", __import__("base64").b64decode(env["CA_TASK_B64"]).decode() == "write hello.py")

# --------------------------------------------------------------- parse_result

d = Path(tempfile.mkdtemp(prefix="ca-gb-"))
(d / "result.json").write_text(json.dumps({"job_id": "j", "status": "completed",
                                           "summary": "did it"}))
r = gb.parse_result(d)
check("parse_result reads result.json",
      r["status"] == "completed" and r["engine"] == "grok-build", r)

d2 = Path(tempfile.mkdtemp(prefix="ca-gb-"))
r = gb.parse_result(d2)
check("parse_result missing file -> failed", r["status"] == "failed")

(d / "result.json").write_text("{not json")
r = gb.parse_result(d)
check("parse_result corrupt file -> failed", r["status"] == "failed")

# --------------------------------------------------------------------- probe

real_run = subprocess.run


calls = []


def fake_ok(*a, **k):
    calls.append(a[0])
    class R:  # noqa: D106
        returncode = 0
        stdout = "grok 1.0.30 (04b7ffed98c6)\n"
        stderr = ""
    return R()


def fake_missing(*a, **k):
    raise FileNotFoundError("no such file")


def fake_bad(*a, **k):
    class R:  # noqa: D106
        returncode = 1
        stdout = ""
        stderr = "binary not found\n"
    return R()


subprocess.run = fake_ok
ok, detail = gb.probe(CONFIG)
check("probe ok on grok --version", ok and "1.0.30" in detail, detail)
check("probe overrides image entrypoint",
      "--entrypoint" in calls[0] and "/usr/local/bin/grok" in calls[0], calls[0])

subprocess.run = fake_missing
ok, detail = gb.probe(CONFIG)
check("probe fails cleanly without container CLI", not ok, detail)

subprocess.run = fake_bad
ok, detail = gb.probe(CONFIG)
check("probe fails on nonzero exit", not ok, detail)
subprocess.run = real_run

# ------------------------------------------------- entrypoint grok helpers

home = Path(tempfile.mkdtemp(prefix="ca-gb-home"))
cfg_path = ep.write_grok_config(str(home), "http://192.168.64.1:18645/v1", "grok-4.6")
text = Path(cfg_path).read_text()
check("config.toml written under ~/.grok",
      cfg_path == str(home / ".grok" / "config.toml"), cfg_path)
check("config base_url points at broker",
      'base_url = "http://192.168.64.1:18645/v1"' in text, text[:120])
check("config model id", 'model = "grok-4.6"' in text)
check("config env_key is CA_CLIENT_TOKEN", 'env_key = "CA_CLIENT_TOKEN"' in text)
check("config default model ca-broker", 'default = "ca-broker"' in text)
check("config disables auto_update", "auto_update = false" in text)

grok_json = json.dumps({"text": "all done", "stopReason": "end_turn",
                        "sessionId": "abc123", "num_turns": 3,
                        "usage": {"input_tokens": 10, "output_tokens": 5}})
t, diag = ep.parse_grok_output(grok_json + "\n")
check("parse_grok_output extracts text", t == "all done", t)
check("parse_grok_output diagnostics",
      diag.get("stop_reason") == "end_turn" and diag.get("num_turns") == 3, diag)

t, _ = ep.parse_grok_output("not json at all\nmore garbage")
check("parse_grok_output falls back to raw tail", "garbage" in t, t[:40])

t, _ = ep.parse_grok_output("")
check("parse_grok_output empty -> empty", t == "")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
