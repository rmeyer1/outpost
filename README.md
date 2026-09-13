# Outpost

**Your agents live at your outpost.** Outpost is a self-hosted dock for AI
agents, running on your own hardware — a Mac mini, a Linux box, even a
Raspberry Pi. Any authorized agent — Atlas, a grok bot harness, future
clients — can delegate long or heavy tasks here instead of doing the work
inline.

This README is written for **client tools**. It tells you what the system is
and exactly how to use it. (Operator details live in `docs/API.md`; the full
design is in the Outpost design doc.)

## What this is

You submit a task. A disposable, isolated worker container spins up on the
host, clones the repo inside itself, runs an AI coding/generalist harness
against your task, commits the result to a branch, and is destroyed. You get
back logs, a manifest, a repo bundle, and any artifacts the task produced
(e.g. an `.xlsx` file for a modeling task).

Each job is fully isolated:

- One container per job; nothing persists between jobs except what the job
  explicitly produces.
- The container never sees the host's home directory, SSH keys, or any
  credential. Model access goes through a host-side broker that attaches the
  real credential server-side.
- Repos are cloned *inside* the container. Only branches, bundles,
  artifacts, logs, summaries, and manifests survive.

## Connecting

The dispatcher exposes a Tailnet-only HTTP API. It is the primary interface;
`bin/agentctl` is just a thin client over it.

- **Base URL:** `http://<host>:18443` — your host's address.
  Resolve it dynamically with `tailscale ip -4`; do not hardcode the IP.
- **Auth:** every request needs `Authorization: Bearer <token>`.
  Your token is provisioned by the human operator (stored mode-600 in
  `~/outpost/config/api.yaml` on the host). Missing/invalid token → 401.
- There is no LAN or internet exposure: the API binds the Tailscale address
  only. Worker containers cannot reach it.

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
  OpenRouter keys stay server-side on the Mac and never appear in any API
  response or log.
- Keep your bearer token secret. If it leaks, ask the operator to rotate it.

## For the human operator

- `bin/agentctl` — same operations from the host's shell.
- `docs/API.md` — full endpoint reference, token provisioning, TLS notes.
- Services: `com.outpost.api` (the API), `com.outpost.controller`
  (the dispatcher), `com.outpost.container-system` (macOS launchd labels;
  systemd units for Linux are on the roadmap).
- Install root: `~/outpost`. Copy `config/api.yaml.example` to
  `config/api.yaml`, fill in bearer tokens (mode `600`), and start the
  services.
