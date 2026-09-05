# syntax=docker/dockerfile:1.7
# commerce-api: the FastAPI modular monolith (ADR 0003). Build from the REPOSITORY ROOT:
#
#   docker build -f infra/docker/commerce-api.Dockerfile -t commerce-api:dev .
#
# See infra/docker/README.md for the full commands and the root .dockerignore to apply.
# Spec 21.8: minimal pinned base images, uv.lock only (no floating dependencies), an
# explicit non-root user, read-only root filesystem friendly (only /tmp is written).
#
# Digest pins: once the base images are mirrored into Artifact Registry, replace each tag
# with `tag@sha256:<digest>` (`docker buildx imagetools inspect <image>` prints it). Tags
# alone are pinned here so the file builds anywhere; the digest is the supply-chain pin.
ARG PYTHON_IMAGE=python:3.14-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.9
ARG PACKAGE=commerce-api
# The ASGI application path. Parameterised on purpose: the module is being written; the
# Pod can override it through the APP_MODULE environment variable without a rebuild.
ARG APP_MODULE=commerce_api.app:app

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------------------------------
# builder: resolve the locked dependency set into a self-contained virtualenv.
# ---------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder
ARG PACKAGE
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_PROGRESS=1
WORKDIR /app

# Layer 1: third-party dependencies only. Only manifests are copied, so this layer is
# reused until uv.lock changes. uv needs every workspace member's pyproject.toml to load
# the workspace; the sources arrive in the next layer.
COPY pyproject.toml uv.lock ./
COPY packages/commerce-domain/pyproject.toml    packages/commerce-domain/
COPY packages/platform-db/pyproject.toml        packages/platform-db/
COPY packages/transaction-kernel/pyproject.toml packages/transaction-kernel/
COPY packages/durable-work/pyproject.toml       packages/durable-work/
COPY packages/merchant-sim/pyproject.toml       packages/merchant-sim/
COPY packages/payment-adapters/pyproject.toml   packages/payment-adapters/
COPY packages/commerce-api/pyproject.toml       packages/commerce-api/
COPY packages/durable-worker/pyproject.toml     packages/durable-worker/
# agent-runtime is a workspace member and a commerce-api dependency. uv loads the whole
# workspace from the root pyproject, so a missing manifest fails the resolve here rather
# than at import time -- which is the good direction, but only if the file is present.
COPY packages/agent-runtime/pyproject.toml      packages/agent-runtime/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package "${PACKAGE}"

# Layer 2: the workspace members, installed non-editable so the runtime image carries the
# virtualenv only and not the source tree.
COPY packages/ packages/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${PACKAGE}"

# ---------------------------------------------------------------------------------------
# runtime: python:slim + the virtualenv. No compiler, no uv, no package caches.
# A shell is still present: there is no distroless Python 3.14 image, so the compensating
# controls are the Kubernetes securityContext (no privilege escalation, dropped
# capabilities, read-only root) and no `kubectl exec` in the demo runbook.
# ---------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ARG APP_MODULE
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="commerce-api" \
      org.opencontainers.image.description="Governed agentic commerce: FastAPI trusted surface and protocol-neutral API over the Transaction Assurance Kernel" \
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
 # Nothing here needs setuid/setgid; strip the bits so no-new-privileges has nothing to guard.
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 infra/docker/entrypoint.py /app/entrypoint.py
# Alembic wants alembic.ini and the versions directory side by side; the platform_db
# package itself is imported from the virtualenv. Used only by the migration Job
# (workingDir /app/db, `alembic upgrade head`).
COPY --chown=10001:10001 packages/platform-db/alembic.ini /app/db/alembic.ini
COPY --chown=10001:10001 packages/platform-db/migrations  /app/db/migrations

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    HOME=/tmp \
    TMPDIR=/tmp \
    APP_SECRETS_DIR=/var/run/secrets/app \
    APP_MODULE="${APP_MODULE}" \
    PORT=8000 \
    WEB_CONCURRENCY=1

WORKDIR /app
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM

# Kubernetes ignores HEALTHCHECK (it uses the probes in the manifests); this is for
# docker/compose users. Exec form, no curl needed.
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/healthz', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["python", "/app/entrypoint.py"]
# ${VAR} is expanded by entrypoint.py (no shell). WEB_CONCURRENCY=1 keeps uvicorn at one
# worker: ADR 0003 D14, the merchant simulator's state lives in the API process.
CMD ["uvicorn", "${APP_MODULE}", "--host", "0.0.0.0", "--port", "${PORT}", \
     "--proxy-headers", "--forwarded-allow-ips=*", "--no-server-header", \
     "--timeout-graceful-shutdown", "20"]
