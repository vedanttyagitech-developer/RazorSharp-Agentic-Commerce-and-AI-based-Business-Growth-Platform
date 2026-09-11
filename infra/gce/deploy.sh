#!/usr/bin/env bash
# Bring the backend up on the VM. Run from the repository root ON the machine:
#
#   sudo bash infra/gce/deploy.sh
#
# Idempotent: re-running rebuilds changed images, re-applies migrations and grants (both
# are guarded), and restarts the services. It does NOT re-seed unless --seed is given,
# because seeding resets the demo catalogue and would discard a judge's session.
set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT="$(pwd)"
ENV_FILE="${ROOT}/infra/gce/.env"
COMPOSE=(docker compose -f infra/gce/compose.yaml --env-file "${ENV_FILE}")
SEED=0
[ "${1:-}" = "--seed" ] && SEED=1

[ -f "${ENV_FILE}" ] || { echo "missing ${ENV_FILE}" >&2; exit 1; }
# The role passwords are in the URLs; read them back rather than keeping a second copy.
set -a; . "${ENV_FILE}"; set +a
url_password() { sed -E 's#^[^:]+://[^:]+:([^@]*)@.*$#\1#' <<<"$1"; }

export VCS_REF="$(git -C "${ROOT}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
export BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "== building images (${VCS_REF})"
"${COMPOSE[@]}" build

echo "== database"
"${COMPOSE[@]}" up -d db
"${COMPOSE[@]}" exec -T db bash -c 'until pg_isready -U postgres -d commerce -q; do sleep 1; done'
# The ADK owns its own schema and versions nothing; it only needs the database to exist.
"${COMPOSE[@]}" exec -T db psql -U postgres -d postgres -v ON_ERROR_STOP=1 -tAc \
  "SELECT 1 FROM pg_database WHERE datname='commerce_adk'" | grep -q 1 || \
  "${COMPOSE[@]}" exec -T db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
    -c "CREATE DATABASE commerce_adk"

echo "== migrations"
# alembic.ini and versions/ ship inside the API image at /app/db (see commerce-api.Dockerfile).
"${COMPOSE[@]}" run --rm --no-deps -w /app/db \
  -e DATABASE_URL="${DATABASE_URL}" api alembic upgrade head

echo "== login roles and grants"
# The repository's own grant file, verbatim, so the deployed grants cannot drift from the
# ones the tests run against. Only the passwords differ: the file ships a shared 'devpw'
# placeholder, which is fine on a laptop and is not fine on a machine with a public address.
"${COMPOSE[@]}" cp scripts/bootstrap_dev_roles.sql db:/tmp/roles.sql 2>/dev/null || \
  docker cp "${ROOT}/scripts/bootstrap_dev_roles.sql" \
    "$("${COMPOSE[@]}" ps -q db)":/tmp/roles.sql
"${COMPOSE[@]}" exec -T db psql -U postgres -d commerce -v ON_ERROR_STOP=1 -q -f /tmp/roles.sql
"${COMPOSE[@]}" exec -T db psql -U postgres -d commerce -v ON_ERROR_STOP=1 -q \
  -c "ALTER ROLE commerce_dev_app    PASSWORD '$(url_password "${DATABASE_URL_APP}")'" \
  -c "ALTER ROLE commerce_dev_kernel PASSWORD '$(url_password "${DATABASE_URL_KERNEL}")'" \
  -c "ALTER ROLE commerce_dev_worker PASSWORD '$(url_password "${DATABASE_URL_WORKER}")'"
"${COMPOSE[@]}" exec -T db rm -f /tmp/roles.sql

# Absence is not proof: assert the three identities are what the RLS design assumes.
echo "== identity check (must print three rows, all f)"
"${COMPOSE[@]}" exec -T db psql -U postgres -d commerce -tA -F' ' -c \
  "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles
    WHERE rolname IN ('commerce_dev_app','commerce_dev_kernel','commerce_dev_worker')
    ORDER BY rolname"

echo "== services"
"${COMPOSE[@]}" up -d --remove-orphans

if [ "${SEED}" = "1" ]; then
  # One definition of what seeding is, in seed.sh. It was duplicated here and the copy
  # drifted immediately: it passed DATABASE_URL and the seeds read SEED_DATABASE_URL, so
  # they fell back to the laptop's own commerce_dev and failed as "connection refused".
  bash infra/gce/seed.sh
fi

echo
"${COMPOSE[@]}" ps
