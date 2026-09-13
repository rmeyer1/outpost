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
  hermes      — in-container Hermes AIAgent (Python, /opt/hermes-agent).
  grok-build  — xAI `grok` CLI headless (`grok -p --output-format json
                --always-approve`). Model access is brokered: a
                ~/.grok/config.toml custom model points base_url at
                CA_PROXY_URL and reads the bearer from CA_CLIENT_TOKEN.
  codex       — OpenAI `codex exec`. ~/.codex/config.toml custom provider
                points base_url at CA_PROXY_URL; env_key = CA_CLIENT_TOKEN.
  goose       — Block `goose run --instructions <task-file>`. OpenAI-compatible
                provider pointed at CA_PROXY_URL; key from CA_CLIENT_TOKEN.
  opencode    — `opencode run`. opencode.json custom provider baseURL =
                CA_PROXY_URL, apiKey = {env:CA_CLIENT_TOKEN}.

The host broker attaches the real credential (SuperGrok) or fails over to
OpenRouter — the container never sees raw credentials.

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
import threading
import time
import traceback

WORK = "/work"
REPO = "/work/repo"
OUT = "/out"
LOG_PATH = "/out/logs.jsonl"
HOME_DIR = "/work/home"
# Non-root user for harness CLIs that refuse to run as root. Created in
# the image (Containerfile); the entrypoint stays root and chowns job dirs.
AGENT_USER = "agent"
AGENT_HOME = "/home/agent"


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
    # Stream the agent's stdout/stderr line-by-line (unbuffered) instead of
    # capturing it all until exit: the runner tails the container log, so
    # the dashboard shows a live session transcript.
    try:
        proc = subprocess.Popen(cmd, cwd=repo, env=env,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except FileNotFoundError:
        return "", {"error": f"grok binary not found at {grok_bin}"}, False
    out_lines: list[str] = []

    def _drain():
        try:
            for line in proc.stdout:
                out_lines.append(line)
                sys.stdout.write(line)
                sys.stdout.flush()
        except Exception:
            pass

    drain = threading.Thread(target=_drain, daemon=True)
    drain.start()
    timed_out = False
    try:
        proc.wait(timeout=max_minutes * 60)
    except subprocess.TimeoutExpired:
        timed_out = True
        log("agent", f"grok loop timed out after {max_minutes} min")
        proc.kill()
    drain.join(timeout=15)
    out = "".join(out_lines)
    if timed_out:
        text, diag = parse_grok_output(out)
        return text, diag, True
    text, diag = parse_grok_output(out)
    if proc.returncode != 0 and not text:
        text = (f"AGENT ERROR: grok exited {proc.returncode}: "
                f"{out.strip()[-500:]}")
    return text, diag, False


TASK_PREAMBLE = (
    "You are working in a disposable container. The current working directory "
    "is a git repository — complete the task below. Do NOT run git commit yourself; "
    "file changes are collected automatically. If the task asks you to produce "
    "deliverable files (documents, spreadsheets, reports), write them under ./artifacts/.\n"
    "\n"
    "Task:\n"
)


def _agent_env(proxy_url: str) -> dict:
    """Process env for a harness CLI: HOME in /work/home, token from CA_CLIENT_TOKEN.

    Never injects raw upstream credentials. The runner has already replaced
    CA_CLIENT_TOKEN with the per-job bearer the broker expects.
    """
    token = os.environ.get("CA_CLIENT_TOKEN", "ca-job-token")
    env = dict(os.environ, HOME=HOME_DIR)
    # OpenAI-compatible CLIs (codex/goose/opencode) read these.
    env["OPENAI_BASE_URL"] = proxy_url
    env["OPENAI_API_KEY"] = token
    return env


def _drop_to_user(username: str) -> None:
    """preexec_fn: drop root privileges to a non-root container user."""
    import pwd
    pw = pwd.getpwnam(username)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)


def _run_cli(cmd, cwd, env, max_minutes, parse_fn, name, bin_path,
             run_as: str | None = None):
    """Run a harness CLI with the job timeout. Returns (text, diag, timed_out).

    run_as: if set, drop privileges to that container user in the child
    process before exec (for CLIs that refuse to run as root).
    """
    log("agent", f"starting {name} agent loop")
    preexec = (lambda: _drop_to_user(run_as)) if run_as else None
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=max_minutes * 60, preexec_fn=preexec)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        text, diag = parse_fn(out if isinstance(out, str) else "")
        log("agent", f"{name} loop timed out after {max_minutes} min")
        return text, diag, True
    except FileNotFoundError:
        return "", {"error": f"{name} binary not found at {bin_path}"}, False
    if (r.stderr or "").strip():
        log("agent_stderr", r.stderr.strip()[-800:])
    text, diag = parse_fn(r.stdout or "")
    if r.returncode != 0 and not text:
        text = (f"AGENT ERROR: {name} exited {r.returncode}: "
                f"{(r.stderr or '').strip()[-500:]}")
    return text, diag, False



# ---------------------------------------------------------------------------
# codex engine helpers (importable for unit tests)
# ---------------------------------------------------------------------------

def write_codex_config(home: str, proxy_url: str, model: str) -> str:
    """Write ~/.codex/config.toml pointing a custom provider at the job broker.

    proxy_url already ends with /v1. env_key names the env var holding the
    per-job bearer token. wire_api = chat because the broker is OpenAI
    chat-completions compatible (not the Responses API).
    Returns the config path written.
    """
    codex_dir = os.path.join(home, ".codex")
    os.makedirs(codex_dir, exist_ok=True)
    cfg = (
        f"model = {json.dumps(model)}\n"
        "model_provider = \"ca-broker\"\n"
        "approval_policy = \"never\"\n"
        "sandbox_mode = \"danger-full-access\"\n"
        f"openai_base_url = {json.dumps(proxy_url)}\n"
        "\n"
        "[model_providers.ca-broker]\n"
        "name = \"Outpost Agent Broker\"\n"
        f"base_url = {json.dumps(proxy_url)}\n"
        "env_key = \"CA_CLIENT_TOKEN\"\n"
        "wire_api = \"chat\"\n"
    )
    path = os.path.join(codex_dir, "config.toml")
    with open(path, "w") as f:
        f.write(cfg)
    return path


def parse_codex_output(stdout: str) -> tuple[str, dict]:
    """Extract the final text from `codex exec` output.

    Without --json, stdout is the final agent message. With --json, stdout
    is JSONL; the last agent_message / turn.completed wins.
    """
    text, diag = "", {}
    raw = (stdout or "").strip()
    last_msg = None
    for line in raw.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        kind = data.get("type") or ""
        item = data.get("item") if isinstance(data.get("item"), dict) else {}
        if kind == "item.completed" and item.get("type") == "agent_message":
            last_msg = item.get("text") or last_msg
        if kind == "turn.completed":
            diag["usage"] = data.get("usage")
        if kind == "turn.failed":
            diag["error"] = data.get("error") or kind
    if last_msg:
        text = last_msg
    if not text:
        text = raw[-6000:]
    return text, diag


def run_agent_codex(task: str, repo: str, model: str, proxy_url: str,
                    job_id: str, max_minutes: int) -> tuple[str, dict, bool]:
    """Run the codex CLI headless agent loop. Returns (final_text, diag, timed_out)."""
    codex_bin = os.environ.get("CA_CODEX_BIN", "/usr/local/bin/codex")
    os.makedirs(HOME_DIR, exist_ok=True)
    cfg_path = write_codex_config(HOME_DIR, proxy_url, model)
    log("agent", f"codex config written to {cfg_path} (model={model})")
    prompt = TASK_PREAMBLE + task
    # Isolated job container: full workspace write.
    # codex pinned to 0.94.0 (last version with the chat wire API): it has no
    # --ephemeral flag, so use --skip-git-repo-check to dodge the
    # trusted-directory gate in disposable containers.
    cmd = [codex_bin, "exec",
           "--sandbox", "danger-full-access",
           "--skip-git-repo-check",
           prompt]
    token = os.environ.get("CA_CLIENT_TOKEN", "ca-job-token")
    env = _agent_env(proxy_url)
    env["CODEX_HOME"] = os.path.join(HOME_DIR, ".codex")
    env["CODEX_API_KEY"] = token
    env["OPENAI_API_KEY"] = token
    env["OPENAI_BASE_URL"] = proxy_url
    return _run_cli(cmd, repo, env, max_minutes, parse_codex_output,
                    "codex", codex_bin)


# ---------------------------------------------------------------------------
# goose engine helpers (importable for unit tests)
# ---------------------------------------------------------------------------

def write_goose_config(home: str, proxy_url: str, model: str) -> str:
    """Write ~/.config/goose/config.yaml pointing goose at the job broker.

    Provider keys stay in the process environment (OPENAI_API_KEY /
    GOOSE_PROVIDER__API_KEY = CA_CLIENT_TOKEN), never in this file.
    Returns the config path written.
    """
    goose_dir = os.path.join(home, ".config", "goose")
    os.makedirs(goose_dir, exist_ok=True)
    cfg = (
        "active_provider: openai\n"
        "providers:\n"
        "  openai:\n"
        "    enabled: true\n"
        f"    model: {json.dumps(model)}\n"
        "    configured: true\n"
        "GOOSE_PROVIDER: openai\n"
        f"GOOSE_MODEL: {json.dumps(model)}\n"
        "GOOSE_MODE: auto\n"
        "GOOSE_DISABLE_SESSION_NAMING: true\n"
    )
    path = os.path.join(goose_dir, "config.yaml")
    with open(path, "w") as f:
        f.write(cfg)
    return path


def parse_goose_output(stdout: str) -> tuple[str, dict]:
    """Extract the final text from `goose run --output-format json` output."""
    text, diag = "", {}
    raw = (stdout or "").strip()
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line)
                    break
                except Exception:
                    continue
    if isinstance(data, dict):
        text = (data.get("response") or data.get("text")
                or data.get("result") or data.get("message") or "")
        if isinstance(text, dict):
            text = text.get("text") or json.dumps(text)
        diag = {"usage": data.get("usage"),
                "session_id": str(data.get("session_id") or data.get("id") or "")[:8]}
    if not text:
        text = raw[-6000:]
    return text, diag


def run_agent_goose(task: str, repo: str, model: str, proxy_url: str,
                    job_id: str, max_minutes: int) -> tuple[str, dict, bool]:
    """Run the goose CLI headless agent loop. Returns (final_text, diag, timed_out)."""
    goose_bin = os.environ.get("CA_GOOSE_BIN", "/usr/local/bin/goose")
    os.makedirs(HOME_DIR, exist_ok=True)
    cfg_path = write_goose_config(HOME_DIR, proxy_url, model)
    log("agent", f"goose config written to {cfg_path} (model={model})")
    instructions_path = os.path.join(HOME_DIR, "goose-instructions.md")
    with open(instructions_path, "w") as f:
        f.write(TASK_PREAMBLE + task + "\n")
    cmd = [goose_bin, "run",
           "--instructions", instructions_path,
           "--quiet",
           "--no-session",
           "--output-format", "json",
           "--provider", "openai",
           "--model", model]
    token = os.environ.get("CA_CLIENT_TOKEN", "ca-job-token")
    env = _agent_env(proxy_url)
    env["GOOSE_PROVIDER"] = "openai"
    env["GOOSE_MODEL"] = model
    env["GOOSE_MODE"] = "auto"
    env["GOOSE_DISABLE_KEYRING"] = "1"
    env["GOOSE_DISABLE_SESSION_NAMING"] = "true"
    env["GOOSE_PROVIDER__TYPE"] = "openai"
    # GOOSE_PROVIDER__HOST is the API origin; OPENAI_HOST likewise. Strip a
    # trailing /v1 so the origin is bare.
    host = proxy_url[:-3] if proxy_url.endswith("/v1") else proxy_url
    env["GOOSE_PROVIDER__HOST"] = host
    env["GOOSE_PROVIDER__API_KEY"] = token
    env["OPENAI_HOST"] = host
    # Verified against goose 1.50.0: the chat POST goes to exactly
    # OPENAI_HOST/OPENAI_BASE_PATH (it does NOT append /chat/completions),
    # while the models check goes to {GOOSE_PROVIDER__HOST}/v1/models.
    env["OPENAI_BASE_PATH"] = "v1/chat/completions"
    env["OPENAI_API_KEY"] = token
    env["OPENAI_BASE_URL"] = proxy_url
    env["XDG_CONFIG_HOME"] = os.path.join(HOME_DIR, ".config")
    return _run_cli(cmd, repo, env, max_minutes, parse_goose_output,
                    "goose", goose_bin)


# ---------------------------------------------------------------------------
# opencode engine helpers (importable for unit tests)
# ---------------------------------------------------------------------------

def write_opencode_config(home: str, proxy_url: str, model: str) -> str:
    """Write opencode.json with a custom OpenAI-compatible provider.

    baseURL is the broker (already /v1). apiKey is interpolated from
    CA_CLIENT_TOKEN at runtime — the file never contains a raw credential.
    Returns the config path written.
    """
    oc_dir = os.path.join(home, ".config", "opencode")
    os.makedirs(oc_dir, exist_ok=True)
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "model": f"ca-broker/{model}",
        "autoupdate": False,
        "provider": {
            "ca-broker": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Outpost Agent Broker",
                "options": {
                    "baseURL": proxy_url,
                    "apiKey": "{env:CA_CLIENT_TOKEN}",
                },
                "models": {
                    model: {"name": model},
                },
            }
        },
    }
    path = os.path.join(oc_dir, "opencode.json")
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    return path


def parse_opencode_output(stdout: str) -> tuple[str, dict]:
    """Extract the final text from `opencode run` output."""
    text, diag = "", {}
    raw = (stdout or "").strip()
    last_text = None
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        data = None
    if isinstance(data, dict):
        last_text = (data.get("text") or data.get("result")
                     or data.get("message") or data.get("data") or "")
        if isinstance(last_text, dict):
            last_text = last_text.get("text") or json.dumps(last_text)
        diag = {"usage": data.get("usage")}
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            part = (obj.get("text") or obj.get("part") or obj.get("message")
                    or "")
            if isinstance(part, dict):
                part = part.get("text") or ""
            if part:
                last_text = part
    if last_text:
        text = last_text if isinstance(last_text, str) else str(last_text)
    if not text:
        text = raw[-6000:]
    return text, diag


def run_agent_opencode(task: str, repo: str, model: str, proxy_url: str,
                       job_id: str, max_minutes: int) -> tuple[str, dict, bool]:
    """Run the opencode CLI headless agent loop. Returns (final_text, diag, timed_out)."""
    opencode_bin = os.environ.get("CA_OPENCODE_BIN", "/usr/local/bin/opencode")
    os.makedirs(HOME_DIR, exist_ok=True)
    cfg_path = write_opencode_config(HOME_DIR, proxy_url, model)
    log("agent", f"opencode config written to {cfg_path} (model={model})")
    prompt = TASK_PREAMBLE + task
    cmd = [opencode_bin, "run",
           "--auto",
           "--model", f"ca-broker/{model}",
           "--dir", repo,
           prompt]
    env = _agent_env(proxy_url)
    env["OPENCODE_CONFIG"] = cfg_path
    env["XDG_CONFIG_HOME"] = os.path.join(HOME_DIR, ".config")
    env["OPENAI_API_KEY"] = os.environ.get("CA_CLIENT_TOKEN", "ca-job-token")
    env["OPENAI_BASE_URL"] = proxy_url
    return _run_cli(cmd, repo, env, max_minutes, parse_opencode_output,
                    "opencode", opencode_bin)


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
    cli_runners = {
        "grok-build": ("grok", run_agent_grok),
        "codex": ("codex", run_agent_codex),
        "goose": ("goose", run_agent_goose),
        "opencode": ("opencode", run_agent_opencode),
    }
    if engine in cli_runners:
        extra_key, runner = cli_runners[engine]
        final, diag, timed_out = runner(task, REPO, model, proxy_url,
                                        job_id, max_minutes)
        extra = {extra_key: diag}
        log("agent", f"{engine} stop={diag.get('stop_reason')} "
                     f"turns={diag.get('num_turns')}")
        if timed_out and not (final or "").startswith("AGENT ERROR"):
            final = f"AGENT ERROR: timed out after {max_minutes} minutes\n{final}"
    else:
        final = run_agent_hermes(task, model, proxy_url, client_token, max_minutes)
    log("agent", f"loop finished ({len(final)} chars final)")
    log("agent_final", final[:1500])

    # 3. commit + proof bundle (diff against the pre-agent HEAD so
    # files_changed is accurate even if the agent committed itself).
    # A completed job must never silently deliver an empty branch: fail
    # loudly if the end-of-job add/commit errors, and flag any leftovers.
    #
    # Wrong-branch hardening: the agent sometimes commits on a branch it
    # cut itself instead of the job branch. Detect that, warn loudly in
    # result.json, and bundle ALL local branches as a safety net so the
    # work survives the container either way.
    r = sh("git", "add", "-A", cwd=REPO)
    if r.returncode != 0:
        raise RuntimeError(f"finalize: git add -A failed: {r.stderr[-500:]}")
    diff = sh("git", "status", "--porcelain", cwd=REPO).stdout.strip()
    if diff:
        r = sh("git", "commit", "-m", f"job {job_id}: agent work", cwd=REPO)
        if r.returncode != 0:
            raise RuntimeError(f"finalize: git commit failed: {r.stderr[-500:]}")
        log("git", f"committed agent work ({len(diff.splitlines())} paths)")
    else:
        log("git", "nothing new to commit (agent already committed)")
    # Belt and braces: if the tree STILL is not clean after the commit,
    # surface it explicitly in result.json instead of silently shipping
    # an incomplete branch.
    leftover = sh("git", "status", "--porcelain", cwd=REPO).stdout.strip()
    uncommitted_warning = ""
    if leftover:
        uncommitted_warning = (
            f"finalize: {len(leftover.splitlines())} paths still uncommitted "
            f"after commit: {leftover[:500]}")
        log("git", "WARNING: " + uncommitted_warning)

    def _commits_ahead(ref):
        # Commits on ref that are not reachable from the pre-agent seed.
        if sh("git", "cat-file", "-t", before_sha,
              cwd=REPO).stdout.strip() != "commit":
            return 0
        rc = sh("git", "rev-list", "--count", f"{before_sha}..{ref}",
                cwd=REPO)
        if rc.returncode != 0:
            return 0
        try:
            return int(rc.stdout.strip())
        except ValueError:
            return 0

    job_tip = sh("git", "rev-parse", "--verify", branch,
                 cwd=REPO).stdout.strip()
    new_commits = _commits_ahead(job_tip) if job_tip else 0
    branch_advanced = new_commits > 0
    work_on_other_branch = []
    if not branch_advanced:
        for ref in sh("git", "for-each-ref", "--format=%(refname:short)",
                      "refs/heads/", cwd=REPO).stdout.splitlines():
            ref = ref.strip()
            if not ref or ref == branch:
                continue
            if _commits_ahead(ref) > 0:
                work_on_other_branch.append(ref)
        if work_on_other_branch:
            log("git", "WARNING: agent committed on other branch(es): "
                + ", ".join(work_on_other_branch)
                + " - bundling all branches as a safety net")
    no_work_warning = ""
    if not branch_advanced and not work_on_other_branch and not leftover:
        no_work_warning = (
            "finalize: job branch did not advance past the seed and no "
            "work was found on any other branch or in the working tree")
        log("git", "WARNING: " + no_work_warning)

    diff_names = sh("git", "diff", "--name-only", before_sha, "HEAD",
                    cwd=REPO).stdout.strip()
    files_changed = [l for l in diff_names.splitlines() if l.strip()]
    # Safety net: bundle every local branch, not just the job branch, so
    # work the agent committed elsewhere is still recoverable.
    bundle_path = f"{OUT}/repo.bundle"
    all_refs = [x.strip() for x in
                sh("git", "for-each-ref", "--format=%(refname)",
                   "refs/heads/", cwd=REPO).stdout.splitlines()
                if x.strip()]
    br = sh("git", "bundle", "create", bundle_path, *all_refs, cwd=REPO)
    bundle_ok = br.returncode == 0
    if not bundle_ok and all_refs:
        log("git", "WARNING: all-branch bundle failed "
            f"({br.stderr[-300:]}); falling back to job branch only")
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
        "uncommitted_changes": uncommitted_warning,
        "new_commits": new_commits,
        "branch_advanced": branch_advanced,
        "work_on_other_branch": work_on_other_branch,
        "no_work_captured": no_work_warning,
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
