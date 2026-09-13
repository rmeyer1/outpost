# hermes-agent (optional)

This directory is a stub so `COPY hermes-agent` in the worker Containerfile
succeeds on a fresh clone.

To enable the in-container Hermes engine, replace this directory with the
vendored hermes-agent source tree (the one that contains `pyproject.toml`)
and rebuild:

```bash
docker build -t ca-worker:latest images/worker
# or: container build -t ca-worker:latest images/worker
```

Do not bake credentials into the image. Model access is always brokered
through `CA_PROXY_URL` + `CA_CLIENT_TOKEN`.
