#!/usr/bin/env bash
# Seed the demo tenant and its recorded state. Separate from deploy.sh's --seed so it can
# be re-run on its own: seeding resets the demo catalogue, and that is a decision, not a
# step you want folded into "restart the services".
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; . infra/gce/.env; set +a
C=(docker compose -f infra/gce/compose.yaml --env-file infra/gce/.env)

# Both scripts read SEED_DATABASE_URL, not DATABASE_URL_KERNEL -- they are launchers that
# *choose* a role rather than services that are handed one, and their fallback names the
# laptop's own commerce_dev. Passing the wrong one fails as a connection refused to
# localhost, which reads like a dead database rather than a misconfigured seed.
run_seed() {
  echo "== $1"
  "${C[@]}" run --rm --no-deps \
    -v "$(pwd)/scripts:/app/scripts:ro" \
    -e DATABASE_URL="${DATABASE_URL}" \
    -e SEED_DATABASE_URL="${DATABASE_URL_KERNEL}" \
    -e SEED_ADMIN_DATABASE_URL="${DATABASE_URL}" \
    -e API_BASE="http://api:8000" \
    -e SCENARIO_KEY="${SCENARIO_KEY}" \
    api python "/app/scripts/$1.py" "${@:2}"
}

# seed_demo_state does not write rows, it *buys* -- so it needs the API listening, not
# merely started. `docker compose up -d` returns when the container is created, which is
# several seconds before uvicorn binds, and the seed reports that gap as "connection
# refused ... start it with make demo": true, unhelpful, and nothing to do with make.
echo "== waiting for the API"
"${C[@]}" exec -T api python -c 'import time, urllib.request
for _ in range(90):
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=2)
        break
    except Exception:
        time.sleep(2)
else:
    raise SystemExit("the API never became healthy")'

run_seed seed_demo_tenant
# "$@" so a caller can pass --reset. A half-finished earlier run leaves orders owned by a
# buyer session that no longer exists, and the next run cannot open a support case on them:
# resuming across buyers is not something this seed can do, and pretending otherwise fails
# forty seconds in with a 404 that reads like a missing order.
run_seed seed_demo_state "$@"
echo "== SEED DONE"
