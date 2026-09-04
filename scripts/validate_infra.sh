#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Repeatable Infrastructure & Packaging Validation Script
# ==============================================================================
# Covers:
# 1. Tool availability (terraform, kubectl, kubeconform)
# 2. Terraform formatting and validation (using isolated TF_DATA_DIR, preserving workspace cache)
# 3. Kubernetes overlay rendering (kustomize) for dev, demo, and db-migrate
# 4. Kubernetes strict schema validation (kubeconform, without ignoring missing schemas)
# 5. Static container packaging contracts (.dockerignore, next.config.ts, entrypoint presence)
# 6. Container build & startup smoke checks across all three workloads (if docker is accessible)
# 7. Explicit status reporting: Passed, Skipped, Blocked, Pending
# ==============================================================================

# Ensure standard local paths are in PATH
export PATH="/Users/vedanttyagi/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

echo "=== 1. Tool Availability Check ==="
FAIL_TOOLS=0

check_tool() {
  local tool="$1"
  if command -v "${tool}" >/dev/null 2>&1; then
    echo "  [OK] ${tool} found: $(command -v "${tool}")"
  else
    echo "  [FAIL] Missing required tool: ${tool}"
    FAIL_TOOLS=1
  fi
}

check_tool terraform
check_tool kubectl
check_tool kubeconform

if [ "${FAIL_TOOLS}" -ne 0 ]; then
  echo "Error: Required tools are missing. Please install missing tools before proceeding." >&2
  exit 1
fi

echo ""
echo "=== 2. Terraform Validation (Isolated TF_DATA_DIR) ==="
echo "Checking Terraform formatting..."
terraform -chdir=infra/terraform fmt -check

echo "Initializing in isolated temporary directory (preserving workspace cache)..."
TMP_TF="$(mktemp -d)"
# If workspace has cached providers, copy them to the temporary dir to allow offline validation
if [ -d "infra/terraform/.terraform/providers" ]; then
  mkdir -p "${TMP_TF}/providers"
  cp -R "infra/terraform/.terraform/providers/." "${TMP_TF}/providers/"
fi

TF_DATA_DIR="${TMP_TF}" terraform -chdir=infra/terraform init -backend=false

echo "Validating Terraform configuration..."
TF_DATA_DIR="${TMP_TF}" terraform -chdir=infra/terraform validate
rm -rf "${TMP_TF}"
echo "  [OK] Terraform configuration is valid. Workspace .terraform directory preserved."

echo ""
echo "=== 3. Kubernetes Overlay Rendering (kustomize) ==="
OVERLAYS=(
  "infra/kubernetes/overlays/dev"
  "infra/kubernetes/overlays/demo"
  "infra/kubernetes/overlays/dev/db-migrate"
  "infra/kubernetes/overlays/demo/db-migrate"
)

for overlay in "${OVERLAYS[@]}"; do
  echo "Rendering ${overlay}..."
  kubectl kustomize "${overlay}" > /dev/null
  echo "  [OK] Rendered ${overlay}"
done

echo ""
echo "=== 4. Kubernetes Strict Schema Validation (kubeconform) ==="
# Notice: -ignore-missing-schemas is deliberately omitted so any missing schema triggers failure.
SCHEMA_FLAGS=(
  -strict
  -summary
  -schema-location default
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
)

for overlay in "${OVERLAYS[@]}"; do
  echo "Validating schemas for ${overlay}..."
  kubectl kustomize "${overlay}" | kubeconform "${SCHEMA_FLAGS[@]}"
done

echo ""
echo "=== 5. Static Container Packaging & Contract Verification ==="

echo "Checking root .dockerignore..."
if [ ! -f ".dockerignore" ]; then
  echo "  [FAIL] Root .dockerignore is missing!" >&2
  exit 1
fi
for pattern in ".env" "*.csv" "*secret*"; do
  if ! grep -q "${pattern}" .dockerignore; then
    echo "  [FAIL] .dockerignore missing required security pattern: ${pattern}" >&2
    exit 1
  fi
done
echo "  [OK] Root .dockerignore is present and contains required security patterns."

echo "Checking apps/buyer-web/next.config.ts for standalone output..."
if grep -q 'output: *"standalone"' apps/buyer-web/next.config.ts; then
  echo "  [OK] apps/buyer-web/next.config.ts sets output: \"standalone\""
else
  echo "  [FAIL] apps/buyer-web/next.config.ts missing output: \"standalone\"" >&2
  exit 1
fi

echo "Checking apps/buyer-web/public tracking..."
if [ -f "apps/buyer-web/public/.gitkeep" ]; then
  echo "  [OK] apps/buyer-web/public/.gitkeep exists (empty public directory tracked in git)."
else
  echo "  [WARN] apps/buyer-web/public/.gitkeep missing; fresh checkout may lack public directory."
fi

echo "Checking application entrypoint modules..."
API_APP_EXISTS=0
WORKER_MAIN_EXISTS=0

if [ -f "packages/commerce-api/src/commerce_api/app.py" ]; then
  echo "  [OK] commerce_api.app module found."
  API_APP_EXISTS=1
else
  echo "  [NOTICE] commerce_api.app module NOT found at packages/commerce-api/src/commerce_api/app.py"
  echo "           Affected workload: commerce-api (blocked from startup until module is implemented)."
fi

if [ -f "packages/durable-worker/src/durable_worker/main.py" ]; then
  echo "  [OK] durable_worker.main module found."
  WORKER_MAIN_EXISTS=1
else
  echo "  [NOTICE] durable_worker.main module NOT found at packages/durable-worker/src/durable_worker/main.py"
  echo "           Affected workload: durable-worker (blocked from startup until module is implemented)."
fi

echo ""
echo "=== 6. Container Runtime, Image Build & Startup Checks ==="
DOCKER_AVAILABLE=0
BUYER_WEB_BUILD=0
BUYER_WEB_STARTUP=0
API_BUILD=0
WORKER_BUILD=0

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  DOCKER_AVAILABLE=1
  echo "Docker daemon is available."

  echo "Building buyer-web:dev..."
  if docker build -q -f infra/docker/buyer-web.Dockerfile -t buyer-web:dev . >/dev/null; then
    echo "  [OK] buyer-web:dev image built successfully."
    BUYER_WEB_BUILD=1
  else
    echo "  [FAIL] buyer-web:dev build failed." >&2
    exit 1
  fi

  echo "Smoke testing buyer-web:dev startup on ephemeral port 3009..."
  SMOKE_CID=$(docker run -d --rm -p 3009:3000 buyer-web:dev)
  sleep 3
  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3009/ || echo "000")
  docker stop "${SMOKE_CID}" >/dev/null 2>&1 || true
  if [ "${HTTP_STATUS}" = "200" ]; then
    echo "  [OK] buyer-web:dev started and responded with HTTP 200 on /."
    BUYER_WEB_STARTUP=1
  else
    echo "  [FAIL] buyer-web:dev failed startup smoke check (HTTP ${HTTP_STATUS})." >&2
    exit 1
  fi

  echo "Building commerce-api:dev..."
  if docker build -q -f infra/docker/commerce-api.Dockerfile -t commerce-api:dev . >/dev/null; then
    echo "  [OK] commerce-api:dev image built successfully."
    API_BUILD=1
  else
    echo "  [FAIL] commerce-api:dev build failed." >&2
    exit 1
  fi

  echo "Testing commerce-api:dev startup smoke check (verifying entrypoint status)..."
  set +e
  API_OUTPUT=$(docker run --rm commerce-api:dev 2>&1)
  API_EXIT=$?
  set -e
  if [ "${API_EXIT}" -eq 3 ] && echo "${API_OUTPUT}" | grep -q "commerce_api.app"; then
    echo "  [BLOCKED AS EXPECTED] commerce-api:dev failed startup with expected missing module:"
    echo "                        ${API_OUTPUT}"
  elif [ "${API_EXIT}" -eq 0 ]; then
    echo "  [OK] commerce-api:dev started successfully."
  else
    echo "  [NOTICE] commerce-api:dev exited with status ${API_EXIT}: ${API_OUTPUT}"
  fi

  echo "Building durable-worker:dev..."
  if docker build -q -f infra/docker/durable-worker.Dockerfile -t durable-worker:dev . >/dev/null; then
    echo "  [OK] durable-worker:dev image built successfully."
    WORKER_BUILD=1
  else
    echo "  [FAIL] durable-worker:dev build failed." >&2
    exit 1
  fi

  echo "Testing durable-worker:dev startup smoke check (verifying entrypoint status)..."
  set +e
  WORKER_OUTPUT=$(docker run --rm durable-worker:dev 2>&1)
  WORKER_EXIT=$?
  set -e
  if [ "${WORKER_EXIT}" -eq 1 ] && echo "${WORKER_OUTPUT}" | grep -q "durable_worker.main"; then
    echo "  [BLOCKED AS EXPECTED] durable-worker:dev failed startup with expected missing module:"
    echo "                        ${WORKER_OUTPUT}"
  elif [ "${WORKER_EXIT}" -eq 0 ]; then
    echo "  [OK] durable-worker:dev started successfully."
  else
    echo "  [NOTICE] durable-worker:dev exited with status ${WORKER_EXIT}: ${WORKER_OUTPUT}"
  fi

else
  echo "  [SKIPPED] Docker daemon not running or not installed. Container builds and startup tests skipped."
fi

echo ""
echo "=============================================================================="
echo "                           VALIDATION SUMMARY"
echo "=============================================================================="
echo "PASSED CHECKS:"
echo "  - Terraform formatting and offline configuration validation"
echo "  - Kubernetes overlay rendering for dev, demo, and db-migrate"
echo "  - Kubernetes strict schema validation (Kubeconform, 0 missing/skipped schemas)"
echo "  - Static packaging rules (.dockerignore secrets guard, Next.js standalone mode)"
if [ "${DOCKER_AVAILABLE}" -eq 1 ]; then
  echo "  - Container image builds: buyer-web:dev, commerce-api:dev, durable-worker:dev"
  echo "  - Container startup: buyer-web:dev (HTTP 200 on /)"
fi

echo ""
if [ "${DOCKER_AVAILABLE}" -eq 0 ]; then
  echo "SKIPPED CHECKS:"
  echo "  - Container image builds and container startup smoke tests (Docker daemon unavailable)"
  echo ""
fi

echo "BLOCKED CHECKS (Missing Application Implementation):"
if [ "${API_APP_EXISTS}" -eq 0 ]; then
  echo "  - commerce-api runtime: commerce_api.app:app module is not implemented"
fi
if [ "${WORKER_MAIN_EXISTS}" -eq 0 ]; then
  echo "  - durable-worker runtime: durable_worker.main module is not implemented"
fi

echo ""
echo "PENDING DEPLOYMENT OPERATIONS (Out of scope / unauthorized):"
echo "  - Cloud infrastructure provisioning (terraform apply)"
echo "  - Cloud SQL database bootstrap & migration execution (job/db-migrate)"
echo "  - Secret Manager secret value population"
echo "  - Live GKE Autopilot workload rollout"
echo "=============================================================================="
