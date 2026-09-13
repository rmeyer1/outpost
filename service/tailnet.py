"""Tailscale helpers shared by the API server and agentctl.

Resolves this machine's Tailscale IPv4 dynamically at call time (never
hardcoded). Two strategies, in order:

1. `tailscale ip -4` CLI.
2. `ifconfig` scan for an address in 100.64.0.0/10 (Tailscale's CGNAT range).

Strategy 2 exists because the macOS Tailscale CLI talks to the GUI app and
fails from minimal environments (e.g. under launchd, where it prints an
error to stdout with exit code 0). Every candidate is validated with the
ipaddress module — garbage output fails closed instead of becoming a bind
address.
"""
from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
from pathlib import Path

# The macOS Tailscale app's CLI is not on the default PATH for SSH sessions.
_TAILSCALE_CANDIDATES = (
    "tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/tailscale",
)

# Tailscale assigns addresses from 100.64.0.0/10.
_TAILNET_RE = re.compile(r"inet\s+(100\.\d+\.\d+\.\d+)\b")


def tailscale_bin() -> str:
    for cand in _TAILSCALE_CANDIDATES:
        if cand.startswith("/"):
            if Path(cand).exists():
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    raise RuntimeError("tailscale CLI not found (checked PATH and the macos app bundle)")


def _valid_ipv4(s: str) -> str | None:
    try:
        return str(ipaddress.IPv4Address(s.strip().split()[0]))
    except Exception:
        return None


def _ip_via_cli() -> str | None:
    try:
        out = subprocess.run(
            [tailscale_bin(), "ip", "-4"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return _valid_ipv4(out.stdout or "")


def _ip_via_ifconfig() -> str | None:
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True,
                             text=True, timeout=15)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    for m in _TAILNET_RE.finditer(out.stdout or ""):
        ip = _valid_ipv4(m.group(1))
        if ip:
            try:
                if ipaddress.IPv4Address(ip) in ipaddress.IPv4Network("100.64.0.0/10"):
                    return ip
            except Exception:
                continue
    return None


def tailscale_ip() -> str:
    """This machine's Tailscale IPv4 address, resolved dynamically."""
    ip = _ip_via_cli() or _ip_via_ifconfig()
    if not ip:
        raise RuntimeError("could not resolve Tailscale IPv4 (CLI and ifconfig both failed)")
    return ip
