#!/usr/bin/env bash
# ==============================================================================
# validate_infra.sh -- check that this repository's deployment artefacts describe
# the application that actually exists, and that none of them carries a secret.
#
# It answers one acceptance-criterion question honestly: "would this deploy, and
# would it deploy *this*?" It does not deploy. Every check either passes, fails
# with a reason, or is reported SKIPPED with the tool that was missing -- there
# is no third state where something is quietly not checked.
#
#   ./scripts/validate_infra.sh          # everything available
#   NO_DOCKER=1 ./scripts/validate_infra.sh   # skip the image builds (slow)
#
# What it checks, and why each check is here rather than trusted:
#
#  1. Tools. Reported, not assumed. A missing kubeconform used to mean the schema
#     step silently did nothing.
#  2. Terraform fmt + offline validate.
#  3. Kustomize renders for every overlay.
#  4. kubeconform --strict with NO -ignore-missing-schemas, so an unknown CRD is a
#     failure rather than a skip. The summary is asserted to report 0 skipped.
#  5. **The manifests match the applications.** Every image the workloads name has
#     a Dockerfile; every workload has a ServiceAccount, a NetworkPolicy and (for
#     the API) WEB_CONCURRENCY=1; every SecretProviderClass a pod mounts exists.
#     This is the check that would have caught the stale API_BASE/NEXT_PUBLIC_API_MODE
#     environment on buyer-web after the frontends were rebuilt.
#  6. **No secret is in any file that ships.** Manifests are scanned for anything
#     that looks like a credential written as an environment literal, for
#     `kind: Secret` with data, and for NEXT_PUBLIC_ names that would be inlined
#     into a browser bundle. The Dockerfiles are scanned for secret-shaped ARG/ENV
#     defaults, which `docker history` would print back.
#  7. Container builds and a startup smoke check for all four images, when Docker
#     is available -- including that the Node secret shim loads a mounted file and
#     that the value does not reach the served HTML.
# ==============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# Homebrew and per-user installs, without hard-coding one developer's home directory.
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"

PASSED=(); FAILED=(); SKIPPED=()

pass() { PASSED+=("$1"); printf '  [OK]      %s\n' "$1"; }
fail() { FAILED+=("$1"); printf '  [FAIL]    %s\n' "$1" >&2; }
skip() { SKIPPED+=("$1"); printf '  [SKIPPED] %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

# The overlays that must render. db-migrate is separate because it is applied first.
OVERLAYS=(
  infra/kubernetes/overlays/dev
  infra/kubernetes/overlays/demo
  infra/kubernetes/overlays/dev/db-migrate
  infra/kubernetes/overlays/demo/db-migrate
)

# image tag -> Dockerfile. The manifests name bare images; the overlay rewrites them to
# Artifact Registry. Both halves are checked below.
IMAGES=(commerce-api durable-worker buyer-web merchant-console)

echo "=== 1. Tools ==="
for tool in terraform kubectl kubeconform docker; do
  if have "${tool}"; then pass "${tool}: $(command -v "${tool}")"; else skip "${tool} not installed"; fi
done

# ------------------------------------------------------------------ 2. terraform

echo
echo "=== 2. Terraform ==="
if have terraform; then
  if terraform -chdir=infra/terraform fmt -check >/dev/null 2>&1; then
    pass "terraform fmt"
  else
    fail "terraform fmt (run: terraform -chdir=infra/terraform fmt)"
  fi

  # An isolated TF_DATA_DIR so a developer's initialised workspace is not disturbed;
  # cached providers are copied in so this works without network access.
  TMP_TF="$(mktemp -d)"
  if [ -d infra/terraform/.terraform/providers ]; then
    mkdir -p "${TMP_TF}/providers"
    cp -R infra/terraform/.terraform/providers/. "${TMP_TF}/providers/"
  fi
  if TF_DATA_DIR="${TMP_TF}" terraform -chdir=infra/terraform init -backend=false >/dev/null 2>&1 \
     && TF_DATA_DIR="${TMP_TF}" terraform -chdir=infra/terraform validate >/dev/null 2>&1; then
    pass "terraform validate"
  else
    fail "terraform validate"
  fi
  rm -rf "${TMP_TF}"
else
  skip "terraform checks (terraform not installed)"
fi

# ----------------------------------------------------------------- 3/4. manifests

echo
echo "=== 3. Kustomize rendering ==="
RENDERED=""
if have kubectl; then
  for overlay in "${OVERLAYS[@]}"; do
    if kubectl kustomize "${overlay}" >/dev/null 2>&1; then
      pass "renders: ${overlay}"
    else
      fail "renders: ${overlay}"
    fi
  done
  RENDERED="$(mktemp)"
  kubectl kustomize infra/kubernetes/overlays/demo > "${RENDERED}" 2>/dev/null || true
else
  skip "kustomize rendering (kubectl not installed)"
fi

echo
echo "=== 4. Kubernetes schema validation (strict, no missing schemas tolerated) ==="
if have kubectl && have kubeconform; then
  for overlay in "${OVERLAYS[@]}"; do
    # -ignore-missing-schemas is deliberately absent: an unrecognised CRD must fail.
    summary="$(kubectl kustomize "${overlay}" | kubeconform \
      -strict -summary \
      -schema-location default \
      -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
      2>&1 | tail -n 1)" || { fail "kubeconform: ${overlay}"; continue; }
    if printf '%s' "${summary}" | grep -q 'Invalid: 0, Errors: 0, Skipped: 0'; then
      pass "kubeconform: ${overlay} (${summary#*- })"
    else
      fail "kubeconform: ${overlay} -- ${summary}"
    fi
  done
else
  skip "kubeconform validation (kubectl or kubeconform not installed)"
fi

# ------------------------------------------------- 5. manifests match the apps

echo
echo "=== 5. Manifests describe the applications that exist ==="

for image in "${IMAGES[@]}"; do
  if [ -f "infra/docker/${image}.Dockerfile" ]; then
    pass "Dockerfile exists: ${image}"
  else
    fail "Dockerfile missing: infra/docker/${image}.Dockerfile"
  fi
  if grep -rq "image: ${image}\$" infra/kubernetes/base/; then
    pass "workload references image: ${image}"
  else
    fail "no base workload names image ${image}; it would never be deployed"
  fi
  if grep -q "name: ${image}\$" infra/kubernetes/overlays/demo/images/kustomization.yaml; then
    pass "overlay rewrites image to Artifact Registry: ${image}"
  else
    fail "overlays/demo/images does not rewrite ${image}; it would pull a bare tag"
  fi
done

# Cloud Build is how the images actually reach Artifact Registry, so an image that has a
# Dockerfile, a workload and an overlay rewrite but no build step would be deployed as
# whatever tag happened to be there already -- or fail to pull. Checked separately from the
# Dockerfile because adding the fourth image meant editing four files, and this is the one
# with no other check pointing at it.
for image in "${IMAGES[@]}"; do
  if grep -q "id: ${image}\$" infra/docker/cloudbuild.yaml \
     && grep -q "/${image}:\${_TAG}" infra/docker/cloudbuild.yaml; then
    pass "Cloud Build builds and publishes: ${image}"
  else
    fail "infra/docker/cloudbuild.yaml has no build step or no images entry for ${image}"
  fi
done

# Both Next apps must build a standalone server, or the images have nothing to run.
for app in buyer-web merchant-console; do
  if grep -q 'output: *"standalone"' "apps/${app}/next.config.ts"; then
    pass "apps/${app}: output \"standalone\""
  else
    fail "apps/${app}/next.config.ts must set output: \"standalone\""
  fi
  if [ -f "apps/${app}/package-lock.json" ]; then
    pass "apps/${app}: package-lock.json present (npm ci is reproducible)"
  else
    fail "apps/${app}: package-lock.json missing; the image build uses npm ci"
  fi
done

# Every environment variable the app code reads on the server must be supplied by the
# Deployment or by a secret mount. This is the check that goes stale first, so it is
# derived from the source rather than from a list written here.
check_env_supplied() {
  local app="$1" manifest="$2" var
  for var in $(grep -rhoE 'process\.env\.[A-Z_0-9]+' "apps/${app}/src" | sed 's/.*process\.env\.//' | sort -u); do
    case "${var}" in
      NODE_ENV) continue ;;                     # set by the image
      NEXT_PUBLIC_*) continue ;;                # baked at build time, checked in step 6
    esac
    if grep -q "name: ${var}" "${manifest}" || grep -rq "path: \"${var}\"" infra/kubernetes/overlays/demo/platform/csi-sm.yaml; then
      pass "${app}: ${var} supplied by manifest or Secret Manager"
    else
      fail "${app}: reads ${var} but no manifest supplies it"
    fi
  done
}
check_env_supplied buyer-web infra/kubernetes/base/workloads/buyer-web.yaml
check_env_supplied merchant-console infra/kubernetes/base/workloads/merchant-console.yaml

# ADR 0003 D14. The API's own settings refuse anything else at startup, so a manifest
# that disagreed would crash-loop rather than serve two shops -- but it would crash-loop
# in a cluster, which is a worse place to find out.
if grep -A2 'WEB_CONCURRENCY' infra/kubernetes/base/platform/configmap.yaml | grep -q '"1"'; then
  pass 'WEB_CONCURRENCY is "1" (ADR 0003 D14)'
else
  fail 'WEB_CONCURRENCY must be "1": the merchant simulator lives in the API process'
fi
if grep -B4 'WEB_CONCURRENCY' infra/kubernetes/base/platform/configmap.yaml | grep -q '^ *#'; then
  pass "WEB_CONCURRENCY carries the reason in a comment"
else
  fail "WEB_CONCURRENCY has no comment saying why it must be 1"
fi
if grep -q 'replicas: 1' infra/kubernetes/base/workloads/commerce-api.yaml; then
  pass "commerce-api runs one replica (same reason)"
else
  fail "commerce-api must run one replica while the merchant simulator is in-process"
fi

# Every SecretProviderClass a pod mounts must exist in the overlay that deploys it.
if have kubectl && [ -n "${RENDERED}" ] && [ -s "${RENDERED}" ]; then
  declare -a WANTED DEFINED
  WANTED=($(grep -oE 'secretProviderClass: [a-z0-9-]+' "${RENDERED}" | awk '{print $2}' | sort -u))
  DEFINED=($(awk '/kind: SecretProviderClass/{f=1} f&&/^  name: /{print $2; f=0}' "${RENDERED}" | sort -u))
  for want in "${WANTED[@]:-}"; do
    [ -n "${want}" ] || continue
    if printf '%s\n' "${DEFINED[@]:-}" | grep -qx "${want}"; then
      pass "SecretProviderClass defined: ${want}"
    else
      fail "pod mounts SecretProviderClass ${want} but no overlay defines it"
    fi
  done

  # A workload with no NetworkPolicy inherits only the default-deny baseline and cannot
  # talk to anything, which fails as a silent readiness timeout in a cluster.
  for image in "${IMAGES[@]}"; do
    if grep -q "app.kubernetes.io/name: ${image}\$" infra/kubernetes/base/workloads/networkpolicies.yaml; then
      pass "NetworkPolicy covers: ${image}"
    else
      fail "no NetworkPolicy selects ${image}; default-deny would isolate it"
    fi
    if grep -q "name: ${image}\$" infra/kubernetes/base/platform/serviceaccounts.yaml; then
      pass "ServiceAccount exists: ${image}"
    else
      fail "no ServiceAccount named ${image}"
    fi
  done
else
  skip "manifest cross-references (needs a rendered overlay)"
fi

# ------------------------------------------------------------- 6. no secrets ship

echo
echo "=== 6. No secret reaches a manifest, an image layer or a browser ==="

if [ -f .dockerignore ]; then
  missing=""
  for pattern in ".env" "*.csv" "*secret*" "*credential*"; do
    grep -q -- "${pattern}" .dockerignore || missing="${missing} ${pattern}"
  done
  if [ -z "${missing}" ]; then
    pass ".dockerignore excludes secret-shaped files from the build context"
  else
    fail ".dockerignore is missing:${missing}"
  fi
else
  fail ".dockerignore is missing; the build context is the repository root"
fi

# A Secret with inline data in a manifest is a secret in git. The CSI mounts are the
# supported path, so there should be no such object at all.
if have kubectl && [ -n "${RENDERED}" ] && [ -s "${RENDERED}" ]; then
  if awk '/^kind: Secret$/{f=1} f&&/^(data|stringData):/{print; exit}' "${RENDERED}" | grep -q .; then
    fail "a rendered manifest contains a Secret with inline data"
  else
    pass "no Kubernetes Secret carries inline data"
  fi

  # An environment literal whose name says credential. This must be checked across lines,
  # not within one: kustomize re-emits `- { name: X, value: Y }` as two block-style lines,
  # so a single-line grep for "name: ...SECRET... value:" silently matches nothing and
  # reports a pass. That is exactly the failure mode this whole script exists to avoid, so
  # the check tracks the name and inspects the entry that follows it.
  #
  # APP_SECRETS_DIR is the directory the secrets are mounted at, not a secret; it is named
  # here rather than pattern-matched away, so a new exemption has to be written down.
  inline_creds="$(awk '
    /^[[:space:]]*-?[[:space:]]*name:[[:space:]]/ {
      name = $NF
      if (name == "APP_SECRETS_DIR") { name = ""; next }
      if (name ~ /SECRET|PASSWORD|TOKEN|KEY/) { pending = name } else { pending = "" }
      next
    }
    pending != "" && /^[[:space:]]*value:[[:space:]]/ { print pending; pending = "" }
    /^[[:space:]]*(valueFrom|name):/ { pending = "" }
  ' "${RENDERED}" | sort -u)"
  if [ -n "${inline_creds}" ]; then
    fail "credential-shaped environment variables carry inline values: $(echo "${inline_creds}" | tr '\n' ' ')"
  else
    pass "no credential-shaped environment variable carries an inline value"
  fi

  # NEXT_PUBLIC_* is inlined into the client bundle by `next build`. A credential named
  # that way is published to every visitor, so the name itself is the defect.
  if grep -oE 'NEXT_PUBLIC_[A-Z_0-9]+' "${RENDERED}" | sort -u | grep -qE 'SECRET|KEY|TOKEN|PASSWORD'; then
    fail "a NEXT_PUBLIC_ variable is named like a credential; it would ship to browsers"
  else
    pass "no NEXT_PUBLIC_ variable is named like a credential"
  fi
fi

# `docker history` prints build arguments and ENV defaults back out of an image.
for image in "${IMAGES[@]}"; do
  dockerfile="infra/docker/${image}.Dockerfile"
  [ -f "${dockerfile}" ] || continue
  if grep -E '^\s*(ARG|ENV)\s+[A-Z_]*(SECRET|PASSWORD|TOKEN)[A-Z_]*\s*=\s*\S' "${dockerfile}" | grep -q .; then
    fail "${dockerfile}: an ARG/ENV default looks like a credential"
  else
    pass "${dockerfile}: no credential-shaped ARG or ENV default"
  fi
done

# ------------------------------------------------------------------- 7. containers

echo
echo "=== 7. Container builds and startup ==="
if [ "${NO_DOCKER:-0}" = "1" ]; then
  skip "container builds (NO_DOCKER=1)"
elif ! have docker || ! docker info >/dev/null 2>&1; then
  skip "container builds (Docker daemon not available)"
else
  for image in "${IMAGES[@]}"; do
    if docker build -q -f "infra/docker/${image}.Dockerfile" -t "${image}:validate" . >/dev/null 2>&1; then
      pass "builds: ${image}:validate"
    else
      fail "builds: ${image} (re-run without -q to see the error)"
    fi
  done

  # The two Python services answer a version query without a database, which is the
  # cheapest proof that the virtualenv resolved and the modules import.
  for image in commerce-api durable-worker; do
    module=$([ "${image}" = "commerce-api" ] && echo commerce_api.app || echo durable_worker.main)
    if docker run --rm --entrypoint python "${image}:validate" -c "import ${module}" >/dev/null 2>&1; then
      pass "imports: ${module}"
    else
      fail "imports: ${module} (the image cannot start)"
    fi
  done

  # The web images must serve, and the console must load a mounted secret file WITHOUT
  # putting its value in the page. Both halves matter: the first proves the shim runs,
  # the second proves it did not turn a server-side secret into a client-side one.
  SECRETS_DIR="$(mktemp -d)"
  CANARY="validate-infra-canary-$$"
  printf '%s\n' "${CANARY}" > "${SECRETS_DIR}/SCENARIO_KEY"
  printf '%s\n' "${CANARY}" > "${SECRETS_DIR}/OPERATOR_COOKIE_SECRET"

  smoke() { # image host_port container_port [mount]
    local image="$1" host_port="$2" container_port="$3" mount="${4:-}"
    local cid status logs
    if [ -n "${mount}" ]; then
      cid="$(docker run -d --rm -p "${host_port}:${container_port}" -v "${mount}:/var/run/secrets/app:ro" "${image}:validate")"
    else
      cid="$(docker run -d --rm -p "${host_port}:${container_port}" "${image}:validate")"
    fi
    sleep 6
    status="$(curl -s -o /tmp/validate-body.$$ -w '%{http_code}' "http://127.0.0.1:${host_port}/" || echo 000)"
    logs="$(docker logs "${cid}" 2>&1 || true)"
    docker stop "${cid}" >/dev/null 2>&1 || true
    [ "${status}" = "200" ] && pass "serves HTTP 200: ${image}" || fail "startup: ${image} answered ${status}"
    if [ -n "${mount}" ]; then
      printf '%s' "${logs}" | grep -q 'loaded 2 secret' \
        && pass "${image}: secret files loaded into the environment" \
        || fail "${image}: the secret shim did not load the mounted files"
      grep -q "${CANARY}" /tmp/validate-body.$$ 2>/dev/null \
        && fail "${image}: a mounted secret's value appears in the served HTML" \
        || pass "${image}: no mounted secret value in the served HTML"
    fi
    rm -f /tmp/validate-body.$$
  }

  smoke buyer-web 3910 3000
  smoke merchant-console 3911 3001 "${SECRETS_DIR}"
  rm -rf "${SECRETS_DIR}"
fi

# ----------------------------------------------------------------------- summary

echo
echo "=============================================================================="
printf 'PASSED %d   FAILED %d   SKIPPED %d\n' "${#PASSED[@]}" "${#FAILED[@]}" "${#SKIPPED[@]}"
if [ "${#SKIPPED[@]}" -gt 0 ]; then
  echo
  echo "SKIPPED (not checked -- do not read these as passes):"
  printf '  - %s\n' "${SKIPPED[@]}"
fi
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo
  echo "FAILED:"
  printf '  - %s\n' "${FAILED[@]}"
fi
echo
echo "NOT DONE HERE (this script never touches a cloud project):"
echo "  - terraform apply, Secret Manager versions, the db-migrate Job, any GKE rollout."
echo "    docs/DEPLOY.md carries those commands. Validating a manifest is not deploying it."
echo "=============================================================================="

[ "${#FAILED[@]}" -eq 0 ]
