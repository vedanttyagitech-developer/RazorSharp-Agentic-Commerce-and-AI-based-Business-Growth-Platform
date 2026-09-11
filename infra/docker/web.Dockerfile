# syntax=docker/dockerfile:1.7
# web: the buyer storefront and the merchant workspace, one Node process. Build from the
# REPOSITORY ROOT:
#
#   docker build -f infra/docker/web.Dockerfile -t web:dev .
#
# WHY THIS IS A NODE IMAGE AND NOT A WORKER
# -----------------------------------------
# `vinext build` emits two things: a Cloudflare Worker bundle (dist/server/wrangler.json)
# and a plain Node production server reachable through `vinext start`. The package's own
# `start` script names the first, which is a scaffold's choice rather than a constraint --
# the second is a normal `node` process listening on a port, and it is what runs here.
# Everything is on one machine as a result, so the app's `/api/*` bridge reaches the API
# over the compose network instead of the internet.
#
# WHAT THE BRIDGE NEEDS, AND WHY IT IS NOT BAKED IN
# -------------------------------------------------
# `app/api/commerce`, `app/api/merchant` and `app/api/voice` are server routes that forward
# to the API with a session the browser never holds. They read COMMERCE_API_URL,
# SCENARIO_KEY and RESERVE_LOCAL_DEMO at request time, from the environment, so none of
# them is a build argument: `docker history` prints build arguments back out of a finished
# image, and one of those three is a key that mints an operator.
ARG NODE_IMAGE=node:26-bookworm-slim

# ---------------------------------------------------------------------------------------
# builder: install the locked dependency set, then build.
# ---------------------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS builder
WORKDIR /app

# Layer 1: dependencies, reused until the lockfile changes. `npm ci` rather than install,
# so the image cannot resolve a version the repository never tested.
COPY apps/razorsharp-concept/package.json apps/razorsharp-concept/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund

# Layer 2: the source, and the build.
COPY apps/razorsharp-concept/ ./
RUN npm run build

# Prune to what a running server needs. The build is done; nothing below compiles.
RUN --mount=type=cache,target=/root/.npm npm prune --omit=dev

# ---------------------------------------------------------------------------------------
# runtime: the same base, the built output, and no build tooling.
# ---------------------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS runtime
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="razorsharp-web" \
      org.opencontainers.image.description="Governed agentic commerce: the buyer storefront and the merchant workspace. Holds no capability; every privileged call is a server route with a session the browser never sees." \
      org.opencontainers.image.vendor="Governed Agentic Commerce (Razorpay AI Buildathon Track 1)" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# The same fixed UID the Python images use, for the same reason (spec 21.8).
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --home-dir /tmp --no-create-home \
      --shell /usr/sbin/nologin app \
 && mkdir -p /app && chown 10001:10001 /app \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/*

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/node_modules ./node_modules
COPY --from=builder --chown=10001:10001 /app/dist ./dist
COPY --from=builder --chown=10001:10001 /app/package.json ./package.json
# `vinext start` reads the app's own config and public assets at boot, not only `dist/`.
COPY --from=builder --chown=10001:10001 /app/public ./public
COPY --from=builder --chown=10001:10001 /app/next.config.ts ./next.config.ts

ENV NODE_ENV=production \
    HOME=/tmp \
    TMPDIR=/tmp \
    PORT=3000 \
    HOSTNAME=0.0.0.0
USER 10001:10001
EXPOSE 3000
STOPSIGNAL SIGTERM

# The page itself, not a synthetic endpoint: it exercises the render path a visitor uses,
# so an image that boots and cannot render is unhealthy rather than merely listening.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]

CMD ["npx", "vinext", "start", "--port", "3000", "--host", "0.0.0.0"]
