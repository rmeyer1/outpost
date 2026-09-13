"""Time-based hard stops for cloud-agent jobs (both tiers).

Policy (from config/spend.yaml):
  checkin_minutes    -> flag job as needs_attention (container keeps running)
  noresponse_minutes -> kill if still unacknowledged this long after the flag
  ceiling_minutes    -> hard kill, no exceptions, even if acknowledged

Kills are requested via cancel_requested + kill_reason; the per-job runner
owns the container and performs the actual destruction (the kill switch).

All thresholds are overridable for tests via env vars:
  CA_CHECKIN_MINUTES, CA_NORESPONSE_MINUTES, CA_CEILING_MINUTES
"""
from __future__ import annotations

import os
import time

from db import flag_attention, get_job, log_event, update_job


def _minutes(env_key: str, default: float) -> float:
    try:
        return float(os.environ.get(env_key, default))
    except (TypeError, ValueError):
        return default


def load_timeouts(spend_cfg: dict, provider: str) -> dict:
    """Resolve effective timeouts for a provider ('supergrok'|'openrouter')."""
    tier = (spend_cfg.get("tiers") or {}).get(provider) or {}
    return {
        "checkin": _minutes("CA_CHECKIN_MINUTES", tier.get("checkin_minutes", 45)),
        "noresponse": _minutes("CA_NORESPONSE_MINUTES", tier.get("noresponse_minutes", 30)),
        "ceiling": _minutes("CA_CEILING_MINUTES", tier.get("ceiling_minutes", 180)),
    }


def enforce(conn, job_id: str, now: float | None = None,
            timeouts: dict | None = None) -> str | None:
    """Apply time-based transitions to one job.

    Returns 'flagged' | 'killed_no_response' | 'killed_ceiling' | None.
    Pure logic over the DB row; safe to call every controller tick.
    """
    now = time.time() if now is None else now
    job = get_job(conn, job_id)
    if not job:
        return None
    if job["status"] not in ("running", "validating", "needs_attention"):
        return None
    if job["cancel_requested"]:
        return None  # already dying; runner owns it from here
    started = job.get("started_at") or now
    elapsed_min = (now - started) / 60.0
    t = timeouts or {"checkin": 45.0, "noresponse": 30.0, "ceiling": 180.0}

    # 1. Hard ceiling — no exceptions.
    if elapsed_min >= t["ceiling"]:
        reason = (f"hard ceiling {t['ceiling']:.0f}m elapsed "
                  f"({elapsed_min:.1f}m) — killing")
        update_job(conn, job_id, cancel_requested=1, kill_reason=reason,
                   status="cancelled", finished_at=now,
                   error=f"cancelled: {reason}")
        log_event(conn, job_id, "timeout", reason)
        return "killed_ceiling"

    # 2. Not yet flagged: check-in point.
    if job["status"] in ("running", "validating") and elapsed_min >= t["checkin"]:
        reason = (f"{elapsed_min:.1f}m elapsed >= check-in {t['checkin']:.0f}m; "
                  f"flagged for review (ack to continue, auto-kill at "
                  f"{t['checkin'] + t['noresponse']:.0f}m if unacknowledged, "
                  f"ceiling {t['ceiling']:.0f}m)")
        if flag_attention(conn, job_id, reason):
            return "flagged"
        return None

    # 3. Flagged but unacknowledged past the no-response window: kill.
    if job["status"] == "needs_attention" and not job.get("attention_acked_at"):
        flagged = job.get("attention_flagged_at") or now
        if (now - flagged) / 60.0 >= t["noresponse"]:
            reason = (f"no response to attention flag for {t['noresponse']:.0f}m "
                      f"({elapsed_min:.1f}m elapsed) — killing")
            update_job(conn, job_id, cancel_requested=1, kill_reason=reason,
                       status="cancelled", finished_at=now,
                       error=f"cancelled: {reason}")
            log_event(conn, job_id, "timeout", reason)
            return "killed_no_response"

    return None
