FROM python:3.12-slim AS builder

WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
# >= 8.1 so QE client-side query analysis (crypt_shared) recognizes the
# $rankFusion hybrid-search stage; keep aligned with the mongod image minor
# (docker-compose.yml mongodb-atlas-local:8.3). crypt_shared is published per
# server patch on the Enterprise downloads CDN.
ARG MONGODB_CRYPT_VERSION=8.3.2

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/mongodb/lib && \
    ARCH="$(dpkg --print-architecture)" && \
    if [ "$ARCH" = "amd64" ]; then ARCH="x86_64"; fi && \
    if [ "$ARCH" = "arm64" ]; then ARCH="aarch64"; fi && \
    curl -fsSL -o /tmp/mongo_crypt_shared.tgz \
      "https://downloads.mongodb.com/linux/mongo_crypt_shared_v1-linux-${ARCH}-enterprise-ubuntu2204-${MONGODB_CRYPT_VERSION}.tgz" && \
    tar -xzf /tmp/mongo_crypt_shared.tgz -C /tmp && \
    cp "$(find /tmp -name mongo_crypt_v1.so -print -quit)" /opt/mongodb/lib/mongo_crypt_v1.so && \
    chmod 755 /opt/mongodb/lib/mongo_crypt_v1.so && \
    rm -rf /tmp/mongo_crypt_shared.tgz /tmp/mongo_crypt_shared_v1*

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip wheel --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV CRYPT_SHARED_LIB_PATH=/opt/mongodb/lib/mongo_crypt_v1.so

COPY --from=builder /wheels /wheels
COPY --from=builder /opt/mongodb /opt/mongodb
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

RUN useradd --create-home --uid 10001 appuser
COPY --chown=appuser:appuser . /app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"
USER appuser

# Precompile the CPython-on-WASI module into the cache at build time so every
# container start (including a recreate) deserializes an already-compiled
# artifact instead of running wasmtime's parallel (rayon) Cranelift compile on
# the hot path -- the compile is the cold-start cost most prone to thread-spawn
# (EAGAIN) / CPU-limit failures under host load. The cache key is derived from
# the wasm file's resolved path + size + mtime + wasmtime version, all identical
# between this build layer and runtime, so the baked artifact is a guaranteed
# hit. Best-effort: if the wasm is absent (run `make fetch-wasm`) or a compile
# hiccups, the worker simply falls back to compiling at startup -- no regression.
RUN if [ -f vendor/python-3.12.0.wasm ]; then \
      echo "Precompiling CPython-on-WASI sandbox module cache..." && \
      python -c "from pathlib import Path; from services.sandbox_worker import _build_engine, _load_module; _load_module(_build_engine(), Path('vendor/python-3.12.0.wasm').resolve(), 'vendor/.wasm-cache'); print('Sandbox module cache ready at vendor/.wasm-cache')" || \
      echo "WARNING: sandbox module precompile failed; worker will compile at startup."; \
    else \
      echo "WARNING: vendor/python-3.12.0.wasm not found; skipping sandbox module precompile (run 'make fetch-wasm')."; \
    fi

# --proxy-headers makes uvicorn honor X-Forwarded-Proto/For, but ONLY from peers in
# FORWARDED_ALLOW_IPS (uvicorn reads that env var; defaults to 127.0.0.1). Set it to
# your ingress/LB address range in production so client IP (rate limiting) and scheme
# (Secure cookies) are correct without trusting spoofable headers from the internet.
CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
