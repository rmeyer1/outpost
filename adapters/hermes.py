"""Hermes worker-engine adapter (Phase 1 default).

Contract (shared by every adapter):
  - probe(config) -> (ok: bool, detail: str)
  - container_env(job, engine_cfg, proxy_url, config) -> dict[str, str]
  - parse_result(out_dir: Path) -> dict  (result contract)

The agent process runs INSIDE the job container. Model access is brokered:
the runner starts `hermes proxy` on the Mac host (xai-oauth subscription);
the container gets only the proxy URL + a placeholder client token, which the
proxy replaces with the real credential per request. The host's
~/.hermes/auth.json is never mounted into the container.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

ENGINE_NAME = "hermes"

# Placeholder the in-container agent sends as its Authorization header.
# The host proxy strips and replaces it — it grants nothing by itself.
JOB_CLIENT_TOKEN = "ca-job-token"


def probe(config: dict) -> tuple[bool, str]:
    import shutil
    hermes_bin = Path(config["hermes_venv"]) / "bin" / "hermes"
    if not hermes_bin.exists():
        return False, f"hermes binary missing at {hermes_bin}"
    return True, f"hermes adapter ready (model={config['engines']['hermes']['model']})"


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
