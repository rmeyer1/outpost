#!/bin/sh
# Outpost standalone install. Idempotent; safe to re-run.
#
# Each installation is a complete Outpost (dispatcher, API, runner, workers).
# It does not contact any other Outpost host.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

log() { printf '%s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
  command -v sudo >/dev/null 2>&1 || die "need root or sudo"
fi

run_root() {
  if [ -z "$SUDO" ]; then
    "$@"
  else
    $SUDO "$@"
  fi
}

OS="$(uname -s)"
case "$OS" in
  Darwin) OS_KIND=macos ;;
  Linux)  OS_KIND=linux ;;
  *)      die "unsupported OS: $OS (need Linux or macOS)" ;;
esac

is_debian() {
  [ -f /etc/os-release ] || return 1
  # Raspberry Pi OS (64-bit) and Debian both set ID=debian or ID=raspbian.
  grep -qE '^ID=(debian|raspbian)$' /etc/os-release \
    || grep -qE '^ID_LIKE=.*(debian|raspbian)' /etc/os-release
}

install_python_deps() {
  if python3 -c "import yaml" >/dev/null 2>&1; then
    return 0
  fi
  if [ "$OS_KIND" = linux ] && is_debian; then
    log "installing python3-yaml..."
    run_root apt-get update -qq
    run_root apt-get install -y python3 python3-yaml python3-venv ca-certificates curl
  else
    die "PyYAML is required (python3 -c 'import yaml'). Install python3-yaml and re-run."
  fi
}

docker_ok() {
  command -v docker >/dev/null 2>&1 || return 1
  docker info >/dev/null 2>&1
}

install_docker_debian() {
  if docker_ok; then
    log "docker already installed and reachable"
    return 0
  fi
  if command -v docker >/dev/null 2>&1; then
    log "docker CLI present but daemon not reachable yet; skipping reinstall"
    return 0
  fi
  log "installing Docker Engine from download.docker.com..."
  run_root apt-get update -qq
  run_root apt-get install -y ca-certificates curl
  run_root install -m 0755 -d /etc/apt/keyrings
  if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    curl -fsSL https://download.docker.com/linux/debian/gpg \
      | run_root tee /etc/apt/keyrings/docker.asc >/dev/null
    run_root chmod a+r /etc/apt/keyrings/docker.asc
  fi
  # Raspberry Pi OS 64-bit is Debian; use the Debian Docker repo.
  CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-bookworm}")"
  ARCH="$(dpkg --print-architecture)"
  echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${CODENAME} stable" \
    | run_root tee /etc/apt/sources.list.d/docker.list >/dev/null
  run_root apt-get update -qq
  run_root apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  if [ "$(id -u)" -ne 0 ]; then
    run_root usermod -aG docker "$(id -un)" || true
    log "added $(id -un) to the docker group (log out/in if docker still needs sudo)"
  fi
}

write_api_yaml() {
  api="$ROOT/config/api.yaml"
  if [ -f "$api" ]; then
    log "keeping existing config/api.yaml"
    return 0
  fi
  log "generating config/api.yaml with fresh random tokens..."
  python3 - "$ROOT/config/api.yaml.example" "$api" <<'PY'
import secrets, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
# Never hardcode tokens. Replace every REPLACE-ME with a unique secret.
while "REPLACE-ME" in text:
    text = text.replace("REPLACE-ME", secrets.token_urlsafe(32), 1)
open(dst, "w").write(text)
os_chmod = __import__("os").chmod
os_chmod(dst, 0o600)
PY
}

tune_linux_agents_yaml() {
  [ "$OS_KIND" = linux ] || return 0
  python3 - "$ROOT/config/agents.yaml" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
text = p.read_text()
changed = False
# Explicit docker backend on Linux (platform default is docker anyway).
if "backend: \"\"" in text:
    text = text.replace("backend: \"\"", "backend: docker", 1)
    changed = True
elif "worker:" not in text or "backend:" not in text:
    extra = (
        "\n# Set by bin/install.sh — this host is a standalone Outpost.\n"
        "worker:\n"
        "  backend: docker\n"
    )
    text = text.rstrip() + extra + "\n"
    changed = True
# Pi-sized defaults: one worker, 2 CPU, 2g RAM. Operators raise these
# after reading the Raspberry Pi section of the README.
# Drop the Mac-only hermes_venv path so Linux does not look at /Users.
for old, new in (
    ("max_workers: 2", "max_workers: 1"),
    ("container_cpus: 4", "container_cpus: 2"),
    ('container_memory: "8g"', 'container_memory: "2g"'),
    ('hermes_venv: "/Users/server/.hermes/hermes-agent/venv"',
     'hermes_venv: ""'),
):
    if old in text:
        text = text.replace(old, new, 1)
        changed = True
if changed:
    p.write_text(text)
PY
}

tune_linux_spend_yaml() {
  [ "$OS_KIND" = linux ] || return 0
  python3 - "$ROOT/config/spend.yaml" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
text = p.read_text()
# The repo default points at the macOS operator's key file. On Linux, use
# the platform-default location, which is also service/openrouter.py's
# fallback when openrouter_key_file is unset.
old = 'openrouter_key_file: "/Users/server/.config/goose/secrets.yaml"'
new = 'openrouter_key_file: "~/.config/outpost/openrouter.yaml"'
if old in text:
    p.write_text(text.replace(old, new, 1))
PY
}

ensure_dirs() {
  mkdir -p "$ROOT/state" "$ROOT/jobs" "$ROOT/artifacts" "$ROOT/logs"
}

build_worker_image() {
  if ! command -v docker >/dev/null 2>&1; then
    log "docker CLI not on PATH — skip image build (install Docker and re-run)"
    return 0
  fi
  if ! docker info >/dev/null 2>&1; then
    log "docker daemon not reachable — skip image build"
    log "  after adding yourself to the docker group, log out/in and re-run:"
    log "  docker build -t ca-worker:latest images/worker"
    return 0
  fi
  log "building ca-worker:latest..."
  docker build -t ca-worker:latest "$ROOT/images/worker"
}

install_systemd() {
  unit_src="$ROOT/service/outpost.service"
  unit_dst="/etc/systemd/system/outpost.service"
  py="$(command -v python3)"
  log "installing systemd unit $unit_dst"
  tmp="$(mktemp)"
  # Substitute install-time paths; keep the rest of the shipped unit.
  sed \
    -e "s|WorkingDirectory=/opt/outpost|WorkingDirectory=$ROOT|" \
    -e "s|ExecStart=/usr/bin/python3 /opt/outpost/bin/outpost-service|ExecStart=$py $ROOT/bin/outpost-service|" \
    "$unit_src" > "$tmp"
  if [ "$(id -u)" -ne 0 ]; then
    user="$(id -un)"
    group="$(id -gn)"
    python3 - "$tmp" "$user" "$group" <<'PY'
import sys
path, user, group = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
needle = "[Service]\n"
if needle in text and f"User={user}" not in text:
    text = text.replace(
        needle,
        needle + f"User={user}\nGroup={group}\n",
        1,
    )
    open(path, "w").write(text)
PY
  fi
  run_root cp "$tmp" "$unit_dst"
  rm -f "$tmp"
  run_root systemctl daemon-reload
  run_root systemctl enable --now outpost.service
  log "systemd: outpost.service enabled and started"
}

print_launchd_notes() {
  cat <<EOF

macOS launchd notes
-------------------
This installer does not write LaunchAgents. Existing operators keep:

  com.cloudagents.api
  com.cloudagents.controller
  com.cloudagents.container-system

To run this checkout by hand:

  python3 $ROOT/service/api.py
  python3 $ROOT/service/controller.py

worker.backend defaults to apple on darwin. Override with:

  worker:
    backend: apple   # or docker, if you use Docker Desktop
EOF
}

dashboard_hint() {
  port="$(python3 - "$ROOT/config/api.yaml" <<'PY' 2>/dev/null || echo 18443
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1])) or {}
print(int(cfg.get("port") or 18443))
PY
)"
  ts_ip=""
  if command -v tailscale >/dev/null 2>&1; then
    ts_ip="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
  fi
  log ""
  log "Dashboard"
  log "---------"
  log "  local:    http://127.0.0.1:${port}/dashboard"
  if [ -n "$ts_ip" ]; then
    log "  tailnet:  http://${ts_ip}:${port}/dashboard"
  else
    log "  Tailscale is optional. If you install it later, the API bind: auto"
    log "  setting will pick up the tailnet address on the next restart."
  fi
  log ""
  log "Next steps"
  log "----------"
  log "  1. Tokens live in config/api.yaml (mode 600). They are never printed."
  log "     Submit jobs with:  bin/agentctl --client atlas submit --type coding --repo scratch --task '...'"
  log "  2. OpenRouter (optional): put OPENROUTER_API_KEY in"
  log "     ~/.config/outpost/openrouter.yaml (Linux installs already point"
  log "     spend.yaml there; macOS keeps its own key path)."
  log "  3. This install is standalone. It does not talk to any other Outpost host."
  log "  4. Pi RAM: keep limits.max_workers at 1 and container_memory at 2g on"
  log "     4 GB boards; an 8 GB Pi can raise memory to 3g. See the README."
}

# --- main -------------------------------------------------------------------

log "Outpost standalone install  root=$ROOT  os=$OS_KIND"
log "This host will be a complete Outpost. It will not contact another install."

install_python_deps
ensure_dirs
write_api_yaml
tune_linux_agents_yaml
tune_linux_spend_yaml

if [ "$OS_KIND" = linux ]; then
  if is_debian; then
    install_docker_debian
  elif ! command -v docker >/dev/null 2>&1; then
    log "not Debian/Raspberry Pi OS — install Docker yourself, then re-run"
  fi
  build_worker_image
  if command -v systemctl >/dev/null 2>&1; then
    install_systemd
  else
    log "systemd not found — start by hand:"
    log "  python3 $ROOT/bin/outpost-service"
  fi
else
  print_launchd_notes
fi

chmod 755 "$ROOT/bin/outpost-service" "$ROOT/bin/agentctl" 2>/dev/null || true
dashboard_hint
log "install: done"
