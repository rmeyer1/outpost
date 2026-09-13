"""OpenCode worker-engine adapter (`opencode` CLI, headless).

Contract (shared by every adapter):
  - probe(config) -> (ok: bool, detail: str)
  - container_env(job, engine_cfg, proxy_url, config) -> dict[str, str]
  - parse_result(out_dir: Path) -> dict  (result contract)

The `opencode` CLI (pinned version baked into the worker image) runs INSIDE
the job container via `opencode run`. Model access is brokered exactly like
the grok-build path: opencode.json registers a custom OpenAI-compatible
provider whose baseURL is CA_PROXY_URL (already suffixed /v1) and whose
apiKey is read from CA_CLIENT_TOKEN. The host broker attaches the real
credential or fails over to OpenRouter on 429s — the container never sees
raw credentials.
"""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

ENGINE_NAME = "opencode"

# Placeholder the in-container agent sends as its Authorization header.
# The runner replaces it with the real per-job bearer token before
# `container run`; the host broker validates it and attaches the real
# upstream credential per request.
JOB_CLIENT_TOKEN = "ca-job-token"

# In-container path of the opencode CLI (baked into the worker image).
OPENCODE_BIN = "/usr/local/bin/opencode"


def _container_bin() -> str:
    """Resolve the container CLI the same way service/runner.py does."""
    import os
    import shutil
    from pathlib import Path
    return (os.environ.get("CA_CONTAINER_BIN")
            or shutil.which("container")
            or str(Path.home() / "cloud-agents" / "rt" / "bin" / "container"))


def probe(config: dict) -> tuple[bool, str]:
    """Health check: the worker image must ship a working opencode binary."""
    image = config["image"]["name"]
    opencode_bin = config["engines"]["opencode"].get("opencode_bin", OPENCODE_BIN)
    cbin = _container_bin()
    try:
        r = subprocess.run(
            [cbin, "run", "--rm", "--entrypoint", opencode_bin,
             image, "--version"],
            capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return False, f"container CLI not found at {cbin}"
    except subprocess.TimeoutExpired:
        return False, "opencode --version probe timed out"
    except Exception as exc:
        return False, f"opencode probe failed: {type(exc).__name__}: {exc}"
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        return False, f"opencode binary check failed: {err[-1] if err else 'exit ' + str(r.returncode)}"
    ver = (r.stdout or "").strip().splitlines()
    return True, f"opencode adapter ready ({ver[0] if ver else 'version unknown'})"


def container_env(job: dict, engine_cfg: dict, proxy_url: str, config: dict) -> dict[str, str]:
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
        "CA_OPENCODE_BIN": engine_cfg.get("opencode_bin", OPENCODE_BIN),
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
