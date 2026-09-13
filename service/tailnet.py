"""Tailscale helpers shared by the API server and agentctl.

Resolves this machine's Tailscale IPv4 dynamically at call time (never
hardcoded). Strategies, in order:

1. `tailscale ip -4` CLI (PATH, macOS app bundle, Linux package paths).
2. `ip -4 addr` scan (Linux) for an address in 100.64.0.0/10.
3. `ifconfig` scan for the same range (macOS; Linux if present).

Strategy 3 exists because the macOS Tailscale CLI talks to the GUI app and
fails from minimal environments (e.g. under launchd, where it prints an
error to stdout with exit code 0). Every candidate is validated with the
ipaddress module — garbage output fails closed instead of becoming a bind
address.

Tailscale is optional. Callers that can run without a tailnet (API
``bind: auto``) catch RuntimeError and fall back to loopback.
"""
from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
from pathlib import Path

# macOS app bundle is not on PATH for SSH/launchd; Linux packages install
# to /usr/bin or /usr/sbin.
_TAILSCALE_CANDIDATES = (
    "tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/tailscale",
    "/usr/bin/tailscale",
    "/usr/sbin/tailscale",
    "/usr/local/bin/tailscale",
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


def _in_tailnet(ip: str) -> bool:
    try:
        return ipaddress.IPv4Address(ip) in ipaddress.IPv4Network("100.64.0.0/10")
    except Exception:
        return False


def _ip_via_ip_cmd() -> str | None:
    """Linux `ip` (iproute2) scan for a Tailscale CGNAT address."""
    for argv in (
        ["ip", "-4", "-o", "addr", "show"],
        ["/sbin/ip", "-4", "-o", "addr", "show"],
        ["/usr/sbin/ip", "-4", "-o", "addr", "show"],
    ):
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        except Exception:
            continue
        if out.returncode != 0:
            continue
        for m in re.finditer(r"inet\s+(100\.\d+\.\d+\.\d+)/\d+", out.stdout or ""):
            ip = _valid_ipv4(m.group(1))
            if ip and _in_tailnet(ip):
                return ip
    return None


def _ip_via_ifconfig() -> str | None:
    for argv in (
        ["ifconfig"],
        ["/sbin/ifconfig"],
        ["/usr/sbin/ifconfig"],
    ):
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        except Exception:
            continue
        if out.returncode != 0:
            continue
        for m in _TAILNET_RE.finditer(out.stdout or ""):
            ip = _valid_ipv4(m.group(1))
            if ip and _in_tailnet(ip):
                return ip
    return None


def tailscale_ip() -> str:
    """This machine's Tailscale IPv4 address, resolved dynamically."""
    ip = _ip_via_cli() or _ip_via_ip_cmd() or _ip_via_ifconfig()
    if not ip:
        raise RuntimeError(
            "could not resolve Tailscale IPv4 (CLI, ip, and ifconfig all failed)")
    return ip
