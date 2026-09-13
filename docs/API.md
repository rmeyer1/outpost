# Cloud Agents Dispatcher API

The HTTP API is the **primary interface** to the cloud-agents dispatcher.
`bin/agentctl` is a thin client over it (same flags, same output). Any
Tailnet client — Atlas, the grok bot harness, future tooling — speaks this API.

## Base URL and auth

- **Base URL:** `http://100.101.54.59:18443` (the Mac's Tailscale IPv4;
  resolve dynamically with `tailscale ip -4`, do not hardcode the IP).
- **Auth:** every endpoint requires `Authorization: Bearer <token>`
  (constant-time comparison). Missing/invalid token → `401 {"error":
  "unauthorized"}` with no information leakage.
- Tokens live in `~/cloud-agents/config/api.yaml` (mode `600`) under
  `clients:` — one token per client (`atlas`, `grok-bot`).

### Adding a client

On the Mac (never print the token anywhere):

```bash
python3 - <<'EOF'
import secrets, yaml
p = "/Users/server/cloud-agents/config/api.yaml"
cfg = yaml.safe_load(open(p))
cfg["clients"]["new-client"] = secrets.token_urlsafe(32)
yaml.safe_dump(cfg, open(p, "w"))
EOF
chmod 600 /Users/server/cloud-agents/config/api.yaml
```

No API restart is needed — tokens are read at startup only, so **restart
`com.cloudagents.api`** after changing them:
`launchctl kickstart -k gui/$(id -u)/com.cloudagents.api`.

## Endpoints

All responses are JSON unless noted. Errors are `{"error": "message"}`.

### `POST /jobs` — submit

```json
{
  "type": "coding",
  "repo": "scratch",
  "base": "main",
  "task": "write a hello.py that prints hi",
  "engine": "auto",
  "provider": "supergrok",
  "budget_usd": 2.0,
  "max_minutes": 30,
  "idempotency_key": "unique-per-request",
  "ref": "pull/8/head"
}
```

`ref` is optional: a git branch, tag, or `pull/N/head` checked out after the
repo is prepared (strict charset: `[A-Za-z0-9/_.-]`, no `..`). It is stored
on the job, shown in `GET /jobs/<id>`, and recorded in the manifest.

### Host-seeded repos (private GitHub access without container credentials)

Repos listed under `repos.host_seeded` in `config/agents.yaml` are private:
the host (which holds the GitHub credential) clones the repo and injects the
tree into the container via `container cp`. The SSH clone URL never enters
the container — the container only ever sees the public allowlist entry
string (e.g. `https://github.com/rmeyer1/rain-room.git`) in `CA_REPO`. When
`ref` is given, the host checks out that ref before injecting.

- `type`: `coding` | `artifact` | `research` | `data`
- `engine`: `auto` (default) | `hermes` | `grok-build`. `auto` routes to
  hermes (the default); `grok-build` selects the xAI grok CLI headless
  worker (coding lane). If the requested engine is unavailable the
  registry falls back along its chain (e.g. `grok-build` → `hermes`).
- `provider`: `supergrok` (default) | `openrouter` | `auto`
- `repo` must be on the allowlist (`config/agents.yaml` → `repos.allowlist`)
- `idempotency_key` makes resubmits safe: the same key returns the original
  job id with `"deduplicated": true`
- → `200 {"id": "job_20260912_...", "deduplicated": false}`
- → `400` on empty task, unknown repo, bad provider/type

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"type":"artifact","repo":"scratch","task":"build an Excel model","idempotency_key":"x1"}' \
  http://100.101.54.59:18443/jobs
```

### `GET /jobs?status=` — list

→ `200 {"jobs": [{"id","status","type","repo","engine_selected","provider","created_at"}]}`

### `GET /jobs/{id}` — status

The same view `agentctl status` prints: state, engine, provider, budget,
timestamps, attention flags, failover record, error, plus
`tokens: {prompt_tokens, completion_tokens, requests, estimated_usd}` and the
parsed `result` when present. → `404` for unknown job.

### `GET /jobs/{id}/logs?tail=N` — logs

→ `200 {"job_id": ..., "entries": [{"ts","kind","msg","raw"}]}` (`raw: true`
marks lines that weren't JSON). → `404` when no log file exists.

### `GET /jobs/{id}/manifest` — manifest

→ `200` with the job's `manifest.json` as parsed JSON. → `404` when absent.

### `POST /jobs/{id}/cancel` — cancel

→ `200 {"ok": true, "message": "cancel requested for ... (runner will destroy
the container)"}`. Already-terminal jobs return `200 {"ok": true,
"already_terminal": true, ...}`. → `404` for unknown job.

### `GET /attention` — hang-watchdog queue

→ `200 {"jobs": [{"id","elapsed_min","acked","type","engine_selected","provider"}]}`

### `POST /jobs/{id}/ack` — acknowledge

→ `200 {"ok": true, "message": "..."}` (idempotent). → `404` unknown job,
`409` when the job isn't awaiting attention.

### `GET /spend` — spend summary

```json
{
  "rolling_usd": 1.5234,
  "cap_usd": 25.0,
  "cap_window_days": 7.0,
  "recent": [
    {"job_id": "...", "prompt_tokens": 14872, "completion_tokens": 261,
     "requests": 3, "estimated_usd": 0.0005}
  ]
}
```

`rolling_usd` is the cash truth from OpenRouter key-usage deltas (the only
thing that counts toward the cap); `recent` is per-job token attribution.

### `GET /jobs/{id}/artifacts/{name}` — download an artifact

Binary download with `Content-Disposition: attachment`. Serves:

- files under `artifacts/<job-id>/` (nested paths allowed),
- the special names `repo.bundle` (`jobs/<job-id>/out/repo.bundle`) and
  `manifest.json` (`jobs/<job-id>/manifest.json`).

`{name}` is validated against path traversal (`..`, absolute paths → `400`).
→ `404` for unknown job or missing file.

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  -OJ http://100.101.54.59:18443/jobs/JOB_ID/artifacts/model.xlsx
```

## Security model

- **Tailnet-only bind.** The API binds the Tailscale IPv4 resolved at startup
  (`tailscale ip -4`), or `127.0.0.1` when fronted by `tailscale serve`. It
  refuses to bind `0.0.0.0`: unreachable from the LAN and from worker
  containers (which live on the container subnet and never see host tailnet
  services).
- **Bearer tokens per client**, stored mode-600 in `config/api.yaml`, compared
  in constant time, never logged.
- **TLS:** all tailnet traffic is WireGuard-encrypted, so the API serves
  plain HTTP on the Tailscale interface. `tailscale serve --https` was
  evaluated as an alternative: it works (verified end-to-end over
  `https://<machine>.ts.net:9444` with a valid bearer token), but without
  `--bg` the serve config is held by the CLI process rather than persisted,
  which fights automation — and the existing `:443`/`:9443` serve entries
  belong to other services (left untouched). Plain HTTP on the tailnet is
  the simpler, more robust choice here.
- **Secrets stay server-side.** The API never handles the SuperGrok
  credential or the OpenRouter key — those remain with the runner/broker on
  the Mac, exactly as before.
- **No logic duplication.** `service/dispatch.py` is the single implementation
  of every operation; `service/api.py` is transport only; `bin/agentctl` is a
  presentation-only HTTP client.

## Operations

- Launch agent: `com.cloudagents.api` (keep-alive, restarts on crash/boot).
- Logs: `~/cloud-agents/build/api.log` (one line per request: client, method,
  path, status, ms — never tokens).
- Health check: `curl -H "Authorization: Bearer $TOKEN" http://100.101.54.59:18443/spend`
