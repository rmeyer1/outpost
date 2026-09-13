#!/usr/bin/env python3
"""Device pairing: one-tap dashboard auth without typing the master token.

Flow:
  1. Dashboard (public page on the tailnet) POSTs /pair/request with a
     device name. The server stores a PENDING pairing (15-minute TTL) and
     returns a short public pairing code, shown on the device screen.
  2. The user reads the code to their assistant (Atlas) in chat. The code
     is public — it is not a secret.
  3. The assistant approves server-side (`agentctl pair-approve <code>` on
     the Mac, authenticated with the master token which never leaves the
     Mac). Approval mints a random bearer token, adds it as a new client in
     config/api.yaml (mode 600), and stages it for one-time pickup.
  4. The dashboard polls GET /pair/status?code=... ; once approved it
     receives the token exactly once, saves it to localStorage, and the
     pairing record is consumed.

Pairing state lives in state/pairings.json. Nothing secret ever crosses
chat: the code is public, the token travels only Mac -> device over the
tailnet.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys_path_note = None  # (kept importable without side effects)

try:
    from dispatch import DispatchError  # noqa: E402
except ImportError:  # pragma: no cover - standalone use
    class DispatchError(Exception):  # type: ignore[no-redef]
        def __init__(self, status: int, message: str):
            super().__init__(message)
            self.status = status
            self.message = message

PAIR_TTL_S = 15 * 60
MAX_PENDING = 20
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 6


def _state_path() -> Path:
    p = ROOT / "state" / "pairings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> dict:
    p = _state_path()
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)


def _prune(data: dict) -> dict:
    now = time.time()
    return {code: rec for code, rec in data.items()
            if now - rec.get("created_at", 0) < PAIR_TTL_S
            and rec.get("status") in ("pending", "approved")}


def _new_code(data: dict) -> str:
    for _ in range(50):
        code = "".join(secrets.choice(_CODE_ALPHABET)
                       for _ in range(_CODE_LEN))
        if code not in data:
            return code
    raise DispatchError(503, "pairing busy, try again")


def request_pairing(device_name: str) -> dict:
    name = (device_name or "").strip()[:40] or "device"
    data = _prune(_load())
    pending = [c for c, r in data.items() if r["status"] == "pending"]
    if len(pending) >= MAX_PENDING:
        raise DispatchError(429, "too many pending pairings")
    code = _new_code(data)
    device_id = secrets.token_hex(8)
    data[code] = {
        "code": code,
        "device_id": device_id,
        "device_name": name,
        "status": "pending",
        "created_at": time.time(),
        "token": None,
        "client": None,
    }
    _save(data)
    return {"code": code, "device_id": device_id,
            "expires_in_s": PAIR_TTL_S}


def pairing_status(code: str) -> dict:
    code = (code or "").strip().upper()
    data = _prune(_load())
    rec = data.get(code)
    if rec is None:
        # Either unknown or expired; reflect pruned state to disk.
        _save(data)
        raise DispatchError(404, "unknown or expired pairing code")
    if rec["status"] == "pending":
        return {"status": "pending",
                "expires_in_s": int(PAIR_TTL_S - (time.time() - rec["created_at"]))}
    # approved: one-time token pickup, then consume the record.
    token = rec.get("token")
    client = rec.get("client")
    del data[code]
    _save(data)
    return {"status": "approved", "token": token, "client": client}


def list_pairings() -> dict:
    data = _prune(_load())
    _save(data)
    return {"pairings": [
        {"code": code, "device_name": r["device_name"],
         "status": r["status"],
         "age_s": int(time.time() - r["created_at"])}
        for code, r in sorted(data.items(),
                              key=lambda kv: kv[1]["created_at"])]}


def approve_pairing(code: str) -> dict:
    import yaml  # local import: only needed for approval

    code = (code or "").strip().upper()
    data = _prune(_load())
    rec = data.get(code)
    if rec is None:
        _save(data)
        raise DispatchError(404, "unknown or expired pairing code")
    if rec["status"] == "approved":
        raise DispatchError(409, "pairing already approved")
    token = secrets.token_hex(32)
    cfg_path = ROOT / "config" / "api.yaml"
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    clients = cfg.setdefault("clients", {})
    base = f"device-{rec['device_id'][:8]}"
    client = base
    n = 2
    while client in clients:
        client = f"{base}-{n}"
        n += 1
    clients[client] = token
    tmp = cfg_path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(cfg, default_flow_style=False))
    os.replace(tmp, cfg_path)
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass
    rec["status"] = "approved"
    rec["token"] = token
    rec["client"] = client
    rec["approved_at"] = time.time()
    data[code] = rec
    _save(data)
    return {"client": client, "device_name": rec["device_name"]}
