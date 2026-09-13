#!/usr/bin/env python3
"""Tests for the codex / goose / opencode worker-engine adapters.

Covers: contract conformance (probe / container_env / parse_result present
with the right signatures), required CA_* keys and the literal client-token
placeholder, parse_result missing/corrupt result.json, and registry select()
returning each requested engine while never returning a disabled one.

Runs against a SCRATCH copy of the outpost tree (never the live
~/outpost install).

Usage (on the host):  python3 tests/test_adapters.py
Exit 0 = all pass; prints PASS/FAIL per case.
"""
from __future__ import annotations

import base64
import inspect
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f" — {detail}" if detail and not cond else ""))


# --- scratch tree -----------------------------------------------------------
scratch = tempfile.mkdtemp(prefix="ca-adapter-test-")
for d in ("adapters", "config"):
    shutil.copytree(os.path.join(SRC, d), os.path.join(scratch, d))
os.makedirs(os.path.join(scratch, "images", "worker"), exist_ok=True)
shutil.copy(os.path.join(SRC, "images", "worker", "entrypoint.py"),
            os.path.join(scratch, "images", "worker", "entrypoint.py"))
sys.path.insert(0, scratch)
sys.path.insert(0, os.path.join(scratch, "images", "worker"))

import adapters.registry as registry  # noqa: E402
import adapters.codex as codex  # noqa: E402
import adapters.goose as goose  # noqa: E402
import adapters.opencode as opencode  # noqa: E402
import entrypoint as ep  # noqa: E402

ENGINES = {
    "codex": codex,
    "goose": goose,
    "opencode": opencode,
}
BIN_KEYS = {
    "codex": "CA_CODEX_BIN",
    "goose": "CA_GOOSE_BIN",
    "opencode": "CA_OPENCODE_BIN",
}
REQUIRED_CA = (
    "CA_JOB_ID", "CA_TYPE", "CA_REPO", "CA_BASE", "CA_BRANCH",
    "CA_TASK_B64", "CA_ENGINE", "CA_MODEL", "CA_PROXY_URL",
    "CA_CLIENT_TOKEN", "CA_MAX_MINUTES",
)
PROXY = "http://192.168.64.1:18645/v1"
JOB = {"id": "job_test123", "type": "coding", "repo": "scratch", "base": "main",
       "task": "write hello.py", "max_minutes": 30}
ENGINE_CFGS = {
    "codex": {"model": "grok-4.6", "codex_bin": "/usr/local/bin/codex"},
    "goose": {"model": "grok-4.6", "goose_bin": "/usr/local/bin/goose"},
    "opencode": {"model": "grok-4.6", "opencode_bin": "/usr/local/bin/opencode"},
}
CONFIG = {"image": {"name": "ca-worker:latest"}, "engines": ENGINE_CFGS}


# ------------------------------------------------------------------ contract

for name, mod in ENGINES.items():
    check(f"{name} ENGINE_NAME", mod.ENGINE_NAME == name, getattr(mod, "ENGINE_NAME", None))
    check(f"{name} JOB_CLIENT_TOKEN placeholder",
          getattr(mod, "JOB_CLIENT_TOKEN", None) == "ca-job-token")
    for fn_name, params in (
        ("probe", ("config",)),
        ("container_env", ("job", "engine_cfg", "proxy_url", "config")),
        ("parse_result", ("out_dir",)),
    ):
        fn = getattr(mod, fn_name, None)
        check(f"{name}.{fn_name} present", callable(fn))
        if callable(fn):
            sig = inspect.signature(fn)
            got = tuple(sig.parameters)
            check(f"{name}.{fn_name} signature", got == params, f"got {got}")


# ------------------------------------------------------------- container_env

for name, mod in ENGINES.items():
    env = mod.container_env(JOB, ENGINE_CFGS[name], PROXY, CONFIG)
    missing = [k for k in REQUIRED_CA if k not in env]
    check(f"{name} container_env has every CA_* key", not missing, missing)
    check(f"{name} CA_ENGINE", env.get("CA_ENGINE") == name, env.get("CA_ENGINE"))
    check(f"{name} CA_CLIENT_TOKEN is placeholder",
          env.get("CA_CLIENT_TOKEN") == mod.JOB_CLIENT_TOKEN
          and env.get("CA_CLIENT_TOKEN") == "ca-job-token",
          env.get("CA_CLIENT_TOKEN"))
    check(f"{name} CA_PROXY_URL passed through", env.get("CA_PROXY_URL") == PROXY)
    check(f"{name} CA_BRANCH namespaced", env.get("CA_BRANCH") == "agent/job_test123")
    check(f"{name} CA_TASK_B64 decodes",
          base64.b64decode(env["CA_TASK_B64"]).decode() == "write hello.py")
    check(f"{name} CA_MAX_MINUTES", env.get("CA_MAX_MINUTES") == "30")
    bin_key = BIN_KEYS[name]
    check(f"{name} {bin_key} set", bin_key in env and env[bin_key].startswith("/usr/local/bin/"),
          env.get(bin_key))
    # No raw-looking secrets besides the documented placeholder.
    secretish = [f"{k}={v}" for k, v in env.items()
                 if k != "CA_CLIENT_TOKEN"
                 and any(s in k.lower() for s in ("key", "token", "secret", "password"))]
    check(f"{name} container_env has no extra credential keys",
          not secretish, secretish)


# --------------------------------------------------------------- parse_result

for name, mod in ENGINES.items():
    d = Path(tempfile.mkdtemp(prefix="ca-ad-"))
    (d / "result.json").write_text(json.dumps({"job_id": "j", "status": "completed",
                                               "summary": "did it"}))
    r = mod.parse_result(d)
    check(f"{name} parse_result reads result.json",
          r["status"] == "completed" and r["engine"] == name, r)

    d2 = Path(tempfile.mkdtemp(prefix="ca-ad-"))
    r = mod.parse_result(d2)
    check(f"{name} parse_result missing file -> failed",
          r.get("status") == "failed" and "result.json" in r.get("error", ""), r)

    (d / "result.json").write_text("{not json")
    r = mod.parse_result(d)
    check(f"{name} parse_result corrupt file -> failed",
          r.get("status") == "failed" and "unreadable" in r.get("error", ""), r)


# ------------------------------------------------------------------ registry

for name in ENGINES:
    info = registry.ENGINES[name]
    check(f"{name} registered enabled", info.get("enabled") is True, info)
    check(f"{name} keeps fallback chain", bool(info.get("fallback")), info)
    adapter = registry.load_adapter(name)
    check(f"registry loads {name} adapter", adapter.ENGINE_NAME == name)

    selected, reason = registry.select(name, "coding")
    check(f"select({name!r}) returns {name}", selected == name, reason)
    check(f"select({name!r}) engine is enabled",
          registry.ENGINES[selected]["enabled"] is True, selected)

# auto still prefers the default (hermes) for coding; never a disabled engine.
name, reason = registry.select("auto", "coding")
check("auto coding still defaults to hermes", name == "hermes", reason)

for requested, job_type in (
    ("auto", "coding"), ("auto", "artifact"), ("auto", "research"),
    ("auto", "data"), ("hermes", "coding"), ("grok-build", "coding"),
    ("codex", "coding"),
    ("goose", "data"), ("opencode", "coding"),
):
    selected, reason = registry.select(requested, job_type)
    check(f"select({requested!r}, {job_type!r}) never disabled",
          registry.ENGINES[selected]["enabled"] is True,
          f"got {selected} ({reason})")

# Disabled engine is skipped: explicit request falls back along its chain.
saved = dict(registry.ENGINES["codex"])
try:
    registry.ENGINES["codex"]["enabled"] = False
    selected, reason = registry.select("codex", "coding")
    check("disabled codex falls back to grok-build",
          selected == "grok-build", reason)
    check("fallback target is enabled",
          registry.ENGINES[selected]["enabled"] is True)
    # Direct select of an enabled engine still works while another is off.
    selected, _ = registry.select("opencode", "coding")
    check("enabled sibling still selectable", selected == "opencode")
    selected, _ = registry.select("auto", "coding")
    check("auto never returns the disabled engine", selected != "codex")
finally:
    registry.ENGINES["codex"] = saved


# ------------------------------------------- entrypoint broker-pointed configs

home = Path(tempfile.mkdtemp(prefix="ca-ad-home"))

cfg = Path(ep.write_codex_config(str(home), PROXY, "grok-4.6"))
text = cfg.read_text()
check("codex config.toml path", cfg == home / ".codex" / "config.toml")
check("codex base_url is broker", f'base_url = "{PROXY}"' in text, text[:200])
check("codex env_key is CA_CLIENT_TOKEN", 'env_key = "CA_CLIENT_TOKEN"' in text)
check("codex openai_base_url is broker", f'openai_base_url = "{PROXY}"' in text)

cfg = Path(ep.write_goose_config(str(home), PROXY, "grok-4.6"))
text = cfg.read_text()
check("goose config.yaml path", cfg == home / ".config" / "goose" / "config.yaml")
check("goose provider is openai-compatible", "active_provider: openai" in text)
check("goose config has no api key field", "API_KEY" not in text and "api_key" not in text)

cfg = Path(ep.write_opencode_config(str(home), PROXY, "grok-4.6"))
text = cfg.read_text()
data = json.loads(text)
check("opencode.json path", cfg == home / ".config" / "opencode" / "opencode.json")
prov = data["provider"]["ca-broker"]
check("opencode baseURL is broker", prov["options"]["baseURL"] == PROXY)
check("opencode apiKey is env placeholder",
      prov["options"]["apiKey"] == "{env:CA_CLIENT_TOKEN}",
      prov["options"].get("apiKey"))
check("opencode model uses ca-broker", data["model"] == "ca-broker/grok-4.6")


print(f"\n{len(passed)} passed, {len(failed)} failed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
