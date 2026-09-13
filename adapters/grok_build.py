"""Grok Build worker-engine adapter (xAI `grok` CLI, headless).

Contract (shared by every adapter):
  - probe(config) -> (ok: bool, detail: str)
  - container_env(job, engine_cfg, proxy_url, config) -> dict[str, str]
  - parse_result(out_dir: Path) -> dict  (result contract)

The `grok` CLI (pinned version baked into the worker image) runs INSIDE the
job container via `grok -p --output-format json --always-approve`. Model
access is brokered exactly like the Hermes path: the entrypoint writes a
~/.grok/config.toml whose custom model points base_url at the per-job broker
(CA_PROXY_URL, already suffixed /v1) and reads the bearer from the
CA_CLIENT_TOKEN env var. The host broker attaches the real credential
(SuperGrok subscription) or fails over to OpenRouter on 429s — the container
never sees raw credentials, and no xAI auth is needed in-container
(verified: headless + custom model works with no XAI_API_KEY).

Spend policy, token telemetry, 45-min attention, and the 3-hour ceiling are
all enforced host-side by the broker/runner/watchdog, so they apply to
grok-build jobs unchanged.
"""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

ENGINE_NAME = "grok-build"

# Placeholder the in-container agent sends as its Authorization header.
# The runner replaces it with the real per-job bearer token before
# `container run`; the host broker validates it and attaches the real
# upstream credential per request.
JOB_CLIENT_TOKEN = "ca-job-token"

# In-container path of the grok CLI (baked into the worker image).
GROK_BIN = "/usr/local/bin/grok"


def _container_bin() -> str:
    """Resolve the container CLI the same way service/runner.py does."""
    import os
    import shutil
    import sys
    from pathlib import Path
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "service"))
        from containers import container_bin
        return container_bin()
    except Exception:
        return (os.environ.get("CA_CONTAINER_BIN")
                or shutil.which("docker")
                or shutil.which("container")
                or str(Path.home() / "cloud-agents" / "rt" / "bin" / "container"))


def probe(config: dict) -> tuple[bool, str]:
    """Health check: the worker image must ship a working grok binary."""
    image = config["image"]["name"]
    grok_bin = config["engines"]["grok-build"].get("grok_bin", GROK_BIN)
    cbin = _container_bin()
    try:
        r = subprocess.run(
            [cbin, "run", "--rm", "--entrypoint", grok_bin,
             image, "--version"],
            capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return False, f"container CLI not found at {cbin}"
    except subprocess.TimeoutExpired:
        return False, "grok --version probe timed out"
    except Exception as exc:
        return False, f"grok probe failed: {type(exc).__name__}: {exc}"
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        return False, f"grok binary check failed: {err[-1] if err else 'exit ' + str(r.returncode)}"
    ver = (r.stdout or "").strip().splitlines()
    return True, f"grok-build adapter ready ({ver[0] if ver else 'version unknown'})"


def container_env(job: dict, engine_cfg: dict, proxy_url: str, config: dict) -> dict:
    task_b64 = base64.b64encode(job["task"].encode("utf-8")).decode("ascii")
    return {
        "CA_JOB_ID": job["id"],
        "CA_TYPE": job["type"],
        "CA_REPO": job["repo"],
        "CA_BASE": job["base"],
        "CA_BRANCH": f"agent/{job['id']}",
        "CA_TASK_B64": task_b64,
        "CA_ENGINE": ENGINE_NAME,
        "CA_MODEL": engine_cfg.get("model", "grok-4.6"),
        "CA_GROK_BIN": engine_cfg.get("grok_bin", GROK_BIN),
        "CA_PROXY_URL": proxy_url,
        "CA_CLIENT_TOKEN": JOB_CLIENT_TOKEN,
        "CA_MAX_MINUTES": str(job["max_minutes"] or 30),
    }


def parse_result(out_dir: Path) -> dict:
    """Read /out/result.json produced by the in-container entrypoint."""
    result_path = out_dir / "result.json"
    if not result_path.exists():
        return {"status": "failed",
                "error": "entrypoint produced no result.json",
                "summary": ""}
    try:
        data = json.loads(result_path.read_text())
    except Exception as exc:  # corrupted JSON
        return {"status": "failed", "error": f"result.json unreadable: {exc}",
                "summary": ""}
    data.setdefault("engine", ENGINE_NAME)
    return data
