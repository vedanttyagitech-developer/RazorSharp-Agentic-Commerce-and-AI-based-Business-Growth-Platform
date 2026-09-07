#!/usr/bin/env bash
#
# Bring a clean machine to the point where the demonstration can run.
#
# Creates both databases, applies the migrations to each, installs the restricted login
# roles, and then PROVES the roles are restricted. That last step is the reason this
# script exists rather than a paragraph in a README: a PostgreSQL superuser bypasses
# row-level security unconditionally, so a bootstrap that quietly left a role SUPERUSER
# would make every tenant-isolation test in this repository pass while proving nothing.
#
# Idempotent. Every step checks before it acts, so re-running is a no-op that re-verifies.
# Nothing here is destructive: no DROP, no TRUNCATE, no DELETE.
#
# Usage:
#   scripts/bootstrap_local.sh              # both databases
#   PGHOST=... PGPORT=... scripts/bootstrap_local.sh
#   BOOTSTRAP_ADMIN_USER=postgres scripts/bootstrap_local.sh
#
set -euo pipefail

# ---------------------------------------------------------------- location and config

# The repository root is derived from where this file lives, so the script works from any
# working directory and on any machine. No home directory is ever hardcoded.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

# uv installs itself under the user's home; add it without assuming whose home it is.
export PATH="${HOME}/.local/bin:${PATH}"

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
# The cluster owner. On a Homebrew install this is the logged-in user; on a Docker image
# it is usually `postgres`. Never hardcoded.
ADMIN_USER="${BOOTSTRAP_ADMIN_USER:-${PGUSER:-$(id -un)}}"

# database name : role prefix : role password. Passwords are development-only and already
# public in scripts/bootstrap_*_roles.sql; nothing secret is introduced here.
DEV_DB="commerce_dev"
TEST_DB="commerce_test"

FAILED_STEP=""

# ------------------------------------------------------------------------- reporting

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
step()  { FAILED_STEP="$*"; printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info()  { printf '    %s\n' "$*"; }
ok()    { printf '    \033[32mok\033[0m  %s\n' "$*"; }

# Fail loudly and specifically: what broke, and the one command that fixes it.
die() {
  printf '\n\033[31mFAILED\033[0m during: %s\n' "${FAILED_STEP:-startup}" >&2
  printf '\n%s\n' "$1" >&2
  if [ "$#" -gt 1 ]; then
    printf '\nRemedy:\n  %s\n' "$2" >&2
  fi
  exit 1
}

# Any unhandled non-zero exit lands here rather than ending the script silently
# half-applied. The message names the step that was in flight.
trap 'die "An unexpected command failed. Nothing after this point ran, so the databases are in whatever state the failing step left; this script is safe to re-run once the cause is fixed."' ERR

# --------------------------------------------------------------------------- helpers

need_binary() {
  command -v "$1" >/dev/null 2>&1 || die \
    "Required command '$1' is not on PATH." "$2"
}

# psql against the admin connection, failing on the first SQL error rather than plodding
# on and reporting success at the end.
admin_psql() {
  local db="$1"; shift
  PGOPTIONS='--client-min-messages=warning' \
    psql --no-psqlrc --quiet -v ON_ERROR_STOP=1 \
      -h "${PGHOST}" -p "${PGPORT}" -U "${ADMIN_USER}" -d "${db}" "$@"
}

admin_scalar() {
  local db="$1"; shift
  admin_psql "${db}" -tAc "$1"
}

role_password_for() {
  case "$1" in
    "${DEV_DB}")  printf 'devpw' ;;
    "${TEST_DB}") printf 'testpw' ;;
    *) die "No role password is defined for database '$1'." ;;
  esac
}

role_prefix_for() {
  case "$1" in
    "${DEV_DB}")  printf 'commerce_dev' ;;
    "${TEST_DB}") printf 'commerce_test' ;;
    *) die "No role prefix is defined for database '$1'." ;;
  esac
}

# ------------------------------------------------------------------- 1. reachability

step "Checking PostgreSQL is reachable at ${PGHOST}:${PGPORT}"

need_binary psql \
  "Install the PostgreSQL 16 client tools. On macOS: brew install postgresql@16 && brew link --force postgresql@16"
need_binary pg_isready \
  "Install the PostgreSQL 16 client tools. On macOS: brew install postgresql@16"
need_binary uv \
  "Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh   (then reopen the shell)"

if ! pg_isready -h "${PGHOST}" -p "${PGPORT}" >/dev/null 2>&1; then
  die "PostgreSQL is not accepting connections at ${PGHOST}:${PGPORT}." \
"Start it, then re-run this script.
    macOS (Homebrew):  brew services start postgresql@16
    Linux (systemd):   sudo systemctl start postgresql
    Docker:            docker run -d --name commerce-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:16
                       then re-run with: BOOTSTRAP_ADMIN_USER=postgres PGPASSWORD=postgres scripts/bootstrap_local.sh"
fi
ok "server is accepting connections"

if ! admin_scalar postgres "SELECT 1" >/dev/null 2>&1; then
  die "PostgreSQL is running but the admin role '${ADMIN_USER}' cannot connect to the 'postgres' database." \
"Set the admin role explicitly, for example:
    BOOTSTRAP_ADMIN_USER=postgres PGPASSWORD=postgres scripts/bootstrap_local.sh
  The admin role must own, or be able to create, the commerce databases."
fi
SERVER_VERSION="$(admin_scalar postgres "SHOW server_version")"
ok "connected as '${ADMIN_USER}' to PostgreSQL ${SERVER_VERSION}"

case "${SERVER_VERSION}" in
  1[6-9]*|2[0-9]*) : ;;
  *) info "warning: this project is developed against PostgreSQL 16; you are on ${SERVER_VERSION}" ;;
esac

# --------------------------------------------------------------------- 2. databases

step "Creating databases if they are absent"

CREATED_DATABASES=()
for db in "${DEV_DB}" "${TEST_DB}"; do
  exists="$(admin_scalar postgres "SELECT 1 FROM pg_database WHERE datname = '${db}'")"
  if [ "${exists}" = "1" ]; then
    ok "${db} already exists"
  else
    admin_psql postgres -c "CREATE DATABASE ${db}" >/dev/null
    CREATED_DATABASES+=("${db}")
    ok "${db} created"
  fi
done

# -------------------------------------------------------------------- 3. migrations

step "Applying migrations (alembic upgrade head)"

# alembic.ini sets `script_location = migrations`, a path relative to the file, so alembic
# must be invoked from inside packages/platform-db. migrations/env.py reads DATABASE_URL.
MIGRATIONS_HOME="${REPO_ROOT}/packages/platform-db"
[ -f "${MIGRATIONS_HOME}/alembic.ini" ] || die \
  "Expected ${MIGRATIONS_HOME}/alembic.ini and it is not there." \
  "Run this script from a complete checkout of the repository."

for db in "${DEV_DB}" "${TEST_DB}"; do
  url="postgresql+psycopg://${ADMIN_USER}@${PGHOST}:${PGPORT}/${db}"
  if ! (cd "${MIGRATIONS_HOME}" && DATABASE_URL="${url}" uv run --no-sync alembic upgrade head >/dev/null); then
    die "alembic upgrade head failed for ${db}." \
"Run it directly to see the error:
    cd ${MIGRATIONS_HOME} && DATABASE_URL='${url}' uv run --no-sync alembic upgrade head"
  fi
  revision="$(cd "${MIGRATIONS_HOME}" && DATABASE_URL="${url}" uv run --no-sync alembic current 2>/dev/null | tail -n 1)"
  ok "${db} at ${revision:-head}"
done

# -------------------------------------------------------------------------- 4. roles

step "Installing restricted login roles"

# Roles are cluster-wide but GRANTs are per-database, so each file runs inside the
# database it governs. Both files are DO-block guarded and re-assert grants, so applying
# them twice changes nothing.
for db in "${DEV_DB}" "${TEST_DB}"; do
  prefix="$(role_prefix_for "${db}")"
  sql_file="${SCRIPT_DIR}/bootstrap_${prefix#commerce_}_roles.sql"
  [ -f "${sql_file}" ] || die \
    "Expected ${sql_file} and it is not there." \
    "Run this script from a complete checkout of the repository."
  if ! admin_psql "${db}" -f "${sql_file}" >/dev/null; then
    die "Applying $(basename "${sql_file}") to ${db} failed." \
"Run it directly to see the error:
    psql -h ${PGHOST} -p ${PGPORT} -U ${ADMIN_USER} -d ${db} -v ON_ERROR_STOP=1 -f ${sql_file}"
  fi
  ok "$(basename "${sql_file}") applied to ${db}"
done

# ----------------------------------------------------------------------- 5. verifty

step "Verifying every login role is NOSUPERUSER, NOBYPASSRLS and can connect"

info "A SUPERUSER or BYPASSRLS role ignores row-level security, which would make the"
info "tenant-isolation suites pass without proving anything. This is the load-bearing check."

VERIFIED_ROLES=()
for db in "${DEV_DB}" "${TEST_DB}"; do
  prefix="$(role_prefix_for "${db}")"
  password="$(role_password_for "${db}")"
  for suffix in app kernel worker; do
    role="${prefix}_${suffix}"

    attributes="$(admin_scalar postgres \
      "SELECT rolsuper::text || ' ' || rolbypassrls::text || ' ' || rolcanlogin::text
         FROM pg_roles WHERE rolname = '${role}'")"
    if [ -z "${attributes}" ]; then
      die "Role '${role}' does not exist after bootstrapping ${db}." \
        "Re-run: psql -U ${ADMIN_USER} -d ${db} -v ON_ERROR_STOP=1 -f ${SCRIPT_DIR}/bootstrap_${prefix#commerce_}_roles.sql"
    fi
    read -r is_super is_bypassrls can_login <<<"${attributes}"

    [ "${is_super}" = "false" ] || die \
      "Role '${role}' is SUPERUSER. Row-level security does not apply to it, so every isolation proof in this repository would be vacuous." \
      "psql -U ${ADMIN_USER} -d postgres -c 'ALTER ROLE ${role} NOSUPERUSER'"
    [ "${is_bypassrls}" = "false" ] || die \
      "Role '${role}' has BYPASSRLS. It would read and write every tenant's rows." \
      "psql -U ${ADMIN_USER} -d postgres -c 'ALTER ROLE ${role} NOBYPASSRLS'"
    [ "${can_login}" = "true" ] || die \
      "Role '${role}' cannot LOGIN, so no process can connect as it." \
      "psql -U ${ADMIN_USER} -d postgres -c \"ALTER ROLE ${role} LOGIN PASSWORD '${password}'\""

    connected="$(PGPASSWORD="${password}" psql --no-psqlrc --quiet -v ON_ERROR_STOP=1 \
      "postgresql://${role}@${PGHOST}:${PGPORT}/${db}" -tAc "SELECT current_user" 2>/dev/null || true)"
    [ "${connected}" = "${role}" ] || die \
      "Role '${role}' exists and is restricted, but could not connect to ${db}." \
"Check that the server accepts password authentication for local connections. In pg_hba.conf,
  host connections to 127.0.0.1/32 must use scram-sha-256 (or md5), not 'reject'. After editing:
    psql -U ${ADMIN_USER} -d postgres -c 'SELECT pg_reload_conf()'"

    VERIFIED_ROLES+=("${role}@${db}")
    ok "${role} -> ${db}: NOSUPERUSER, NOBYPASSRLS, connects"
  done
done

# ------------------------------------------------------------------------ 6. summary

trap - ERR
FAILED_STEP=""

printf '\n'
bold "Bootstrap complete."
printf '\n'
printf '  Server            %s at %s:%s (admin role: %s)\n' \
  "PostgreSQL ${SERVER_VERSION}" "${PGHOST}" "${PGPORT}" "${ADMIN_USER}"
if [ "${#CREATED_DATABASES[@]}" -gt 0 ]; then
  printf '  Databases created %s\n' "${CREATED_DATABASES[*]}"
else
  printf '  Databases created none (both already existed)\n'
fi
printf '  Migrations        head on %s and %s\n' "${DEV_DB}" "${TEST_DB}"
printf '  Roles verified    %d (%s)\n' "${#VERIFIED_ROLES[@]}" "${VERIFIED_ROLES[*]}"
printf '\n'
printf '  %s is the demonstration database. %s is for the test suite.\n' "${DEV_DB}" "${TEST_DB}"
printf '\n'
printf 'Next:\n'
printf '  make seed     seed the demo tenant and merchant into %s\n' "${DEV_DB}"
printf '  make demo     start the API and the Action Executor\n'
printf '  make test     run the backend suites against %s\n' "${TEST_DB}"
printf '\n'
