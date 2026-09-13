"""OpenRouter provider groundwork (Tier 2 overflow).

Responsibilities:
  - read the OpenRouter key from its secure location ON THE HOST ONLY
    (service/openrouter.py runs on the host, never inside a container).
  - poll the key-usage endpoint: GET https://openrouter.ai/api/v1/auth/key
  - pre-flight cap check before starting an OpenRouter job
  - rolling 7-day spend ledger helpers (ledger itself lives in db.py)

The key is NEVER logged, printed, or copied off the Mac. It is registered
with service/redact.py the moment it is read so it can never leak into
logs. The container never sees it directly: provider-agnostic engines
(goose/opencode) receive it only via a redacted, scoped channel (Phase 3).

Spend model:
  - /auth/key returns data.usage = lifetime USD consumed on the key.
  - We record per-job deltas (end-of-job usage minus start-of-job baseline)
    into spend_ledger, and compute the rolling 7-day window from OUR ledger.
  - Cap: tiers.openrouter.cap_usd over cap_window_days (default $25 / 7d).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from db import flag_attention, get_job, log_event, record_spend, rolling_spend_usd, update_job
from redact import register_secret

USAGE_URL = "https://openrouter.ai/api/v1/auth/key"
_usage_unreachable_flagged: set[str] = set()


def read_key(spend_cfg: dict) -> str:
    """Read the OpenRouter key server-side. Raises if missing.

    CA_OR_KEY_FILE (test-only) overrides the configured key file location.
    """
    path = os.environ.get("CA_OR_KEY_FILE") or spend_cfg.get(
        "openrouter_key_file", "~/.config/outpost/secrets.yaml")
    name = spend_cfg.get("openrouter_key_name", "OPENROUTER_API_KEY")
    key = None
    with open(os.path.expanduser(path)) as f:
        for line in f:
            line = line.strip()
            if line.startswith(name + ":"):
                key = line.split(":", 1)[1].strip().strip("'\"")
                break
    if not key:
        raise RuntimeError(f"{name} not found in {path}")
    register_secret(key)  # never let it leak into logs
    return key


def key_usage_usd(api_key: str, timeout: float = 15.0) -> float | None:
    """Return lifetime USD consumed on the key, or None if unreachable.

    The key travels only in the Authorization header of this one HTTPS
    request; nothing about it is logged.

    CA_OR_USAGE_MOCK (test-only): when set, return its float value instead
    of hitting the network, so failover/cap logic is testable offline.
    """
    mock = os.environ.get("CA_OR_USAGE_MOCK")
    if mock is not None:
        try:
            return float(mock)
        except ValueError:
            return None
    try:
        req = urllib.request.Request(
            USAGE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return float(data["data"]["usage"])
    except Exception:
        return None


def cap_policy(spend_cfg: dict) -> tuple[float, float]:
    tier = (spend_cfg.get("tiers") or {}).get("openrouter") or {}
    return (float(tier.get("cap_usd", 25.0)),
            float(tier.get("cap_window_days", 7.0)))


def resolve_provider(job: dict, engine_name: str, spend_cfg: dict) -> str:
    """Resolve 'supergrok' vs 'openrouter' for a job.

    - job.provider == 'openrouter'  -> explicit per-task selection.
    - job.provider == 'auto' (or anything else) and the engine is
      provider-agnostic (needs a raw API key, can't use the brokered
      SuperGrok proxy) -> 'openrouter'.
    - otherwise -> 'supergrok' (the default).
    """
    requested = (job.get("provider") or "supergrok").lower()
    if requested == "openrouter":
        return "openrouter"
    routing = spend_cfg.get("routing") or {}
    agnostic = routing.get("provider_agnostic_engines") or []
    if engine_name in agnostic:
        return "openrouter"
    return "supergrok"


def preflight(conn, spend_cfg: dict, job_id: str) -> tuple[bool, str]:
    """Pre-flight cap check for an OpenRouter job.

    Returns (ok, reason). Fail-safe: if the usage endpoint is unreachable we
    ALLOW the job but flag it needs_attention so spend is visibly untracked.

    Note: this does NOT hard-require a readable key — the runner performs
    the authoritative key read (and fails the job) immediately after
    preflight. The broker's failover_precheck, which has no runner step
    after it, does its own strict key verification.
    """
    cap, window = cap_policy(spend_cfg)
    try:
        api_key = read_key(spend_cfg)
    except Exception:
        api_key = ""
    usage = key_usage_usd(api_key)
    if usage is None:
        reason = ("openrouter usage endpoint unreachable — job allowed but "
                  "spend is untracked; flagged for review")
        job = get_job(conn, job_id)
        if job and job["status"] in ("running", "validating"):
            flag_attention(conn, job_id, reason)
        else:
            log_event(conn, job_id, "spend", reason)
        return True, reason
    # Record the baseline so mid-job/end-of-job deltas are measurable.
    update_job(conn, job_id, or_baseline_usd=usage)
    spent = rolling_spend_usd(conn, window)
    if spent >= cap:
        reason = (f"openrouter rolling {window:.0f}d spend ${spent:.2f} "
                  f">= cap ${cap:.2f} — job denied")
        log_event(conn, job_id, "spend", reason)
        return False, reason
    log_event(conn, job_id, "spend",
              f"preflight ok: rolling {window:.0f}d spend ${spent:.2f} "
              f"of ${cap:.2f} cap; baseline ${usage:.4f}")
    return True, f"rolling spend ${spent:.2f} of ${cap:.2f}"


def midjob_check(conn, spend_cfg: dict, job_id: str,
                 api_key: str) -> tuple[bool, str]:
    """Periodic check during an OpenRouter job.

    Returns (ok, reason). ok=False means the cap is breached and the caller
    must kill the job. Unreachable endpoint -> (True, 'untracked') and the
    job is flagged once via needs_attention.
    """
    cap, window = cap_policy(spend_cfg)
    job = get_job(conn, job_id)
    if not job:
        return True, "no job row"
    usage = key_usage_usd(api_key)
    if usage is None:
        if job_id not in _usage_unreachable_flagged:
            _usage_unreachable_flagged.add(job_id)
            if job["status"] in ("running", "validating"):
                flag_attention(conn, job_id,
                               "openrouter usage endpoint unreachable mid-job — "
                               "spend untracked")
            else:
                log_event(conn, job_id, "spend",
                          "usage endpoint unreachable mid-job — spend untracked")
        return True, "usage endpoint unreachable — spend untracked"
    baseline = job.get("or_baseline_usd")
    if baseline is None:
        baseline = usage
        update_job(conn, job_id, or_baseline_usd=baseline)
    in_flight = max(0.0, usage - baseline)
    total = rolling_spend_usd(conn, window) + in_flight
    if total >= cap:
        reason = (f"openrouter cap breached mid-job: rolling ${total:.2f} "
                  f">= ${cap:.2f}")
        log_event(conn, job_id, "spend", reason)
        return False, reason
    return True, f"rolling+in-flight ${total:.2f} of ${cap:.2f}"


def finalize(conn, spend_cfg: dict, job_id: str, api_key: str) -> float:
    """End-of-job: record the key-usage delta in the ledger.

    Returns the USD attributed to this job (0.0 if unmeasurable).
    """
    job = get_job(conn, job_id) or {}
    baseline = job.get("or_baseline_usd")
    usage = key_usage_usd(api_key)
    if baseline is None or usage is None:
        record_spend(conn, job_id, 0.0, "or_key_delta",
                     "unmeasurable (endpoint unreachable or no baseline)")
        return 0.0
    delta = max(0.0, usage - baseline)
    record_spend(conn, job_id, delta, "or_key_delta",
                 f"baseline ${baseline:.4f} -> end ${usage:.4f}")
    update_job(conn, job_id, spend_usd=delta)
    log_event(conn, job_id, "spend", f"job spend ${delta:.4f} (openrouter delta)")
    return delta


def failover_precheck(conn, spend_cfg: dict, job_id: str
                      ) -> tuple[bool, str, float | None]:
    """Cap pre-check run by the broker BEFORE a mid-flight failover.

    Returns (ok, reason, baseline_or_None).
      - ok=True:  safe to fail over; baseline is the current key usage (or
        None when the usage endpoint is unreachable).
      - ok=False: the rolling cap is already hit (or the key is unreadable) —
        the caller must NOT fail over.

    Fail-safe on unreachable endpoint: allow the failover but flag the job
    needs_attention so the untracked overflow spend is visible, mirroring
    the preflight behavior for jobs that start on Tier 2.
    """
    cap, window = cap_policy(spend_cfg)
    try:
        api_key = read_key(spend_cfg)
    except Exception as exc:
        return False, f"openrouter key unreadable, cannot fail over: {exc}", None
    usage = key_usage_usd(api_key)
    if usage is None:
        reason = ("supergrok rate-limited; failing over to openrouter but the "
                  "usage endpoint is unreachable — overflow spend untracked")
        job = get_job(conn, job_id)
        if job and job["status"] in ("running", "validating"):
            flag_attention(conn, job_id, reason)
        else:
            log_event(conn, job_id, "spend", reason)
        return True, reason, None
    spent = rolling_spend_usd(conn, window)
    if spent >= cap:
        reason = (f"supergrok rate-limited but openrouter rolling {window:.0f}d "
                  f"spend ${spent:.2f} >= cap ${cap:.2f} — no overflow budget; "
                  f"NOT failing over")
        log_event(conn, job_id, "spend", reason)
        return False, reason, None
    return (True,
            f"failover cap ok: rolling {window:.0f}d spend ${spent:.2f} "
            f"of ${cap:.2f}; baseline ${usage:.4f}",
            usage)
