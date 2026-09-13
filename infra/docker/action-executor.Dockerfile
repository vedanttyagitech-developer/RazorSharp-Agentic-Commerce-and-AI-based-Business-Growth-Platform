# syntax=docker/dockerfile:1.7
# action-executor: the only process that talks to Razorpay (ADR 0003 D3). Build from the
# REPOSITORY ROOT:
#
#   docker build -f infra/docker/action-executor.Dockerfile -t action-executor:dev .
#
# See infra/docker/README.md. Same layout and controls as commerce-api.Dockerfile; the
# differences are the workspace package, the command and the health port.
#
# Digest pins: replace each tag with `tag@sha256:<digest>` once mirrored (see README).
ARG PYTHON_IMAGE=python:3.14-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.9
ARG PACKAGE=action-executor
# Entry module (`python -m ...`). Parameterised: the module is being written, and the Pod
# can override it through the WORKER_MODULE environment variable without a rebuild.
ARG WORKER_MODULE=action_executor.main

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder
ARG PACKAGE
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_PROGRESS=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/commerce-domain/pyproject.toml    packages/commerce-domain/
COPY packages/platform-db/pyproject.toml        packages/platform-db/
COPY packages/transaction-kernel/pyproject.toml packages/transaction-kernel/
COPY packages/durable-work/pyproject.toml       packages/durable-work/
COPY packages/merchant-sim/pyproject.toml       packages/merchant-sim/
COPY packages/payment-adapters/pyproject.toml   packages/payment-adapters/
COPY packages/commerce-api/pyproject.toml       packages/commerce-api/
COPY packages/action-executor/pyproject.toml     packages/action-executor/
# agent-runtime is a workspace member and a commerce-api dependency. uv loads the whole
# workspace from the root pyproject, so a missing manifest fails the resolve here rather
# than at import time -- which is the good direction, but only if the file is present.
COPY packages/agent-runtime/pyproject.toml      packages/agent-runtime/
COPY packages/reserve-trust/pyproject.toml packages/reserve-trust/
COPY packages/merchant-controller/pyproject.toml packages/merchant-controller/
COPY packages/merchant-adapter/pyproject.toml packages/merchant-adapter/
COPY packages/reserve-signer/pyproject.toml packages/reserve-signer/
COPY packages/commerce-protocols/pyproject.toml packages/commerce-protocols/
COPY packages/voice-runtime/pyproject.toml packages/voice-runtime/
COPY packages/platform-observability/pyproject.toml packages/platform-observability/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package "${PACKAGE}"
COPY packages/ packages/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${PACKAGE}"

FROM ${PYTHON_IMAGE} AS runtime
ARG WORKER_MODULE
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="action-executor" \
      org.opencontainers.image.description="Governed agentic commerce: executes admitted outbox commands under single-use Execution Grants; the only Razorpay caller" \
      org.opencontainers.image.vendor="Governed Agentic Commerce (Razorpay AI Buildathon Track 1)" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.base.name="docker.io/library/python:3.14-slim-bookworm" \
      org.opencontainers.image.source="https://github.com/OWNER/REPO"

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
    WORKER_MODULE="${WORKER_MODULE}" \
    WORKER_HEALTH_PORT=8001

WORKDIR /app
USER 10001:10001
EXPOSE 8001
STOPSIGNAL SIGTERM

# Contract for action_executor.main (docs/DEPLOY.md, "Contracts the services must honour"):
# serve GET /healthz on WORKER_HEALTH_PORT and answer 200 while the outbox loop is alive.
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os, sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('WORKER_HEALTH_PORT', '8001') + '/healthz', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["python", "/app/entrypoint.py"]
CMD ["python", "-m", "${WORKER_MODULE}"]
