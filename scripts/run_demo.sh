#!/usr/bin/env bash
#
# Start the demonstration: the API and the durable worker, together, in the foreground,
# and stop both cleanly on Ctrl-C.
#
# Three processes rather than one because the split is the architecture (ADR 0003 D1/D3):
# the API admits money actions and writes an outbox row; the worker is the only process
# that talks to Razorpay; the voice gateway is the only one that holds a microphone.
# Running them from one script means the demonstration cannot be recorded with part of it
# missing.
#
# That sentence was written when there were two, and it stayed true for two while the
# third went unstarted for three days. The gateway landed on 6 September, this script was
# edited again on the 8th, and nothing here ever mentioned it -- so the storefront opened,
# the microphone button drew, and the copilot said "the voice connection dropped" to
# everybody who ran the demonstration. Nothing was broken. Nothing was started either,
# which is the failure this script exists to make impossible and did not.
#
# Usage:
#   scripts/run_demo.sh                 # API + worker + voice, against commerce_dev
#   scripts/run_demo.sh --api-only
#   scripts/run_demo.sh --worker-only
#   scripts/run_demo.sh --no-voice
#   PORT=8080 scripts/run_demo.sh
#   DEMO_DB=commerce_test scripts/run_demo.sh
#
# An isolated stack, so a fault injected here disturbs nobody else's demonstration:
# any database works, and one this script has no login for is driven by the three role
# URLs the caller supplies. They win over the ones in .env, and the port must be free.
#
#   DEMO_DB=commerce_fail PORT=8010 \
#   DATABASE_URL_APP=postgresql+psycopg://commerce_fail_app:pw@localhost:5432/commerce_fail \
#   DATABASE_URL_KERNEL=postgresql+psycopg://commerce_fail_kernel:pw@localhost:5432/commerce_fail \
#   DATABASE_URL_WORKER=postgresql+psycopg://commerce_fail_worker:pw@localhost:5432/commerce_fail \
#   scripts/run_demo.sh
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
START_VOICE=1
VOICE_PORT="${VOICE_GATEWAY_PORT:-8100}"
for arg in "$@"; do
  case "${arg}" in
    --api-only)    START_WORKER=0; START_VOICE=0 ;;
    --worker-only) START_API=0;    START_VOICE=0 ;;
    --no-voice)    START_VOICE=0 ;;
    # Printed by reading down to the first line that is not a comment, rather than by a
    # fixed line range: the usage block grew once already, and a range would have gone on
    # printing the old half of it without anybody noticing.
    -h|--help)     awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) die "Unknown argument: ${arg}" "Valid arguments: --api-only, --worker-only, --no-voice" ;;
  esac
done

# ------------------------------------------------------------------------ environment

bold "Environment"

# Whatever the caller exported, captured before the env file is sourced over the top of
# it. `.env` names the three role URLs, and `set -a` means sourcing it silently replaces
# an isolated stack's credentials with the shared database's -- so the values below are
# the only surviving record of what the caller actually asked for. They are consulted on
# one path only (a database this script has no login for), which leaves the two known
# databases resolving exactly as they always have.
PRESET_URL_APP="${DATABASE_URL_APP:-}"
PRESET_URL_KERNEL="${DATABASE_URL_KERNEL:-}"
PRESET_URL_WORKER="${DATABASE_URL_WORKER:-}"

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

# The demonstration runs against commerce_dev. A developer's .env points the role URLs at
# whichever database their suites need, which is not always this one, so a disagreement is
# resolved here, out loud, rather than by silently driving the demo through that database.
#
# Two databases have logins this script can construct from nothing, and every other one
# needs the caller to say what its logins are. That second path is not a fallback for a
# typo -- it is how a second stack runs beside the shared one, on its own database and its
# own port, so that a fault injected into it is not injected into everybody's demo.
DEMO_DB="${DEMO_DB:-commerce_dev}"
DEMO_PGHOST="${DEMO_PGHOST:-${PGHOST:-localhost}}"
DEMO_PGPORT="${DEMO_PGPORT:-${PGPORT:-5432}}"
ROLE_PREFIX=""
ROLE_PASSWORD=""
case "${DEMO_DB}" in
  commerce_dev)  ROLE_PREFIX="commerce_dev";  ROLE_PASSWORD="devpw" ;;
  commerce_test) ROLE_PREFIX="commerce_test"; ROLE_PASSWORD="testpw" ;;
esac

url_for_role() {
  printf 'postgresql+psycopg://%s_%s:%s@%s:%s/%s' \
    "${ROLE_PREFIX}" "$1" "${ROLE_PASSWORD}" "${DEMO_PGHOST}" "${DEMO_PGPORT}" "${DEMO_DB}"
}

# The login half of a role URL, for the summary line only. Trimming at the first `@` and
# then at the first `:` keeps a password out of the terminal and out of any recording of
# it, which matters more here than being exact about an exotic URL.
role_user() {
  trimmed="${1#*://}"
  trimmed="${trimmed%%@*}"
  printf '%s' "${trimmed%%:*}"
}

if [ -n "${ROLE_PREFIX}" ]; then
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
  DB_SUMMARY="${DEMO_DB} at ${DEMO_PGHOST}:${DEMO_PGPORT}"
  ROLE_SUMMARY="${ROLE_PREFIX}_app (reads), ${ROLE_PREFIX}_kernel (mutations), ${ROLE_PREFIX}_worker (outbox)"
else
  # An unknown database is driven by the caller's own URLs, used as given: this script has
  # no business inventing a login for a database it has never heard of, and a guessed one
  # would fail at connect time with a Postgres error rather than here with an explanation.
  missing_urls=()
  wrong_db=()
  for pair in "APP:app" "KERNEL:kernel" "WORKER:worker"; do
    var="DATABASE_URL_${pair%%:*}"
    preset="$(eval "printf '%s' \"\${PRESET_URL_${pair%%:*}:-}\"")"
    ambient="$(eval "printf '%s' \"\${${var}:-}\"")"
    if [ -n "${preset}" ]; then
      # What the caller exported for this run, captured before .env was sourced over it.
      # Whatever it names, it is an answer, and a wrong one is worth saying so about.
      current="${preset}"
    else
      # Only what came out of the env file. That file describes the shared stack, so it
      # is an answer about this database only when it happens to name it -- which is how
      # ENV_FILE can point at an isolated stack's own file. Otherwise the caller has said
      # nothing about ${DEMO_DB}, and the instructions below are what they need to see,
      # not a complaint that .env names the database it was always going to name.
      case "${ambient}" in
        */"${DEMO_DB}"|*/"${DEMO_DB}"\?*) current="${ambient}" ;;
        *) current="" ;;
      esac
    fi
    if [ -z "${current}" ]; then
      missing_urls+=("${var}")
      continue
    fi
    # A trailing `?sslmode=...` is ordinary; anything else after the database name is not
    # this database. The URL must name DEMO_DB, because every later line of output --
    # and the runbook the operator is reading -- says the stack is running against it.
    case "${current}" in
      */"${DEMO_DB}"|*/"${DEMO_DB}"\?*) export "${var}=${current}" ;;
      *) wrong_db+=("${var}") ;;
    esac
  done

  if [ "${#missing_urls[@]}" -gt 0 ]; then
    die "DEMO_DB is '${DEMO_DB}', which has no login roles this script knows." \
"Only commerce_dev and commerce_test are configured from nothing. Any other database
  runs on credentials you supply -- three URLs that name ${DEMO_DB}:

    DEMO_DB=${DEMO_DB} PORT=8010 \\
    DATABASE_URL_APP=postgresql+psycopg://<user>:<pw>@${DEMO_PGHOST}:${DEMO_PGPORT}/${DEMO_DB} \\
    DATABASE_URL_KERNEL=postgresql+psycopg://<user>:<pw>@${DEMO_PGHOST}:${DEMO_PGPORT}/${DEMO_DB} \\
    DATABASE_URL_WORKER=postgresql+psycopg://<user>:<pw>@${DEMO_PGHOST}:${DEMO_PGPORT}/${DEMO_DB} \\
    scripts/run_demo.sh

  No URL naming ${DEMO_DB} was given for: ${missing_urls[*]}"
  fi
  if [ "${#wrong_db[@]}" -gt 0 ]; then
    die "These role URLs do not name ${DEMO_DB}: ${wrong_db[*]}" \
"Every role must point at the database DEMO_DB names, or half the stack would run
  against one database and half against another. Export them in the environment
  (they win over ${ENV_FILE}) or set DEMO_DB to the database they really name."
  fi

  DB_SUMMARY="${DEMO_DB}, from the supplied role URLs"
  ROLE_SUMMARY="$(role_user "${DATABASE_URL_APP}") (reads), $(role_user "${DATABASE_URL_KERNEL}") (mutations), $(role_user "${DATABASE_URL_WORKER}") (outbox)"
fi
info "database        ${DB_SUMMARY}"
info "roles           ${ROLE_SUMMARY}"

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

# The worker/executor's entry point is being written as this script is used, so rather than guess
# one name, ask Python which of the conventional ones is importable and callable. The
# answer comes back as `module` or `module:function` -- never as a command line, because a
# command line held in a variable word-splits and silently runs the wrong thing.
WORKER_TARGET=""
if [ "${START_WORKER}" = "1" ]; then
  WORKER_TARGET="$(uv run --no-sync python - <<'PY' 2>/dev/null || true
import importlib
import importlib.util

for pkg in ("action_executor", "durable_worker"):
    if importlib.util.find_spec(f"{pkg}.__main__") is not None:
        print(pkg)
        raise SystemExit(0)

    for sub, attribute in (
        ("main", "main"),
        ("runner", "main"),
        ("run", "main"),
        ("worker", "main"),
        ("loop", "main"),
    ):
        module = f"{pkg}.{sub}"
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
    warn "the Action Executor is not runnable yet: action_executor has no entry point."
    info "looked for action_executor.__main__, and main() in main / runner / run / worker / loop."
    info "this is expected while the worker is still being written; the API can still run."
  else
    info "executor        ${WORKER_TARGET}"
  fi
fi

if [ "${START_VOICE}" = "1" ]; then
  if ! voice_error="$(uv run --no-sync python -c 'import voice_runtime.gateway' 2>&1)"; then
    START_VOICE=0
    warn "the voice gateway is not runnable: voice_runtime.gateway does not import."
    info "  $(printf '%s' "${voice_error}" | tail -n 1)"
    info "the demonstration still runs; the copilot will say speech is unavailable."
  else
    info "voice           voice_runtime.gateway on ${VOICE_PORT}"
  fi
fi

if [ "${START_API}" = "0" ] && [ "${START_WORKER}" = "0" ] && [ "${START_VOICE}" = "0" ]; then
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

if [ "${START_VOICE}" = "1" ]; then
  # The browser reaches this through the storefront's own two routes, so the gateway only
  # ever needs to admit the storefront's origin. Speech being unconfigured is not a
  # failure here either: the socket opens and says recognition is unavailable, which is
  # the visible degradation rather than a dead button.
  VOICE_GATEWAY_API_BASE_URL="${VOICE_GATEWAY_API_BASE_URL:-http://${HOST}:${PORT}}" \
  VOICE_GATEWAY_ALLOWED_ORIGINS="${VOICE_GATEWAY_ALLOWED_ORIGINS:-http://localhost:3000,http://127.0.0.1:3000}" \
  VOICE_GATEWAY_PORT="${VOICE_PORT}" \
      uv run --no-sync python -m voice_runtime.gateway \
      > >(prefix '[voice] ') 2>&1 &
  PIDS+=("$!")
  info "voice    ws://127.0.0.1:${VOICE_PORT}; the only process that holds a microphone"
fi

printf '\n'

# ------------------------------------------------------------------ did they come up?
#
# Spawning a process is not the same as serving. Every failure this script was supposed to
# prevent has looked identical from the outside -- the storefront loads, the buttons draw,
# and the missing half announces itself only to whoever presses the one control that needs
# it. So the surfaces are asked, by name, and what is unreachable is said out loud.
#
# It reports rather than exits. A gateway that will not start is a demonstration without
# speech, which is worth running; a demonstration that silently has no speech is not.
reachable() {
  curl -fsS -o /dev/null --max-time 2 "$1" 2>/dev/null
}

wait_for() {
  local url="$1" tries="${2:-25}"
  while [ "${tries}" -gt 0 ]; do
    reachable "${url}" && return 0
    tries=$((tries - 1))
    sleep 1
  done
  return 1
}

bold "Reachable"
DEGRADED=0
if [ "${START_API}" = "1" ]; then
  if wait_for "http://${HOST}:${PORT}/openapi.json"; then
    info "api      http://${HOST}:${PORT}"
  else
    warn "api      NOT ANSWERING on ${PORT} -- the log above says why"
    DEGRADED=1
  fi
fi
if [ "${START_VOICE}" = "1" ]; then
  # The gateway serves its own health; a socket upgrade is not something curl should try.
  if wait_for "http://127.0.0.1:${VOICE_PORT}/healthz" 20; then
    info "voice    ws://127.0.0.1:${VOICE_PORT}"
  else
    warn "voice    NOT ANSWERING on ${VOICE_PORT} -- the copilot will say the connection dropped"
    DEGRADED=1
  fi
fi
# The two front ends are not this script's to start, and saying nothing about them is how
# a demonstration gets recorded against a storefront that is not running either.
for row in "3000:storefront:buyer-web" "3001:console:merchant-console"; do
  port="${row%%:*}"; rest="${row#*:}"; name="${rest%%:*}"; dir="${rest##*:}"
  if reachable "http://localhost:${port}/"; then
    info "$(printf '%-11s' "${name}")http://localhost:${port}"
  else
    warn "$(printf '%-11s' "${name}")not running -- (cd apps/${dir} && npm run dev)"
  fi
done
if [ "${DEGRADED}" = "1" ]; then
  printf '\n'
  warn "Something this script started is not answering. The demonstration will look"
  warn "like it works until somebody presses the control that needs it."
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
