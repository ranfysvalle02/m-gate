FROM python:3.12-slim AS builder

WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ARG MONGODB_CRYPT_VERSION=8.0.1

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
# --proxy-headers makes uvicorn honor X-Forwarded-Proto/For, but ONLY from peers in
# FORWARDED_ALLOW_IPS (uvicorn reads that env var; defaults to 127.0.0.1). Set it to
# your ingress/LB address range in production so client IP (rate limiting) and scheme
# (Secure cookies) are correct without trusting spoofable headers from the internet.
CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
