# syntax=docker/dockerfile:1.7
# voice-gateway: the realtime speech surface (ADR 0006). Build from the REPOSITORY ROOT:
#
#   docker build -f infra/docker/voice-gateway.Dockerfile -t voice-gateway:dev .
#
# This is the third service, alongside commerce-api and action-executor, and it is the one
# with the unusual shape: a WebSocket at /v1/voice/stream that stays open for the whole of a
# buyer's conversation, with two persistent outbound streams to Google per session.
#
# WHY IT IS NOT A CLOUD RUN SERVICE
# --------------------------------
# Two properties of the code decide this, not preference. The ticket store is in-memory and
# process-local (`wire/tickets.py`) while minting is an HTTP POST and redemption is a
# separate WebSocket upgrade -- two requests that must reach the same process. And the socket
# loop has no duration bound on purpose: STREAM_ROTATION_MARGIN_S sits under the provider's
# stream limit so a buyer's session outlives Google's, indefinitely. A per-request runtime
# with a request ceiling and free instance movement breaks both. One long-lived Pod does not.
#
# Spec 21.8: minimal pinned base images, uv.lock only, an explicit non-root user, and a
# filesystem where only /tmp is written.
ARG PYTHON_IMAGE=python:3.14-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.9
ARG PACKAGE=voice-runtime
# `voice_runtime.gateway.app` exposes the factory `create_app()` and no module-level `app`,
# for the same reason commerce-api does: importing the module must not require a configured
# environment. An APP_MODULE override must therefore also name a factory.
ARG APP_MODULE=voice_runtime.gateway.app:create_app

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------------------------------
# builder: resolve the locked dependency set into a self-contained virtualenv.
# ---------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder
ARG PACKAGE
# git is a build-time dependency only: uv.lock pins at least one source to a git commit, and
# resolving it makes uv shell out to a real git binary. The runtime stage copies /app/.venv
# and nothing else, so neither git nor apt's lists survive into the shipped image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_PROGRESS=1
WORKDIR /app

# Layer 1: third-party dependencies only, reused until uv.lock changes. uv loads the whole
# workspace from the root pyproject, so every member's manifest must be present even though
# voice-runtime depends on exactly one of them.
COPY pyproject.toml uv.lock ./
COPY packages/commerce-domain/pyproject.toml    packages/commerce-domain/
COPY packages/voice-runtime/pyproject.toml      packages/voice-runtime/
COPY packages/platform-db/pyproject.toml        packages/platform-db/
COPY packages/transaction-kernel/pyproject.toml packages/transaction-kernel/
COPY packages/durable-work/pyproject.toml       packages/durable-work/
COPY packages/merchant-sim/pyproject.toml       packages/merchant-sim/
COPY packages/payment-adapters/pyproject.toml   packages/payment-adapters/
COPY packages/commerce-api/pyproject.toml       packages/commerce-api/
COPY packages/action-executor/pyproject.toml    packages/action-executor/
COPY packages/agent-runtime/pyproject.toml      packages/agent-runtime/
COPY packages/commerce-protocols/pyproject.toml packages/commerce-protocols/
COPY packages/merchant-adapter/pyproject.toml   packages/merchant-adapter/
COPY packages/merchant-controller/pyproject.toml packages/merchant-controller/
COPY packages/platform-observability/pyproject.toml packages/platform-observability/
COPY packages/reserve-trust/pyproject.toml packages/reserve-trust/
COPY packages/reserve-signer/pyproject.toml packages/reserve-signer/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package "${PACKAGE}"

# Layer 2: the workspace members, installed non-editable so the runtime image carries the
# virtualenv only and not the source tree.
COPY packages/ packages/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${PACKAGE}"

# ---------------------------------------------------------------------------------------
# runtime: python:slim + the virtualenv. No compiler, no uv, no package caches.
# ---------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ARG APP_MODULE
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="voice-gateway" \
      org.opencontainers.image.description="Governed agentic commerce: realtime speech gateway. Mints no principal, holds no capability, records no consent." \
      org.opencontainers.image.vendor="Governed Agentic Commerce (Razorpay AI Buildathon Track 1)" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.base.name="docker.io/library/python:3.14-slim-bookworm" \
      org.opencontainers.image.source="https://github.com/OWNER/REPO"

# Fixed UID/GID so the Pod's runAsUser can pin the same identity (spec 21.8).
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --home-dir /tmp --no-create-home \
      --shell /usr/sbin/nologin app \
 && mkdir -p /app && chown 10001:10001 /app \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 infra/docker/entrypoint.py /app/entrypoint.py

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    HOME=/tmp \
    TMPDIR=/tmp \
    APP_SECRETS_DIR=/var/run/secrets/app \
    APP_MODULE="${APP_MODULE}" \
    PORT=8100 \
    WEB_CONCURRENCY=1

WORKDIR /app
USER 10001:10001
EXPOSE 8100
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8100') + '/healthz', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["python", "/app/entrypoint.py"]
# WEB_CONCURRENCY=1 is not a performance setting here, it is a correctness one: the voice
# ticket store is in-process, so a second worker would mint tickets the other cannot redeem.
# Scale this service by Pods behind session affinity, never by workers inside a Pod.
#
# The graceful-shutdown window is longer than the API's because a drain here is a live
# conversation: 60s lets an in-flight turn finish and the socket close cleanly instead of
# being severed mid-sentence.
CMD ["uvicorn", "${APP_MODULE}", "--factory", "--host", "0.0.0.0", "--port", "${PORT}", \
     "--proxy-headers", "--forwarded-allow-ips=*", "--no-server-header", \
     "--ws", "websockets", "--timeout-graceful-shutdown", "60"]
