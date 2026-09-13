"""Secret redaction for logs, manifests, and stored evidence.

Canary rule: anything that looks like a credential is replaced before it is
written to disk. Patterns are deliberately broad — false positives are
acceptable, leaks are not.
"""
from __future__ import annotations

import os
import re

_PATTERNS = [
    # Explicit secret-ish env values we pass around (values, not names).
    (re.compile(r"(?i)(api[_-]?key|bearer|token|secret|password|passwd|pwd)\s*[:=]\s*"
                r"(['\"]?)([A-Za-z0-9_\-./+]{12,})\2"),
     r"\1=<REDACTED>"),
    # Bearer tokens in Authorization headers.
    (re.compile(r"(?i)(authorization:\s*bearer\s+)[A-Za-z0-9_\-./+~]{12,}"),
     r"\1<REDACTED>"),
    # OpenRouter / xAI style keys.
    (re.compile(r"\bsk-or-[A-Za-z0-9]{16,}\b"), "sk-or-<REDACTED>"),
    (re.compile(r"\bxai-[A-Za-z0-9]{16,}\b"), "xai-<REDACTED>"),
    # Long hex/base64 blobs that are probably tokens (conservative length).
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "<REDACTED-LONG-TOKEN>"),
]

# Values explicitly registered for this job (e.g. the per-job proxy token).
_registered: list[str] = []


def register_secret(value: str) -> None:
    if value and len(value) >= 8 and value not in _registered:
        _registered.append(value)


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for value in _registered:
        out = out.replace(value, "<REDACTED>")
    for rx, repl in _PATTERNS:
        out = rx.sub(repl, out)
    return out


def redact_env(env: dict) -> dict:
    """Return a copy of env safe to persist in a manifest."""
    safe_keys = {
        "CA_JOB_ID", "CA_TYPE", "CA_REPO", "CA_BASE", "CA_BRANCH",
        "CA_MODEL", "CA_MAX_MINUTES", "CA_ENGINE", "CA_PROXY_URL",
        "PATH", "HOME", "LANG",
    }
    return {k: redact(v) for k, v in env.items() if k in safe_keys}
