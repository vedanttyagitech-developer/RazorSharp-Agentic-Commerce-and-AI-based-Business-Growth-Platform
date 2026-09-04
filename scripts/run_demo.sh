#!/usr/bin/env bash
#
# Start the demonstration: the API and the durable worker, together, in the foreground,
# and stop both cleanly on Ctrl-C.
#
# Two processes rather than one because the split is the architecture (ADR 0003 D1/D3):
# the API admits money actions and writes an outbox row; the worker is the only process
# that talks to Razorpay. Running them from one script means the demonstration cannot be
# recorded with half of it missing.
#
# Usage:
#   scripts/run_demo.sh                 # API + worker
#   scripts/run_demo.sh --api-only
#   scripts/run_demo.sh --worker-only
#   PORT=8080 scripts/run_demo.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
export PATH="${HOME}/.local/bin:${PATH}"
cd "${REPO_ROOT}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m%s\033[0m\n' "$1" >&2; [ "$#" -gt 1 ] && printf '\n%s\n' "$2" >&2; exit 1; }

# Tag each child's output with which process wrote it. A read loop rather than `sed`,
# because BSD sed block-buffers when its output is not a terminal, and a demonstration
# whose API log appears only after the API stops is worse than no log at all.
prefix() { while IFS= read -r line; do printf '%s %s\n' "$1" "${line}"; done; }

START_API=1
START_WORKER=1
for arg in "$@"; do
  case "${arg}" in
    --api-only)    START_WORKER=0 ;;
    --worker-only) START_API=0 ;;
    -h|--help)     sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown argument: ${arg}" "Valid arguments: --api-only, --worker-only" ;;
  esac
done

# ------------------------------------------------------------------------ environment

bold "Environment"

ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
if [ -f "${ENV_FILE}" ]; then
  # `set -a` exports everything the file defines. The file is shell syntax and is
  # sourced, so it is trusted input -- it is the developer's own secrets file.
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
  info "loaded $(basename "${ENV_FILE}") ($(grep -c '^[A-Za-z_][A-Za-z0-9_]*=' "${ENV_FILE}") entries)"
else
  warn "no .env at ${ENV_FILE}; relying on the ambient environment"
  info "copy .env.example to .env and fill in the Razorpay test-mode keys"
fi

# The demonstration runs against commerce_dev. The repository's .env points the role URLs
# at commerce_test because that is what the suites need, so the disagreement is resolved
# here, out loud, rather than by silently driving the demo through the test database.
DEMO_DB="${DEMO_DB:-commerce_dev}"
DEMO_PGHOST="${DEMO_PGHOST:-${PGHOST:-localhost}}"
DEMO_PGPORT="${DEMO_PGPORT:-${PGPORT:-5432}}"
case "${DEMO_DB}" in
  commerce_dev)  ROLE_PREFIX="commerce_dev";  ROLE_PASSWORD="devpw" ;;
  commerce_test) ROLE_PREFIX="commerce_test"; ROLE_PASSWORD="testpw" ;;
  *) die "DEMO_DB is '${DEMO_DB}', which has no known login roles." \
         "Set DATABASE_URL_APP, DATABASE_URL_KERNEL and DATABASE_URL_WORKER yourself and re-run with DEMO_DB set to their database." ;;
esac

url_for_role() {
  printf 'postgresql+psycopg://%s_%s:%s@%s:%s/%s' \
    "${ROLE_PREFIX}" "$1" "${ROLE_PASSWORD}" "${DEMO_PGHOST}" "${DEMO_PGPORT}" "${DEMO_DB}"
}

# Point each role at DEMO_DB unless the ambient value already names that database.
for pair in "APP:app" "KERNEL:kernel" "WORKER:worker"; do
  var="DATABASE_URL_${pair%%:*}"
  role="${pair##*:}"
  current="$(eval "printf '%s' \"\${${var}:-}\"")"
  wanted="$(url_for_role "${role}")"
  case "${current}" in
    */"${DEMO_DB}") : ;;
    "") export "${var}=${wanted}" ;;
    *)  export "${var}=${wanted}"
        warn "${var} named a different database; overridden to ${DEMO_DB} for this demo run" ;;
  esac
done
info "database        ${DEMO_DB} at ${DEMO_PGHOST}:${DEMO_PGPORT}"
info "roles           ${ROLE_PREFIX}_app (reads), ${ROLE_PREFIX}_kernel (mutations), ${ROLE_PREFIX}_worker (outbox)"

export PROFILE="${PROFILE:-development}"
# ADR 0003 D14: the merchant simulator's catalogue and inventory live in this process's
# memory, so a second worker process would serve a different merchant state and the
# step-5 price change would apply to some requests and not others.
export WEB_CONCURRENCY=1
# ADR 0003 D11: no key means the scenario controller does not exist in the process, and
# step 5 of the demonstration cannot be performed. A fixed local default so the runbook
# can quote it; override it in .env for anything that is not a laptop.
export SCENARIO_KEY="${SCENARIO_KEY:-local-demo-scenario-key}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
info "profile         ${PROFILE}   web_concurrency=1   scenario key: ${SCENARIO_KEY}"

missing_keys=()
for key in RAZORPAY_KEY_ID RAZORPAY_KEY_SECRET RAZORPAY_WEBHOOK_SECRET; do
  [ -n "$(eval "printf '%s' \"\${${key}:-}\"")" ] || missing_keys+=("${key}")
done
if [ "${#missing_keys[@]}" -gt 0 ]; then
  die "Missing Razorpay test-mode credentials: ${missing_keys[*]}" \
"Both processes validate these at start-up and refuse to run without them.
  Put them in ${ENV_FILE} (see .env.example). They must be TEST-mode keys: a
  rzp_live_ key is refused outside an explicit production profile."
fi
case "${RAZORPAY_KEY_ID}" in
  rzp_test_*) info "razorpay        test mode (${RAZORPAY_KEY_ID:0:12}...)" ;;
  *) warn "RAZORPAY_KEY_ID does not start with rzp_test_; start-up will refuse it outside a production profile" ;;
esac

# ----------------------------------------------------------------- what can actually run

bold "Processes"

# The API and the worker are being written by other people while this script exists. A
# missing or half-written module must produce one clear sentence, never a traceback.
api_error=""
if [ "${START_API}" = "1" ]; then
  if ! api_error="$(uv run --no-sync python -c 'import commerce_api.app' 2>&1)"; then
    START_API=0
    warn "the API is not runnable yet: commerce_api.app does not import."
    info "first line of the import error:"
    info "  $(printf '%s' "${api_error}" | tail -n 1)"
    info "this is expected while the HTTP layer is still being written; the worker can still run."
  else
    info "api             commerce_api.app:create_app"
  fi
fi

# The worker's entry point is being written as this script is used, so rather than guess
# one name, ask Python which of the conventional ones is importable and callable. The
# answer comes back as `module` or `module:function` -- never as a command line, because a
# command line held in a variable word-splits and silently runs the wrong thing.
WORKER_TARGET=""
if [ "${START_WORKER}" = "1" ]; then
  WORKER_TARGET="$(uv run --no-sync python - <<'PY' 2>/dev/null || true
import importlib
import importlib.util

if importlib.util.find_spec("durable_worker.__main__") is not None:
    print("durable_worker")
    raise SystemExit(0)

for module, attribute in (
    ("durable_worker.main", "main"),
    ("durable_worker.runner", "main"),
    ("durable_worker.run", "main"),
    ("durable_worker.worker", "main"),
    ("durable_worker.loop", "main"),
):
    if importlib.util.find_spec(module) is None:
        continue
    try:
        loaded = importlib.import_module(module)
    except Exception:
        continue
    if callable(getattr(loaded, attribute, None)):
        print(f"{module}:{attribute}")
        raise SystemExit(0)
PY
)"
  if [ -z "${WORKER_TARGET}" ]; then
    START_WORKER=0
    warn "the worker is not runnable yet: durable_worker has no entry point."
    info "looked for durable_worker.__main__, and main() in main / runner / run / worker / loop."
    info "this is expected while the worker is still being written; the API can still run."
  else
    info "worker          ${WORKER_TARGET}"
  fi
fi

if [ "${START_API}" = "0" ] && [ "${START_WORKER}" = "0" ]; then
  die "Nothing to start." \
"Neither the API nor the worker is runnable in this checkout. Both are under active
  development; re-run this script once either one imports. Everything else is ready:
  the database is bootstrapped and the demo tenant is seeded."
fi

if command -v lsof >/dev/null 2>&1 && [ "${START_API}" = "1" ]; then
  if lsof -ti "tcp:${PORT}" >/dev/null 2>&1; then
    die "Port ${PORT} is already in use." \
"Stop whatever holds it, or choose another port:
    PORT=8080 scripts/run_demo.sh
  To see the holder:  lsof -i tcp:${PORT}"
  fi
fi

# ------------------------------------------------------------------------- supervision

PIDS=()
SHUTTING_DOWN=0

shutdown() {
  [ "${SHUTTING_DOWN}" = "1" ] && return
  SHUTTING_DOWN=1
  printf '\n'
  bold "Stopping"
  for pid in "${PIDS[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill -TERM "${pid}" 2>/dev/null || true
    fi
  done
  # Give each process its own shutdown: uvicorn finishes in-flight requests, and the
  # worker must return its leased outbox rows rather than stranding them for a lease
  # timeout while a demonstration is being recorded.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    still_running=0
    for pid in "${PIDS[@]}"; do
      kill -0 "${pid}" 2>/dev/null && still_running=1
    done
    [ "${still_running}" = "0" ] && break
    sleep 1
  done
  for pid in "${PIDS[@]}"; do
    kill -0 "${pid}" 2>/dev/null && kill -KILL "${pid}" 2>/dev/null || true
  done
  info "both processes stopped"
}
trap shutdown INT TERM EXIT

printf '\n'
bold "Running. Ctrl-C stops everything."
printf '\n'

if [ "${START_API}" = "1" ]; then
  uv run --no-sync uvicorn commerce_api.app:create_app --factory \
      --host "${HOST}" --port "${PORT}" \
      > >(prefix '[api]   ') 2>&1 &
  PIDS+=("$!")
  info "api      http://${HOST}:${PORT}   docs at http://${HOST}:${PORT}/docs"
fi

if [ "${START_WORKER}" = "1" ]; then
  case "${WORKER_TARGET}" in
    *:*)
      worker_module="${WORKER_TARGET%%:*}"
      worker_function="${WORKER_TARGET##*:}"
      uv run --no-sync python \
          -c "from ${worker_module} import ${worker_function}; ${worker_function}()" \
          > >(prefix '[worker]') 2>&1 &
      ;;
    *)
      uv run --no-sync python -m "${WORKER_TARGET}" \
          > >(prefix '[worker]') 2>&1 &
      ;;
  esac
  PIDS+=("$!")
  info "worker   leasing outbox rows; the only process that calls Razorpay"
fi

printf '\n'

# Exit as soon as either process dies: a demonstration with a dead worker looks like a
# hung payment, and finding out immediately is worth more than staying up. Polled rather
# than `wait -n`, which macOS's bash 3.2 does not have.
while :; do
  for pid in "${PIDS[@]}"; do
    if ! kill -0 "${pid}" 2>/dev/null; then
      [ "${SHUTTING_DOWN}" = "1" ] && exit 0
      printf '\n'
      warn "a process exited; stopping the rest"
      exit 1
    fi
  done
  sleep 1
done
