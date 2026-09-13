#!/usr/bin/env python3
"""Outpost worker entrypoint — runs INSIDE the disposable job container.

Reads the job from CA_* env vars, prepares /work/repo (scratch init or
allowlisted clone), runs the selected worker engine against the brokered
model proxy, then exports the proof bundle to /out:
  /out/result.json    — result contract (summary, branch, files, usage)
  /out/logs.jsonl     — entrypoint + agent log lines
  /out/repo.bundle    — git bundle of the agent's branch (if any)
  /out/artifacts/     — files the task produced

Engines:
  hermes     — in-container Hermes AIAgent (Python, /opt/hermes-agent).
  grok-build — xAI `grok` CLI headless (`grok -p --output-format json
               --always-approve`). Model access is brokered exactly like the
               Hermes path: a ~/.grok/config.toml custom model points
               base_url at CA_PROXY_URL and reads the bearer from
               CA_CLIENT_TOKEN. The host broker attaches the real credential
               (SuperGrok) or fails over to OpenRouter — the container never
               sees raw credentials and no xAI auth is needed in-container.

The container never sees host credentials: model access goes through
CA_PROXY_URL (host-side proxy/broker, which attaches the real credential).
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import traceback

WORK = "/work"
REPO = "/work/repo"
OUT = "/out"
LOG_PATH = "/out/logs.jsonl"
HOME_DIR = "/work/home"


def sh(*args, cwd=None, check=False, input=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, input=input,
                       timeout=300)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {r.stderr[-500:]}")
    return r


def log(kind, msg):
    os.makedirs(OUT, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps({"ts": time.time(), "kind": kind, "msg": str(msg)}) + "\n")
    print(f"[{kind}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# grok-build engine helpers (importable for unit tests)
# ---------------------------------------------------------------------------
# Host-seeded repos (importable for unit tests)
# ---------------------------------------------------------------------------

def wait_for_host_seed(repo_dir: str, timeout_s: float = 120.0) -> None:
    """Wait for the host to inject a repo tree via `container cp`.

    Private repos are cloned on the host (which holds the GitHub
    credential) and copied into the container after start. The SSH URL
    and any credential stay on the host and never enter the container.
    Raises RuntimeError on timeout.
    """
    deadline = time.time() + timeout_s
    while not os.path.isdir(os.path.join(repo_dir, ".git")):
        if time.time() > deadline:
            raise RuntimeError(
                f"timed out after {timeout_s:.0f}s waiting for host-seeded "
                f"{repo_dir}/.git")
        time.sleep(2)


# ---------------------------------------------------------------------------

def write_grok_config(home: str, proxy_url: str, model: str) -> str:
    """Write ~/.grok/config.toml pointing a custom model at the job broker.

    proxy_url already ends with /v1; the CLI appends /chat/completions.
    env_key names the env var holding the per-job bearer token.
    Returns the config path written.
    """
    grok_dir = os.path.join(home, ".grok")
    os.makedirs(grok_dir, exist_ok=True)
    cfg = (
        "[model.ca-broker]\n"
        f"model = {json.dumps(model)}\n"
        f"base_url = {json.dumps(proxy_url)}\n"
        "name = \"Outpost Agent Broker\"\n"
        "env_key = \"CA_CLIENT_TOKEN\"\n"
        "\n"
        "[models]\n"
        "default = \"ca-broker\"\n"
        "\n"
        "[cli]\n"
        "auto_update = false\n"
    )
    path = os.path.join(grok_dir, "config.toml")
    with open(path, "w") as f:
        f.write(cfg)
    return path


def parse_grok_output(stdout: str) -> tuple[str, dict]:
    """Extract the final text from `grok -p --output-format json` output.

    Returns (text, diagnostics). Falls back to raw stdout tail when the
    output is not parseable JSON.
    """
    text, diag = "", {}
    try:
        data = json.loads((stdout or "").strip())
    except Exception:
        data = None
    if isinstance(data, dict):
        text = data.get("text") or ""
        diag = {"stop_reason": data.get("stopReason"),
                "num_turns": data.get("num_turns"),
                "usage": data.get("usage"),
                "session_id": (data.get("sessionId") or "")[:8]}
    if not text:
        text = (stdout or "").strip()[-6000:]
    return text, diag


def run_agent_grok(task: str, repo: str, model: str, proxy_url: str,
                   job_id: str, max_minutes: int) -> tuple[str, dict, bool]:
    """Run the grok CLI headless agent loop. Returns (final_text, diag, timed_out)."""
    import uuid
    grok_bin = os.environ.get("CA_GROK_BIN", "/usr/local/bin/grok")
    os.makedirs(HOME_DIR, exist_ok=True)
    cfg_path = write_grok_config(HOME_DIR, proxy_url, model)
    log("agent", f"grok config written to {cfg_path} (model={model})")
    # --session-id requires a UUID; derive one deterministically from the job
    # id so a follow-up job for the same task could resume the session.
    session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"outpost:{job_id}"))
    prompt = (
        "You are working in a disposable container. The current working directory "
        "is a git repository — complete the task below. Do NOT run git commit yourself; "
        "file changes are collected automatically. If the task asks you to produce "
        "deliverable files (documents, spreadsheets, reports), write them under ./artifacts/.\n"
        "\n"
        f"Task:\n{task}"
    )
    cmd = [grok_bin, "--no-auto-update", "--no-alt-screen", "--always-approve",
           "--output-format", "json", "-m", "ca-broker",
           "--cwd", repo, "--session-id", session_id, "-p", prompt]
    env = dict(os.environ, HOME=HOME_DIR)
    log("agent", "starting grok-build agent loop")
    try:
        r = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True,
                           timeout=max_minutes * 60)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        text, diag = parse_grok_output(out if isinstance(out, str) else "")
        log("agent", f"grok loop timed out after {max_minutes} min")
        return text, diag, True
    except FileNotFoundError:
        return "", {"error": f"grok binary not found at {grok_bin}"}, False
    if r.stderr.strip():
        log("agent_stderr", r.stderr.strip()[-800:])
    text, diag = parse_grok_output(r.stdout or "")
    if r.returncode != 0 and not text:
        text = f"AGENT ERROR: grok exited {r.returncode}: {(r.stderr or '').strip()[-500:]}"
    return text, diag, False


def run_agent_hermes(task: str, model: str, proxy_url: str, client_token: str,
                     max_minutes: int) -> str:
    """Run the in-container Hermes AIAgent loop. Returns final text."""
    sys.path.insert(0, "/opt/hermes-agent")
    os.environ["HOME"] = HOME_DIR
    os.makedirs(HOME_DIR, exist_ok=True)
    from run_agent import AIAgent

    agent = AIAgent(
        base_url=proxy_url,
        api_key=client_token,   # placeholder — host proxy replaces it
        model=model,
        max_iterations=80,
        run_budget_seconds=max_minutes * 60,
        enabled_toolsets=["terminal", "file"],
        skip_memory=True,
        skip_context_files=True,
        load_soul_identity=False,
        quiet_mode=True,
    )
    log("agent", "starting hermes agent loop")
    final = ""
    try:
        final = agent.chat(task)
    except Exception as exc:
        log("agent_error", f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        final = f"AGENT ERROR: {exc}"
    return final


def main() -> int:
    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    # NEVER trust /out from the image: wipe it first. (A crashed build-time
    # run once baked a stale result.json into an image, which made the
    # runner collect a bogus result instantly.)
    for p in os.listdir(OUT):
        try:
            full = os.path.join(OUT, p)
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
        except Exception:
            pass
    job_id = os.environ["CA_JOB_ID"]
    task = base64.b64decode(os.environ["CA_TASK_B64"]).decode("utf-8")
    repo = os.environ.get("CA_REPO", "scratch")
    base = os.environ.get("CA_BASE", "main")
    branch = os.environ.get("CA_BRANCH", f"agent/{job_id}")
    engine = os.environ.get("CA_ENGINE", "hermes")
    model = os.environ.get("CA_MODEL", "grok-4.6")
    proxy_url = os.environ["CA_PROXY_URL"]
    client_token = os.environ.get("CA_CLIENT_TOKEN", "ca-job-token")
    max_minutes = int(os.environ.get("CA_MAX_MINUTES", "30"))

    log("init", f"job={job_id} type={os.environ.get('CA_TYPE')} engine={engine} "
                f"model={model} repo={repo}")

    # 1. workspace
    os.makedirs(WORK, exist_ok=True)
    if os.environ.get("CA_HOST_SEED") == "1":
        # Private repo, seeded by the host: the runner cloned it with the
        # host-side GitHub credential and injects the tree via
        # `container cp` right after start. Wait for it instead of
        # cloning (the container has no credential and must never get one).
        # CA_REPO still carries the public allowlist entry for the audit trail.
        os.makedirs(REPO, exist_ok=True)
        wait_for_host_seed(REPO)
        log("repo", "host-seeded repo ready")
        # The tree was copied in from the host, so its files are owned by a
        # different UID than this process; tell git the directory is safe.
        sh("git", "config", "--global", "--add", "safe.directory", REPO,
           check=True)
        sh("git", "config", "user.email", "outpost-agent@local", cwd=REPO)
        sh("git", "config", "user.name", "outpost-agent", cwd=REPO)
    elif repo == "scratch":
        os.makedirs(REPO, exist_ok=True)
        sh("git", "init", "-b", base, cwd=REPO, check=True)
        sh("git", "config", "user.email", "outpost-agent@local", cwd=REPO)
        sh("git", "config", "user.name", "outpost-agent", cwd=REPO)
        sh("git", "commit", "--allow-empty", "-m", f"job {job_id}: empty base", cwd=REPO)
    else:
        # Runner validated the allowlist; re-check here at the boundary.
        sh("git", "clone", "--depth", "50", repo, REPO, check=True)
        sh("git", "config", "user.email", "outpost-agent@local", cwd=REPO)
        sh("git", "config", "user.name", "outpost-agent", cwd=REPO)
    sh("git", "checkout", "-b", branch, cwd=REPO, check=True)
    with open(f"{WORK}/task.md", "w") as f:
        f.write(f"# Job {job_id}\n\n{task}\n")
    log("repo", f"branch {branch} ready")
    try:
        before_sha = sh("git", "rev-parse", "HEAD", cwd=REPO).stdout.strip()
    except Exception:
        before_sha = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # empty tree

    # 2. run the agent (tools execute inside THIS container)
    os.chdir(REPO)
    final, extra = "", {}
    timed_out = False
    if engine == "grok-build":
        final, diag, timed_out = run_agent_grok(task, REPO, model, proxy_url,
                                                job_id, max_minutes)
        extra = {"grok": diag}
        log("agent", f"grok stop={diag.get('stop_reason')} "
                     f"turns={diag.get('num_turns')}")
        if timed_out and not final.startswith("AGENT ERROR"):
            final = f"AGENT ERROR: timed out after {max_minutes} minutes\n{final}"
    else:
        final = run_agent_hermes(task, model, proxy_url, client_token, max_minutes)
    log("agent", f"loop finished ({len(final)} chars final)")
    log("agent_final", final[:1500])

    # 3. commit + proof bundle (diff against the pre-agent HEAD so
    # files_changed is accurate even if the agent committed itself)
    sh("git", "add", "-A", cwd=REPO)
    diff = sh("git", "status", "--porcelain", cwd=REPO).stdout.strip()
    if diff:
        sh("git", "commit", "-m",
           f"job {job_id}: agent work", cwd=REPO)
    else:
        log("git", "nothing new to commit (agent already committed)")
    diff_names = sh("git", "diff", "--name-only", before_sha, "HEAD",
                    cwd=REPO).stdout.strip()
    files_changed = [l for l in diff_names.splitlines() if l.strip()]
    bundle_path = f"{OUT}/repo.bundle"
    br = sh("git", "bundle", "create", bundle_path, branch, cwd=REPO)
    bundle_ok = br.returncode == 0

    art_src, art_files = f"{REPO}/artifacts", []
    os.makedirs(f"{OUT}/artifacts", exist_ok=True)
    if os.path.isdir(art_src):
        for root, _, files in os.walk(art_src):
            for fn in files:
                src = os.path.join(root, fn)
                rel = os.path.relpath(src, art_src)
                dst = os.path.join(f"{OUT}/artifacts", rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(src, "rb") as s, open(dst, "wb") as d:
                    d.write(s.read())
                art_files.append(rel)

    result = {
        "job_id": job_id,
        "status": "completed" if "AGENT ERROR" not in final else "failed",
        "engine": engine,
        "model": model,
        "branch": branch,
        "summary": final[:6000],
        "files_changed": files_changed,
        "bundle": "repo.bundle" if bundle_ok else None,
        "artifacts": art_files,
        "duration_s": round(time.time() - t0, 1),
    }
    result.update(extra)
    if "AGENT ERROR" in final:
        result["error"] = final[:500]
    with open(f"{OUT}/result.json", "w") as f:
        json.dump(result, f, indent=2)
    log("done", f"status={result['status']} files={len(files_changed)} "
                f"duration={result['duration_s']}s")
    # Stay alive so the runner can copy /out (it kills us after collecting).
    # Without this, the container exits and `container cp` refuses the copy.
    linger = max(60, max_minutes * 60 - int(time.time() - t0))
    log("linger", f"result written; lingering up to {linger}s for collection")
    time.sleep(min(linger, 1800))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        try:
            log("fatal", f"{type(exc).__name__}: {exc}")
            os.makedirs(OUT, exist_ok=True)
            with open(f"{OUT}/result.json", "w") as f:
                json.dump({"job_id": os.environ.get("CA_JOB_ID", "?"),
                           "status": "failed",
                           "engine": os.environ.get("CA_ENGINE", "hermes"),
                           "summary": "", "error": f"{type(exc).__name__}: {exc}"}, f)
        except Exception:
            pass
        traceback.print_exc()
        # Linger briefly so the runner can collect the failure result.
        try:
            time.sleep(300)
        except Exception:
            pass
        sys.exit(1)
