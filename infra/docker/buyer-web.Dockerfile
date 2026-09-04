# syntax=docker/dockerfile:1.7
# buyer-web: the Next.js 16 buyer storefront, built as a standalone server and run on a
# distroless Node 24 image (no shell, no package manager). Build from the REPOSITORY ROOT:
#
#   docker build -f infra/docker/buyer-web.Dockerfile -t buyer-web:dev .
#
# PREREQUISITE (owned by apps/buyer-web): next.config.ts must contain
#     output: "standalone",
# The build stage checks for .next/standalone and fails with a clear message otherwise.
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
COPY apps/buyer-web/package.json apps/buyer-web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

# ---------------------------------------------------------------------------------------
# build: `next build` with standalone output. NEXT_PUBLIC_* values are baked into the
# client bundle at build time; the server-only API_BASE is read at runtime (see
# apps/buyer-web/src/lib/server/env.ts) and is set by the Deployment.
# ---------------------------------------------------------------------------------------
FROM deps AS build
ARG NEXT_PUBLIC_API_MODE=live
ARG NEXT_PUBLIC_API_BASE=http://commerce-api:8000
ENV NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PUBLIC_API_MODE=${NEXT_PUBLIC_API_MODE} \
    NEXT_PUBLIC_API_BASE=${NEXT_PUBLIC_API_BASE}
COPY apps/buyer-web/ ./
RUN npm run build \
 && if [ ! -f .next/standalone/server.js ]; then \
      echo >&2 'buyer-web: .next/standalone/server.js is missing. Add `output: "standalone"` to apps/buyer-web/next.config.ts.'; \
      exit 1; \
    fi \
 # Next does not copy static assets into the standalone tree; place them where server.js expects them.
 && mkdir -p .next/standalone/.next .next/standalone/public \
 && cp -r .next/static .next/standalone/.next/static \
 && cp -r public/. .next/standalone/public/ \
 && chown -R 65532:65532 .next/standalone

# ---------------------------------------------------------------------------------------
# runtime: distroless Node 24, user 65532 (nonroot), entrypoint /nodejs/bin/node.
# ---------------------------------------------------------------------------------------
FROM ${RUNTIME_IMAGE} AS runtime
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="buyer-web" \
      org.opencontainers.image.description="Governed agentic commerce: Next.js buyer storefront (trusted surface, never an authorization layer)" \
      org.opencontainers.image.vendor="Governed Agentic Commerce (Razorpay AI Buildathon Track 1)" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.base.name="gcr.io/distroless/nodejs24-debian12:nonroot" \
      org.opencontainers.image.source="https://github.com/OWNER/REPO"

WORKDIR /app
COPY --from=build --chown=65532:65532 /app/.next/standalone ./

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000 \
    NEXT_PUBLIC_API_MODE=live

USER 65532:65532
EXPOSE 3000
STOPSIGNAL SIGTERM

# Kubernetes uses the manifest probes; this is for docker/compose users. No shell, so the
# check runs through node itself. Add a `/healthz` route handler to make it cheaper.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD ["/nodejs/bin/node", "-e", "fetch('http://127.0.0.1:' + (process.env.PORT || '3000') + '/').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"]

# The distroless entrypoint is /nodejs/bin/node; the standalone server is server.js.
CMD ["server.js"]
