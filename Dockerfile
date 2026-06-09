FROM python:3.12-slim AS builder

WORKDIR /app
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip wheel --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY --from=builder /wheels /wheels
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
