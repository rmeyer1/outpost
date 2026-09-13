"""Dispatcher controller: queue -> workers, with reboot recovery.

Usage: python3 service/controller.py [--once]

Loop:
  - on startup: any job stuck in preparing/running/validating (e.g. after a
    host reboot) goes back to queued with an event note; any stale ca-*
    containers are destroyed.
  - claim queued jobs up to limits.max_workers, spawning one
    service/runner.py <job-id> subprocess per job.
  - reap finished runners.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "service"))

import yaml  # noqa: E402
from db import ACTIVE, connect, claim_next, count_active, get_job, log_event, next_queued, running_jobs, update_job  # noqa: E402
from timeouts import enforce, load_timeouts  # noqa: E402
from containers import get_backend  # noqa: E402


def load_config() -> dict:
    with open(ROOT / "config" / "agents.yaml") as f:
        return yaml.safe_load(f)


def load_spend() -> dict:
    with open(ROOT / "config" / "spend.yaml") as f:
        return yaml.safe_load(f)


def container_bin() -> str:
    from containers import container_bin as _cb
    return _cb()


def recover(config) -> None:
    db = connect(ROOT / config["paths"]["state"])
    backend = get_backend(config)
    # Destroy any leftover job containers from before a reboot/crash.
    for n in backend.list_job_names():
        try:
            backend.remove(n)
        except Exception:
            pass
    # Requeue jobs that never finished. needs_attention jobs are requeued too
    # (their runner died with the controller); the flag is cleared and the
    # spend path will re-flag if spend is still untracked.
    rows = db.execute(
        "SELECT id FROM jobs WHERE status IN ('preparing','running','validating','needs_attention')"
    ).fetchall()
    for row in rows:
        jid = row["id"]
        update_job(db, jid, status="queued", started_at=None,
                   attention_flagged_at=None, attention_acked_at=None,
                   engine_selected=None, error="requeued after controller restart")
        log_event(db, jid, "recover", "controller restart: requeued (container was destroyed)")
        print(f"recover: requeued {jid}", flush=True)
    db.close()


def main() -> int:
    config = load_config()
    spend_cfg = load_spend()
    # Wait for the local container runtime (Apple container system on macOS,
    # Docker Engine on Linux). This host is a standalone Outpost — it does
    # not contact any other installation.
    backend = get_backend(config)
    for _ in range(60):
        if backend.system_ready():
            break
        time.sleep(5)
    else:
        print("controller: container system not ready after 5m — exiting",
              flush=True)
        return 1
    recover(config)
    db = connect(ROOT / config["paths"]["state"])
    runners: dict[str, subprocess.Popen] = {}
    print(f"controller: up (max_workers={config['limits']['max_workers']})", flush=True)
    once = "--once" in sys.argv
    timeouts_cache: dict[str, dict] = {}  # provider -> resolved timeouts
    try:
        while True:
            # Watchdog: time-based hard stops (no-response kill for
            # unacknowledged spend flags, ceiling kill). Kills are requested
            # via cancel_requested; the runner performs the actual
            # container destruction.
            for job in running_jobs(db):
                prov = job.get("provider") or "supergrok"
                if prov not in timeouts_cache:
                    timeouts_cache[prov] = load_timeouts(spend_cfg, prov)
                action = enforce(db, job["id"], timeouts=timeouts_cache[prov])
                if action:
                    print(f"controller: watchdog {action} for {job['id']}",
                          flush=True)
            # Reap finished runners.
            for jid, proc in list(runners.items()):
                if proc.poll() is not None:
                    log_event(db, jid, "runner",
                              f"runner exited code={proc.returncode}")
                    del runners[jid]
            # Launch up to max_workers.
            while len(runners) < config["limits"]["max_workers"]:
                nxt = next_queued(db)
                if not nxt:
                    break
                if not claim_next(db, nxt["id"]):
                    continue  # lost the race; try next
                log_event(db, nxt["id"], "claim", "controller claimed job")
                proc = subprocess.Popen(
                    [sys.executable, str(ROOT / "service" / "runner.py"), nxt["id"]],
                    cwd=str(ROOT),
                )
                runners[nxt["id"]] = proc
                print(f"controller: launched runner for {nxt['id']}", flush=True)
            if once and not runners and not next_queued(db):
                break
            time.sleep(2)
    except KeyboardInterrupt:
        print("controller: stopping", flush=True)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
