# Cloud Agents

Self-hosted "cloud agents". Each Outpost installation is a complete,
standalone dispatcher (API, runner, workers) — a Mac mini, a Raspberry Pi,
or any Linux host with Docker. Installations do not communicate with each
other. Any authorized client — Atlas, the grok bot harness, future tools —
submits jobs to whichever installation's API it chooses.

This README is written for **client tools**. It tells you what the system is
and exactly how to use it. (Operator details live in `docs/API.md`; the full
design is in the Cloud Agents design PDF.)

## What this is

You submit a task. A disposable, isolated worker container spins up on
**this** Outpost host, clones the repo inside itself, runs an AI
coding/generalist harness against your task, commits the result to a
branch, and is destroyed. You get back logs, a manifest, a repo bundle,
and any artifacts the task produced (e.g. an `.xlsx` file for a modeling
task).

Each job is fully isolated:

- One container per job; nothing persists between jobs except what the job
  explicitly produces.
- The container never sees the host's home directory, SSH keys, or any
  credential. Model access goes through a host-side broker that attaches the
  real credential server-side.
- Repos are cloned *inside* the container. Only branches, bundles,
  artifacts, logs, summaries, and manifests survive.

## Connecting

The dispatcher exposes an HTTP API on this installation (Tailscale address
when available, otherwise loopback). It is the primary interface;
`bin/agentctl` is just a thin client over it.

- **Base URL:** this installation (`http://<host>:18443`). On a tailnet,
  resolve it dynamically with `tailscale ip -4` on **that** host; do not
  hardcode the IP. Tailscale is optional.
- **Auth:** every request needs `Authorization: Bearer <token>`.
  Your token is provisioned by the operator of that installation (stored
  mode-600 in `config/api.yaml`). Missing/invalid token → 401.
- The API never binds `0.0.0.0`. Worker containers cannot reach it.

## Delegating work: the job lifecycle

**1. Submit.** `POST /jobs` with a JSON body:

```json
{
  "type": "coding",
  "repo": "scratch",
  "base": "main",
  "task": "Add input validation to process_upload() and cover it with tests",
  "engine": "auto",
  "provider": "supergrok",
  "idempotency_key": "my-unique-key-001"
}
```

- `type`: `coding` | `artifact` | `research` | `data`. Use `artifact` for
  non-code deliverables ("build an Excel model, return the .xlsx").
- `engine`: `auto` (default → Hermes) | `hermes` | `grok-build` (xAI grok CLI
  headless worker, best for large coding tasks). Unavailable engines fall
  back along a registry chain.
- `provider`: `supergrok` (default, subscription-funded) | `openrouter`
  (metered, $25 rolling 7-day cap) | `auto`.
- `repo` must be on the server's allowlist. `scratch` always works for
  standalone tasks.
- `idempotency_key`: resubmitting with the same key returns the original job
  (`"deduplicated": true`) instead of launching a duplicate. Always set one.

→ `200 {"id": "job_20260912_...", "deduplicated": false}`

**2. Poll.** `GET /jobs/{id}` returns the full status view: state, engine,
provider, elapsed time, attention flags, token counts, spend estimate, and
the parsed result once finished. Poll every 30–60 seconds; most jobs finish
in minutes, long ones can run up to 3 hours (hard ceiling).

**3. Collect.** When the job is done:

- `GET /jobs/{id}/manifest` — the complete result record (changed files,
  commit, tokens, spend, failover events).
- `GET /jobs/{id}/artifacts/{name}` — download produced files
  (`model.xlsx`, `repo.bundle`, `manifest.json`, …).
- `GET /jobs/{id}/logs?tail=200` — what happened, if you need to debug.

**4. Manage.** `POST /jobs/{id}/cancel` kills a running job (container
destroyed). `GET /attention` lists jobs the watchdog flagged; `POST
/jobs/{id}/ack` acknowledges one. `GET /spend` shows the OpenRouter rolling
spend vs the $25 cap plus per-job token attribution.

Minimal shell example:

```bash
TOKEN=...  # your bearer token
API=http://$(tailscale ip -4):18443

JOB=$(curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"type":"artifact","repo":"scratch","task":"Build a 3-statement Excel model","idempotency_key":"q3-model-1"}' \
  $API/jobs | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')

while true; do
  STATE=$(curl -s -H "Authorization: Bearer $TOKEN" $API/jobs/$JOB | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')
  echo "$STATE"; [ "$STATE" = done ] && break; sleep 45
done

curl -s -H "Authorization: Bearer $TOKEN" -OJ $API/jobs/$JOB/artifacts/model.xlsx
```

## Guardrails you should know about

- **Spend tiers.** SuperGrok is subscription-funded (no dollar cap, usage can
  be liberal). OpenRouter is capped at $25 per rolling 7 days, enforced from
  real key-usage deltas; token-level per-job estimates are reported for
  attribution.
- **Automatic failover.** If SuperGrok rate-limits mid-job (429s), the job
  transparently hops to OpenRouter without restarting. The manifest records
  it; expect a possible shift in response style after a hop.
- **Hang watchdog.** Jobs running past 45 minutes are flagged for attention
  (visible via `GET /attention`); unacknowledged jobs are killed at 75
  minutes. Acknowledged jobs may run to the absolute 3-hour ceiling.
- **Budgets.** You can set `budget_usd` and `max_minutes` per job at submit
  time.

## Security model (for clients)

- Your token identifies your client; it carries no access beyond the API.
- You never see, handle, or need the model credentials — SuperGrok and
  OpenRouter keys stay server-side on that installation and never appear
  in any API response or log.
- Keep your bearer token secret. If it leaks, ask the operator to rotate it.

## For the human operator

- `bin/agentctl` — same operations from this host's shell.
- `docs/API.md` — full endpoint reference, token provisioning, TLS notes.
- Linux: `outpost.service` (systemd) via `./bin/install.sh`.
- macOS: `com.cloudagents.api` (the API), `com.cloudagents.controller`
  (the dispatcher), `com.cloudagents.container-system`.
- Each Outpost checkout is a complete, standalone installation.

## Install on a Raspberry Pi

The Pi is a **first-class Outpost host**. After install it has its own
dispatcher, API, runner, and workers. It does **not** communicate with any
other Outpost host — the Mac mini can be powered off or decommissioned and
the Pi keeps working. An agent harness submits jobs to whichever
installation's API it chooses.

Live Docker / Pi verification of this path is still pending on a real
Docker host; the steps below are what `bin/install.sh` implements.

### 1. 64-bit Pi OS prep

- Raspberry Pi OS **64-bit** (Debian Bookworm or later). 32-bit is not
  supported — the worker image only builds for `linux/arm64` and
  `linux/amd64`.
- A Pi 4 or 5 with **4 GB RAM minimum** (8 GB is more comfortable).
- Network so the host can pull `python:3.12-slim` and the pinned CLIs.
- Tailscale is **optional**. The API binds the tailnet address when
  `tailscale ip -4` works; otherwise it binds `127.0.0.1`. You do not
  need Tailscale for a local or LAN install (`bind:` in
  `config/api.yaml`).

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-yaml
git clone https://github.com/rmeyer1/outpost.git
cd outpost
```

### 2. Install

```bash
./bin/install.sh
```

The script is idempotent (safe to re-run). It will:

- Install Docker Engine from the official Docker apt repo if it is missing.
- `docker build -t ca-worker:latest images/worker` (Containerfile is
  multi-arch; `python:3.12-slim` plus arm64 node/goose/grok binaries).
- Generate `config/api.yaml` from the example with **fresh random tokens**
  (never hardcoded; mode 600). Existing `api.yaml` is left untouched.
- Set `worker.backend: docker` and Pi-sized limits (`max_workers: 1`,
  `container_memory: 2g`).
- Install and start `service/outpost.service` via systemd.

On macOS the same script prints launchd notes and does not write
LaunchAgents.

### 3. Dashboard URL

Printed at the end of the installer:

- Local: `http://127.0.0.1:18443/dashboard`
- Tailnet (if Tailscale is up): `http://<tailscale-ipv4>:18443/dashboard`

Tokens are in `config/api.yaml` and are never printed. Pair a dashboard
from the UI, or copy a client token onto the device yourself.

### 4. Submit the first job

From the Pi (or any client that can reach this API):

```bash
bin/agentctl --client atlas submit \
  --type coding --repo scratch \
  --task "Write hello.py that prints hi" \
  --idempotency-key pi-first-job-1
bin/agentctl status <job-id>
```

Or `POST /jobs` against this installation's base URL with
`Authorization: Bearer <token>`. Point the harness at **this** API — not
another host's.

### 5. Size worker concurrency for the Pi's RAM

`config/agents.yaml` `limits:`:

| Board RAM | `max_workers` | `container_memory` | `container_cpus` |
|-----------|---------------|--------------------|------------------|
| 4 GB      | 1             | `2g`               | 2                |
| 8 GB      | 1             | `3g`               | 2–3              |
| 8 GB+     | 2             | `2g` each          | 2                |

Keep `max_workers: 1` on 4 GB boards. The worker image plus the model
proxy and OS will otherwise swap. Restart `outpost.service` after
editing limits.

This Pi install is independent and does not communicate with any other
Outpost host.
