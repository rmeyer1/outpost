"""Dispatcher operations — the single implementation behind the API and agentctl.

Transport-agnostic: every public function returns JSON-serializable payloads
or raises DispatchError(status, message). service/api.py maps these onto
HTTP; bin/agentctl is a thin HTTP client that only pretty-prints. There is
exactly one implementation of each operation, and it lives here.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path, PurePosixPath

import yaml

from db import (ack_attention, attention_queue, connect, find_by_idempotency,
                get_job, insert_job, list_jobs, log_event, new_job_id,
                rolling_spend_usd, token_breakdown, token_totals, update_job)
import pricing
import openrouter as or_provider

ROOT = Path(__file__).resolve().parent.parent

JOB_TYPES = ("coding", "artifact", "research", "data")
PROVIDERS = ("supergrok", "openrouter", "auto")
TERMINAL = ("completed", "failed", "cancelled")

# Git refs accepted for host-seeded repos: branch/tag names plus the
# pull/N/head form. Strictly limited so a malicious ref can never become
# shell injection (the runner only ever passes refs as argv elements,
# but belt-and-suspenders at the API boundary).
_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9/_.\-]{0,127}$")


def validate_ref(ref: str | None) -> str | None:
    """Validate an optional git ref. Returns the normalized ref or None."""
    if ref is None or ref == "":
        return None
    if not isinstance(ref, str) or ".." in ref or not _REF_RE.match(ref):
        raise DispatchError(
            400,
            f"invalid ref {ref!r}: use a branch/tag name or pull/N/head")
    return ref


class DispatchError(Exception):
    """An expected dispatcher failure. Carries the HTTP status to report."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def load_config() -> dict:
    with open(ROOT / "config" / "agents.yaml") as f:
        return yaml.safe_load(f)


def load_spend() -> dict:
    with open(ROOT / "config" / "spend.yaml") as f:
        return yaml.safe_load(f)


def _db():
    config = load_config()
    return connect(ROOT / config["paths"]["state"])


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def submit_job(*, type: str, repo: str, base: str = "main", task: str = "",
               engine: str = "auto", provider: str = "supergrok",
               budget_usd: float | None = None, max_minutes: int | None = None,
               idempotency_key: str | None = None,
               ref: str | None = None) -> dict:
    config = load_config()
    if type not in JOB_TYPES:
        raise DispatchError(400,
                            f"type must be one of {list(JOB_TYPES)}, got {type!r}")
    if not (task or "").strip():
        raise DispatchError(400, "empty task")
    if repo not in config["repos"]["allowlist"]:
        raise DispatchError(
            400,
            f"repo '{repo}' not in allowlist {config['repos']['allowlist']}")
    provider = (provider or "supergrok").lower()
    if provider not in PROVIDERS:
        raise DispatchError(
            400, f"provider must be supergrok|openrouter|auto, got {provider}")
    ref = validate_ref(ref)
    db = _db()
    try:
        if idempotency_key:
            existing = find_by_idempotency(db, idempotency_key)
            if existing:
                return {"id": existing["id"], "deduplicated": True}
        job = insert_job(
            db,
            job_id=new_job_id(),
            type=type,
            repo=repo,
            base=base,
            task=task,
            engine_requested=engine,
            budget_usd=(budget_usd if budget_usd is not None
                        else config["limits"]["default_budget_usd"]),
            max_minutes=(max_minutes if max_minutes is not None
                         else config["limits"]["default_max_minutes"]),
            idempotency_key=idempotency_key,
            provider=provider,
            ref=ref,
        )
        log_event(db, job["id"], "submit",
                  f"type={job['type']} repo={repo} engine={engine} "
                  f"provider={provider} ref={ref}")
        return {"id": job["id"], "deduplicated": False}
    finally:
        db.close()


_STATUS_KEYS = (
    "id", "type", "repo", "ref", "status", "engine_requested", "engine_selected",
    "engine_reason", "provider", "budget_usd", "max_minutes", "created_at",
    "started_at", "finished_at", "attention_flagged_at",
    "attention_acked_at", "spend_usd", "kill_reason", "failover_at",
    "failover_from", "failover_to", "failover_reason", "error",
)


def job_status(job_id: str) -> dict:
    spend_cfg = load_spend()
    db = _db()
    try:
        job = get_job(db, job_id)
        if not job:
            raise DispatchError(404, f"unknown job {job_id}")
        view = {k: job[k] for k in _STATUS_KEYS if k in job}
        toks = token_totals(db, job_id)
        est_usd, _ = pricing.estimate_job_usd(
            spend_cfg, token_breakdown(db, job_id))
        view["tokens"] = {**toks, "estimated_usd": est_usd}
        if job.get("result_json"):
            try:
                view["result"] = json.loads(job["result_json"])
            except Exception:
                pass
        return view
    finally:
        db.close()


def job_logs(job_id: str, tail: int = 0) -> dict:
    config = load_config()
    p = ROOT / config["paths"]["logs"] / f"{job_id}.jsonl"
    if not p.exists():
        raise DispatchError(404, f"no logs for {job_id}")
    lines = p.read_text().splitlines()
    if tail:
        lines = lines[-tail:]
    entries = []
    for line in lines:
        try:
            e = json.loads(line)
            entries.append({"ts": e.get("ts"), "kind": e.get("kind"),
                            "msg": e.get("msg"), "raw": False})
        except Exception:
            entries.append({"ts": None, "kind": None, "msg": line, "raw": True})
    return {"job_id": job_id, "entries": entries}


def jobs_list(status: str | None = None) -> dict:
    db = _db()
    try:
        jobs = [{
            "id": j["id"], "status": j["status"], "type": j["type"],
            "repo": j["repo"], "engine_selected": j.get("engine_selected"),
            "provider": j.get("provider"), "created_at": j.get("created_at"),
        } for j in list_jobs(db, status=status)]
        return {"jobs": jobs}
    finally:
        db.close()


def cancel_job(job_id: str) -> dict:
    db = _db()
    try:
        job = get_job(db, job_id)
        if not job:
            raise DispatchError(404, f"unknown job {job_id}")
        if job["status"] in TERMINAL:
            return {"ok": True, "already_terminal": True,
                    "message": f"job already {job['status']}"}
        update_job(db, job_id, cancel_requested=1)
        log_event(db, job_id, "cancel", "cancel requested via API")
        return {"ok": True, "already_terminal": False,
                "message": f"cancel requested for {job_id} "
                           f"(runner will destroy the container)"}
    finally:
        db.close()


def job_manifest(job_id: str) -> dict:
    config = load_config()
    p = ROOT / config["paths"]["jobs"] / job_id / "manifest.json"
    if not p.exists():
        raise DispatchError(404, f"no manifest for {job_id}")
    return json.loads(p.read_text())


# ---------------------------------------------------------------------------
# Attention queue
# ---------------------------------------------------------------------------

def attention_list() -> dict:
    db = _db()
    try:
        out = []
        for j in attention_queue(db):
            started = j.get("started_at") or j.get("created_at") or time.time()
            out.append({
                "id": j["id"],
                "elapsed_min": round((time.time() - started) / 60.0, 1),
                "acked": bool(j.get("attention_acked_at")),
                "type": j["type"],
                "engine_selected": j.get("engine_selected"),
                "provider": j.get("provider"),
            })
        return {"jobs": out}
    finally:
        db.close()


def ack_job(job_id: str) -> dict:
    db = _db()
    try:
        if ack_attention(db, job_id):
            return {"ok": True,
                    "message": f"{job_id}: acknowledged — job runs to its "
                               f"hard ceiling"}
        job = get_job(db, job_id)
        if not job:
            raise DispatchError(404, f"unknown job {job_id}")
        raise DispatchError(
            409, f"job {job_id} is not awaiting attention "
                 f"(status={job['status']})")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------

def spend_summary() -> dict:
    spend_cfg = load_spend()
    cap_usd, cap_window = or_provider.cap_policy(spend_cfg)
    db = _db()
    try:
        recent = []
        rows = db.execute(
            "SELECT job_id, SUM(prompt_tokens) p, SUM(completion_tokens) c, "
            "COUNT(*) n FROM token_usage GROUP BY job_id "
            "ORDER BY MAX(ts) DESC LIMIT 10").fetchall()
        for r in rows:
            est, _ = pricing.estimate_job_usd(
                spend_cfg, token_breakdown(db, r["job_id"]))
            recent.append({"job_id": r["job_id"],
                           "prompt_tokens": int(r["p"]),
                           "completion_tokens": int(r["c"]),
                           "requests": int(r["n"]),
                           "estimated_usd": est})
        return {"rolling_usd": round(rolling_spend_usd(db, cap_window), 4),
                "cap_usd": cap_usd,
                "cap_window_days": cap_window,
                "recent": recent}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

_CONTENT_TYPES = {
    ".bundle": "application/octet-stream",
    ".json": "application/json",
    ".jsonl": "application/jsonl",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".py": "text/x-python",
}


def content_type_for(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def artifact_path(job_id: str, name: str) -> Path:
    """Resolve a downloadable artifact path, rejecting path traversal.

    Serves files under artifacts/<job-id>/, plus the special names
    'repo.bundle' (jobs/<job-id>/out/repo.bundle) and 'manifest.json'
    (jobs/<job-id>/manifest.json).
    """
    config = load_config()
    db = _db()
    try:
        if not get_job(db, job_id):
            raise DispatchError(404, f"unknown job {job_id}")
    finally:
        db.close()
    parts = PurePosixPath(name or "").parts
    if not parts or name.startswith("/") or ".." in parts:
        raise DispatchError(400, f"invalid artifact name {name!r}")
    job_dir = ROOT / config["paths"]["jobs"] / job_id
    special = {"repo.bundle": job_dir / "out" / "repo.bundle",
               "manifest.json": job_dir / "manifest.json"}
    if name in special:
        p = special[name]
        if p.is_file():
            return p
        raise DispatchError(404, f"artifact not found: {name}")
    art_base = (ROOT / config["paths"]["artifacts"] / job_id).resolve()
    p = (art_base / name).resolve()
    if p != art_base and art_base not in p.parents:
        raise DispatchError(400, f"invalid artifact name {name!r}")
    if not p.is_file():
        raise DispatchError(404, f"artifact not found: {name}")
    return p
