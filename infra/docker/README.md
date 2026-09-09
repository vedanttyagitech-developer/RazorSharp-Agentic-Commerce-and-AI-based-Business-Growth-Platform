# Container images

Four images, one build context. **Every build runs from the repository root** with
`-f infra/docker/<file>`: the Python images need `pyproject.toml`, `uv.lock` and
`packages/`, each web image needs its own directory under `apps/`, and all four now need
`infra/docker/` for their entrypoint shim. A `.dockerignore` next to the Dockerfiles would
be ignored (Docker reads the one at the context root), so apply the root `.dockerignore`
listed at the end of this file.

| Image | Dockerfile | Base (build → run) | Command | Port |
| --- | --- | --- | --- | --- |
| `commerce-api` | `commerce-api.Dockerfile` | `python:3.14-slim-bookworm` + `ghcr.io/astral-sh/uv:0.12.9` → `python:3.14-slim-bookworm` | `uvicorn ${APP_MODULE} --factory --host 0.0.0.0 --port ${PORT}` (`APP_MODULE=commerce_api.app:create_app`, a factory) | 8000 |
| `action-executor` | `action-executor.Dockerfile` | same | `python -m ${WORKER_MODULE}` (`WORKER_MODULE=action_executor.main`) | 8001 (health only) |

The Python images are built with `uv sync --frozen --no-dev --no-editable --package <name>`
against the root `uv.lock`, in two layers (third-party dependencies, then the workspace
members) so a source change does not re-download dependencies. The runtime stage carries
only `/app/.venv`, `infra/docker/entrypoint.py` and, for `commerce-api`, the Alembic
configuration and migration versions under `/app/db` (used by the migration Job).

The command is parameterised through `APP_MODULE` / `WORKER_MODULE` (build arg and
environment variable) so a rename needs no image change. Both modules exist and both images
have been started; `scripts/validate_infra.sh` imports each inside its built image.

Every workspace member's `pyproject.toml` must be copied in the dependency layer, including
ones the target package only depends on transitively: `uv` loads the whole workspace from
the root manifest, so a missing file fails the resolve. `agent-runtime` is there because
`commerce-api` depends on it.

## The two Node images

Gone with the front end on 2026-09-09. They were `node:24-bookworm-slim` building to
`gcr.io/distroless/nodejs24-debian12:nonroot`, running `node entrypoint.mjs` →
`server.js`, on 3000 and 3001, and they required `output: "standalone"` in each
`next.config.ts` — the build stage failed with an explicit message when
`.next/standalone/server.js` was absent, rather than producing an image with nothing to
run. A Node image that returns needs that check back.

## Build locally

GKE Autopilot nodes are `linux/amd64`. On Apple Silicon add `--platform linux/amd64`
(slower, uses emulation) or build in Cloud Build (below), which is the recommended path.

```sh
cd "$(git rev-parse --show-toplevel)"        # repository root: the build context
export VCS_REF="$(git rev-parse --short HEAD)"
export BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

docker build --platform linux/amd64 -f infra/docker/commerce-api.Dockerfile \
  --build-arg VCS_REF="$VCS_REF" --build-arg BUILD_DATE="$BUILD_DATE" \
  -t commerce-api:dev .

docker build --platform linux/amd64 -f infra/docker/action-executor.Dockerfile \
  --build-arg VCS_REF="$VCS_REF" --build-arg BUILD_DATE="$BUILD_DATE" \
  -t action-executor:dev .
```

Nothing secret may ever be a build argument:
`docker history` prints them back out of a finished image.

Run one locally (secrets come from the environment when no secret files are mounted):

```sh
docker run --rm -p 8000:8000 --read-only --tmpfs /tmp \
  -e DATABASE_URL_APP -e DATABASE_URL_KERNEL \
  -e RAZORPAY_KEY_ID -e RAZORPAY_KEY_SECRET -e RAZORPAY_WEBHOOK_SECRET \
  -e RAZORPAY_PROFILE=DEVELOPMENT commerce-api:dev
```

## Build and push with Cloud Build

```sh
cd "$(git rev-parse --show-toplevel)"
gcloud builds submit --config infra/docker/cloudbuild.yaml \
  --substitutions=_TAG=demo,_REGISTRY=asia-south1-docker.pkg.dev/$PROJECT_ID/commerce .
```

Or push local images:

```sh
gcloud auth configure-docker asia-south1-docker.pkg.dev
REG=asia-south1-docker.pkg.dev/$PROJECT_ID/commerce
for img in commerce-api action-executor; do
  docker tag $img:dev $REG/$img:demo && docker push $REG/$img:demo
done
```

## How secrets reach the process

The GKE Secret Manager add-on mounts secrets as files and does not sync them into
Kubernetes Secrets or environment variables. The SecretProviderClass in `infra/kubernetes`
therefore names each mounted file after the variable it carries, e.g. Secret Manager
`db-url-app` → file `DATABASE_URL_APP`, and each image loads them at start.

- **Python** (`entrypoint.py`): exports every well-named file in `APP_SECRETS_DIR`
  (`/var/run/secrets/app`), expands `${VAR}` in the command line, then `execvp`s the
  service so the service is PID 1 and receives SIGTERM directly.
- **Next.js** (`node-entrypoint.mjs`, removed with the images): the same file contract, then `await import('./server.js')`
  in the same process — node is already PID 1 under the distroless entrypoint, so Next's own
  graceful shutdown is untouched. A Next standalone server reads `process.env` and nothing
  else, so without this the only way to give these two their credentials would be an
  environment literal in a manifest.

Both share the same rules: a variable already set in the environment wins (so `docker run -e`
and a laptop run behave normally), only the trailing newline is stripped, an empty file is
not exported, a name that is not environment-variable-shaped is skipped, and the names are
logged while the values never are.

## Security properties (spec 21.8)

- Base images pinned by tag; a comment marks where the `@sha256:` digest pin goes once the
  images are mirrored into Artifact Registry.
- `uv.lock` / `package-lock.json` only; nothing floats (`--frozen`, `npm ci`).
- Explicit non-root user: UID 10001 (Python images), UID 65532 (distroless Node).
- Read-only root filesystem friendly: only `/tmp` (and for Next.js `/app/.next/cache`)
  is written. `HOME=/tmp` for the Python images.
- No shell in either web runtime (distroless). The Python runtime keeps Debian's shell
  because no distroless Python 3.14 exists; setuid/setgid bits are stripped and the
  Kubernetes securityContext drops every capability.
- `HEALTHCHECK` in exec form without curl; OCI labels carry revision and build date.
- Scan before deploy: `trivy image --severity HIGH,CRITICAL <image>`; Artifact Registry
  vulnerability scanning is enabled by Terraform (`containerscanning.googleapis.com`).

## Recommended root `.dockerignore`

Create `/.dockerignore` (repository root) with exactly this content. It keeps the context
small (the virtualenv, `node_modules` and `.next` alone are ~1 GB) and keeps every secret
pattern from `.gitignore` out of any image layer.

```gitignore
# Build context is the repository root (infra/docker/README.md). Keep it minimal.

# --- secrets: never in a layer ---
.env
.env.*
**/*.csv
**/*secret*
**/*credential*
**/*_api_keys*
**/service-account*.json
**/*.pem
**/*.key

# --- version control, CI, docs ---
.git
.github
docs
*.md
PROJECT_SPECIFICATION.md
fixtures
scripts
infra/terraform
infra/kubernetes
infra/sql

# --- python ---
.venv
**/__pycache__
**/*.py[cod]
.mypy_cache
.pytest_cache
.ruff_cache
.hypothesis
htmlcov
.coverage
packages/*/tests

# --- node ---
**/node_modules
**/.next
**/out
**/coverage
**/*.tsbuildinfo
**/.env.local

# --- os / editor / logs ---
.DS_Store
.idea
.vscode
**/*.log
```
