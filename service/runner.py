"""Per-job runner: disposable container -> task -> artifacts -> destroy.

Usage: python3 service/runner.py <job-id>

One OS process per job, spawned by the controller. Flow:
  1. probe + select engine (registry)
  2. start per-job model proxy on the host (hermes proxy, xai-oauth)
  3. container run --detach with the job env (no host mounts, ever)
  4. poll until exit / timeout / cancel_requested
  5. copy /out from the container, write manifest, record result
  6. ALWAYS: container rm -f + proxy shutdown (kill switch)

Hard limits enforced here: wall time (max_minutes), CPU, memory.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "service"))

import yaml  # noqa: E402
from db import (  # noqa: E402
    ACTIVE, connect, get_job, log_event, record_spend, rolling_spend_usd,
    token_breakdown, token_totals, update_job,
)
from redact import redact, redact_env, register_secret  # noqa: E402
import adapters.registry as registry  # noqa: E402
import openrouter as or_provider  # noqa: E402
import pricing  # noqa: E402
from timeouts import load_timeouts  # noqa: E402


def load_config() -> dict:
    with open(ROOT / "config" / "agents.yaml") as f:
        return yaml.safe_load(f)


def load_spend() -> dict:
    with open(ROOT / "config" / "spend.yaml") as f:
        return yaml.safe_load(f)


def container_bin() -> str:
    return os.environ.get("CA_CONTAINER_BIN", str(Path.home() / "cloud-agents" / "rt" / "bin" / "container"))


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 60), **kw)


def host_seed_clone_url(config: dict, repo: str) -> str | None:
    """Host-side clone URL for a host-seeded repo, or None.

    Host-seeded repos are private: the host (which holds the GitHub
    credential) clones the repo and injects the tree into the container
    via `container cp`. The SSH URL never enters the container — the
    container only ever sees the public allowlist entry string.
    """
    seed_cfg = (config.get("repos") or {}).get("host_seeded") or {}
    entry = seed_cfg.get(repo)
    if isinstance(entry, dict):
        url = entry.get("clone_url")
        return url if isinstance(url, str) and url else None
    return None


def seed_repo_on_host(clone_url: str, ref: str | None, seed_dir: str, log) -> None:
    """Clone a private repo on the HOST into seed_dir, checking out ref.

    All git invocations use argv arrays (no shell). `ref` was validated at
    the API boundary (branch/tag charset or pull/N/head); `clone_url` comes
    from the host-side config, never from the client.
    """
    os.makedirs(seed_dir, mode=0o700, exist_ok=True)
    r = run(["git", "clone", "--depth", "50", clone_url, seed_dir],
            timeout=300)
    if r.returncode != 0:
        raise RuntimeError(
            f"host seed clone failed: {r.stderr.strip()[-500:]}")
    if ref:
        # The clone is shallow, which implies --single-branch, so the
        # requested ref is not present yet. Fetch it explicitly (branch,
        # tag, or pull/N/head) and check out the fetched head.
        r = run(["git", "-C", seed_dir, "fetch", "--depth", "50",
                 "origin", f"{ref}:seed-ref"], timeout=300)
        if r.returncode != 0:
            raise RuntimeError(
                f"host seed fetch of ref {ref!r} failed: "
                f"{r.stderr.strip()[-300:]}")
        r = run(["git", "-C", seed_dir, "checkout", "seed-ref"],
                timeout=60)
        if r.returncode != 0:
            raise RuntimeError(
                f"host seed checkout of ref {ref!r} failed: "
                f"{r.stderr.strip()[-300:]}")
    log("seed", f"host clone ready ref={ref or 'default'}")


def wait_tcp(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def container_running(cbin: str, name: str) -> bool:
    r = run([cbin, "inspect", name])
    if r.returncode != 0:
        return False
    try:
        info = json.loads(r.stdout)
        # inspect returns a list; status lives at [0].status.state
        item = info[0] if isinstance(info, list) and info else info
        status = item.get("status") or {}
        state = str(status.get("state") or item.get("state") or "").lower()
        return state in ("running", "created", "starting")
    except Exception:
        return True  # inspect worked but schema unknown -> assume alive


def kill_container(cbin: str, name: str) -> None:
    run([cbin, "rm", "-f", name], timeout=60)


def main(job_id: str) -> int:
    config = load_config()
    spend_cfg = load_spend()
    cbin = container_bin()
    db = connect(ROOT / config["paths"]["state"])
    job = get_job(db, job_id)
    if not job or job["status"] not in ("preparing", "running"):
        print(f"runner: job {job_id} not runnable", flush=True)
        return 2

    name = f"ca-{job_id.replace('_', '-')}"[:48]
    job_dir = ROOT / config["paths"]["jobs"] / job_id
    out_stage = job_dir / "out"
    job_dir.mkdir(parents=True, exist_ok=True)
    log_path = ROOT / config["paths"]["logs"] / f"{job_id}.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(kind, msg):
        line = json.dumps({"ts": time.time(), "kind": kind, "msg": redact(str(msg))})
        with open(log_path, "a") as f:
            f.write(line + "\n")
        log_event(db, job_id, kind, redact(str(msg))[:500])

    proxy_proc = None
    broker_proc = None
    seed_dir = None  # host-side repo seed; cleaned in the finally block
    stop_agent_tail = threading.Event()  # set in finally; stops the log tail
    provider = "supergrok"   # resolved after engine selection
    or_api_key = None
    or_poll_seconds = 300.0
    max_minutes = job["max_minutes"] or config["limits"]["default_max_minutes"]
    deadline = time.time() + max_minutes * 60
    try:
        # 1. engine selection
        engine_name, reason = registry.select(job["engine_requested"], job["type"])
        adapter = registry.load_adapter(engine_name)
        ok, detail = adapter.probe(config)
        if not ok:
            raise RuntimeError(f"engine probe failed: {detail}")
        engine_cfg = config["engines"][engine_name]
        update_job(db, job_id, status="running",
                   engine_selected=engine_name, engine_reason=reason)
        log("engine", f"selected={engine_name} reason={reason}")

        # 1b. provider routing: supergrok (default) vs openrouter (overflow).
        provider = or_provider.resolve_provider(job, engine_name, spend_cfg)
        update_job(db, job_id, provider=provider)
        log("provider", f"resolved provider={provider}")
        if provider == "openrouter":
            or_api_key = or_provider.read_key(spend_cfg)
            ok, why = or_provider.preflight(db, spend_cfg, job_id)
            if not ok:
                raise RuntimeError(f"openrouter preflight denied: {why}")
            log("spend", f"openrouter preflight passed: {why}")
            or_tier = (spend_cfg.get("tiers") or {}).get("openrouter") or {}
            try:
                or_poll_seconds = float(os.environ.get(
                    "CA_OR_POLL_SECONDS", or_tier.get("usage_poll_seconds", 300)))
            except (TypeError, ValueError):
                or_poll_seconds = 300.0
            # Point the in-container agent at OpenRouter's OpenAI-compatible
            # endpoint instead of the brokered SuperGrok proxy.
            engine_cfg = dict(engine_cfg)
            engine_cfg["model"] = or_tier.get("model",
                                              "deepseek/deepseek-v4-flash-0731")

        # 2. repo allowlist (checked again at the boundary)
        allow = config["repos"]["allowlist"]
        if job["repo"] not in allow:
            raise RuntimeError(f"repo '{job['repo']}' not in allowlist")

        # 2b. host-side seeding for private repos. The host holds the GitHub
        # credential; it clones the repo now (fail fast, before the
        # container starts) and the tree is injected via `container cp`
        # after launch. No credential or SSH URL ever enters the container.
        seed_clone_url = host_seed_clone_url(config, job["repo"])
        if seed_clone_url:
            seed_dir = os.path.join(tempfile.gettempdir(),
                                    f"ca-seed-{job_id}")
            log("seed", f"host-seeding {job['repo']} "
                        f"ref={job.get('ref') or 'default'}")
            seed_repo_on_host(seed_clone_url, job.get("ref"), seed_dir, log)

        # 3. model access for the container.
        #
        # Tier 1 (supergrok): per-job model proxy on the host. The Hermes
        # proxy (which attaches the real upstream credential) binds ONLY to
        # 127.0.0.1. A small broker binds 0.0.0.0 for the container subnet
        # and relays only requests carrying the random per-job bearer.
        #
        # Tier 2 (openrouter): no host proxy. The container talks to
        # OpenRouter's OpenAI-compatible endpoint directly, authenticated
        # with the real key as its client token (redacted from every log).
        proxy_cfg = config["proxy"]
        if provider == "openrouter":
            proxy_url = "https://openrouter.ai/api/v1"
            job_token = or_api_key
            log("proxy", "openrouter direct endpoint (no host proxy); "
                         "key injected as client token, redacted in logs")
        else:
            port = proxy_cfg["base_port"] + (abs(hash(job_id)) % 200)
            broker_port = port + 1000
            container_host = proxy_cfg["container_host"]
            hermes_bin = str(Path(config["hermes_venv"]) / "bin" / "hermes")
            job_token = secrets.token_urlsafe(32)
            proxy_proc = subprocess.Popen(
                [hermes_bin, "proxy", "start", "--provider", proxy_cfg["provider"],
                 "--host", "127.0.0.1", "--port", str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if not wait_tcp("127.0.0.1", port, timeout=25):
                raise RuntimeError("model proxy did not become ready")
            broker_proc = subprocess.Popen(
                [sys.executable, str(ROOT / "service" / "proxy_broker.py"),
                 str(broker_port), str(port), job_token, job_id,
                 str(ROOT / config["paths"]["state"]),
                 str(ROOT / "config" / "spend.yaml"),
                 str(log_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if not wait_tcp("127.0.0.1", broker_port, timeout=15):
                raise RuntimeError("proxy broker did not become ready")
            proxy_url = f"http://{container_host}:{broker_port}/v1"
            log("proxy", f"model proxy ready for job (provider={proxy_cfg['provider']}, "
                         f"brokered with per-job bearer)")
            register_secret(proxy_url)
            register_secret(job_token)

        # 4. launch the disposable container (no mounts — repo is cloned inside)
        env = adapter.container_env(job, engine_cfg, proxy_url, config)
        # The container's bearer must be the real per-job token the broker
        # expects (replaces the adapter's placeholder).
        env["CA_CLIENT_TOKEN"] = job_token
        register_secret(env.get("CA_CLIENT_TOKEN", ""))
        if seed_dir:
            # Private repo: the host injected the tree; the entrypoint
            # waits for /work/repo instead of cloning.
            env["CA_HOST_SEED"] = "1"
        cmd = [cbin, "run", "-d", "--name", name,
               "-c", str(config["limits"]["container_cpus"]),
               "-m", str(config["limits"]["container_memory"])]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += ["--label", f"ca.job={job_id}", config["image"]["name"]]
        r = run(cmd, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"container run failed: {r.stderr.strip()[-500:]}")
        log("container", f"started {name}")

        # Stream the agent's session output into the job log so the
        # dashboard shows a live transcript. Daemon thread, fully
        # exception-safe: it can never fail the job.
        # NOTE: the tail uses its OWN sqlite connection — the main
        # thread's connection cannot be shared across threads.
        def _tail_agent_logs():
            import sqlite3 as _sqlite3
            tdb = _sqlite3.connect(
                str(ROOT / config["paths"]["state"]), timeout=30.0)
            tdb.row_factory = _sqlite3.Row

            def tlog(kind, msg):
                line = json.dumps({"ts": time.time(), "kind": kind,
                                   "msg": redact(str(msg))})
                with open(log_path, "a") as f:
                    f.write(line + "\n")
                log_event(tdb, job_id, kind, redact(str(msg))[:500])

            seen = 0
            tailed = 0
            try:
                while not stop_agent_tail.is_set():
                    try:
                        r = run([cbin, "logs", "-n", "300", name],
                                timeout=30)
                    except Exception:
                        break
                    if r.returncode == 0 and r.stdout:
                        lines = r.stdout.splitlines()
                        if len(lines) < seen:
                            seen = 0  # log truncated/rotated; resync
                        for ln in lines[seen:]:
                            if tailed >= 3000:
                                break
                            ln = ln.strip()
                            if ln:
                                tlog("agent", ln[:2000])
                                tailed += 1
                        seen = len(lines)
                        if tailed >= 3000:
                            tlog("warn",
                                 "agent log truncated at 3000 lines")
                            break
                    if stop_agent_tail.wait(5):
                        break
            except Exception as exc:
                try:
                    tlog("warn",
                         f"agent log tail ended: {type(exc).__name__}")
                except Exception:
                    pass
            finally:
                try:
                    tdb.close()
                except Exception:
                    pass

        threading.Thread(target=_tail_agent_logs, daemon=True).start()

        # 4b. inject the host-seeded repo tree. The entrypoint is already
        # waiting for /work/repo/.git (up to 120s).
        if seed_dir:
            cp = run([cbin, "cp", seed_dir + "/.", f"{name}:/work/repo"],
                     timeout=300)
            if cp.returncode != 0:
                raise RuntimeError(
                    f"seed copy into container failed: "
                    f"{cp.stderr.strip()[-300:]}")
            log("seed", "repo tree injected into container")
            shutil.rmtree(seed_dir, ignore_errors=True)
            seed_dir = None

        # 5. poll until the entrypoint writes /out/result.json FOR THIS JOB,
        # or the container dies unexpectedly, or we hit timeout / cancel.
        # (The entrypoint lingers after writing the result so `container cp`
        # works — cp refuses stopped containers. We validate the job_id in
        # the result because a stale result.json can exist in the image.)
        result_seen = False
        next_or_poll = time.time() + or_poll_seconds
        while True:
            job = get_job(db, job_id)
            # Mid-flight failover pickup: the broker flips the job row to
            # provider=openrouter when SuperGrok 429s persist. Adopt the new
            # provider here so mid-job spend polling + finalize follow it.
            # The container is untouched — it keeps talking to the same
            # broker port; only the broker's upstream changed.
            if job.get("provider") == "openrouter" and provider == "supergrok":
                provider = "openrouter"
                or_api_key = or_provider.read_key(spend_cfg)
                or_tier = (spend_cfg.get("tiers") or {}).get("openrouter") or {}
                try:
                    or_poll_seconds = float(os.environ.get(
                        "CA_OR_POLL_SECONDS", or_tier.get("usage_poll_seconds", 300)))
                except (TypeError, ValueError):
                    or_poll_seconds = 300.0
                next_or_poll = time.time()  # check spend promptly
                log("failover",
                    f"broker failed over {job.get('failover_from')} -> "
                    f"{job.get('failover_to')}: {job.get('failover_reason')}")
            if job["cancel_requested"]:
                reason = job.get("kill_reason") or "cancel requested by caller"
                log("cancel", f"{reason} — killing container")
                update_job(db, job_id, status="cancelled",
                           finished_at=time.time(),
                           error=f"cancelled: {reason}")
                return 3
            if time.time() > deadline:
                log("timeout", f"max_minutes={max_minutes} exceeded — killing container")
                update_job(db, job_id, status="failed",
                           finished_at=time.time(),
                           error=f"wall-time budget exceeded ({max_minutes}m)")
                return 4
            if not container_running(cbin, name):
                log("warn", "container exited before writing result")
                break
            er = run([cbin, "exec", name, "test", "-f", "/out/result.json"], timeout=15)
            if er.returncode == 0:
                # copy to a scratch spot and check the job_id before trusting it
                chk_path = job_dir / "result.check.json"
                cr = run([cbin, "cp", f"{name}:/out/result.json", str(chk_path)],
                         timeout=30)
                if cr.returncode == 0:
                    try:
                        chk = json.loads(chk_path.read_text())
                    except Exception:
                        chk = {}
                    if chk.get("job_id") == job_id:
                        result_seen = True
                        log("container", "result.json detected")
                        break
                    log("warn", f"ignoring stale result.json "
                               f"(job_id={chk.get('job_id')!r}) — removing it")
                    run([cbin, "exec", name, "rm", "-f", "/out/result.json"],
                        timeout=15)
            # Tier 2 mid-job spend check: poll the key-usage endpoint; kill
            # the job the moment the rolling cap is breached.
            if provider == "openrouter" and or_api_key and \
                    time.time() >= next_or_poll:
                next_or_poll = time.time() + or_poll_seconds
                ok, why = or_provider.midjob_check(db, spend_cfg, job_id,
                                                   or_api_key)
                if not ok:
                    log("spend", f"{why} — requesting kill")
                    update_job(db, job_id, cancel_requested=1, kill_reason=why)
                    continue  # the cancel check at the top fires next pass
                log("spend", f"mid-job check ok: {why}")
            time.sleep(5)

        # 6. collect logs + /out
        rl = run([cbin, "logs", name], timeout=60)
        with open(log_path, "a") as f:
            for line in (rl.stdout or "").splitlines():
                f.write(json.dumps({"ts": time.time(), "kind": "container",
                                    "msg": redact(line)}) + "\n")
        out_stage.mkdir(parents=True, exist_ok=True)
        rc = run([cbin, "cp", f"{name}:/out/.", str(out_stage)], timeout=120)
        if rc.returncode != 0:
            log("warn", f"container cp /out failed: {rc.stderr.strip()[-300:]}")

        # 7. parse + manifest
        result = adapter.parse_result(out_stage)
        job = get_job(db, job_id) or job
        provider_now = job.get("provider") or provider
        cap_usd, cap_window = or_provider.cap_policy(spend_cfg)
        toks = token_totals(db, job_id)
        breakdown = token_breakdown(db, job_id)
        est_usd, est_detail = pricing.estimate_job_usd(spend_cfg, breakdown)
        failover_info = None
        if job.get("failover_at"):
            failover_info = {
                "at": job.get("failover_at"),
                "from": job.get("failover_from"),
                "to": job.get("failover_to"),
                "reason": job.get("failover_reason"),
            }
        manifest = {
            "job_id": job_id,
            "type": job["type"],
            "repo": job["repo"],
            "ref": job.get("ref"),
            "engine": {"requested": job["engine_requested"],
                       "selected": engine_name, "reason": reason},
            "provider": provider_now,
            "container": {"name": name, "image": config["image"]["name"]},
            "limits": {"max_minutes": max_minutes,
                       "budget_usd": job["budget_usd"],
                       "cpus": config["limits"]["container_cpus"],
                       "memory": config["limits"]["container_memory"]},
            "spend": {
                "provider": provider_now,
                # Tier 1: subscription — no marginal dollar cost.
                # Tier 2: patched with the measured key-usage delta in the
                # finally block below (finalize runs after this write).
                "usd": job.get("spend_usd"),
                "source": "subscription" if provider_now == "supergrok"
                          else "or_key_delta",
                "rolling_cap_usd": cap_usd if provider_now == "openrouter" else None,
                "rolling_window_days": (cap_window
                                        if provider_now == "openrouter"
                                        else None),
                # Token telemetry (attribution layer; reconciled vs the
                # measured delta at finalize).
                "tokens": {
                    "prompt": toks["prompt_tokens"],
                    "completion": toks["completion_tokens"],
                    "requests": toks["requests"],
                    "by_provider_model": est_detail,
                },
                "estimated_usd": est_usd,
                "estimate_source": "token_estimate",
                "failover": failover_info,
            },
            "env": redact_env(env),
            "result": result,
            "finished_at": time.time(),
        }
        (job_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        art_dir = ROOT / config["paths"]["artifacts"] / job_id
        if (out_stage / "artifacts").exists():
            shutil.rmtree(art_dir, ignore_errors=True)
            shutil.copytree(out_stage / "artifacts", art_dir)

        status = "completed" if result.get("status") == "completed" else "failed"
        update_job(db, job_id, status=status, finished_at=time.time(),
                   result_json=json.dumps(result),
                   error=result.get("error"))
        log("done", f"status={status}")
        return 0 if status == "completed" else 5
    except Exception as exc:  # noqa: BLE001
        msg = redact(f"{type(exc).__name__}: {exc}")
        try:
            log("error", msg)
            update_job(db, job_id, status="failed", finished_at=time.time(), error=msg[:2000])
        except Exception:
            pass
        return 1
    finally:
        # Stop the agent-log tail before the container is destroyed.
        try:
            stop_agent_tail.set()
        except Exception:
            pass
        # Host seed dirs must never accumulate (normally removed right
        # after the container copy; this covers failure paths).
        try:
            if seed_dir and os.path.isdir(seed_dir):
                shutil.rmtree(seed_dir, ignore_errors=True)
        except Exception:
            pass
        # Tier 2 spend accounting: measure the key-usage delta for this job,
        # record it in the ledger, and patch the manifest's spend record.
        # Runs on every exit path (success, failure, cancel, kill) so real
        # spend is never silently dropped. The provider is re-read fresh
        # from the job row so a mid-flight failover is accounted even if
        # the poll loop never got another iteration after it.
        try:
            fin_job = get_job(db, job_id) or {}
        except Exception:
            fin_job = {}
        fin_provider = fin_job.get("provider") or provider
        if fin_provider == "openrouter":
            if or_api_key is None:
                try:
                    or_api_key = or_provider.read_key(spend_cfg)
                except Exception:
                    or_api_key = None
            try:
                delta = (or_provider.finalize(db, spend_cfg, job_id, or_api_key)
                         if or_api_key else 0.0)
                # Token-estimate attribution, reconciled against the
                # measured key-usage delta (cash truth). The estimate row
                # does NOT count toward the rolling cap (rolling_spend_usd
                # only sums 'or_key_delta').
                toks = token_totals(db, job_id)
                breakdown = token_breakdown(db, job_id)
                est_usd, _ = pricing.estimate_job_usd(spend_cfg, breakdown)
                record_spend(
                    db, job_id, est_usd, "token_estimate",
                    f"{toks['prompt_tokens']} prompt + "
                    f"{toks['completion_tokens']} completion tokens "
                    f"({toks['requests']} requests); measured delta "
                    f"${delta:.4f}; drift ${delta - est_usd:+.4f}")
                mp = job_dir / "manifest.json"
                if mp.exists():
                    m = json.loads(mp.read_text())
                    sp = m.setdefault("spend", {})
                    sp["usd"] = round(delta, 4)
                    sp["source"] = "or_key_delta"
                    sp["measured_usd"] = round(delta, 4)
                    sp["drift_usd"] = round(delta - est_usd, 4)
                    sp["measured_at"] = time.time()
                    mp.write_text(json.dumps(m, indent=2))
            except Exception as exc:
                try:
                    log("spend", f"finalize failed: {type(exc).__name__}")
                except Exception:
                    pass
        # Kill switch: container, broker, and proxy MUST NOT survive the job.
        try:
            kill_container(cbin, name)
        except Exception:
            pass
        for proc in (broker_proc, proxy_proc):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
        try:
            log("cleanup", "container destroyed, proxy stopped")
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
