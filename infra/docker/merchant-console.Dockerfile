# syntax=docker/dockerfile:1.7
# merchant-console: the Next.js 16 operator console, built as a standalone server and run
# on a distroless Node 24 image (no shell, no package manager). Build from the REPOSITORY
# ROOT:
#
#   docker build -f infra/docker/merchant-console.Dockerfile -t merchant-console:dev .
#
# PREREQUISITE (owned by apps/merchant-console): next.config.ts must contain
#     output: "standalone",
# The build stage checks for .next/standalone and fails with a clear message otherwise.
#
# This image is the higher-privilege of the two web surfaces. Its server-side proxy
# (src/app/api/backend/[...path]/route.ts) holds an operator bearer token *and* the
# scenario key, and neither may ever be baked into a layer or shipped to a browser:
#
#   * There is no NEXT_PUBLIC_* build argument here beyond the tenant slug, and there
#     never should be. Anything prefixed NEXT_PUBLIC_ is inlined into the client bundle at
#     `next build` time, so a scenario key passed that way would be readable in a network
#     panel by anyone who opened the console. SCENARIO_KEY and OPERATOR_COOKIE_SECRET are
#     read at *runtime*, from the Pod's environment, which is mounted from Secret Manager.
#   * No ARG or ENV in this file may carry a secret value. A build argument is recorded in
#     the image history and `docker history` prints it back.
#
# Digest pins: replace each tag with `tag@sha256:<digest>` once mirrored (see README).
ARG NODE_IMAGE=node:24-bookworm-slim
ARG RUNTIME_IMAGE=gcr.io/distroless/nodejs24-debian12:nonroot

# ---------------------------------------------------------------------------------------
# deps: exact lockfile install, dev dependencies included (typescript, tailwind, eslint
# are build-time tools). NODE_ENV is deliberately unset here so `npm ci` keeps them.
# ---------------------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY apps/merchant-console/package.json apps/merchant-console/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

# ---------------------------------------------------------------------------------------
# build: `next build` with standalone output.
#
# NEXT_PUBLIC_TENANT_SLUG is the one value that is legitimately baked in: it names which
# demo tenant this console is for, it is not a credential, and the pages need it during
# prerender. COMMERCE_API_URL is deliberately *not* set here -- it is read per request by
# the proxy route, so the same image serves a cluster and a laptop.
# ---------------------------------------------------------------------------------------
FROM deps AS build
ARG NEXT_PUBLIC_TENANT_SLUG=demo
ENV NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PUBLIC_TENANT_SLUG=${NEXT_PUBLIC_TENANT_SLUG}
COPY apps/merchant-console/ ./
RUN npm run build \
 && if [ ! -f .next/standalone/server.js ]; then \
      echo >&2 'merchant-console: .next/standalone/server.js is missing. Add `output: "standalone"` to apps/merchant-console/next.config.ts.'; \
      exit 1; \
    fi \
 # Next does not copy static assets into the standalone tree; place them where server.js expects them.
 && mkdir -p .next/standalone/.next .next/standalone/public \
 && cp -r .next/static .next/standalone/.next/static \
 && if [ -d public ] && [ -n "$(ls -A public 2>/dev/null)" ]; then cp -r public/. .next/standalone/public/; fi \
 && chown -R 65532:65532 .next/standalone

# ---------------------------------------------------------------------------------------
# runtime: distroless Node 24, user 65532 (nonroot), entrypoint /nodejs/bin/node.
# ---------------------------------------------------------------------------------------
FROM ${RUNTIME_IMAGE} AS runtime
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="merchant-console" \
      org.opencontainers.image.description="Governed agentic commerce: Next.js operator console (reads the tenant's rows through a server-side proxy; never an authorization layer)" \
      org.opencontainers.image.vendor="Governed Agentic Commerce (Razorpay AI Buildathon Track 1)" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.base.name="gcr.io/distroless/nodejs24-debian12:nonroot" \
      org.opencontainers.image.source="https://github.com/OWNER/REPO"

WORKDIR /app
COPY --from=build --chown=65532:65532 /app/.next/standalone ./
# Secret Manager mounts arrive as files; this loads them into process.env before the
# server starts. See the module comment for why a Next standalone server needs it.
COPY --chown=65532:65532 infra/docker/node-entrypoint.mjs ./entrypoint.mjs

# PORT 3001 matches the app's own `next start -p 3001` and the Service in
# infra/kubernetes/base/workloads/merchant-console.yaml. COMMERCE_API_URL, SCENARIO_KEY and
# OPERATOR_COOKIE_SECRET are supplied by the Deployment; the last two come from Secret
# Manager and appear in no layer of this image.
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    APP_SECRETS_DIR=/var/run/secrets/app \
    PORT=3001

USER 65532:65532
EXPOSE 3001
STOPSIGNAL SIGTERM

# Kubernetes uses the manifest probes; this is for docker/compose users. No shell, so the
# check runs through node itself.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD ["/nodejs/bin/node", "-e", "fetch('http://127.0.0.1:' + (process.env.PORT || '3001') + '/').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"]

# The distroless entrypoint is /nodejs/bin/node. The shim loads the secret files and then
# imports the standalone server.js in this same process, so node stays PID 1 and
# Kubernetes' SIGTERM reaches Next's own graceful shutdown directly.
CMD ["entrypoint.mjs"]
