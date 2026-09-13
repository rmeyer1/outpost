# Outpost worker image (linux/amd64 and linux/arm64).
# Also shipped as Dockerfile so `docker build .` works without -f.
# Build with either:
#   docker build -t ca-worker:latest .
#   container build -t ca-worker:latest .
# python:3.12-slim is multi-arch; ARCH below picks node/goose/grok binaries.
# Pinned python base; hermes-agent installed from the vendored source tree
# (exact-pinned deps in its pyproject.toml) when present next to this file.
#
# Harness CLIs (codex / opencode via npm; goose via GitHub
# release tarball) are pinned below. Rebuild on the host after changing
# versions, then run each adapter's probe(). Do not bake credentials.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NODE_VERSION=22.23.2 \
    # Pinned to 0.94.0: last codex with the chat wire API (0.95.0+
    # removed it; the broker is chat-completions only). Verified
    # 2026-09-13: 0.94.0 accepts wire_api="chat" (deprecation warning
    # only) and POSTs /v1/chat/completions.
    CODEX_VERSION=0.94.0 \
    OPENCODE_VERSION=1.18.30 \
    GOOSE_VERSION=1.50.0 \
    # xAI Grok Build CLI (standalone Rust binary, versioned independently of
    # the npm meta-package). Installed directly from the registry tarball
    # below so /usr/local/bin/grok is the real binary the engine probe
    # expects. NOTE: grok was silently lost by the 2026-09-13 image rebuilds
    # because no install step existed — keep this step.
    GROK_VERSION=1.0.30

# git: in-container clone/commit. curl+xz-utils: Node tarball + goose download
# (purged at the end of the CLI layer so they do not stay in the image).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl xz-utils \
 && rm -rf /var/lib/apt/lists/*

# Vendored hermes-agent source (optional). A stub directory ships in git so
# `COPY` succeeds for both `docker build` and `container build`; replace it
# with the real tree to enable the in-container hermes engine. Other engines
# (grok-build, codex, goose, opencode) do not need it.
COPY hermes-agent /opt/hermes-agent
RUN if [ -f /opt/hermes-agent/pyproject.toml ] || [ -f /opt/hermes-agent/setup.py ]; then \
      pip install --no-cache-dir /opt/hermes-agent ; \
    else \
      echo "hermes-agent stub only — hermes engine not baked into this image" ; \
    fi

# Node 22 (pinned) plus the three harness CLIs.
# npm packages: @openai/codex, opencode-ai.
# goose: official GitHub release binary (Rust), not npm.
# Single layer so Node, CLIs, and the curl/xz build deps collapse together.
RUN ARCH="$(dpkg --print-architecture)" \
 && case "$ARCH" in \
      amd64) NODE_ARCH=x64;   GOOSE_ARCH=x86_64-unknown-linux-gnu; GROK_ARCH=x64 ;; \
      arm64) NODE_ARCH=arm64; GOOSE_ARCH=aarch64-unknown-linux-gnu; GROK_ARCH=arm64 ;; \
      *) echo "unsupported arch: $ARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" \
      | tar -xJ -C /usr/local --strip-components=1 \
 && npm install -g --omit=dev \
      "@openai/codex@${CODEX_VERSION}" \
      "opencode-ai@${OPENCODE_VERSION}" \
 && npm cache clean --force \
 && mkdir -p /tmp/goose-extract \
 && curl -fsSL "https://github.com/block/goose/releases/download/v${GOOSE_VERSION}/goose-${GOOSE_ARCH}.tar.gz" \
      | tar -xz -C /tmp/goose-extract \
 && GOOSE_SRC="$(find /tmp/goose-extract -type f -name goose | head -n 1)" \
 && test -n "$GOOSE_SRC" \
 && install -m 0755 "$GOOSE_SRC" /usr/local/bin/goose \
 && curl -fsSL "https://registry.npmjs.org/@xai-official/grok-linux-${GROK_ARCH}/-/grok-linux-${GROK_ARCH}-${GROK_VERSION}.tgz" -o /tmp/grok.tgz \
 && tar -xzf /tmp/grok.tgz -C /tmp package/bin/grok.br \
 && node -e "process.stdout.write(require('zlib').brotliDecompressSync(require('fs').readFileSync('/tmp/package/bin/grok.br')))" > /usr/local/bin/grok \
 && chmod 0755 /usr/local/bin/grok \
 && rm -rf /tmp/goose-extract /tmp/grok.tgz /tmp/package /root/.npm /tmp/* \
 && apt-get purge -y curl xz-utils \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && node --version \
 && codex --version \
 && opencode --version \
 && goose --version \
 && grok --version

# Non-root user for harness CLIs that refuse to run as root.
# The entrypoint runs as root and chowns the per-job dirs to this user.
RUN useradd -m -u 1000 agent \
 && mkdir -p /work /out \
 && chown -R agent:agent /work /out /home/agent

COPY entrypoint.py /opt/ca/entrypoint.py
RUN chmod +x /opt/ca/entrypoint.py

WORKDIR /work
ENTRYPOINT ["python3", "/opt/ca/entrypoint.py"]
