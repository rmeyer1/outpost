"""Harness registry: which worker engines exist, which are enabled,
and how a job picks one. Adding a new engine means an entry here plus
an adapter module — no caller changes."""
from __future__ import annotations

ENGINES = {
    "hermes": {
        "enabled": True,
        "default": True,
        "lanes": ["general", "coding", "artifact", "research"],
        "adapter": "adapters.hermes",
        "fallback": ["grok-build"],
    },
    "grok-build": {
        "enabled": True,    # xAI grok CLI headless, brokered model access
        "default": False,   # auto still routes to hermes unless config changes
        "lanes": ["coding"],
        "adapter": "adapters.grok_build",
        "fallback": ["hermes"],
    },
    "codex":      {"enabled": True, "lanes": ["coding"], "adapter": "adapters.codex",      "fallback": ["grok-build"]},
    "goose":      {"enabled": True, "lanes": ["general"], "adapter": "adapters.goose",     "fallback": ["hermes"]},
    "opencode":   {"enabled": True, "lanes": ["coding"], "adapter": "adapters.opencode",   "fallback": ["hermes"]},
}

TYPE_TO_LANE = {
    "coding": "coding",
    "artifact": "artifact",
    "research": "research",
    "data": "general",
}


def _lane_for(job_type: str) -> str:
    return TYPE_TO_LANE.get(job_type, "general")


def select(requested: str, job_type: str) -> tuple[str, str]:
    """Return (engine_name, reason). Never returns a disabled engine."""
    lane = _lane_for(job_type)
    if requested and requested != "auto":
        info = ENGINES.get(requested)
        if info and info["enabled"]:
            return requested, f"explicit request for {requested}"
        # Requested engine unavailable: fall back along its chain.
        chain = (info or {}).get("fallback", []) if info else []
        for fb in chain:
            if ENGINES.get(fb, {}).get("enabled"):
                return fb, f"{requested} unavailable; fell back to {fb}"
        raise RuntimeError(f"requested engine '{requested}' is not available")
    # auto: default engine if it serves the lane, else first enabled engine for lane.
    for name, info in ENGINES.items():
        if info.get("default") and info["enabled"] and lane in info["lanes"]:
            return name, f"auto: default engine for lane '{lane}'"
    for name, info in ENGINES.items():
        if info["enabled"] and lane in info["lanes"]:
            return name, f"auto: first enabled engine for lane '{lane}'"
    raise RuntimeError(f"no enabled engine serves lane '{lane}'")


def load_adapter(engine_name: str):
    import importlib

    info = ENGINES[engine_name]
    return importlib.import_module(info["adapter"])
